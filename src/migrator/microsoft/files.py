from __future__ import annotations

import logging

from .graph_client import GRAPH_BASE, GraphClient

log = logging.getLogger(__name__)

# Chunk size must be a multiple of 320 KiB; use 10 MiB
_CHUNK_SIZE = 10 * 1024 * 1024

# `drive_root` is the full Graph path prefix to a drive, without a leading slash:
#   - OneDrive:   "users/<user-id>/drive"
#   - SharePoint: "drives/<drive-id>"
# `user_key` is the rate-limiter key (mailbox/user id, or drive id for SharePoint).


def ensure_folder(
    gc: GraphClient,
    drive_root: str,
    user_key: str,
    parent_id: str | None,
    folder_name: str,
) -> str:
    """Create folder if absent, return its driveItem ID."""
    if parent_id:
        path = f"/{drive_root}/items/{parent_id}/children"
    else:
        path = f"/{drive_root}/root/children"

    flt = {"$filter": f"name eq '{folder_name}' and folder ne null"}
    existing = gc.get(path, user_key=user_key, params=flt)
    for item in existing.get("value", []):
        if item["name"] == folder_name:
            return str(item["id"])

    body = {
        "name": folder_name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    result = gc.post(path, user_key=user_key, json=body)
    return str(result["id"])


def upload_small_file(
    gc: GraphClient,
    drive_root: str,
    user_key: str,
    parent_id: str,
    file_name: str,
    content: bytes,
) -> str:
    result = gc.post(
        f"/{drive_root}/items/{parent_id}:/{file_name}:/content",
        user_key=user_key,
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    return str(result["id"])


def upload_large_file(
    gc: GraphClient,
    drive_root: str,
    user_key: str,
    parent_id: str,
    file_name: str,
    content: bytes,
) -> str:
    """Chunked upload session for files that may be large."""
    session = gc.post(
        f"/{drive_root}/items/{parent_id}:/{file_name}:/createUploadSession",
        user_key=user_key,
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
        result = gc.put_raw(upload_url, data=chunk, user_key=user_key, headers=headers)
        offset = end

    return str(result["id"]) if result else ""


def update_file_content(
    gc: GraphClient,
    drive_root: str,
    user_key: str,
    dest_id: str,
    content: bytes,
) -> None:
    """Replace the content of an existing driveItem in-place (delta updates)."""
    if len(content) <= _CHUNK_SIZE:
        url = f"{GRAPH_BASE}/{drive_root}/items/{dest_id}/content"
        gc.put_raw(
            url, data=content, user_key=user_key,
            headers={"Content-Type": "application/octet-stream"},
        )
    else:
        session = gc.post(
            f"/{drive_root}/items/{dest_id}/createUploadSession",
            user_key=user_key,
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        upload_url: str = session["uploadUrl"]
        total = len(content)
        offset = 0
        while offset < total:
            end = min(offset + _CHUNK_SIZE, total)
            chunk = content[offset:end]
            gc.put_raw(upload_url, data=chunk, user_key=user_key, headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end - 1}/{total}",
                "Content-Type": "application/octet-stream",
            })
            offset = end
