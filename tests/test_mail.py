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


def test_fallback_retries_with_cleaned_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "migrator.microsoft.mail._cleaned_mime", lambda _raw: b"CLEANED\r\n\r\nbody\r\n"
    )
    gc = _FlakyGC(fail_first=1)  # original 400s, cleaned retry succeeds
    dest = import_mime_message(gc, "u", "inbox", b"From: a\nSubject: hi\n\nbody\n")  # type: ignore[arg-type]
    assert dest == "msg-2"
    assert gc.calls == 2
    assert base64.b64decode(gc.posted[1]) == b"CLEANED\r\n\r\nbody\r\n"


def test_falls_back_to_json_when_cleanup_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cleanup can't help (returns None) -> JSON create is the last resort.
    monkeypatch.setattr("migrator.microsoft.mail._cleaned_mime", lambda _raw: None)
    monkeypatch.setattr(
        "migrator.microsoft.mail._import_via_json", lambda _gc, _u, _f, _raw: "json-id"
    )
    gc = _FlakyGC(fail_first=1)
    dest = import_mime_message(gc, "u", "inbox", b"From: a\nSubject: hi\n\nbody\n")  # type: ignore[arg-type]
    assert dest == "json-id"
    assert gc.calls == 1  # only the original MIME post was attempted on gc


def test_falls_back_to_json_when_cleaned_also_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "migrator.microsoft.mail._cleaned_mime", lambda _raw: b"CLEANED\r\n\r\nbody\r\n"
    )
    monkeypatch.setattr(
        "migrator.microsoft.mail._import_via_json", lambda _gc, _u, _f, _raw: "json-id"
    )
    gc = _FlakyGC(fail_first=2)  # both original and cleaned 400
    dest = import_mime_message(gc, "u", "inbox", b"From: a\nSubject: hi\n\nbody\n")  # type: ignore[arg-type]
    assert dest == "json-id"
    assert gc.calls == 2  # original + cleaned, then JSON (mocked)


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


def test_header_line_stats_finds_longest_lines() -> None:
    from migrator.microsoft.mail import _header_line_stats

    raw = (
        b"From: a@x.com\r\n"
        b"DKIM-Signature: " + b"A" * 3000 + b"\r\n"
        b"Subject: hi\r\n"
        b"\r\n"
        b"body\r\n"
    )
    stats = _header_line_stats(raw)
    assert stats[0][0] == "DKIM-Signature"
    assert stats[0][1] > 998


def test_header_line_stats_counts_folded_continuation() -> None:
    from migrator.microsoft.mail import _header_line_stats

    raw = (
        b"References: <a>\r\n " + b"B" * 2000 + b"\r\n"
        b"Subject: hi\r\n\r\nbody\r\n"
    )
    stats = dict(_header_line_stats(raw))
    assert stats["References"] > 1998


def test_maybe_dump_rejected_writes_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import migrator.microsoft.mail as mailmod

    monkeypatch.setenv("MIGRATOR_DUMP_REJECTED_MIME", str(tmp_path))
    monkeypatch.setattr(mailmod, "_dump_count", 0)
    mailmod._maybe_dump_rejected(b"From: a\r\n\r\nbody\r\n")
    dumps = list(tmp_path.glob("rejected_mime_*.eml"))
    assert len(dumps) == 1
    assert dumps[0].read_bytes() == b"From: a\r\n\r\nbody\r\n"


def test_maybe_dump_rejected_noop_without_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import migrator.microsoft.mail as mailmod

    monkeypatch.delenv("MIGRATOR_DUMP_REJECTED_MIME", raising=False)
    monkeypatch.setattr(mailmod, "_dump_count", 0)
    mailmod._maybe_dump_rejected(b"From: a\r\n\r\nbody\r\n")
    assert list(tmp_path.iterdir()) == []


def test_cleaned_mime_strips_trace_headers() -> None:
    from migrator.microsoft.mail import _cleaned_mime

    raw = (
        b"Received: by 10.0.0.1 with very long trace data\r\n"
        b"DKIM-Signature: a=rsa; b=AAAABBBBCCCC\r\n"
        b"From: alice@x.com\r\n"
        b"To: bob@y.com\r\n"
        b"Subject: hi\r\n"
        b"\r\n"
        b"body\r\n"
    )
    cleaned = _cleaned_mime(raw)
    assert cleaned is not None
    assert b"Received:" not in cleaned
    assert b"DKIM-Signature:" not in cleaned
    assert b"From: alice@x.com" in cleaned
    assert b"Subject: hi" in cleaned
    assert b"body" in cleaned


def test_import_via_json_builds_message() -> None:
    from email.message import EmailMessage

    from migrator.microsoft.mail import _import_via_json

    m = EmailMessage()
    m["Subject"] = "Hello"
    m["From"] = "Alice <alice@x.com>"
    m["To"] = "Bob <bob@y.com>, carol@z.com"
    m["Cc"] = "dan@w.com"
    m["Date"] = "Tue, 09 Dec 2025 13:42:55 -0800"
    m["Message-ID"] = "<abc123@x.com>"
    m.set_content("plain body")
    m.add_alternative("<p>html body</p>", subtype="html")
    m.add_attachment(b"FILEDATA", maintype="application", subtype="octet-stream", filename="f.bin")

    class _JsonGC:
        def __init__(self) -> None:
            self.json: dict[str, object] | None = None

        def post(self, path: str, user_key: str | None = None, **kwargs: object) -> dict[str, str]:
            self.json = kwargs.get("json")  # type: ignore[assignment]
            return {"id": "j1"}

    gc = _JsonGC()
    dest = _import_via_json(gc, "u", "inbox", m.as_bytes())  # type: ignore[arg-type]
    assert dest == "j1"
    gm = gc.json
    assert gm is not None
    assert gm["subject"] == "Hello"
    assert gm["body"]["contentType"] == "html"  # type: ignore[index]
    assert "html body" in gm["body"]["content"]  # type: ignore[index]
    assert len(gm["toRecipients"]) == 2  # type: ignore[arg-type]
    assert len(gm["ccRecipients"]) == 1  # type: ignore[arg-type]
    # fidelity fields
    assert gm["from"]["emailAddress"]["address"] == "alice@x.com"  # type: ignore[index]
    assert gm["internetMessageId"] == "<abc123@x.com>"
    assert str(gm["sentDateTime"]).startswith("2025-12-09T13:42:55")  # type: ignore[index]
    assert gm["receivedDateTime"] == gm["sentDateTime"]
    attachments = gm["attachments"]  # type: ignore[index]
    assert len(attachments) == 1
    assert attachments[0]["name"] == "f.bin"
    assert base64.b64decode(attachments[0]["contentBytes"]) == b"FILEDATA"


def test_import_via_json_retries_minimal_when_fidelity_rejected() -> None:
    from email.message import EmailMessage

    from migrator.microsoft.mail import _import_via_json

    m = EmailMessage()
    m["Subject"] = "Hi"
    m["From"] = "ext@sender.com"
    m["To"] = "owner@dest.com"
    m["Date"] = "Tue, 09 Dec 2025 13:42:55 -0800"
    m.set_content("body")

    class _PickyGC:
        """Rejects the first (fidelity) create with a 400, accepts the minimal one."""

        def __init__(self) -> None:
            self.bodies: list[dict[str, object]] = []

        def post(self, path: str, user_key: str | None = None, **kwargs: object) -> dict[str, str]:
            body = kwargs.get("json")
            self.bodies.append(body)  # type: ignore[arg-type]
            if "from" in body:  # type: ignore[operator]
                resp = httpx.Response(
                    400, text='{"error":{"code":"ErrorInvalidProperty"}}',
                    request=httpx.Request("POST", "https://g/x"),
                )
                raise httpx.HTTPStatusError("boom", request=resp.request, response=resp)
            return {"id": "minimal-1"}

    gc = _PickyGC()
    dest = _import_via_json(gc, "u", "inbox", m.as_bytes())  # type: ignore[arg-type]
    assert dest == "minimal-1"
    assert len(gc.bodies) == 2
    assert "from" in gc.bodies[0]
    assert "from" not in gc.bodies[1]
    assert "sentDateTime" not in gc.bodies[1]
