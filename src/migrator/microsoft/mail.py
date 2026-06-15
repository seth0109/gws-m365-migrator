from __future__ import annotations

import base64
import logging
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
            return folder["id"]

    created = gc.post(path, user_key=ms_user_id, json={"displayName": display_name})
    return created["id"]


def import_mime_message(
    gc: GraphClient,
    ms_user_id: str,
    folder_id: str,
    raw_mime: bytes,
) -> str:
    """Upload a raw MIME message as base64 to the given mail folder."""
    b64 = base64.b64encode(raw_mime).decode()
    result = gc.post(
        f"/users/{ms_user_id}/mailFolders/{folder_id}/messages",
        user_key=ms_user_id,
        content=b64,
        headers={
            "Content-Type": "text/plain",
        },
    )
    return result["id"]


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
