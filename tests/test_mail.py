"""Tests for MIME line-ending normalization in the mail writer.

Graph's MIME importer rejects bare-LF line endings with an opaque 400
UnableToDeserializePostBody; Gmail raw and Python's email re-serialization both
emit bare LF, so _post_mime must send CRLF.
"""
from __future__ import annotations

import base64

import httpx
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


def _deserialize_400() -> httpx.HTTPStatusError:
    body = '{"error":{"code":"UnableToDeserializePostBody","message":"were unable to..."}}'
    resp = httpx.Response(400, text=body, request=httpx.Request("POST", "https://g/x"))
    return httpx.HTTPStatusError("boom", request=resp.request, response=resp)


class _FlakyGC:
    """Raises a deserialize-400 for the first `fail_first` posts, then succeeds."""

    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.calls = 0
        self.posted: list[str] = []

    def post(self, path: str, user_key: str | None = None, **kwargs: object) -> dict[str, str]:
        self.calls += 1
        self.posted.append(kwargs.get("content"))  # type: ignore[arg-type]
        if self.calls <= self.fail_first:
            raise _deserialize_400()
        return {"id": f"msg-{self.calls}"}


def test_fallback_retries_with_reserialized_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "migrator.microsoft.mail._rebuild_mime", lambda _raw: b"REBUILT\r\n\r\nbody\r\n"
    )
    gc = _FlakyGC(fail_first=1)
    dest = import_mime_message(gc, "u", "inbox", b"From: a\nSubject: hi\n\nbody\n")  # type: ignore[arg-type]
    assert dest == "msg-2"
    assert gc.calls == 2
    assert base64.b64decode(gc.posted[1]) == b"REBUILT\r\n\r\nbody\r\n"


def test_fallback_gives_up_when_rebuild_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-serialization produced identical bytes — nothing to gain, re-raise the 400.
    monkeypatch.setattr("migrator.microsoft.mail._rebuild_mime", lambda raw: raw)
    gc = _FlakyGC(fail_first=99)
    with pytest.raises(httpx.HTTPStatusError):
        import_mime_message(gc, "u", "inbox", b"From: a\nSubject: hi\n\nbody\n")  # type: ignore[arg-type]
    assert gc.calls == 1


def test_non_deserialize_400_propagates_without_fallback() -> None:
    class _GC:
        def post(self, path: str, user_key: str | None = None, **kwargs: object) -> dict[str, str]:
            resp = httpx.Response(
                400, text='{"error":{"code":"InvalidRequest"}}',
                request=httpx.Request("POST", "https://g/x"),
            )
            raise httpx.HTTPStatusError("boom", request=resp.request, response=resp)

    with pytest.raises(httpx.HTTPStatusError):
        import_mime_message(_GC(), "u", "inbox", b"From: a\n\nbody\n")  # type: ignore[arg-type]
