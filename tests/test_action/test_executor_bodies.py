"""ActionExecutor with non-JSON bodies: form, multipart, raw binary (httpx MockTransport)."""

from __future__ import annotations

import base64
import json
import logging
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qsl

import httpx

from liquid.action.executor import ActionExecutor
from liquid.body import BodyLimits, FilePart, RequestBody
from liquid.models.action import ActionConfig, ActionErrorType, ActionMapping
from liquid.models.schema import APISchema, AuthRequirement, Endpoint, EndpointKind
from liquid.sync.retry import RetryPolicy

SECRET = b"TOP-SECRET-AUDIO-BYTES"


class FakeVault:
    async def store(self, key: str, value: str) -> None:
        pass

    async def get(self, key: str) -> str:
        return "test-token"

    async def delete(self, key: str) -> None:
        pass


def _schema(*endpoints: Endpoint) -> APISchema:
    return APISchema(
        source_url="https://api.example.com",
        service_name="Example",
        discovery_method="openapi",
        endpoints=list(endpoints),
        auth=AuthRequirement(type="bearer", tier="A"),
    )


def _action(path: str, method: str = "POST", fields: tuple[str, ...] = ()) -> ActionConfig:
    return ActionConfig(
        endpoint_path=path,
        endpoint_method=method,
        mappings=[ActionMapping(source_field=f, target_path=f) for f in fields],
        verified_by="admin",
    )


class Recorder:
    """MockTransport that records requests and replies with a canned response."""

    def __init__(self, status: int = 200, body: dict | None = None):
        self.requests: list[httpx.Request] = []
        self._status = status
        self._body = body if body is not None else {"id": 1}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status, json=self._body)


def _executor(client: httpx.AsyncClient, **kw) -> ActionExecutor:
    return ActionExecutor(
        http_client=client,
        vault=FakeVault(),
        retry_policy=RetryPolicy(max_retries=2, base_delay=0.0, max_delay=0.0),
        **kw,
    )


def _multipart(request: httpx.Request) -> dict[str, tuple[str | None, str, bytes]]:
    raw = b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    msg = BytesParser(policy=policy.HTTP).parsebytes(raw)
    return {
        p.get_param("name", header="content-disposition"): (
            p.get_filename(),
            p.get_content_type(),
            p.get_payload(decode=True),
        )
        for p in msg.iter_parts()
    }


async def _run(rec: Recorder, ep: Endpoint, action: ActionConfig, data: dict, body: RequestBody | None = None, **kw):
    async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
        return await _executor(client, **kw).execute(
            action=action, data=data, schema=_schema(ep), auth_ref="ref", body=body
        )


# --- JSON stays exactly as before --------------------------------------------


async def test_json_default_is_unchanged():
    rec = Recorder()
    ep = Endpoint(path="/orders", method="POST", kind=EndpointKind.WRITE)
    result = await _run(rec, ep, _action("/orders", fields=("amount",)), {"amount": 5})
    assert result.success
    req = rec.requests[0]
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {"amount": 5}
    assert req.headers["authorization"] == "Bearer test-token"
    assert req.url.query == b""


# --- form ---------------------------------------------------------------------


async def test_form_body_explicit():
    rec = Recorder()
    ep = Endpoint(path="/Messages.json", method="POST", kind=EndpointKind.WRITE)
    result = await _run(
        rec,
        ep,
        _action("/Messages.json", fields=("To", "From", "Body")),
        {"To": "+15551234567", "From": "+15557654321", "Body": "hi there"},
        RequestBody(encoding="form"),
    )
    assert result.success
    req = rec.requests[0]
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert dict(parse_qsl(req.content.decode())) == {"To": "+15551234567", "From": "+15557654321", "Body": "hi there"}


async def test_form_inferred_from_declared_content_type():
    rec = Recorder()
    ep = Endpoint(
        path="/Messages.json",
        method="POST",
        kind=EndpointKind.WRITE,
        request_content_types=["application/x-www-form-urlencoded"],
    )
    await _run(rec, ep, _action("/Messages.json", fields=("Body",)), {"Body": "x"})
    assert rec.requests[0].headers["content-type"] == "application/x-www-form-urlencoded"
    assert rec.requests[0].content == b"Body=x"


async def test_json_wins_when_endpoint_offers_both():
    rec = Recorder()
    ep = Endpoint(
        path="/x",
        method="POST",
        kind=EndpointKind.WRITE,
        request_content_types=["application/x-www-form-urlencoded", "application/json"],
    )
    await _run(rec, ep, _action("/x", fields=("a",)), {"a": 1})
    assert rec.requests[0].headers["content-type"] == "application/json"


# --- multipart ----------------------------------------------------------------


async def test_multipart_upload_with_fields_and_file(tmp_path):
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"\xff\xd8\xffJPEG")
    rec = Recorder(201, {"id": 42, "source_url": "https://x/pic.jpg"})
    ep = Endpoint(
        path="/wp-json/wp/v2/media",
        method="POST",
        kind=EndpointKind.WRITE,
        request_content_types=["multipart/form-data"],
    )
    result = await _run(
        rec,
        ep,
        _action("/wp-json/wp/v2/media", fields=("title", "alt_text")),
        {"title": "Cover", "alt_text": "A cover"},
        RequestBody(files=[FilePart(field="file", path=str(img))]),
    )
    assert result.success
    assert result.status_code == 201
    assert result.response_body == {"id": 42, "source_url": "https://x/pic.jpg"}
    req = rec.requests[0]
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert req.headers["authorization"] == "Bearer test-token"
    parts = _multipart(req)
    assert parts["title"][2] == b"Cover"
    assert parts["alt_text"][2] == b"A cover"
    assert parts["file"] == ("pic.jpg", "image/jpeg", b"\xff\xd8\xffJPEG")


async def test_multipart_file_satisfies_a_required_binary_schema_field():
    rec = Recorder()
    ep = Endpoint(
        path="/upload",
        method="POST",
        kind=EndpointKind.WRITE,
        request_schema={
            "type": "object",
            "required": ["file", "purpose"],
            "properties": {"file": {"type": "string", "format": "binary"}, "purpose": {"type": "string"}},
        },
        request_content_types=["multipart/form-data"],
    )
    ok = await _run(
        rec,
        ep,
        _action("/upload", fields=("purpose",)),
        {"purpose": "assistants"},
        RequestBody(files=[FilePart(field="file", content=b"data", filename="d.txt")]),
    )
    assert ok.success
    # without the file part the required field is reported missing, and nothing is sent
    rec2 = Recorder()
    bad = await _run(
        rec2, ep, _action("/upload", fields=("purpose",)), {"purpose": "x"}, RequestBody(encoding="multipart")
    )
    assert not bad.success
    assert bad.error.type == ActionErrorType.VALIDATION_ERROR
    assert "file" in str(bad.error.details)
    assert rec2.requests == []


async def test_multipart_retry_resends_identical_body():
    attempts: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.content)
        return httpx.Response(503 if len(attempts) < 2 else 200, json={"ok": True})

    ep = Endpoint(path="/up", method="POST", kind=EndpointKind.WRITE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _executor(client).execute(
            action=_action("/up"),
            data={},
            schema=_schema(ep),
            auth_ref="ref",
            body=RequestBody(files=[FilePart(content=b"payload", filename="p.bin")]),
        )
    assert result.success
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]  # same boundary, same bytes


# --- binary -------------------------------------------------------------------


async def test_binary_audio_upload_with_query_params(tmp_path):
    wav = tmp_path / "call.wav"
    wav.write_bytes(b"RIFF....WAVEfmt ")
    rec = Recorder(200, {"results": {"channels": []}})
    ep = Endpoint(path="/v1/listen", method="POST", kind=EndpointKind.WRITE, request_content_types=["audio/*"])
    result = await _run(
        rec,
        ep,
        _action("/v1/listen"),
        {},
        RequestBody(
            binary=FilePart(path=str(wav), content_type="audio/wav"), query={"model": "nova-2", "smart_format": True}
        ),
    )
    assert result.success
    req = rec.requests[0]
    assert req.content == b"RIFF....WAVEfmt "
    assert req.headers["content-type"] == "audio/wav"
    assert dict(parse_qsl(req.url.query.decode())) == {"model": "nova-2", "smart_format": "true"}


async def test_binary_media_upload_content_disposition():
    rec = Recorder(201, {"id": 7})
    ep = Endpoint(path="/wp-json/wp/v2/media", method="POST", kind=EndpointKind.WRITE)
    await _run(
        rec,
        ep,
        _action("/wp-json/wp/v2/media"),
        {},
        RequestBody(binary=FilePart(content_base64=base64.b64encode(b"\x89PNG").decode(), filename="logo.png")),
    )
    req = rec.requests[0]
    assert req.content == b"\x89PNG"
    assert req.headers["content-type"] == "image/png"
    assert req.headers["content-disposition"] == 'attachment; filename="logo.png"'


async def test_binary_skips_json_schema_validation():
    rec = Recorder()
    ep = Endpoint(
        path="/v1/listen",
        method="POST",
        kind=EndpointKind.WRITE,
        request_schema={"type": "string", "format": "binary"},
        request_content_types=["application/octet-stream"],
    )
    result = await _run(rec, ep, _action("/v1/listen"), {}, RequestBody(binary=FilePart(content=b"abc")))
    assert result.success
    assert rec.requests[0].content == b"abc"


async def test_query_params_also_work_with_json():
    rec = Recorder()
    ep = Endpoint(path="/x", method="POST", kind=EndpointKind.WRITE)
    await _run(rec, ep, _action("/x", fields=("a",)), {"a": 1}, RequestBody(query={"dry_run": "1"}))
    assert rec.requests[0].url.query == b"dry_run=1"
    assert json.loads(rec.requests[0].content) == {"a": 1}


# --- safety -------------------------------------------------------------------


async def test_oversized_body_is_refused_and_nothing_is_sent():
    rec = Recorder()
    ep = Endpoint(path="/up", method="POST", kind=EndpointKind.WRITE)
    result = await _run(
        rec,
        ep,
        _action("/up"),
        {},
        RequestBody(binary=FilePart(content=b"x" * 2048)),
        body_limits=BodyLimits(max_bytes=1024),
    )
    assert not result.success
    assert result.status_code == 0
    assert result.error.type == ActionErrorType.VALIDATION_ERROR
    assert "1024" in result.error.message
    assert result.error.details == {"limit_bytes": 1024}
    assert result.error.recovery.retry_safe is False
    assert rec.requests == []


async def test_path_outside_allowed_root_is_refused_and_nothing_is_sent(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(SECRET)
    rec = Recorder()
    ep = Endpoint(path="/up", method="POST", kind=EndpointKind.WRITE)
    result = await _run(
        rec,
        ep,
        _action("/up"),
        {},
        RequestBody(files=[FilePart(path=str(secret))]),
        body_limits=BodyLimits.build(allowed_roots=[root]),
    )
    assert not result.success
    assert result.error.type == ActionErrorType.VALIDATION_ERROR
    assert "outside the allowed upload roots" in result.error.message
    assert "TOP-SECRET" not in result.model_dump_json()
    assert rec.requests == []


async def test_file_contents_never_logged_or_returned_in_errors(caplog):
    caplog.set_level(logging.DEBUG)
    rec = Recorder(400, {"message": "bad"})
    ep = Endpoint(path="/up", method="POST", kind=EndpointKind.WRITE)
    result = await _run(
        rec,
        ep,
        _action("/up"),
        {},
        RequestBody(
            files=[FilePart(content=SECRET, filename="s.bin")],
        ),
    )
    assert not result.success  # the server said 400
    assert "TOP-SECRET" not in result.model_dump_json()
    for record in caplog.records:
        assert "TOP-SECRET" not in record.getMessage()
        assert base64.b64encode(SECRET).decode() not in record.getMessage()
    # but the upload itself is logged by size, so operators can see it happened
    assert any("multipart body" in r.getMessage() and "bytes" in r.getMessage() for r in caplog.records)


async def test_declared_type_mismatch_warns_but_still_sends(caplog):
    caplog.set_level(logging.WARNING)
    rec = Recorder()
    ep = Endpoint(path="/x", method="POST", kind=EndpointKind.WRITE, request_content_types=["application/json"])
    result = await _run(rec, ep, _action("/x"), {}, RequestBody(binary=FilePart(content=b"x")))
    assert result.success
    assert any("declares" in r.getMessage() for r in caplog.records)


async def test_graphql_endpoint_refuses_uploads():
    ep = Endpoint(path="/graphql#mutation.createThing", method="POST", kind=EndpointKind.WRITE)
    rec = Recorder()
    result = await _run(
        rec, ep, _action("/graphql#mutation.createThing"), {}, RequestBody(files=[FilePart(content=b"x")])
    )
    assert not result.success
    assert "plain REST" in result.error.message
    assert rec.requests == []
