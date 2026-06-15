from __future__ import annotations

import logging
from typing import Any

from .graph_client import GRAPH_BASE, GraphClient

log = logging.getLogger(__name__)

# Chunk size must be a multiple of 320 KiB; use 10 MiB
_CHUNK_SIZE = 10 * 1024 * 1024


def ensure_folder(
    gc: GraphClient,
    ms_user_id: str,
    parent_id: str | None,
    folder_name: str,
    drive_path: str = "me/drive",
) -> str:
    """Create folder if absent, return its driveItem ID."""
    if parent_id:
        path = f"/users/{ms_user_id}/{drive_path}/items/{parent_id}/children"
    else:
        path = f"/users/{ms_user_id}/{drive_path}/root/children"

    existing = gc.get(path, user_key=ms_user_id, params={"$filter": f"name eq '{folder_name}' and folder ne null"})
    for item in existing.get("value", []):
        if item["name"] == folder_name:
            return item["id"]

    body = {
        "name": folder_name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    if parent_id:
        result = gc.post(
            f"/users/{ms_user_id}/{drive_path}/items/{parent_id}/children",
            user_key=ms_user_id,
            json=body,
        )
    else:
        result = gc.post(
            f"/users/{ms_user_id}/{drive_path}/root/children",
            user_key=ms_user_id,
            json=body,
        )
    return result["id"]


def upload_small_file(
    gc: GraphClient,
    ms_user_id: str,
    parent_id: str,
    file_name: str,
    content: bytes,
    drive_path: str = "me/drive",
) -> str:
    result = gc.post(
        f"/users/{ms_user_id}/{drive_path}/items/{parent_id}:/{file_name}:/content",
        user_key=ms_user_id,
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    return result["id"]


def upload_large_file(
    gc: GraphClient,
    ms_user_id: str,
    parent_id: str,
    file_name: str,
    content: bytes,
    drive_path: str = "me/drive",
) -> str:
    """Chunked upload session for files that may be large."""
    session = gc.post(
        f"/users/{ms_user_id}/{drive_path}/items/{parent_id}:/{file_name}:/createUploadSession",
        user_key=ms_user_id,
        json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
    )
    upload_url: str = session["uploadUrl"]

    total = len(content)
    offset = 0
    result = None
    while offset < total:
        end = min(offset + _CHUNK_SIZE, total)
        chunk = content[offset:end]
        headers = {
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {offset}-{end - 1}/{total}",
            "Content-Type": "application/octet-stream",
        }
        result = gc.put_raw(upload_url, data=chunk, user_key=ms_user_id, headers=headers)
        offset = end

    return result["id"] if result else ""


def update_file_content(
    gc: GraphClient,
    ms_user_id: str,
    dest_id: str,
    content: bytes,
    drive_path: str = "me/drive",
) -> None:
    """Replace the content of an existing driveItem in-place (delta updates)."""
    if len(content) <= _CHUNK_SIZE:
        url = f"{GRAPH_BASE}/users/{ms_user_id}/{drive_path}/items/{dest_id}/content"
        gc.put_raw(url, data=content, user_key=ms_user_id, headers={"Content-Type": "application/octet-stream"})
    else:
        session = gc.post(
            f"/users/{ms_user_id}/{drive_path}/items/{dest_id}/createUploadSession",
            user_key=ms_user_id,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        upload_url: str = session["uploadUrl"]
        total = len(content)
        offset = 0
        while offset < total:
            end = min(offset + _CHUNK_SIZE, total)
            chunk = content[offset:end]
            gc.put_raw(upload_url, data=chunk, user_key=ms_user_id, headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end - 1}/{total}",
                "Content-Type": "application/octet-stream",
            })
            offset = end
