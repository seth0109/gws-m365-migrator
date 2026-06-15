from __future__ import annotations

import logging
from typing import Any

from .graph_client import GraphClient

log = logging.getLogger(__name__)


def ensure_contact_folder(gc: GraphClient, ms_user_id: str, display_name: str) -> str:
    """Return the Graph ID of the contact folder, creating it if absent."""
    folders = gc.get(f"/users/{ms_user_id}/contactFolders")
    for folder in folders.get("value", []):
        if folder["displayName"] == display_name:
            return folder["id"]
    created = gc.post(
        f"/users/{ms_user_id}/contactFolders",
        user_key=ms_user_id,
        json={"displayName": display_name},
    )
    return created["id"]


def create_contact(
    gc: GraphClient,
    ms_user_id: str,
    folder_id: str,
    contact_body: dict[str, Any],
) -> str:
    result = gc.post(
        f"/users/{ms_user_id}/contactFolders/{folder_id}/contacts",
        user_key=ms_user_id,
        json=contact_body,
    )
    return result["id"]
