"""OpenAPI discovery carries non-JSON request body content types through to Endpoint."""

from __future__ import annotations

import httpx

from liquid.body import BodyEncoding
from liquid.discovery.openapi import OpenAPIDiscovery


def _spec_v3(paths: dict) -> dict:
    return {"openapi": "3.0.3", "info": {"title": "Bodies"}, "paths": paths}


async def _discover(spec: dict) -> dict[tuple[str, str], object]:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=spec) if req.url.path == "/openapi.json" else httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        schema = await OpenAPIDiscovery(http_client=client).discover("https://api.example.com")
    assert schema is not None
    return {(e.method, e.path): e for e in schema.endpoints}


async def test_json_only_endpoint_declares_json():
    eps = await _discover(
        _spec_v3(
            {
                "/orders": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object", "properties": {"a": {"type": "string"}}}
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    ep = eps[("POST", "/orders")]
    assert ep.request_content_types == ["application/json"]
    assert ep.request_schema == {"type": "object", "properties": {"a": {"type": "string"}}}
    assert ep.preferred_body_encoding() == BodyEncoding.JSON


async def test_no_request_body_declares_nothing():
    eps = await _discover(_spec_v3({"/orders": {"get": {"responses": {"200": {"description": "ok"}}}}}))
    ep = eps[("GET", "/orders")]
    assert ep.request_content_types == []
    assert ep.request_schema is None
    assert ep.preferred_body_encoding() is None


async def test_multipart_upload_schema_and_types():
    eps = await _discover(
        _spec_v3(
            {
                "/media": {
                    "post": {
                        "requestBody": {
                            "required": True,
                            "content": {
                                "multipart/form-data": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["file"],
                                        "properties": {
                                            "file": {"type": "string", "format": "binary"},
                                            "title": {"type": "string"},
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {"201": {"description": "created"}},
                    }
                }
            }
        )
    )
    ep = eps[("POST", "/media")]
    assert ep.request_content_types == ["multipart/form-data"]
    assert ep.request_schema["required"] == ["file"]
    assert ep.request_schema["properties"]["file"] == {"type": "string", "format": "binary"}
    assert ep.preferred_body_encoding() == BodyEncoding.MULTIPART


async def test_form_urlencoded_with_ref_schema():
    spec = _spec_v3(
        {
            "/Messages.json": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/x-www-form-urlencoded": {"schema": {"$ref": "#/components/schemas/NewMessage"}}
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                }
            }
        }
    )
    spec["components"] = {
        "schemas": {"NewMessage": {"type": "object", "required": ["To"], "properties": {"To": {"type": "string"}}}}
    }
    ep = (await _discover(spec))[("POST", "/Messages.json")]
    assert ep.request_content_types == ["application/x-www-form-urlencoded"]
    assert ep.request_schema == {"type": "object", "required": ["To"], "properties": {"To": {"type": "string"}}}
    assert ep.preferred_body_encoding() == BodyEncoding.FORM


async def test_raw_binary_body_types():
    eps = await _discover(
        _spec_v3(
            {
                "/v1/listen": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "audio/wav": {"schema": {"type": "string", "format": "binary"}},
                                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    ep = eps[("POST", "/v1/listen")]
    assert ep.request_content_types == ["audio/wav", "application/octet-stream"]
    assert ep.request_schema == {"type": "string", "format": "binary"}
    assert ep.preferred_body_encoding() == BodyEncoding.BINARY


async def test_multiple_types_keep_order_and_json_schema_wins():
    eps = await _discover(
        _spec_v3(
            {
                "/things": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "multipart/form-data": {
                                    "schema": {"type": "object", "properties": {"f": {"type": "string"}}}
                                },
                                "application/json": {
                                    "schema": {"type": "object", "properties": {"j": {"type": "string"}}}
                                },
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
    )
    ep = eps[("POST", "/things")]
    assert ep.request_content_types == ["multipart/form-data", "application/json"]
    assert "j" in ep.request_schema["properties"]  # JSON schema is preferred for validation/mapping
    assert ep.preferred_body_encoding() == BodyEncoding.JSON  # JSON wins when offered


async def test_swagger2_form_data_with_file_param():
    spec = {
        "swagger": "2.0",
        "info": {"title": "Swag"},
        "paths": {
            "/pets/{id}/photo": {
                "post": {
                    "consumes": ["multipart/form-data"],
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "type": "integer"},
                        {"name": "file", "in": "formData", "type": "file", "required": True},
                        {"name": "caption", "in": "formData", "type": "string"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    ep = (await _discover(spec))[("POST", "/pets/{id}/photo")]
    assert ep.request_content_types == ["multipart/form-data"]
    assert ep.request_schema == {
        "type": "object",
        "required": ["file"],
        "properties": {"file": {"type": "string", "format": "binary"}, "caption": {"type": "string"}},
    }
    # form fields are request-body fields, not URL query parameters
    assert [p.name for p in ep.parameters] == ["id"]


async def test_swagger2_form_data_without_consumes_is_inferred():
    spec = {
        "swagger": "2.0",
        "info": {"title": "Swag"},
        "paths": {
            "/login": {
                "post": {
                    "parameters": [{"name": "user", "in": "formData", "type": "string"}],
                    "responses": {"200": {"description": "ok"}},
                },
                "put": {
                    "parameters": [{"name": "doc", "in": "formData", "type": "file"}],
                    "responses": {"200": {"description": "ok"}},
                },
            }
        },
    }
    eps = await _discover(spec)
    assert eps[("POST", "/login")].request_content_types == ["application/x-www-form-urlencoded"]
    assert eps[("PUT", "/login")].request_content_types == ["multipart/form-data"]


async def test_swagger2_body_param_uses_root_consumes():
    spec = {
        "swagger": "2.0",
        "info": {"title": "Swag"},
        "consumes": ["application/json", "application/xml"],
        "paths": {
            "/a": {
                "post": {
                    "parameters": [{"name": "b", "in": "body", "schema": {"type": "object"}}],
                    "responses": {"200": {"description": "ok"}},
                },
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        },
    }
    eps = await _discover(spec)
    assert eps[("POST", "/a")].request_content_types == ["application/json", "application/xml"]
    assert eps[("GET", "/a")].request_content_types == []


def test_endpoint_roundtrips_content_types_through_json():
    from liquid.models.schema import Endpoint

    ep = Endpoint(path="/m", method="POST", request_content_types=["multipart/form-data"])
    assert Endpoint.model_validate_json(ep.model_dump_json()).request_content_types == ["multipart/form-data"]
    # adapters saved before this field existed still load
    assert Endpoint.model_validate({"path": "/m", "method": "POST"}).request_content_types == []
