from __future__ import annotations

import base64
import email
import logging
from email import policy
from typing import Any

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
    """Return the ID of the named folder, creating it (and parent chain) if absent."""
    if parent_folder_id:
        path = f"/users/{ms_user_id}/mailFolders/{parent_folder_id}/childFolders"
    else:
        path = f"/users/{ms_user_id}/mailFolders"

    existing = gc.get(path, user_key=ms_user_id)
    for folder in existing.get("value", []):
        if folder["displayName"] == display_name:
            return str(folder["id"])

    created = gc.post(path, user_key=ms_user_id, json={"displayName": display_name})
    return str(created["id"])


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
    over 3 MB) so the body/headers keep full MIME fidelity."""
    if not raw_mime:
        # An empty body POSTs as "" and Graph rejects it with the opaque
        # UnableToDeserializePostBody 400. Fail with a clear reason instead.
        raise ValueError("source returned empty MIME body; nothing to import")
    # Normalize up front so the small/large threshold reflects what we'll actually
    # send (CRLF expansion grows the message); _post_mime re-normalizes too, since
    # the large-attachment path re-serializes back to bare LF.
    raw_mime = _normalize_crlf(raw_mime)
    if len(raw_mime) <= _MAX_MIME_SINGLE_POST:
        return _post_mime(gc, ms_user_id, folder_id, raw_mime)

    stripped, attachments = _split_large_attachments(raw_mime)
    message_id = _post_mime(gc, ms_user_id, folder_id, stripped)
    for name, content_type, data in attachments:
        _add_attachment(gc, ms_user_id, message_id, name, content_type, data)
    return message_id


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
