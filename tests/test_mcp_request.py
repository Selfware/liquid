"""MCP `liquid_request` tool: schema, write gate, upload roots, and an end-to-end call."""

from __future__ import annotations

import base64
import os
from email import policy
from email.parser import BytesParser

import httpx
import pytest

pytest.importorskip("mcp")

from liquid.client import Liquid
from liquid.mcp_server import _file_roots, _max_body_bytes, _tool_definitions, _writes_enabled
from liquid.models.adapter import AdapterConfig, FieldMapping, SyncConfig
from liquid.models.schema import APISchema, AuthRequirement, Endpoint, EndpointKind
from liquid.persistence import FileAdapterRegistry, FileVault


def _tool():
    return {t.name: t for t in _tool_definitions(allow_writes=True)}["liquid_request"]


# --- catalog / schema -----------------------------------------------------------


def test_request_tool_is_hidden_unless_writes_are_allowed():
    assert "liquid_request" not in {t.name for t in _tool_definitions()}
    assert "liquid_request" in {t.name for t in _tool_definitions(allow_writes=True)}


def test_request_tool_input_schema():
    schema = _tool().inputSchema
    assert schema["required"] == ["adapter_id", "endpoint"]
    props = schema["properties"]
    assert set(props) == {"adapter_id", "endpoint", "method", "fields", "encoding", "files", "binary", "query"}
    assert props["encoding"]["enum"] == ["json", "form", "multipart", "binary"]
    assert props["method"]["enum"] == ["POST", "PUT", "PATCH", "DELETE"]
    assert props["files"]["type"] == "array"
    # the file part: three ways to say where the bytes come from, no raw `content`
    file_props = props["files"]["items"]["properties"]
    assert set(file_props) == {"field", "path", "content_base64", "filename", "content_type"}
    assert props["files"]["items"]["additionalProperties"] is False
    assert props["binary"]["properties"] == file_props
    for name, prop in props.items():
        assert prop.get("description"), f"{name} has no description"
    for name, prop in file_props.items():
        assert prop.get("description"), f"file part {name} has no description"


def test_request_tool_is_documented_like_the_other_tools():
    tool = _tool()
    assert tool.title
    assert len(tool.description) > 120
    assert "liquid_execute" in tool.description  # cross-tool guidance
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.openWorldHint is True
    assert tool.outputSchema is not None


def test_request_tool_output_schema_accepts_success_and_error_results():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _tool().outputSchema
    jsonschema.validate(
        {"success": True, "status_code": 201, "endpoint": "/media", "method": "POST", "response_body": {"id": 1}},
        schema,
    )
    jsonschema.validate(
        {
            "success": False,
            "status_code": 0,
            "error": "Request body exceeds the 1024-byte limit",
            "error_detail": {"type": "validation_error", "recovery_hint": "x", "details": {"limit_bytes": 1024}},
        },
        schema,
    )
    jsonschema.validate({"error": "RuntimeError: boom"}, schema)


def test_request_tool_input_schema_is_valid_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _tool().inputSchema
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(
        {
            "adapter_id": "a",
            "endpoint": "/media",
            "fields": {"title": "x"},
            "files": [{"field": "file", "path": "cover.jpg", "content_type": "image/jpeg"}],
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"adapter_id": "a", "endpoint": "/m", "files": [{"content": "raw"}]}, schema)


# --- env configuration -----------------------------------------------------------


def test_file_roots_default_and_override(tmp_path, monkeypatch):
    monkeypatch.delenv("LIQUID_FILE_ROOTS", raising=False)
    monkeypatch.setenv("LIQUID_HOME", str(tmp_path))
    assert _file_roots() == [tmp_path / "uploads"]
    monkeypatch.setenv("LIQUID_FILE_ROOTS", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert [str(p) for p in _file_roots()] == [str(tmp_path / "a"), str(tmp_path / "b")]
    monkeypatch.setenv("LIQUID_FILE_ROOTS", os.pathsep)  # nothing usable → safe default, never "unrestricted"
    assert _file_roots() == [tmp_path / "uploads"]


def test_max_body_bytes_env(monkeypatch):
    monkeypatch.delenv("LIQUID_MAX_BODY_BYTES", raising=False)
    assert _max_body_bytes() is None
    monkeypatch.setenv("LIQUID_MAX_BODY_BYTES", "1048576")
    assert _max_body_bytes() == 1048576
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("LIQUID_MAX_BODY_BYTES", bad)
        assert _max_body_bytes() is None


# --- end to end through the server's call_tool handler --------------------------


def _adapter() -> AdapterConfig:
    schema = APISchema(
        source_url="https://cms.example.com",
        service_name="CMS",
        discovery_method="openapi",
        endpoints=[
            Endpoint(
                path="/media", method="POST", kind=EndpointKind.WRITE, request_content_types=["multipart/form-data"]
            ),
            Endpoint(path="/listen", method="POST", kind=EndpointKind.WRITE, request_content_types=["audio/wav"]),
        ],
        auth=AuthRequirement(type="bearer", tier="A"),
    )
    return AdapterConfig(
        schema=schema,
        auth_ref="vault/cms",
        mappings=[FieldMapping(source_path="a", target_field="b")],
        sync=SyncConfig(endpoints=["/media"]),
    )


class _Env:
    def __init__(self, server, requests, adapter_id, root):
        self.server, self.requests, self.adapter_id, self.root = server, requests, adapter_id, root

    async def call(self, name: str, arguments: dict) -> dict:
        from mcp import types

        handler = self.server.request_handlers[types.CallToolRequest]
        req = types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments)
        )
        result = (await handler(req)).root
        assert not result.isError, result.content
        return result.structuredContent


@pytest.fixture
async def env(tmp_path, monkeypatch):
    import liquid.mcp_server as mcp_server

    monkeypatch.setenv("LIQUID_HOME", str(tmp_path))
    monkeypatch.setenv("LIQUID_ALLOW_WRITES", "1")
    monkeypatch.delenv("LIQUID_FILE_ROOTS", raising=False)
    monkeypatch.delenv("LIQUID_MAX_BODY_BYTES", raising=False)
    root = tmp_path / "uploads"
    root.mkdir()
    registry = FileAdapterRegistry(tmp_path / "adapters")
    config = _adapter()
    await registry.save(config, "m")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": 11})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    vault = FileVault(tmp_path / "vault.json")
    await vault.store(config.auth_ref, "cms-token")

    def build():
        from liquid._defaults import CollectorSink

        liquid = Liquid(
            llm=None,
            vault=vault,
            sink=CollectorSink(),
            registry=registry,
            http_client=client,
            max_body_bytes=mcp_server._max_body_bytes(),
            file_roots=mcp_server._file_roots(),
        )
        return liquid, registry

    monkeypatch.setattr(mcp_server, "_build_liquid", build)
    yield _Env(mcp_server.create_server(), requests, config.config_id, root)
    await client.aclose()


async def test_call_uploads_a_file_from_an_allowed_root(env):
    (env.root / "cover.jpg").write_bytes(b"jpegbytes")
    out = await env.call(
        "liquid_request",
        {
            "adapter_id": env.adapter_id,
            "endpoint": "/media",
            "fields": {"title": "Cover"},
            "files": [{"field": "file", "path": "cover.jpg"}],  # relative → under the first root
        },
    )
    assert out["success"] is True
    assert out["status_code"] == 201
    assert out["response_body"] == {"id": 11}
    req = env.requests[0]
    raw = b"Content-Type: " + req.headers["content-type"].encode() + b"\r\n\r\n" + req.content
    parts = {
        p.get_param("name", header="content-disposition"): p
        for p in BytesParser(policy=policy.HTTP).parsebytes(raw).iter_parts()
    }
    assert parts["title"].get_payload(decode=True) == b"Cover"
    assert parts["file"].get_filename() == "cover.jpg"
    assert parts["file"].get_payload(decode=True) == b"jpegbytes"


async def test_call_refuses_a_path_outside_the_roots(env, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP-SECRET")
    for path in (str(secret), "../secret.txt", "/etc/passwd"):
        out = await env.call(
            "liquid_request",
            {"adapter_id": env.adapter_id, "endpoint": "/media", "files": [{"path": path}]},
        )
        assert out["success"] is False
        assert "outside the allowed upload roots" in out["error"]
        assert "TOP-SECRET" not in str(out)
    assert env.requests == []


async def test_call_accepts_inline_base64_audio_with_query(env):
    out = await env.call(
        "liquid_request",
        {
            "adapter_id": env.adapter_id,
            "endpoint": "/listen",
            "binary": {"content_base64": base64.b64encode(b"wav").decode(), "content_type": "audio/wav"},
            "query": {"model": "nova-2"},
        },
    )
    assert out["success"] is True
    assert env.requests[0].content == b"wav"
    assert env.requests[0].url.params["model"] == "nova-2"


async def test_call_reports_oversized_body_without_sending(env, monkeypatch):
    import liquid.mcp_server as mcp_server

    monkeypatch.setenv("LIQUID_MAX_BODY_BYTES", "100")
    server = mcp_server.create_server()  # picks the cap up from the environment
    env.server = server
    out = await env.call(
        "liquid_request",
        {
            "adapter_id": env.adapter_id,
            "endpoint": "/listen",
            "binary": {"content_base64": base64.b64encode(b"x" * 500).decode()},
        },
    )
    assert out["success"] is False
    assert "100-byte limit" in out["error"]
    assert out["error_detail"]["details"] == {"limit_bytes": 100}
    assert env.requests == []


async def test_call_surfaces_bad_arguments_without_echoing_content(env):
    out = await env.call(
        "liquid_request",
        {
            "adapter_id": env.adapter_id,
            "endpoint": "/media",
            "files": [{"path": "x", "content_base64": "VE9QLVNFQ1JFVA=="}],
        },
    )
    assert "error" in out
    assert "VE9QLVNFQ1JFVA" not in str(out)
    assert env.requests == []


async def test_call_is_refused_when_writes_are_disabled(env, monkeypatch):
    import liquid.mcp_server as mcp_server

    monkeypatch.delenv("LIQUID_ALLOW_WRITES")
    assert not _writes_enabled()
    env.server = mcp_server.create_server()
    out = await env.call("liquid_request", {"adapter_id": env.adapter_id, "endpoint": "/media", "fields": {"a": 1}})
    assert "writes are disabled" in out["error"]
    assert env.requests == []


def test_write_endpoints_expose_declared_body_types():
    from liquid.mcp_server import _write_endpoints

    assert _write_endpoints(_adapter().schema_) == [
        {"method": "POST", "path": "/media", "request_content_types": ["multipart/form-data"]},
        {"method": "POST", "path": "/listen", "request_content_types": ["audio/wav"]},
    ]
