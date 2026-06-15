from __future__ import annotations

import logging
from typing import Any

from ..config import UserMapping
from ..google.drive import (
    EXPORT_MIME_MAP,
    download_file,
    export_native_file,
    get_changes_start_token,
    iter_drive_changes,
    iter_my_drive_files,
    list_shared_drives,
)
from ..microsoft.files import ensure_folder, update_file_content, upload_large_file, upload_small_file
from ..microsoft.graph_client import GraphClient
from ..state.db import get_cursor, get_item_hash, is_done, save_cursor, session_scope, upsert_folder, upsert_item
from ..transform.paths import sanitize_segment

log = logging.getLogger(__name__)

_SMALL_FILE_THRESHOLD = 4 * 1024 * 1024  # 4 MB


def run_files(user: UserMapping, gc: GraphClient | None, mode: str) -> None:
    import migrator as _pkg
    cfg = _pkg._current_config
    assert cfg is not None

    google_cfg = cfg.google

    if mode == "whatif":
        _whatif_files(user, google_cfg)
        return

    assert gc is not None, "GraphClient required outside whatif mode"

    ms_user = gc.get(f"/users/{user.ms_upn}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    folder_id_cache: dict[str, str] = {}

    if mode == "full":
        # Capture the cursor before reading so the delta pass sees everything changed after this point.
        start_token = get_changes_start_token(google_cfg, user.google_email)
        with session_scope() as s:
            save_cursor(s, user.google_email, "files", start_token)

        for gfile in iter_my_drive_files(google_cfg, user.google_email):
            _process_file(gc, google_cfg, user, ms_user_id, gfile, folder_id_cache, "me/drive", mode)

    else:
        with session_scope() as s:
            page_token = get_cursor(s, user.google_email, "files")

        if not page_token:
            log.warning("No files cursor found for %s — run a full pass first", user.google_email)
            return

        changed_files, new_token = iter_drive_changes(google_cfg, user.google_email, page_token)
        for gfile in changed_files:
            _process_file(gc, google_cfg, user, ms_user_id, gfile, folder_id_cache, "me/drive", mode)

        with session_scope() as s:
            save_cursor(s, user.google_email, "files", new_token)


def _get_file_hash(gfile: dict[str, Any]) -> str | None:
    # Binary files have md5Checksum; native Google Docs have version instead.
    return gfile.get("md5Checksum") or gfile.get("version")


def _process_file(
    gc: GraphClient,
    google_cfg: Any,
    user: UserMapping,
    ms_user_id: str,
    gfile: dict[str, Any],
    folder_id_cache: dict[str, str],
    drive_path: str,
    mode: str,
) -> None:
    file_id: str = gfile["id"]
    workload = "files"
    current_hash = _get_file_hash(gfile)

    with session_scope() as s:
        if mode == "full":
            if is_done(s, user.google_email, workload, file_id):
                return
        else:
            # Delta: skip only if the hash is unchanged (guards against re-processing
            # the same change on a restarted delta run).
            stored_hash = get_item_hash(s, user.google_email, workload, file_id)
            if stored_hash is not None and stored_hash == current_hash:
                return

    mime = gfile.get("mimeType", "")
    name = sanitize_segment(gfile.get("name", file_id))

    if mime == "application/vnd.google-apps.folder":
        parent_ids = gfile.get("parents", [])
        parent_dest = folder_id_cache.get(parent_ids[0]) if parent_ids else None
        dest_id = ensure_folder(gc, ms_user_id, parent_dest, name, drive_path)
        folder_id_cache[file_id] = dest_id
        with session_scope() as s:
            upsert_folder(s, user.google_email, "files", file_id, dest_id, name)
            upsert_item(s, user.google_email, "files", file_id, source_hash=current_hash, dest_id=dest_id, status="done")
        return

    export_info = EXPORT_MIME_MAP.get(mime)
    if export_info is None and mime.startswith("application/vnd.google-apps."):
        log.info("Skipping unsupported native type %s for %s", mime, file_id)
        with session_scope() as s:
            upsert_item(s, user.google_email, "files", file_id, status="skipped")
        return

    try:
        if export_info:
            export_mime, ext = export_info
            content = export_native_file(google_cfg, user.google_email, file_id, export_mime)
            if not name.endswith(ext):
                name += ext
        else:
            content = download_file(google_cfg, user.google_email, file_id)

        # On a delta run, update the existing OneDrive item in-place if we have its ID.
        existing_dest_id: str | None = None
        if mode == "delta":
            from sqlalchemy import select
            from ..state.models import ItemMap
            with session_scope() as s:
                row = s.execute(
                    select(ItemMap.dest_id).where(
                        ItemMap.user_email == user.google_email,
                        ItemMap.workload == workload,
                        ItemMap.source_id == file_id,
                    )
                ).scalar_one_or_none()
                existing_dest_id = row

        if existing_dest_id:
            update_file_content(gc, ms_user_id, existing_dest_id, content, drive_path)
            dest_id = existing_dest_id
        else:
            parent_ids = gfile.get("parents", [])
            parent_dest = folder_id_cache.get(parent_ids[0]) if parent_ids else None
            if parent_dest is None:
                parent_dest = _get_root_id(gc, ms_user_id, drive_path)

            if len(content) <= _SMALL_FILE_THRESHOLD:
                dest_id = upload_small_file(gc, ms_user_id, parent_dest, name, content, drive_path)
            else:
                dest_id = upload_large_file(gc, ms_user_id, parent_dest, name, content, drive_path)

        with session_scope() as s:
            upsert_item(s, user.google_email, "files", file_id, source_hash=current_hash, dest_id=dest_id, status="done")
    except Exception as exc:
        log.error("Failed file %s (%s): %s", file_id, name, exc)
        with session_scope() as s:
            upsert_item(s, user.google_email, "files", file_id, status="failed", last_error=str(exc))


def _get_root_id(gc: GraphClient, ms_user_id: str, drive_path: str) -> str:
    root = gc.get(f"/users/{ms_user_id}/{drive_path}/root", params={"$select": "id"})
    return root["id"]


def _whatif_files(user: UserMapping, google_cfg: Any) -> None:
    import migrator as _pkg
    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"

    all_files = list(iter_my_drive_files(google_cfg, user.google_email))
    folder_lookup = {
        f["id"]: f
        for f in all_files
        if f.get("mimeType") == "application/vnd.google-apps.folder"
    }

    def resolve_path(f: dict[str, Any], _seen: set[str] | None = None) -> str:
        _seen = _seen or set()
        if f["id"] in _seen:
            return f.get("name", f["id"])
        _seen.add(f["id"])
        parents = f.get("parents", [])
        name = f.get("name", f["id"])
        if not parents:
            return name
        parent = folder_lookup.get(parents[0])
        if parent is None:
            return f"(external:{parents[0]})/{name}"
        return f"{resolve_path(parent, _seen)}/{name}"

    for gfile in all_files:
        file_id: str = gfile["id"]
        mime = gfile.get("mimeType", "")
        name = gfile.get("name", file_id)
        size = gfile.get("size", "")
        modified = gfile.get("modifiedTime", "")

        if mime == "application/vnd.google-apps.folder":
            action = "create-folder"
            notes = ""
        else:
            export_info = EXPORT_MIME_MAP.get(mime)
            if export_info is None and mime.startswith("application/vnd.google-apps."):
                action = "skip"
                notes = f"unsupported native type: {mime}"
            elif export_info:
                _, ext = export_info
                action = f"export-as-{ext.lstrip('.')}"
                notes = "native Google Doc"
            else:
                action = "migrate"
                notes = ""

        manifest.add(
            user_email=user.google_email,
            ms_upn=user.ms_upn,
            workload="files",
            source_id=file_id,
            source_path=resolve_path(gfile),
            name=name,
            size_bytes=size,
            mime_type=mime,
            modified_time=modified,
            action=action,
            notes=notes,
        )
