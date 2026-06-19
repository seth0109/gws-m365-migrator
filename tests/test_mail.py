"""Tests for MIME line-ending normalization in the mail writer.

Graph's MIME importer rejects bare-LF line endings with an opaque 400
UnableToDeserializePostBody; Gmail raw and Python's email re-serialization both
emit bare LF, so _post_mime must send CRLF.
"""
from __future__ import annotations

import base64

import pytest

from migrator.microsoft.mail import _normalize_crlf, import_mime_message


def test_bare_lf_becomes_crlf() -> None:
    assert _normalize_crlf(b"From: a\nTo: b\n\nbody\n") == b"From: a\r\nTo: b\r\n\r\nbody\r\n"


def test_existing_crlf_is_idempotent() -> None:
    crlf = b"From: a\r\nTo: b\r\n\r\nbody\r\n"
    assert _normalize_crlf(crlf) == crlf
    assert _normalize_crlf(_normalize_crlf(crlf)) == crlf


def test_lone_cr_becomes_crlf() -> None:
    assert _normalize_crlf(b"a\rb") == b"a\r\nb"


def test_mixed_endings_normalize() -> None:
    assert _normalize_crlf(b"a\r\nb\nc\rd") == b"a\r\nb\r\nc\r\nd"


class _CapturingGC:
    def __init__(self) -> None:
        self.sent_content: str | None = None
        self.sent_headers: dict[str, str] | None = None

    def post(self, path: str, user_key: str | None = None, **kwargs: object) -> dict[str, str]:
        self.sent_content = kwargs.get("content")  # type: ignore[assignment]
        self.sent_headers = kwargs.get("headers")  # type: ignore[assignment]
        return {"id": "msg-1"}


def test_import_mime_message_sends_crlf_normalized_body() -> None:
    gc = _CapturingGC()
    dest_id = import_mime_message(gc, "user-id", "inbox", b"From: a\nSubject: hi\n\nbody\n")  # type: ignore[arg-type]

    assert dest_id == "msg-1"
    assert gc.sent_headers == {"Content-Type": "text/plain"}
    decoded = base64.b64decode(gc.sent_content or "")
    assert b"\n" not in decoded.replace(b"\r\n", b"")  # no bare LF remains
    assert decoded == b"From: a\r\nSubject: hi\r\n\r\nbody\r\n"


def test_import_mime_message_rejects_empty_body() -> None:
    gc = _CapturingGC()
    with pytest.raises(ValueError, match="empty MIME body"):
        import_mime_message(gc, "user-id", "inbox", b"")  # type: ignore[arg-type]
