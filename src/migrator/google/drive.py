from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from ..auth.google_auth import build_service
from ..config import GoogleConfig
from ..ratelimit import registry

log = logging.getLogger(__name__)

# Maps Google native mime types to export formats
EXPORT_MIME_MAP = {
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("image/svg+xml", ".svg"),
    "application/vnd.google-apps.form": None,  # export not supported, skip
    "application/vnd.google-apps.script": None,  # no direct export
}

_FILE_FIELDS = "id, name, mimeType, parents, size, modifiedTime, md5Checksum, version, trashed, owners, permissions"


def _svc(cfg: GoogleConfig, email: str):
    return build_service("drive", "v3", cfg.service_account_key_file, email)


def iter_my_drive_files(
    cfg: GoogleConfig,
    user_email: str,
    page_token: str | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Yield all non-trashed files from the user's My Drive."""
    svc = _svc(cfg, user_email)
    params: dict[str, Any] = {
        "corpora": "user",
        "fields": f"nextPageToken, files({_FILE_FIELDS})",
        "pageSize": 1000,
        "q": "trashed = false",
    }
    if page_token:
        params["pageToken"] = page_token
    while True:
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.files().list(**params).execute()
        for f in resp.get("files", []):
            yield f
        next_token = resp.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token


def iter_shared_drive_files(
    cfg: GoogleConfig,
    user_email: str,
    drive_id: str,
) -> Generator[dict[str, Any], None, None]:
    svc = _svc(cfg, user_email)
    params: dict[str, Any] = {
        "corpora": "drive",
        "driveId": drive_id,
        "includeItemsFromAllDrives": True,
        "supportsAllDrives": True,
        "fields": f"nextPageToken, files({_FILE_FIELDS})",
        "pageSize": 1000,
        "q": "trashed = false",
    }
    while True:
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.files().list(**params).execute()
        for f in resp.get("files", []):
            yield f
        next_token = resp.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token


def list_shared_drives(cfg: GoogleConfig, user_email: str) -> list[dict[str, Any]]:
    svc = _svc(cfg, user_email)
    drives = []
    page_token = None
    while True:
        params: dict[str, Any] = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.drives().list(**params).execute()
        drives.extend(resp.get("drives", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return drives


def download_file(cfg: GoogleConfig, user_email: str, file_id: str) -> bytes:
    svc = _svc(cfg, user_email)
    request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    return request.execute()


def export_native_file(
    cfg: GoogleConfig, user_email: str, file_id: str, export_mime: str
) -> bytes:
    svc = _svc(cfg, user_email)
    request = svc.files().export_media(fileId=file_id, mimeType=export_mime)
    return request.execute()


def get_changes_start_token(cfg: GoogleConfig, user_email: str) -> str:
    svc = _svc(cfg, user_email)
    resp = svc.changes().getStartPageToken().execute()
    return resp["startPageToken"]


def iter_drive_changes(
    cfg: GoogleConfig,
    user_email: str,
    page_token: str,
) -> tuple[list[dict[str, Any]], str]:
    """Return (changed_files, new_page_token) using the Drive Changes API.

    Only returns non-trashed, non-removed file changes. The new_page_token
    should be saved as the cursor for the next delta run.
    """
    svc = _svc(cfg, user_email)
    files: list[dict[str, Any]] = []
    current_token = page_token

    while True:
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.changes().list(
            pageToken=current_token,
            fields=f"nextPageToken, newStartPageToken, changes(type, removed, fileId, file({_FILE_FIELDS}))",
            pageSize=1000,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()

        for change in resp.get("changes", []):
            if change.get("type") != "file" or change.get("removed"):
                continue
            file_data = change.get("file")
            if file_data and not file_data.get("trashed"):
                files.append(file_data)

        if "newStartPageToken" in resp:
            return files, resp["newStartPageToken"]
        current_token = resp["nextPageToken"]
