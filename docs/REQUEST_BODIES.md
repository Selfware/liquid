# Forms, file uploads and raw binary bodies

A write is not always JSON. Liquid's write path sends four body encodings:

| Encoding | Wire format | Typical use |
|---|---|---|
| `json` (default) | `application/json` | most REST APIs |
| `form` | `application/x-www-form-urlencoded` | SMS/e-mail APIs, OAuth token calls, legacy CMS endpoints |
| `multipart` | `multipart/form-data` — plain fields plus file parts | CMS media uploads, document/voice uploads |
| `binary` | the file *is* the body, with its own `Content-Type` | `audio/*` transcription endpoints, `application/octet-stream`, WordPress `/media` |

Everything below works the same through the Python API and the MCP server.

## Which encoding is used

1. The one you pass: `encoding="form" | "multipart" | "binary" | "json"`.
2. Otherwise it is implied by what you pass: `binary=` → `binary`, `files=` → `multipart`.
3. Otherwise the endpoint's declared content type: discovery copies the OpenAPI
   `requestBody.content` keys (Swagger 2: `consumes`, or `in: formData` parameters) to
   `Endpoint.request_content_types`. JSON wins when it is offered; otherwise the first
   declared type decides (`multipart/form-data` → `multipart`, `application/x-www-form-urlencoded`
   → `form`, anything else such as `audio/wav` or `application/octet-stream` → `binary`).
4. Otherwise JSON.

If you force an encoding the endpoint doesn't declare, the request is still sent (specs are
often incomplete) and a warning is logged.

```python
ep = next(e for e in adapter.schema_.endpoints if e.path == "/media")
ep.request_content_types        # ["multipart/form-data"]
ep.preferred_body_encoding()    # BodyEncoding.MULTIPART
```

## The calls

`Liquid.execute(config, action_id, data, ...)` runs a verified action;
`Liquid.execute_endpoint(config, path, data, ...)` addresses an endpoint directly (no
pre-built action; off until you pass `allow_write=True`, like `Liquid.write`). Both take:

| Argument | Meaning |
|---|---|
| `data` | the plain fields: form fields, multipart text fields, or the JSON body |
| `encoding` | see above |
| `files` | multipart file parts: a list of `FilePart` / dicts |
| `binary` | one file part sent as the whole raw body |
| `query` | URL query parameters (a raw upload has no body fields, so options like `?model=nova-2` go here) |

A **file part** is `{"field", "path" | "content" | "content_base64", "filename", "content_type"}`
(or `liquid.FilePart`): exactly one of `path` (a local file), `content` (raw bytes; Python only) or
`content_base64`. `filename` defaults to the path's base name; `content_type` is guessed from the
filename, else `application/octet-stream`. As a multipart part `field` is the form field name
(default `file`); for `binary` it is ignored.

Nested `data` becomes `a[b]=…` and a list of scalars repeats the key (`tag=x&tag=y`); `None`
values are dropped and booleans are sent as `true` / `false`.

### 1. Form — `application/x-www-form-urlencoded`

```python
result = await liquid.execute_endpoint(
    sms, "/Messages.json",
    {"To": "+15551234567", "From": "+15557654321", "Body": "Your code is 4821"},
    allow_write=True,
    encoding="form",          # or leave out: taken from the endpoint's declared content type
)
```

### 2. Multipart — `multipart/form-data`

```python
result = await liquid.execute_endpoint(
    cms, "/wp-json/wp/v2/media",
    {"title": "Cover", "alt_text": "The cover"},          # plain fields
    allow_write=True,
    files=[
        {"field": "file", "path": "/data/uploads/cover.jpg"},                          # from a path
        # {"field": "file", "content": png_bytes, "filename": "cover.png"},             # or raw bytes
        # {"field": "file", "content_base64": b64, "filename": "cover.png",             # or base64
        #  "content_type": "image/png"},
    ],
)
print(result.status_code, result.response_body)
```

### 3. Raw binary — `audio/*`, `application/octet-stream`, …

```python
# Speech-to-text: the audio is the body, options are query parameters.
result = await liquid.execute_endpoint(
    speech, "/v1/listen",
    allow_write=True,
    binary={"path": "/data/uploads/call.wav", "content_type": "audio/wav"},
    query={"model": "nova-2", "smart_format": True},
)

# CMS media upload that wants the raw image: a named file also sets
# `Content-Disposition: attachment; filename="logo.png"`.
await liquid.execute_endpoint(
    cms, "/wp-json/wp/v2/media", allow_write=True,
    binary={"path": "/data/uploads/logo.png"},
)
```

## From an MCP client

Start the server with `LIQUID_ALLOW_WRITES=1` and the `liquid_request` tool appears. Its
`write_endpoints` (returned by `liquid_connect` / `liquid_list_adapters`) show which body types each
endpoint declares.

```jsonc
// multipart upload
{"adapter_id": "…", "endpoint": "/wp-json/wp/v2/media",
 "fields": {"title": "Cover"},
 "files": [{"field": "file", "path": "cover.jpg"}]}        // relative → under the first upload root

// raw audio, inline base64
{"adapter_id": "…", "endpoint": "/v1/listen",
 "binary": {"content_base64": "UklGRi4A…", "content_type": "audio/wav"},
 "query": {"model": "nova-2"}}

// form
{"adapter_id": "…", "endpoint": "/Messages.json", "encoding": "form",
 "fields": {"To": "+15551234567", "Body": "hi"}}
```

## Safety

- **Size cap.** The whole encoded body is capped at **25 MiB** by default. Configure it with
  `Liquid(max_body_bytes=…)` or, for the MCP server, `LIQUID_MAX_BODY_BYTES`. Files are `stat`-ed
  and reads are bounded, and base64 is size-checked before it is decoded, so an oversized input is
  refused before it is loaded. Nothing is sent: the call returns a failed `ActionResult`
  (`validation_error`, `details={"limit_bytes": …}`).
- **Allowed roots.** `Liquid(file_roots=[…])` confines file `path` parts to those directories
  (symlinks are resolved first, so a link cannot escape; `..` is resolved too). Default for the
  Python API: unrestricted — you are the trusted caller. The **MCP server always sets roots**:
  `LIQUID_FILE_ROOTS` (`os.pathsep`-separated), default `~/.liquid/uploads`; a relative path is taken
  relative to the first root. An agent can always send `content_base64` instead (it counts toward
  the cap). `liquid_request` is hidden unless the operator started the server with `LIQUID_ALLOW_WRITES=1`.
- **No contents in logs.** File content fields are hidden from `repr` and from validation errors,
  error messages never contain content, and the only log line is the body's kind, size and file
  count.
- **Retries** (`429` / `5xx`) re-send the identical pre-encoded bytes.

## Not covered (yet)

- Non-bearer authentication on REST writes: the action executor sends `Authorization: Bearer <secret>`,
  so APIs that need Basic or a custom scheme (`Token …`) are not covered by this change.
- Non-JSON bodies on GraphQL / MCP-tool actions (refused with a clear error).
- Streaming uploads — a body is fully encoded in memory, hence the cap.
