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
    # dates travel as MAPI extended properties (UTC), not JSON date fields
    assert "sentDateTime" not in gm
    props = {p["id"]: p["value"] for p in gm["singleValueExtendedProperties"]}  # type: ignore[union-attr, index]
    assert props["Integer 0x0E07"] == "1"  # non-draft, read
    assert props["SystemTime 0x0039"] == "2025-12-09T21:42:55Z"  # -0800 folded into UTC
    assert props["SystemTime 0x0E06"] == "2025-12-09T21:42:55Z"
    assert "Subject: Hello" in props["String 0x007D"]  # original header block
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
    # the reduced retry keeps the create-time flags/dates (the whole point of
    # the JSON path) and drops only the risky fidelity extras
    reduced_props = {p["id"] for p in gc.bodies[1]["singleValueExtendedProperties"]}  # type: ignore[index, union-attr]
    assert "Integer 0x0E07" in reduced_props
    assert "String 0x007D" not in reduced_props


class _RecordingGC:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []

    def post(self, path: str, user_key: str | None = None, **kwargs: object) -> dict[str, str]:
        self.posts.append((path, kwargs))
        return {"id": f"id-{len(self.posts)}"}


def _simple_mime(unread_marker: str = "body") -> bytes:
    from email.message import EmailMessage

    m = EmailMessage()
    m["Subject"] = "S"
    m["From"] = "a@x.com"
    m["To"] = "b@y.com"
    m["Date"] = "Tue, 09 Dec 2025 13:42:55 +0000"
    m.set_content(unread_marker)
    return m.as_bytes()


def test_import_json_message_unread_and_categories() -> None:
    from migrator.microsoft.mail import import_json_message

    gc = _RecordingGC()
    import_json_message(
        gc, "u", "inbox", _simple_mime(),  # type: ignore[arg-type]
        is_read=False, categories=["Migrated", "ProjectX"],
    )
    _, kwargs = gc.posts[0]
    gm = kwargs["json"]
    assert gm["isRead"] is False  # type: ignore[index]
    assert gm["categories"] == ["Migrated", "ProjectX"]  # type: ignore[index]
    props = {p["id"]: p["value"] for p in gm["singleValueExtendedProperties"]}  # type: ignore[index]
    assert props["Integer 0x0E07"] == "0"  # unread, still non-draft
    assert "flag" not in gm  # not starred -> no follow-up flag


def test_import_json_message_flagged_sets_followup_flag() -> None:
    from migrator.microsoft.mail import import_json_message

    gc = _RecordingGC()
    import_json_message(gc, "u", "inbox", _simple_mime(), is_flagged=True)  # type: ignore[arg-type]
    gm = gc.posts[0][1]["json"]
    assert gm["flag"] == {"flagStatus": "flagged"}  # type: ignore[index]


def test_patch_message_flags_includes_flag_and_categories() -> None:
    from migrator.microsoft.mail import patch_message_flags

    class _PatchGC:
        def __init__(self) -> None:
            self.body: dict[str, object] | None = None

        def patch(self, path: str, user_key: str | None = None, **kwargs: object) -> None:
            self.body = kwargs.get("json")  # type: ignore[assignment]

    gc = _PatchGC()
    patch_message_flags(gc, "u", "m1", is_read=False, categories=["Important"], is_flagged=True)  # type: ignore[arg-type]
    assert gc.body == {
        "isRead": False,
        "categories": ["Important"],
        "flag": {"flagStatus": "flagged"},
    }


def test_import_json_message_large_attachments_added_after_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from email.message import EmailMessage

    from migrator.microsoft.mail import import_json_message

    monkeypatch.setattr("migrator.microsoft.mail._MAX_INLINE_ATTACH_TOTAL", 4)
    m = EmailMessage()
    m["Subject"] = "S"
    m["To"] = "b@y.com"
    m.set_content("body")
    m.add_attachment(b"BIGDATA", maintype="application", subtype="octet-stream", filename="f.bin")

    gc = _RecordingGC()
    dest = import_json_message(gc, "u", "inbox", m.as_bytes())  # type: ignore[arg-type]
    assert dest == "id-1"
    create_path, create_kwargs = gc.posts[0]
    assert "attachments" not in create_kwargs["json"]  # type: ignore[operator]
    attach_path, attach_kwargs = gc.posts[1]
    assert attach_path == "/users/u/messages/id-1/attachments"
    assert base64.b64decode(attach_kwargs["json"]["contentBytes"]) == b"BIGDATA"  # type: ignore[index]


def test_import_message_dispatches_on_mode() -> None:
    from migrator.microsoft.mail import import_message

    json_gc = _RecordingGC()
    import_message(json_gc, "u", "inbox", _simple_mime(), mode="json")  # type: ignore[arg-type]
    assert "json" in json_gc.posts[0][1]

    mime_gc = _RecordingGC()
    import_message(mime_gc, "u", "inbox", _simple_mime(), mode="mime")  # type: ignore[arg-type]
    assert "content" in mime_gc.posts[0][1]  # raw base64 MIME post


def test_received_date_preferred_over_date_header() -> None:
    from email import message_from_bytes, policy

    from migrator.microsoft.mail import _extended_properties

    raw = (
        b"Received: from mx.example (mx.example) by mail.example; "
        b"Wed, 10 Dec 2025 08:00:00 +0000\r\n"
        b"From: a@x.com\r\n"
        b"Date: Tue, 09 Dec 2025 13:42:55 +0000\r\n"
        b"Subject: hi\r\n\r\nbody\r\n"
    )
    msg = message_from_bytes(raw, policy=policy.default)
    props = {p["id"]: p["value"] for p in _extended_properties(
        msg, raw, is_read=True, include_headers=False
    )}
    assert props["SystemTime 0x0039"] == "2025-12-09T13:42:55Z"  # sent = Date header
    assert props["SystemTime 0x0E06"] == "2025-12-10T08:00:00Z"  # received = Received hop


# ── attached emails (message/rfc822) ─────────────────────────────────────────


def _forward_with_attached_message() -> bytes:
    from email.message import EmailMessage

    inner = EmailMessage()
    inner["From"] = "ceo@corp.example"
    inner["Subject"] = "CONFIDENTIAL plan"
    inner.set_content("Inner body: do not forward.")

    outer = EmailMessage()
    outer["From"] = "me@corp.example"
    outer["Subject"] = "FYI"
    outer.set_content("Outer body: see attached.")
    outer.add_attachment(inner, filename="original.eml")  # message/rfc822, attachment
    return outer.as_bytes()


def test_json_body_keeps_attached_message_as_eml() -> None:
    import email
    from email import policy

    from migrator.microsoft.mail import _graph_body_and_attachments

    msg = email.message_from_bytes(_forward_with_attached_message(), policy=policy.default)
    body, attachments = _graph_body_and_attachments(msg)
    # The inner body must not be spliced into ours...
    assert body["content"].strip() == "Outer body: see attached."
    # ...it travels as an .eml file attachment instead.
    assert [a["name"] for a in attachments] == ["original.eml"]
    assert attachments[0]["contentType"] == "message/rfc822"
    eml = base64.b64decode(attachments[0]["contentBytes"])
    assert b"CONFIDENTIAL plan" in eml and b"do not forward" in eml


def test_split_large_attachments_extracts_attached_message_whole() -> None:
    from migrator.microsoft.mail import _split_large_attachments

    stripped, attachments = _split_large_attachments(_forward_with_attached_message())
    assert [(name, ctype) for name, ctype, _ in attachments] == [("original.eml", "message/rfc822")]
    assert b"CONFIDENTIAL plan" not in stripped
    assert b"Outer body" in stripped


def test_split_large_attachments_leaves_inline_forward_alone() -> None:
    from email.message import EmailMessage

    from migrator.microsoft.mail import _split_large_attachments

    inner = EmailMessage()
    inner["Subject"] = "quoted"
    inner.set_content("inner")
    outer = EmailMessage()
    outer["Subject"] = "Fwd"
    outer.set_content("outer")
    outer.add_attachment(inner, disposition="inline")  # inline forward, not an attachment

    stripped, attachments = _split_large_attachments(outer.as_bytes())
    assert attachments == []
    assert b"inner" in stripped  # kept in place, not descended into
