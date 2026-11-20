"""Public client API for non-JSON write bodies: Liquid.execute / execute_action / execute_endpoint."""

from __future__ import annotations

import base64
import json
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qsl

import httpx
import pytest

from liquid.body import FilePart
from liquid.client import Liquid
from liquid.exceptions import LiquidError
from liquid.models.action import ActionConfig, ActionErrorType, ActionMapping
from liquid.models.adapter import AdapterConfig, FieldMapping, SyncConfig
from liquid.models.schema import APISchema, AuthRequirement, Endpoint, EndpointKind


class FakeVault:
    async def store(self, key: str, value: str) -> None:
        pass

    async def get(self, key: str) -> str:
        return "fake-token"

    async def delete(self, key: str) -> None:
        pass


class FakeSink:
    async def deliver(self, records):
        from liquid.models.llm import DeliveryResult

        return DeliveryResult(delivered=len(records), failed=0)


def _adapter(actions: list[ActionConfig] | None = None) -> AdapterConfig:
    write = EndpointKind.WRITE
    schema = APISchema(
        source_url="https://cms.example.com",
        service_name="CMS",
        discovery_method="openapi",
        endpoints=[
            Endpoint(path="/media", method="POST", kind=write, request_content_types=["multipart/form-data"]),
            Endpoint(
                path="/messages", method="POST", kind=write, request_content_types=["application/x-www-form-urlencoded"]
            ),
            Endpoint(path="/listen", method="POST", kind=write, request_content_types=["audio/wav"]),
            Endpoint(path="/posts/{id}", method="PUT", kind=write),
            Endpoint(path="/posts/{id}", method="PATCH", kind=write),
            Endpoint(path="/posts", method="GET"),
        ],
        auth=AuthRequirement(type="bearer", tier="A"),
    )
    return AdapterConfig(
        schema=schema,
        auth_ref="vault/cms",
        mappings=[FieldMapping(source_path="a", target_field="b")],
        sync=SyncConfig(endpoints=["/posts"]),
        actions=actions or [],
    )


class Server:
    def __init__(self, status: int = 201, body: dict | None = None):
        self.requests: list[httpx.Request] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self._status, self._body = status, body if body is not None else {"id": 9}

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, json=self._body)

    def liquid(self, **kw) -> Liquid:
        return Liquid(llm=None, vault=FakeVault(), sink=FakeSink(), http_client=self.client, **kw)


def _parts(req: httpx.Request) -> dict:
    raw = b"Content-Type: " + req.headers["content-type"].encode() + b"\r\n\r\n" + req.content
    msg = BytesParser(policy=policy.HTTP).parsebytes(raw)
    return {
        p.get_param("name", header="content-disposition"): (
            p.get_filename(),
            p.get_content_type(),
            p.get_payload(decode=True),
        )
        for p in msg.iter_parts()
    }


# --- execute() with an action -------------------------------------------------


async def test_execute_multipart_with_file_dicts():
    srv = Server()
    action = ActionConfig(
        action_id="upload",
        endpoint_path="/media",
        endpoint_method="POST",
        mappings=[ActionMapping(source_field="title", target_path="title")],
        verified_by="admin",
    )
    result = await srv.liquid().execute(
        _adapter([action]),
        "upload",
        {"title": "Hello"},
        files=[{"field": "file", "content_base64": base64.b64encode(b"pixels").decode(), "filename": "a.png"}],
    )
    assert result.success
    parts = _parts(srv.requests[0])
    assert parts["title"][2] == b"Hello"
    assert parts["file"] == ("a.png", "image/png", b"pixels")


async def test_execute_infers_encoding_from_the_endpoint():
    srv = Server()
    action = ActionConfig(
        action_id="send",
        endpoint_path="/messages",
        endpoint_method="POST",
        mappings=[ActionMapping(source_field="Body", target_path="Body")],
        verified_by="admin",
    )
    await srv.liquid().execute(_adapter([action]), "send", {"Body": "hi"})
    assert srv.requests[0].headers["content-type"] == "application/x-www-form-urlencoded"
    assert dict(parse_qsl(srv.requests[0].content.decode())) == {"Body": "hi"}


async def test_execute_encoding_override_beats_declared_type():
    srv = Server()
    action = ActionConfig(
        action_id="send",
        endpoint_path="/messages",
        endpoint_method="POST",
        mappings=[ActionMapping(source_field="Body", target_path="Body")],
        verified_by="admin",
    )
    await srv.liquid().execute(_adapter([action]), "send", {"Body": "hi"}, encoding="json")
    assert srv.requests[0].headers["content-type"] == "application/json"
    assert json.loads(srv.requests[0].content) == {"Body": "hi"}


async def test_execute_binary_with_query():
    srv = Server(200, {"ok": True})
    action = ActionConfig(action_id="listen", endpoint_path="/listen", endpoint_method="POST", verified_by="admin")
    result = await srv.liquid().execute(
        _adapter([action]),
        "listen",
        {},
        binary=FilePart(content=b"RIFFWAVE", content_type="audio/wav"),
        query={"model": "nova-2"},
    )
    assert result.success
    req = srv.requests[0]
    assert req.content == b"RIFFWAVE"
    assert req.headers["content-type"] == "audio/wav"
    assert req.url.params["model"] == "nova-2"


async def test_execute_action_passes_bodies_through():
    srv = Server()
    action = ActionConfig(action_id="listen", endpoint_path="/listen", endpoint_method="POST", verified_by="admin")
    result = await srv.liquid().execute_action(
        _adapter([action]), "POST /listen", {}, binary={"content": b"abc", "content_type": "audio/wav"}
    )
    assert result.success
    assert srv.requests[0].content == b"abc"


async def test_bad_file_options_raise_validation_error_without_content():
    srv = Server()
    action = ActionConfig(action_id="up", endpoint_path="/media", endpoint_method="POST", verified_by="admin")
    with pytest.raises(ValueError) as exc:
        await srv.liquid().execute(
            _adapter([action]), "up", {}, files=[{"content": b"TOP-SECRET", "content_base64": "VE9QLVNFQ1JFVA=="}]
        )
    assert "TOP-SECRET" not in str(exc.value)
    assert "VE9QLVNFQ1JFVA" not in str(exc.value)
    assert srv.requests == []


# --- limits configured on the client ------------------------------------------


async def test_max_body_bytes_is_configurable():
    srv = Server()
    action = ActionConfig(action_id="listen", endpoint_path="/listen", endpoint_method="POST", verified_by="admin")
    liquid = srv.liquid(max_body_bytes=10)
    result = await liquid.execute(_adapter([action]), "listen", {}, binary=FilePart(content=b"x" * 11))
    assert not result.success
    assert result.error.type == ActionErrorType.VALIDATION_ERROR
    assert srv.requests == []
    ok = await liquid.execute(_adapter([action]), "listen", {}, binary=FilePart(content=b"x" * 10))
    assert ok.success


async def test_default_limit_is_25_mib():
    assert Liquid(llm=None, vault=FakeVault(), sink=FakeSink())._body_limits.max_bytes == 25 * 1024 * 1024


async def test_file_roots_confine_paths(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "ok.wav").write_bytes(b"audio")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"nope")
    srv = Server()
    action = ActionConfig(action_id="listen", endpoint_path="/listen", endpoint_method="POST", verified_by="admin")
    liquid = srv.liquid(file_roots=[root])
    ok = await liquid.execute(_adapter([action]), "listen", {}, binary={"path": str(root / "ok.wav")})
    assert ok.success
    assert srv.requests[0].content == b"audio"
    bad = await liquid.execute(_adapter([action]), "listen", {}, binary={"path": str(outside)})
    assert not bad.success
    assert "outside the allowed upload roots" in bad.error.message
    assert len(srv.requests) == 1


# --- execute_endpoint (no pre-built action) -----------------------------------


async def test_execute_endpoint_is_gated_by_allow_write():
    srv = Server()
    with pytest.raises(LiquidError, match="disabled by default"):
        await srv.liquid().execute_endpoint(_adapter(), "/media", {"title": "x"})
    assert srv.requests == []


async def test_execute_endpoint_multipart_identity_fields():
    srv = Server()
    result = await srv.liquid().execute_endpoint(
        _adapter(),
        "/media",
        {"title": "Cover", "alt_text": "alt"},
        allow_write=True,
        files=[{"content": b"jpegbytes", "filename": "cover.jpg"}],
    )
    assert result.success
    parts = _parts(srv.requests[0])
    assert parts["title"][2] == b"Cover"
    assert parts["alt_text"][2] == b"alt"
    assert parts["file"] == ("cover.jpg", "image/jpeg", b"jpegbytes")
    assert srv.requests[0].headers["authorization"] == "Bearer fake-token"


async def test_execute_endpoint_binary_upload():
    srv = Server(200, {"transcript": "hi"})
    result = await srv.liquid().execute_endpoint(
        _adapter(),
        "/listen",
        allow_write=True,
        binary={"content_base64": base64.b64encode(b"wavdata").decode(), "content_type": "audio/wav"},
        query={"language": "en"},
    )
    assert result.success
    assert result.response_body == {"transcript": "hi"}
    req = srv.requests[0]
    assert (req.content, req.headers["content-type"], req.url.params["language"]) == (b"wavdata", "audio/wav", "en")


async def test_execute_endpoint_fills_path_params_and_keeps_them_out_of_the_body():
    srv = Server(200, {})
    await srv.liquid().execute_endpoint(
        _adapter(), "/posts/{id}", {"id": 7, "title": "T"}, method="PUT", allow_write=True
    )
    req = srv.requests[0]
    assert (req.method, req.url.path) == ("PUT", "/posts/7")
    assert json.loads(req.content) == {"title": "T"}


async def test_execute_endpoint_prefers_a_verified_action_mapping():
    srv = Server(200, {})
    action = ActionConfig(
        action_id="p",
        endpoint_path="/messages",
        endpoint_method="POST",
        mappings=[ActionMapping(source_field="text", target_path="Body")],
        verified_by="admin",
    )
    await srv.liquid().execute_endpoint(_adapter([action]), "/messages", {"text": "yo"}, allow_write=True)
    assert dict(parse_qsl(srv.requests[0].content.decode())) == {"Body": "yo"}


async def test_execute_endpoint_errors():
    liquid = Server().liquid()
    with pytest.raises(ValueError, match="No write endpoint"):
        await liquid.execute_endpoint(_adapter(), "/nope", {}, allow_write=True)
    with pytest.raises(ValueError, match="No write endpoint"):
        await liquid.execute_endpoint(_adapter(), "/posts", {}, allow_write=True)  # GET only: use fetch
    with pytest.raises(ValueError, match="several write methods"):
        await liquid.execute_endpoint(_adapter(), "/posts/{id}", {"id": 1}, allow_write=True)
