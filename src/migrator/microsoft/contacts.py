from __future__ import annotations

import logging
from typing import Any

import httpx

from .graph_client import GRAPH_BASE, GraphClient

log = logging.getLogger(__name__)


def ensure_contact_folder(gc: GraphClient, ms_user_id: str, display_name: str) -> str:
    """Return the Graph ID of the contact folder, creating it if absent.

    Lists with pagination (Graph returns only 10 folders per page by default,
    so a naive single GET misses folders past page one and then 409s on create)
    and resolves a 409 duplicate-name create by re-listing — same pattern as the
    mail and drive folder writers."""
    path = f"/users/{ms_user_id}/contactFolders"
    existing = _find_contact_folder(gc, ms_user_id, path, display_name)
    if existing:
        return existing
    try:
        created = gc.post(path, user_key=ms_user_id, json={"displayName": display_name})
        return str(created["id"])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise
        resolved = _find_contact_folder(gc, ms_user_id, path, display_name)
        if resolved:
            return resolved
        raise


def _find_contact_folder(
    gc: GraphClient, ms_user_id: str, path: str, display_name: str
) -> str | None:
    for page in gc.paginate(
        path, user_key=ms_user_id, params={"$top": 100, "$select": "id,displayName"}
    ):
        for folder in page:
            if folder.get("displayName") == display_name:
                return str(folder["id"])
    return None


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


def update_contact(
    gc: GraphClient,
    ms_user_id: str,
    contact_id: str,
    contact_body: dict[str, Any],
) -> None:
    """PATCH an already-migrated contact in place (delta modifications)."""
    gc.patch(f"/users/{ms_user_id}/contacts/{contact_id}", user_key=ms_user_id, json=contact_body)


def delete_contact(gc: GraphClient, ms_user_id: str, contact_id: str) -> None:
    """DELETE a migrated contact whose source copy was deleted. Already gone
    (404) counts as done."""
    try:
        gc.delete(
            f"/users/{ms_user_id}/contacts/{contact_id}",
            user_key=ms_user_id, quiet_statuses=(404,),
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise


def set_contact_photo(gc: GraphClient, ms_user_id: str, contact_id: str, data: bytes) -> None:
    """PUT the contact's photo (Graph stores/converts it; JPEG is canonical)."""
    gc.put_raw(
        f"{GRAPH_BASE}/users/{ms_user_id}/contacts/{contact_id}/photo/$value",
        data=data,
        user_key=ms_user_id,
        headers={"Content-Type": "image/jpeg"},
    )
