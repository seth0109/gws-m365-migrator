from __future__ import annotations

import base64
import email
import logging
from email import policy
from typing import Any

import httpx

from .graph_client import GraphClient

log = logging.getLogger(__name__)

# System label → well-known Outlook folder name
SYSTEM_FOLDER_MAP = {
    "INBOX": "Inbox",
    "SENT": "SentItems",
    "DRAFT": "Drafts",
    "TRASH": "DeletedItems",
    "SPAM": "JunkEmail",
}

# Canonical well-known folder tokens (as emitted by connectors via
# transform/labels.SYSTEM_LABEL_FOLDER and ImapSource) → Graph well-known folder
# identifiers usable directly as a mailFolder id in API paths. Mapping a system
# folder to its well-known id lands mail in the *real* Inbox/Sent/etc. instead of
# creating a duplicate custom folder with the same display name.
WELL_KNOWN_FOLDER_IDS = {
    "Inbox": "inbox",
    "SentItems": "sentitems",
    "Drafts": "drafts",
    "DeletedItems": "deleteditems",
    "JunkEmail": "junkemail",
    "Archive": "archive",
}

# Graph caps a single MIME `Create message` request at 4 MB (HTTP 413 above
# that). Keep raw MIME comfortably under that so the base64 body (~+33%) fits.
_MAX_MIME_SINGLE_POST = 3 * 1024 * 1024
# Graph requires an attachment upload session above 3 MB; below it a single POST
# to the attachments collection is allowed.
_LARGE_ATTACHMENT_THRESHOLD = 3 * 1024 * 1024
# Upload-session chunk: a multiple of 320 KiB and under the 4 MB per-PUT cap.
_ATTACHMENT_CHUNK = 10 * 320 * 1024


def resolve_folder_segment(part: str, is_top_level: bool) -> str | None:
    """Return the Graph well-known folder id for a top-level system-folder token,
    else None (caller creates/looks up a normal folder by display name).

    Only the top segment is matched: a user folder literally named "Inbox" nested
    under another folder is a genuine custom folder, not the well-known Inbox."""
    if is_top_level:
        return WELL_KNOWN_FOLDER_IDS.get(part)
    return None


def ensure_mail_folder(
    gc: GraphClient,
    ms_user_id: str,
    display_name: str,
    parent_folder_id: str | None = None,
) -> str:
    """Return the ID of the named folder, creating it if absent.

    Lists existing folders with pagination: Graph returns only 10 folders per
    page by default, so a single naive GET misses any folder past the first page
    and then 409s (ErrorFolderExists) on create. On a 409 (paging miss or a
    concurrent create) we re-resolve and return the existing folder's id."""
    if parent_folder_id:
        path = f"/users/{ms_user_id}/mailFolders/{parent_folder_id}/childFolders"
    else:
        path = f"/users/{ms_user_id}/mailFolders"

    existing = _find_folder_by_name(gc, ms_user_id, path, display_name)
    if existing:
        return existing

    try:
        created = gc.post(path, user_key=ms_user_id, json={"displayName": display_name})
        return str(created["id"])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise
        resolved = _find_folder_by_name(gc, ms_user_id, path, display_name)
        if resolved:
            return resolved
        raise


def _find_folder_by_name(
    gc: GraphClient, ms_user_id: str, path: str, display_name: str
) -> str | None:
    for page in gc.paginate(
        path, user_key=ms_user_id, params={"$top": 100, "$select": "id,displayName"}
    ):
        for folder in page:
            if folder.get("displayName") == display_name:
                return str(folder["id"])
    return None


def import_mime_message(
    gc: GraphClient,
    ms_user_id: str,
    folder_id: str,
    raw_mime: bytes,
) -> str:
    """Import a raw MIME message into the given mail folder, returning its id.

    Messages under Graph's 4 MB MIME request cap are posted directly. Larger
    messages are imported with their bulk attachments stripped out, then those
    attachments are re-added via the attachment APIs (upload session for parts
    over 3 MB) so the body/headers keep full MIME fidelity.

    If Graph rejects the raw MIME with UnableToDeserializePostBody (a malformed
    or non-conformant message it can't parse), we log a diagnostic of the
    offending content and retry once with the message re-serialized through
    Python's email engine (policy.SMTP), which rebuilds RFC-conformant headers,
    boundaries, and CRLF endings."""
    if not raw_mime:
        # An empty body POSTs as "" and Graph rejects it with the opaque
        # UnableToDeserializePostBody 400. Fail with a clear reason instead.
        raise ValueError("source returned empty MIME body; nothing to import")
    # Normalize up front so the small/large threshold reflects what we'll actually
    # send (CRLF expansion grows the message); _post_mime re-normalizes too, since
    # the large-attachment path re-serializes back to bare LF.
    raw_mime = _normalize_crlf(raw_mime)
    try:
        return _import_mime(gc, ms_user_id, folder_id, raw_mime)
    except httpx.HTTPStatusError as exc:
        if not _is_deserialize_400(exc):
            raise
        _log_mime_diagnostic(raw_mime)

    # Graph couldn't parse the raw MIME. Strip Gmail's bulky trace/auth headers
    # (Received/ARC/DKIM/X-Google-* — their long, often unfoldable lines are the
    # usual cause) and re-serialize to RFC-conformant bytes. This keeps full MIME
    # fidelity for from/date/recipients/body/attachments.
    cleaned = _cleaned_mime(raw_mime)
    if cleaned is not None and cleaned != raw_mime:
        try:
            log.warning("Retrying import with trace headers stripped (%d bytes)", len(cleaned))
            return _import_mime(gc, ms_user_id, folder_id, cleaned)
        except httpx.HTTPStatusError as exc:
            if not _is_deserialize_400(exc):
                raise

    # Last resort: build the message via the JSON API. Graph sets `from` to the
    # mailbox owner (original sender/timestamps not preserved), but the message
    # content, recipients, and attachments are migrated rather than dropped.
    log.warning(
        "MIME import failed after cleanup; falling back to JSON message create "
        "(sender/timestamp fidelity lost)"
    )
    return _import_via_json(gc, ms_user_id, folder_id, raw_mime)


def _import_mime(gc: GraphClient, ms_user_id: str, folder_id: str, raw_mime: bytes) -> str:
    """Core MIME import: single POST under the cap, else strip-and-reattach."""
    if len(raw_mime) <= _MAX_MIME_SINGLE_POST:
        return _post_mime(gc, ms_user_id, folder_id, raw_mime)

    stripped, attachments = _split_large_attachments(raw_mime)
    message_id = _post_mime(gc, ms_user_id, folder_id, stripped)
    for name, content_type, data in attachments:
        _add_attachment(gc, ms_user_id, message_id, name, content_type, data)
    return message_id


def _is_deserialize_400(exc: httpx.HTTPStatusError) -> bool:
    return (
        exc.response.status_code == 400
        and "UnableToDeserializePostBody" in exc.response.text
    )


# Gmail/transit trace + auth headers. They carry no mailbox value and their long,
# frequently unfoldable lines are the usual trigger for Graph's MIME deserializer
# to reject an otherwise-clean message. Stripped before the cleanup retry.
_TRACE_HEADERS = frozenset(
    {
        "received",
        "received-spf",
        "arc-seal",
        "arc-message-signature",
        "arc-authentication-results",
        "dkim-signature",
        "x-google-dkim-signature",
        "authentication-results",
        "authentication-results-original",
        "x-forwarded-encrypted",
        "x-forwarded-for",
        "x-forwarded-to",
        "x-received",
        "x-google-smtp-source",
        "x-gm-message-state",
        "x-gm-gmsgid",
        "x-gm-thrid",
        "x-gm-labels",
        "x-spam-status",
        "x-spam-score",
        "x-spam-flag",
        "x-spam-checker-version",
    }
)


def _cleaned_mime(raw_mime: bytes) -> bytes | None:
    """Strip bulky trace/auth headers and re-serialize to RFC-conformant bytes.

    Returns None if the message can't be parsed. The remaining headers and all
    body parts are preserved, so from/date/recipients/body/attachments survive."""
    try:
        msg = email.message_from_bytes(raw_mime, policy=policy.SMTP)
    except Exception as exc:  # noqa: BLE001 - best-effort fallback
        log.warning("Could not parse MIME for cleanup fallback: %s", exc)
        return None
    for name in {k.lower() for k in msg.keys()} & _TRACE_HEADERS:
        del msg[name]
    try:
        return msg.as_bytes(policy=policy.SMTP)
    except Exception as exc:  # noqa: BLE001 - best-effort fallback
        log.warning("Could not re-serialize cleaned MIME: %s", exc)
        return None


def _import_via_json(gc: GraphClient, ms_user_id: str, folder_id: str, raw_mime: bytes) -> str:
    """Create the message via the JSON message API (MIME-import last resort).

    Parses the MIME into a Graph message resource preserving as much fidelity as
    Graph allows on create: subject, body (HTML preferred), recipients, sender,
    reply-to, message id, and sent/received timestamps, plus attachments. If Graph
    rejects the writable-on-create identity/timestamp fields (tenant policy
    varies), retry with a minimal body so the message still migrates."""
    msg = email.message_from_bytes(raw_mime, policy=policy.default)
    path = f"/users/{ms_user_id}/mailFolders/{folder_id}/messages"

    try:
        result = gc.post(path, user_key=ms_user_id, json=_mime_to_graph_message(msg, fidelity=True))
        return str(result["id"])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 400:
            raise
        log.warning(
            "JSON create rejected fidelity fields (from/sender/dates); "
            "retrying with minimal message body"
        )
    result = gc.post(path, user_key=ms_user_id, json=_mime_to_graph_message(msg, fidelity=False))
    return str(result["id"])


def _mime_to_graph_message(msg: Any, *, fidelity: bool) -> dict[str, Any]:
    """Build a Graph message resource from a parsed email message.

    With ``fidelity=True`` includes the identity/timestamp fields that are
    writable on create (from/sender/replyTo/internetMessageId/sent+received
    DateTime); with ``fidelity=False`` only the always-accepted content fields."""
    body, attachments = _graph_body_and_attachments(msg)
    graph_msg: dict[str, Any] = {
        "subject": str(msg["Subject"] or ""),
        "body": body,
        "toRecipients": _graph_recipients(msg, "To"),
        "ccRecipients": _graph_recipients(msg, "Cc"),
        "bccRecipients": _graph_recipients(msg, "Bcc"),
    }
    if attachments:
        graph_msg["attachments"] = attachments
    if not fidelity:
        return graph_msg

    if (frm := _graph_recipients(msg, "From")):
        graph_msg["from"] = frm[0]
    if (sender := _graph_recipients(msg, "Sender")):
        graph_msg["sender"] = sender[0]
    if (reply_to := _graph_recipients(msg, "Reply-To")):
        graph_msg["replyTo"] = reply_to
    if msg["Message-ID"]:
        graph_msg["internetMessageId"] = str(msg["Message-ID"]).strip()
    if (sent := _parse_internet_date(msg["Date"])):
        graph_msg["sentDateTime"] = sent
        graph_msg["receivedDateTime"] = sent
    return graph_msg


def _parse_internet_date(value: Any) -> str | None:
    """RFC 2822 date header -> ISO 8601 string (with offset) for Graph, or None."""
    if not value:
        return None
    from datetime import UTC
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _graph_recipients(msg: Any, header: str) -> list[dict[str, Any]]:
    from email.utils import getaddresses

    out: list[dict[str, Any]] = []
    for name, addr in getaddresses(msg.get_all(header, [])):
        if not addr or "@" not in addr:
            continue
        ea: dict[str, str] = {"address": addr}
        if name:
            ea["name"] = name
        out.append({"emailAddress": ea})
    return out


def _graph_body_and_attachments(msg: Any) -> tuple[dict[str, str], list[dict[str, Any]]]:
    html_parts: list[str] = []
    text_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = part.get_content_disposition()
        filename = part.get_filename()
        is_attachment = disp == "attachment" or bool(filename) or (
            disp == "inline" and not ctype.startswith("text/")
        )
        if is_attachment:
            try:
                data: bytes = part.get_payload(decode=True) or b""
            except Exception:  # noqa: BLE001 - skip undecodable part content
                data = b""
            att: dict[str, Any] = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename or "attachment",
                "contentType": ctype or "application/octet-stream",
                "contentBytes": base64.b64encode(data).decode(),
            }
            cid = part.get("Content-ID")
            if cid:
                att["isInline"] = True
                att["contentId"] = str(cid).strip("<>")
            attachments.append(att)
        elif ctype == "text/html":
            html_parts.append(str(part.get_content()))
        elif ctype == "text/plain":
            text_parts.append(str(part.get_content()))
    if html_parts:
        return {"contentType": "html", "content": "".join(html_parts)}, attachments
    return {"contentType": "text", "content": "".join(text_parts)}, attachments


def _log_mime_diagnostic(raw_mime: bytes) -> None:
    """Surface what Graph choked on: the message's leading bytes (headers) plus a
    few structural flags. The Graph 400 body itself is uninformative."""
    head = raw_mime[:400]
    has_bare_lf = b"\n" in raw_mime.replace(b"\r\n", b"")
    non_ascii = any(b > 127 for b in raw_mime)
    has_8bit_cte = b"content-transfer-encoding: 8bit" in raw_mime[:8192].lower()
    max_line_len = max((len(line) for line in raw_mime.split(b"\r\n")), default=0)
    log.warning(
        "MIME rejected by Graph (size=%d, max_line=%d, bare_lf=%s, non_ascii=%s, "
        "8bit_cte=%s). Head: %r",
        len(raw_mime),
        max_line_len,
        has_bare_lf,
        non_ascii,
        has_8bit_cte,
        head,
    )


def _normalize_crlf(raw: bytes) -> bytes:
    """Force RFC 5322 CRLF line endings.

    Graph's MIME importer rejects bare-LF (or lone-CR) line endings with an
    opaque 400 UnableToDeserializePostBody. Gmail's ``format=raw`` and Python's
    ``email`` re-serialization (``policy.default`` uses ``\\n``) both emit bare
    LF, so normalize before sending. Collapsing to LF first makes this idempotent
    for messages that are already CRLF."""
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")


def _post_mime(gc: GraphClient, ms_user_id: str, folder_id: str, raw_mime: bytes) -> str:
    """Single-request MIME import (subject to Graph's 4 MB request cap)."""
    b64 = base64.b64encode(_normalize_crlf(raw_mime)).decode()
    result = gc.post(
        f"/users/{ms_user_id}/mailFolders/{folder_id}/messages",
        user_key=ms_user_id,
        content=b64,
        headers={"Content-Type": "text/plain"},
    )
    return str(result["id"])


def _split_large_attachments(raw_mime: bytes) -> tuple[bytes, list[tuple[str, str, bytes]]]:
    """Remove `Content-Disposition: attachment` parts from the MIME and return the
    stripped message bytes plus the extracted attachments as (name, type, bytes).

    Inline parts (e.g. cid: images referenced by the HTML body) are left in place
    so the body renders correctly."""
    msg = email.message_from_bytes(raw_mime, policy=policy.default)
    attachments: list[tuple[str, str, bytes]] = []

    # `part` is an email.message.EmailMessage; the stdlib stubs are too loose to
    # type the recursion usefully, so we keep it Any and coerce at the edges.
    def prune(part: Any) -> bool:
        """Return True if `part` is an attachment the parent should drop."""
        if part.is_multipart():
            kept = [child for child in part.iter_parts() if not prune(child)]
            part.set_payload(kept)
            return False
        if part.get_content_disposition() == "attachment":
            data: bytes = part.get_payload(decode=True) or b""
            name = str(part.get_filename() or "attachment")
            attachments.append((name, str(part.get_content_type()), data))
            return True
        return False

    prune(msg)
    return msg.as_bytes(), attachments


def _add_attachment(
    gc: GraphClient,
    ms_user_id: str,
    message_id: str,
    name: str,
    content_type: str,
    data: bytes,
) -> None:
    size = len(data)
    content_type = content_type or "application/octet-stream"

    if size <= _LARGE_ATTACHMENT_THRESHOLD:
        gc.post(
            f"/users/{ms_user_id}/messages/{message_id}/attachments",
            user_key=ms_user_id,
            json={
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": content_type,
                "contentBytes": base64.b64encode(data).decode(),
            },
        )
        return

    session = gc.post(
        f"/users/{ms_user_id}/messages/{message_id}/attachments/createUploadSession",
        user_key=ms_user_id,
        json={
            "AttachmentItem": {
                "attachmentType": "file",
                "name": name,
                "size": size,
                "contentType": content_type,
            }
        },
    )
    upload_url: str = session["uploadUrl"]
    offset = 0
    while offset < size:
        end = min(offset + _ATTACHMENT_CHUNK, size)
        chunk = data[offset:end]
        gc.put_raw(
            upload_url,
            data=chunk,
            user_key=ms_user_id,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end - 1}/{size}",
            },
        )
        offset = end


def patch_message_flags(
    gc: GraphClient,
    ms_user_id: str,
    message_id: str,
    is_read: bool,
    categories: list[str] | None = None,
) -> None:
    body: dict[str, Any] = {"isRead": is_read}
    if categories:
        body["categories"] = categories
    gc.patch(f"/users/{ms_user_id}/messages/{message_id}", user_key=ms_user_id, json=body)
