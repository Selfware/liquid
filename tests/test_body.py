"""Request bodies beyond JSON: form-urlencoded, multipart/form-data, raw binary."""

from __future__ import annotations

import base64
import logging
import os
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qsl

import pytest
from pydantic import ValidationError

from liquid.body import (
    DEFAULT_MAX_BODY_BYTES,
    BodyEncoding,
    BodyLimits,
    FilePart,
    RequestBody,
    encode_body,
    encoding_for_content_type,
    preferred_encoding,
)
from liquid.exceptions import BodyTooLargeError, FileAccessError, RequestBodyError

SECRET = b"TOP-SECRET-FILE-CONTENTS-0123456789"


def parse_multipart(content: bytes, content_type: str) -> dict[str, tuple[str | None, str, bytes]]:
    """Parse a multipart body back into {field: (filename, content_type, payload)}."""
    raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + content
    msg = BytesParser(policy=policy.HTTP).parsebytes(raw)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = (part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
    return parts


# --- content-type → encoding -------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/json", BodyEncoding.JSON),
        ("application/json; charset=utf-8", BodyEncoding.JSON),
        ("application/vnd.api+json", BodyEncoding.JSON),
        ("application/x-www-form-urlencoded", BodyEncoding.FORM),
        ("multipart/form-data", BodyEncoding.MULTIPART),
        ("Multipart/Form-Data; boundary=x", BodyEncoding.MULTIPART),
        ("audio/wav", BodyEncoding.BINARY),
        ("application/octet-stream", BodyEncoding.BINARY),
        ("image/png", BodyEncoding.BINARY),
        ("*/*", BodyEncoding.BINARY),
    ],
)
def test_encoding_for_content_type(content_type, expected):
    assert encoding_for_content_type(content_type) == expected


def test_preferred_encoding_json_wins_otherwise_first_declared():
    assert preferred_encoding([]) is None
    assert preferred_encoding(["multipart/form-data", "application/json"]) == BodyEncoding.JSON
    assert preferred_encoding(["multipart/form-data", "application/x-www-form-urlencoded"]) == BodyEncoding.MULTIPART
    assert preferred_encoding(["audio/wav"]) == BodyEncoding.BINARY


# --- models -------------------------------------------------------------------


def test_file_part_needs_exactly_one_source():
    with pytest.raises(ValidationError):
        FilePart()
    with pytest.raises(ValidationError):
        FilePart(path="/x", content=b"y")
    assert FilePart(content=b"y").field == "file"


def test_file_part_hides_contents_in_repr_and_errors():
    part = FilePart(content=SECRET, content_base64=None)
    assert "TOP-SECRET" not in repr(part)
    assert "TOP-SECRET" not in str(part)
    with pytest.raises(ValidationError) as exc:
        FilePart(content=SECRET, content_base64=base64.b64encode(SECRET).decode())
    assert "TOP-SECRET" not in str(exc.value)
    assert base64.b64encode(SECRET).decode() not in str(exc.value)


def test_request_body_consistency():
    with pytest.raises(ValidationError):
        RequestBody(files=[FilePart(content=b"x")], binary=FilePart(content=b"y"))
    with pytest.raises(ValidationError):
        RequestBody(encoding="form", files=[FilePart(content=b"x")])
    with pytest.raises(ValidationError):
        RequestBody(encoding="json", binary=FilePart(content=b"y"))
    with pytest.raises(ValidationError):
        RequestBody(encoding="binary")


def test_resolve_encoding_precedence():
    assert RequestBody().resolve_encoding() == BodyEncoding.JSON
    assert RequestBody().resolve_encoding(BodyEncoding.FORM) == BodyEncoding.FORM
    assert RequestBody(files=[FilePart(content=b"x")]).resolve_encoding(BodyEncoding.FORM) == BodyEncoding.MULTIPART
    assert RequestBody(binary=FilePart(content=b"x")).resolve_encoding() == BodyEncoding.BINARY
    assert RequestBody(encoding="multipart").resolve_encoding(BodyEncoding.JSON) == BodyEncoding.MULTIPART


# --- JSON ---------------------------------------------------------------------


async def test_json_is_passed_through_untouched():
    fields = {"a": 1, "b": {"c": [1, 2]}}
    enc = await encode_body(BodyEncoding.JSON, fields)
    assert enc.json == fields
    assert enc.content is None
    assert enc.headers == {}


# --- form-urlencoded ----------------------------------------------------------


async def test_form_encodes_fields():
    enc = await encode_body(BodyEncoding.FORM, {"To": "+15551234567", "Body": "hello world & more", "n": 3})
    assert enc.headers == {"Content-Type": "application/x-www-form-urlencoded"}
    assert parse_qsl(enc.content.decode()) == [("To", "+15551234567"), ("Body", "hello world & more"), ("n", "3")]


async def test_form_flattens_nested_lists_booleans_and_drops_none():
    fields = {
        "meta": {"a": "1", "b": {"c": "2"}},
        "tag": ["x", "y"],
        "items": [{"id": 1}, {"id": 2}],
        "on": True,
        "off": False,
        "skip": None,
    }
    enc = await encode_body(BodyEncoding.FORM, fields)
    pairs = parse_qsl(enc.content.decode())
    assert pairs == [
        ("meta[a]", "1"),
        ("meta[b][c]", "2"),
        ("tag", "x"),
        ("tag", "y"),
        ("items[0][id]", "1"),
        ("items[1][id]", "2"),
        ("on", "true"),
        ("off", "false"),
    ]


async def test_form_unicode_roundtrip():
    enc = await encode_body(BodyEncoding.FORM, {"name": "Zoë Ünïcode"})
    assert dict(parse_qsl(enc.content.decode())) == {"name": "Zoë Ünïcode"}


# --- multipart ----------------------------------------------------------------


async def test_multipart_fields_and_files_from_all_three_sources(tmp_path):
    on_disk = tmp_path / "photo.png"
    on_disk.write_bytes(b"\x89PNG-bytes")
    body = RequestBody(
        files=[
            FilePart(field="file", path=str(on_disk)),
            FilePart(field="raw", content=b"\x00\x01\x02binary", filename="blob.bin"),
            FilePart(
                field="b64",
                content_base64=base64.b64encode(b"hello").decode(),
                filename="note.txt",
                content_type="text/plain",
            ),
        ]
    )
    enc = await encode_body(BodyEncoding.MULTIPART, {"title": "My photo", "status": "publish"}, body)
    assert enc.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert enc.file_count == 3
    parts = parse_multipart(enc.content, enc.headers["Content-Type"])
    assert parts["title"][2] == b"My photo"
    assert parts["status"][2] == b"publish"
    # path source: filename from the path, content-type guessed from the extension
    assert parts["file"] == ("photo.png", "image/png", b"\x89PNG-bytes")
    # raw bytes: explicit filename, unknown extension → octet-stream
    assert parts["raw"] == ("blob.bin", "application/octet-stream", b"\x00\x01\x02binary")
    # base64: decoded, explicit content-type honoured
    assert parts["b64"] == ("note.txt", "text/plain", b"hello")


async def test_multipart_fields_only_is_still_multipart():
    enc = await encode_body(BodyEncoding.MULTIPART, {"a": "1", "nested": {"b": "2"}})
    parts = parse_multipart(enc.content, enc.headers["Content-Type"])
    assert parts["a"][2] == b"1"
    assert parts["nested[b]"][2] == b"2"


async def test_multipart_filename_is_sanitised():
    body = RequestBody(files=[FilePart(content=b"x", filename='../../etc/pa"ss\r\nwd.txt')])
    enc = await encode_body(BodyEncoding.MULTIPART, {}, body)
    head = enc.content.split(b"\r\n\r\n", 1)[0].decode()
    assert "../" not in head
    assert head.count("\r\n") == 2  # boundary line, Content-Disposition, Content-Type — no injected header lines
    parts = parse_multipart(enc.content, enc.headers["Content-Type"])
    # directory parts dropped, control characters stripped, the quote escaped (WHATWG %22)
    assert parts["file"][0] == "pa%22sswd.txt"


async def test_multipart_unicode_filename():
    body = RequestBody(files=[FilePart(content=b"x", filename="отчёт.pdf")])
    enc = await encode_body(BodyEncoding.MULTIPART, {}, body)
    assert parse_multipart(enc.content, enc.headers["Content-Type"])["file"][0] == "отчёт.pdf"


# --- binary -------------------------------------------------------------------


async def test_binary_body_with_content_type_and_filename():
    body = RequestBody(binary=FilePart(content=b"RIFFxxxxWAVE", content_type="audio/wav", filename="call.wav"))
    enc = await encode_body(BodyEncoding.BINARY, {}, body)
    assert enc.content == b"RIFFxxxxWAVE"
    assert enc.headers["Content-Type"] == "audio/wav"
    assert enc.headers["Content-Disposition"] == 'attachment; filename="call.wav"'


async def test_binary_from_path_guesses_type_and_names_the_file(tmp_path):
    f = tmp_path / "clip.mp3"
    f.write_bytes(b"ID3data")
    enc = await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=str(f))))
    assert enc.content == b"ID3data"
    assert enc.headers["Content-Type"] == "audio/mpeg"
    assert enc.headers["Content-Disposition"] == 'attachment; filename="clip.mp3"'


async def test_binary_anonymous_has_no_content_disposition():
    enc = await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(content=b"x")))
    assert enc.headers == {"Content-Type": "application/octet-stream"}


async def test_binary_non_ascii_filename_uses_rfc5987():
    enc = await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(content=b"x", filename="отчёт.pdf")))
    assert enc.headers["Content-Disposition"].startswith("attachment; filename*=UTF-8''")
    assert "%D0%BE" in enc.headers["Content-Disposition"]


async def test_binary_requires_a_file():
    with pytest.raises(RequestBodyError):
        await encode_body(BodyEncoding.BINARY, {}, RequestBody())


# --- size cap -----------------------------------------------------------------


def test_default_cap_is_25_mib():
    assert DEFAULT_MAX_BODY_BYTES == 25 * 1024 * 1024
    assert BodyLimits().max_bytes == 25 * 1024 * 1024
    assert BodyLimits.build().max_bytes == 25 * 1024 * 1024
    assert BodyLimits.build(max_bytes=10).max_bytes == 10


async def test_cap_rejects_oversized_bytes_binary_and_multipart():
    limits = BodyLimits(max_bytes=100)
    big = FilePart(content=b"x" * 101)
    with pytest.raises(BodyTooLargeError) as exc:
        await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=big), limits)
    assert exc.value.details == {"limit_bytes": 100}
    with pytest.raises(BodyTooLargeError):
        await encode_body(BodyEncoding.MULTIPART, {}, RequestBody(files=[big]), limits)


async def test_cap_at_exact_limit_is_accepted_for_binary():
    enc = await encode_body(
        BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(content=b"x" * 100)), BodyLimits(max_bytes=100)
    )
    assert enc.size == 100


async def test_cap_counts_the_whole_multipart_body_not_just_one_file():
    limits = BodyLimits(max_bytes=600)
    files = [FilePart(field=f"f{i}", content=b"x" * 150, filename="a.bin") for i in range(4)]
    with pytest.raises(BodyTooLargeError):
        await encode_body(BodyEncoding.MULTIPART, {}, RequestBody(files=files), limits)
    # the same content fits when the cap leaves room for the framing
    enc = await encode_body(BodyEncoding.MULTIPART, {}, RequestBody(files=files[:2]), BodyLimits(max_bytes=1000))
    assert enc.size <= 1000


async def test_cap_applies_to_plain_fields_and_form():
    with pytest.raises(BodyTooLargeError):
        await encode_body(BodyEncoding.FORM, {"a": "x" * 200}, limits=BodyLimits(max_bytes=100))
    with pytest.raises(BodyTooLargeError):
        await encode_body(BodyEncoding.MULTIPART, {"a": "x" * 200}, limits=BodyLimits(max_bytes=100))


async def test_oversized_file_is_refused_before_it_is_read(tmp_path, monkeypatch):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 5000)
    read_sizes: list[int] = []
    real_open = type(f).open

    def spy_open(self, *a, **k):  # would be called only if the size pre-check let it through
        read_sizes.append(self.stat().st_size)
        return real_open(self, *a, **k)

    monkeypatch.setattr(type(f), "open", spy_open)
    with pytest.raises(BodyTooLargeError):
        await encode_body(
            BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=str(f))), BodyLimits(max_bytes=1000)
        )
    assert read_sizes == []


async def test_oversized_base64_is_refused_before_decoding(monkeypatch):
    import liquid.body as body_mod

    def boom(*a, **k):
        raise AssertionError("decoded an oversized payload")

    monkeypatch.setattr(body_mod.base64, "b64decode", boom)
    huge = "A" * 4000
    with pytest.raises(BodyTooLargeError):
        await encode_body(
            BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(content_base64=huge)), BodyLimits(max_bytes=1000)
        )


async def test_invalid_base64_is_a_clean_error_without_echoing_input():
    bad = "not base64 !!! TOP-SECRET"
    with pytest.raises(RequestBodyError) as exc:
        await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(content_base64=bad)))
    assert "TOP-SECRET" not in str(exc.value)


async def test_base64_with_whitespace_is_accepted():
    payload = base64.b64encode(b"x" * 100).decode()
    spaced = payload[:40] + "\n" + payload[40:]
    enc = await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(content_base64=spaced)))
    assert enc.content == b"x" * 100


# --- allowed roots ------------------------------------------------------------


def _limits(root, **kw):
    return BodyLimits.build(allowed_roots=[root], **kw)


async def test_path_inside_root_is_allowed(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (root / "a.txt").write_bytes(b"hi")
    enc = await encode_body(
        BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=str(root / "a.txt"))), _limits(root)
    )
    assert enc.content == b"hi"


async def test_relative_path_resolves_against_first_root(tmp_path):
    root = tmp_path / "uploads"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_bytes(b"rel")
    enc = await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path="sub/a.txt")), _limits(root))
    assert enc.content == b"rel"


async def test_path_outside_root_is_refused(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(SECRET)
    with pytest.raises(FileAccessError) as exc:
        await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=str(secret))), _limits(root))
    assert "outside" in exc.value.message
    assert "TOP-SECRET" not in str(exc.value)


async def test_dotdot_traversal_is_refused(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (tmp_path / "secret.txt").write_bytes(SECRET)
    for p in ("../secret.txt", f"{root}/../secret.txt"):
        with pytest.raises(FileAccessError):
            await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=p)), _limits(root))


async def test_symlink_escaping_the_root_is_refused(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(SECRET)
    os.symlink(secret, root / "innocent.txt")
    with pytest.raises(FileAccessError):
        await encode_body(
            BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=str(root / "innocent.txt"))), _limits(root)
        )


async def test_prefix_sibling_of_root_is_refused(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    sibling = tmp_path / "uploads-evil"
    sibling.mkdir()
    (sibling / "x.txt").write_bytes(b"x")
    with pytest.raises(FileAccessError):
        await encode_body(
            BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=str(sibling / "x.txt"))), _limits(root)
        )


async def test_missing_file_and_directory_are_clean_errors(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    for p in (root / "nope.txt", root):
        with pytest.raises(FileAccessError):
            await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=str(p))), _limits(root))


async def test_no_roots_means_unrestricted(tmp_path):
    f = tmp_path / "anywhere.txt"
    f.write_bytes(b"ok")
    enc = await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(path=str(f))))
    assert enc.content == b"ok"


async def test_roots_do_not_restrict_inline_content():
    enc = await encode_body(
        BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(content=b"inline")), BodyLimits.build(allowed_roots=[])
    )
    assert enc.content == b"inline"


# --- never log contents -------------------------------------------------------


async def test_encoding_never_logs_contents(tmp_path, caplog):
    f = tmp_path / "s.bin"
    f.write_bytes(SECRET)
    caplog.set_level(logging.DEBUG)
    body = RequestBody(
        files=[
            FilePart(field="a", path=str(f)),
            FilePart(field="b", content=SECRET),
            FilePart(field="c", content_base64=base64.b64encode(SECRET).decode()),
        ]
    )
    await encode_body(BodyEncoding.MULTIPART, {"k": "v"}, body)
    await encode_body(BodyEncoding.BINARY, {}, RequestBody(binary=FilePart(content=SECRET)))
    for record in caplog.records:
        assert "TOP-SECRET" not in record.getMessage()
        assert base64.b64encode(SECRET).decode() not in record.getMessage()
