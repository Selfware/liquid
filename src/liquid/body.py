"""Request bodies beyond JSON: form-urlencoded, multipart/form-data and raw binary.

Liquid's write path used to send ``json=`` only. This module adds the three other
encodings real APIs ask for:

* ``form`` — ``application/x-www-form-urlencoded`` (Twilio, Stripe-style APIs, OAuth token calls);
* ``multipart`` — ``multipart/form-data`` with plain fields plus file parts
  (CMS media uploads, voice/document uploads);
* ``binary`` — the whole body *is* the file, with its own ``Content-Type``
  (Deepgram ``/listen`` takes ``audio/*``; WordPress ``/media`` takes the raw image).

A file can come from a local path, raw bytes or base64. Everything is encoded up
front into one ``bytes`` payload, so a retry re-sends exactly the same body.

Safety, by design:

* **Size cap** — one limit for the whole encoded body (default 25 MiB, configurable
  through :class:`BodyLimits`). Paths are ``stat``-ed and reads are bounded, base64
  is size-checked before it is decoded, so an oversized input is refused before it
  is read into memory.
* **Allowed roots** — when :attr:`BodyLimits.allowed_roots` is set, a file path must
  resolve (symlinks followed) to somewhere under one of the roots. The MCP server
  always sets it.
* **No file contents in logs, reprs or error messages.** Content fields are hidden
  from ``repr`` and from pydantic validation errors, and no code path here logs a
  body.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, model_validator

from liquid.exceptions import BodyTooLargeError, FileAccessError, Recovery, RequestBodyError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

DEFAULT_MAX_BODY_BYTES = 25 * 1024 * 1024
"""Default cap on an encoded request body (25 MiB)."""

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class BodyEncoding(StrEnum):
    """How a request body is put on the wire."""

    JSON = "json"
    FORM = "form"
    MULTIPART = "multipart"
    BINARY = "binary"


_CONTENT_TYPE_BY_ENCODING = {
    BodyEncoding.JSON: "application/json",
    BodyEncoding.FORM: "application/x-www-form-urlencoded",
    BodyEncoding.MULTIPART: "multipart/form-data",
}


def media_type(content_type: str) -> str:
    """``"Application/JSON; charset=utf-8"`` → ``"application/json"``."""
    return content_type.split(";", 1)[0].strip().lower()


def encoding_for_content_type(content_type: str) -> BodyEncoding:
    """Map a request media type (as declared by an OpenAPI spec) to a body encoding.

    JSON (``application/json``, ``*+json``), form-urlencoded and ``multipart/form-data``
    have their own encodings; every other type (``audio/*``, ``image/*``,
    ``application/octet-stream``, ``text/plain``, ``*/*`` …) is a raw ``binary`` body.
    """
    ct = media_type(content_type)
    if ct in ("application/json", "text/json") or ct.endswith("+json"):
        return BodyEncoding.JSON
    if ct == "application/x-www-form-urlencoded":
        return BodyEncoding.FORM
    if ct == "multipart/form-data":
        return BodyEncoding.MULTIPART
    return BodyEncoding.BINARY


def preferred_encoding(content_types: Iterable[str]) -> BodyEncoding | None:
    """The encoding to use for an endpoint that declares ``content_types``.

    JSON wins when it is offered (the historical default); otherwise the first
    declared type decides. ``None`` when nothing is declared.
    """
    encodings = [encoding_for_content_type(ct) for ct in content_types]
    if not encodings:
        return None
    return BodyEncoding.JSON if BodyEncoding.JSON in encodings else encodings[0]


class FilePart(BaseModel):
    """One file to upload. Give exactly one of ``path`` / ``content`` / ``content_base64``.

    As a multipart part ``field`` is the form field name; as a raw binary body it is
    ignored. ``filename`` defaults to the path's base name; ``content_type`` is guessed
    from the filename, else ``application/octet-stream``.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    field: str = "file"
    path: str | None = None
    content: bytes | None = Field(default=None, repr=False)
    content_base64: str | None = Field(default=None, repr=False)
    filename: str | None = None
    content_type: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> FilePart:
        given = [n for n in ("path", "content", "content_base64") if getattr(self, n) is not None]
        if len(given) != 1:
            raise ValueError("give exactly one of: path, content, content_base64")
        if not self.field:
            raise ValueError("field must not be empty")
        return self


class RequestBody(BaseModel):
    """Non-JSON body options for one write call.

    The plain fields still travel in the call's ``data``; this carries what is
    *beyond* them: which ``encoding`` to use (inferred when omitted), the ``files``
    of a multipart upload, or a raw ``binary`` body, plus URL ``query`` parameters
    (a raw upload has no body fields, so options like ``?model=nova-2`` go here).
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    encoding: BodyEncoding | None = None
    files: list[FilePart] = Field(default_factory=list)
    binary: FilePart | None = None
    query: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> RequestBody:
        if self.files and self.binary is not None:
            raise ValueError("give either files (multipart) or binary (raw body), not both")
        if self.files and self.encoding not in (None, BodyEncoding.MULTIPART):
            raise ValueError("files require the multipart encoding")
        if self.binary is not None and self.encoding not in (None, BodyEncoding.BINARY):
            raise ValueError("binary requires the binary encoding")
        if self.encoding == BodyEncoding.BINARY and self.binary is None:
            raise ValueError("the binary encoding needs a binary file part")
        return self

    def resolve_encoding(self, endpoint_default: BodyEncoding | None = None) -> BodyEncoding:
        """Explicit encoding → implied by files/binary → the endpoint's declared one → JSON."""
        if self.encoding is not None:
            return self.encoding
        if self.binary is not None:
            return BodyEncoding.BINARY
        if self.files:
            return BodyEncoding.MULTIPART
        return endpoint_default or BodyEncoding.JSON


@dataclass(frozen=True, slots=True)
class BodyLimits:
    """Safety limits applied while encoding a body.

    ``max_bytes`` caps the whole encoded body. ``allowed_roots`` — when not ``None`` —
    confines file ``path`` parts to those directories (symlinks are resolved first, so a
    link can't escape). ``None`` means unrestricted (trusted in-process callers); the
    MCP server always passes roots.
    """

    max_bytes: int = DEFAULT_MAX_BODY_BYTES
    allowed_roots: tuple[Path, ...] | None = None

    @classmethod
    def build(
        cls,
        max_bytes: int | None = None,
        allowed_roots: Sequence[str | os.PathLike[str]] | None = None,
    ) -> BodyLimits:
        roots = None if allowed_roots is None else tuple(Path(r).expanduser().resolve() for r in allowed_roots)
        return cls(max_bytes=max_bytes if max_bytes is not None else DEFAULT_MAX_BODY_BYTES, allowed_roots=roots)


@dataclass(slots=True)
class EncodedBody:
    """A request body ready for the wire.

    For ``json`` the payload is ``json`` (httpx serializes it as before); for every
    other encoding it is ``content`` (bytes) and ``headers`` carries the Content-Type
    (and Content-Disposition for a named raw upload).
    """

    encoding: BodyEncoding
    json: Any = None
    content: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    file_count: int = 0

    @property
    def size(self) -> int:
        return len(self.content) if self.content is not None else 0


def _too_large(limit: int) -> BodyTooLargeError:
    mib = limit / (1024 * 1024)
    return BodyTooLargeError(
        f"Request body exceeds the {limit}-byte limit ({mib:g} MiB)",
        recovery=Recovery(
            hint="Send a smaller file, or raise the limit (max_body_bytes / LIQUID_MAX_BODY_BYTES)",
            retry_safe=False,
        ),
        details={"limit_bytes": limit},
    )


def _file_error(message: str, hint: str) -> FileAccessError:
    return FileAccessError(message, recovery=Recovery(hint=hint, retry_safe=False))


def _flatten_fields(fields: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Flatten a nested dict to ``(name, value)`` pairs for form/multipart fields.

    Nested dicts use bracket notation (``a[b]=1``), a list of scalars repeats the
    key (``tag=a&tag=b``), a list of containers is indexed (``a[0][b]``), ``None``
    is dropped and booleans become ``true`` / ``false``.
    """
    pairs: list[tuple[str, str]] = []

    def walk(name: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key, sub in value.items():
                walk(f"{name}[{key}]", sub)
        elif isinstance(value, list | tuple):
            for i, item in enumerate(value):
                walk(f"{name}[{i}]" if isinstance(item, dict | list | tuple) else name, item)
        elif isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        elif isinstance(value, bytes):
            pairs.append((name, value.decode("utf-8", errors="replace")))
        else:
            pairs.append((name, str(value)))

    for key, value in fields.items():
        walk(str(key), value)
    return pairs


def _clean_filename(name: str | None, fallback: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/"))
    base = _CONTROL_CHARS.sub("", base).strip()
    return base or fallback


def _quote(value: str) -> str:
    """Escape a value for a quoted Content-Disposition parameter (WHATWG form-data rules)."""
    return value.replace("\r", "%0D").replace("\n", "%0A").replace('"', "%22")


def _guess_content_type(filename: str | None, explicit: str | None) -> str:
    if explicit:
        return _CONTROL_CHARS.sub("", explicit).strip() or "application/octet-stream"
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def _resolve_path(part_path: str, limits: BodyLimits) -> Path:
    candidate = Path(part_path).expanduser()
    roots = limits.allowed_roots
    if roots is not None and not candidate.is_absolute():
        if not roots:
            raise _file_error("No upload root is configured", "Configure an allowed upload root")
        candidate = roots[0] / candidate
    resolved = candidate.resolve()  # follows symlinks; non-strict so a miss is reported after the root check
    if roots is not None and not any(resolved.is_relative_to(root) for root in roots):
        raise _file_error(
            "File path is outside the allowed upload roots",
            "Use a file under one of the allowed upload roots (LIQUID_FILE_ROOTS), or send content_base64",
        )
    return resolved


def _read_file_sync(part_path: str, limits: BodyLimits, budget: int) -> tuple[bytes, str]:
    resolved = _resolve_path(part_path, limits)
    try:
        if not resolved.is_file():
            raise _file_error("File not found or not a regular file", "Check the file path")
        if resolved.stat().st_size > budget:
            raise _too_large(limits.max_bytes)
        with resolved.open("rb") as fh:
            data = fh.read(budget + 1)  # bounded read: a file that grew since stat() is still refused
    except OSError as exc:
        raise _file_error(
            f"File could not be read ({type(exc).__name__})", "Check the file path and permissions"
        ) from exc
    if len(data) > budget:
        raise _too_large(limits.max_bytes)
    return data, resolved.name


def _decode_base64(text: str, limits: BodyLimits, budget: int) -> bytes:
    compact = "".join(text.split())
    if len(compact) * 3 // 4 - 2 > budget:  # cheap pre-check, before allocating the decoded bytes
        raise _too_large(limits.max_bytes)
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RequestBodyError(
            "content_base64 is not valid base64",
            recovery=Recovery(hint="Send standard base64 (RFC 4648) without a data: prefix", retry_safe=False),
        ) from exc
    if len(data) > budget:
        raise _too_large(limits.max_bytes)
    return data


@dataclass(slots=True)
class _LoadedFile:
    data: bytes
    filename: str | None
    content_type: str


async def _load_part(part: FilePart, limits: BodyLimits, budget: int) -> _LoadedFile:
    """Materialise a file part into bytes, never reading past ``budget`` bytes."""
    if part.path is not None:
        data, default_name = await asyncio.to_thread(_read_file_sync, part.path, limits, budget)
        filename = _clean_filename(part.filename, default_name)
    elif part.content_base64 is not None:
        data = _decode_base64(part.content_base64, limits, budget)
        filename = _clean_filename(part.filename, "") or None
    else:
        data = part.content or b""
        if len(data) > budget:
            raise _too_large(limits.max_bytes)
        filename = _clean_filename(part.filename, "") or None
    return _LoadedFile(data=data, filename=filename, content_type=_guess_content_type(filename, part.content_type))


def _content_disposition(filename: str) -> str:
    """``attachment; filename=…`` for a raw upload (WordPress ``/media`` requires it)."""
    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        from urllib.parse import quote

        return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    return f'attachment; filename="{_quote(filename)}"'


async def encode_body(
    encoding: BodyEncoding,
    fields: dict[str, Any],
    body: RequestBody | None = None,
    limits: BodyLimits | None = None,
) -> EncodedBody:
    """Encode ``fields`` (+ files) for the wire, enforcing ``limits``.

    Raises :class:`~liquid.exceptions.BodyTooLargeError`, :class:`~liquid.exceptions.FileAccessError`
    or :class:`~liquid.exceptions.RequestBodyError`. Never logs or echoes file contents.
    """
    limits = limits or BodyLimits()
    body = body or RequestBody()

    if encoding == BodyEncoding.JSON:
        return EncodedBody(encoding=encoding, json=fields)

    if encoding == BodyEncoding.FORM:
        encoded = urlencode(_flatten_fields(fields)).encode("ascii")
        if len(encoded) > limits.max_bytes:
            raise _too_large(limits.max_bytes)
        return EncodedBody(
            encoding=encoding,
            content=encoded,
            headers={"Content-Type": _CONTENT_TYPE_BY_ENCODING[BodyEncoding.FORM]},
        )

    if encoding == BodyEncoding.BINARY:
        if body.binary is None:
            raise RequestBodyError(
                "A binary body needs a file (path, content or content_base64)",
                recovery=Recovery(hint="Pass binary={'path': ...} or binary={'content_base64': ...}", retry_safe=False),
            )
        loaded = await _load_part(body.binary, limits, limits.max_bytes)
        headers = {"Content-Type": loaded.content_type}
        if loaded.filename:
            headers["Content-Disposition"] = _content_disposition(loaded.filename)
        return EncodedBody(encoding=encoding, content=loaded.data, headers=headers, file_count=1)

    # multipart/form-data
    boundary = f"liquid-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    used = 0
    for name, value in _flatten_fields(fields):
        chunk = f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"\r\n\r\n{value}\r\n'.encode()
        used += len(chunk)
        if used > limits.max_bytes:
            raise _too_large(limits.max_bytes)
        chunks.append(chunk)
    for part in body.files:
        # Reserve room for this part's framing so the *encoded* body stays within the cap.
        filename_probe = _clean_filename(part.filename or part.path, "file")
        head_room = len(boundary) + len(part.field) + 2 * len(filename_probe) + 200
        budget = limits.max_bytes - used - head_room
        if budget < 0:
            raise _too_large(limits.max_bytes)
        loaded = await _load_part(part, limits, budget)
        filename = loaded.filename or _clean_filename(None, "file")
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(part.field)}"; '
            f'filename="{_quote(filename)}"\r\nContent-Type: {loaded.content_type}\r\n\r\n'
        ).encode()
        used += len(head) + len(loaded.data) + 2
        if used > limits.max_bytes:
            raise _too_large(limits.max_bytes)
        chunks.extend((head, loaded.data, b"\r\n"))
    chunks.append(f"--{boundary}--\r\n".encode())
    return EncodedBody(
        encoding=encoding,
        content=b"".join(chunks),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        file_count=len(body.files),
    )
