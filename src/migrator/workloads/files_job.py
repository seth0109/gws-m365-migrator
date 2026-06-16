from __future__ import annotations

import logging

from sqlalchemy import select

from ..connectors.base import SourceFile
from ..context import JobContext
from ..microsoft.files import (
    ensure_folder,
    update_file_content,
    upload_large_file,
    upload_small_file,
)
from ..microsoft.graph_client import GraphClient
from ..microsoft.sharepoint import ensure_site_for_drive, resolve_existing_site_drive
from ..state.db import (
    get_cursor,
    get_item_hash,
    is_done,
    save_cursor,
    session_scope,
    upsert_folder,
    upsert_item,
)
from ..state.models import ItemMap

log = logging.getLogger(__name__)

_SMALL_FILE_THRESHOLD = 4 * 1024 * 1024  # 4 MB


def run_files(ctx: JobContext) -> None:
    ctx.require_capability("files")
    user = ctx.user

    if ctx.mode == "whatif":
        _whatif_files(ctx)
        return

    gc = ctx.dest_gc
    assert gc is not None, "GraphClient required outside whatif mode"

    ms_user = gc.get(f"/users/{user.dest_id}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    drive_root = f"users/{ms_user_id}/drive"  # personal files → OneDrive
    folder_id_cache: dict[str, str] = {}

    since: str | None = None
    if ctx.mode == "delta":
        with session_scope() as s:
            since = get_cursor(s, user.source_id, "files")
        if not since:
            log.warning("No files cursor found for %s — run a full pass first", user.source_id)
            return

    for f in ctx.source.iter_files(user, since):
        _process_file(ctx, gc, drive_root, ms_user_id, f, folder_id_cache, workload="files")

    new_cursor = ctx.source.get_last_cursor("files")
    if new_cursor:
        with session_scope() as s:
            save_cursor(s, user.source_id, "files", new_cursor)


def run_shared_drives(ctx: JobContext) -> None:
    """Migrate Google Shared Drives → SharePoint, auto-provisioning a site per
    drive. Tenant-level: `ctx.user` is the Google impersonation account used to
    enumerate/download Drive content."""
    if ctx.config.source.type != "google_workspace":
        raise RuntimeError("shared-drives migration requires a google_workspace source")
    ctx.require_capability("files")

    gc = ctx.dest_gc
    assert gc is not None, "GraphClient required outside whatif mode"

    available = {d.drive_id: d for d in ctx.source.list_shared_drives(ctx.user)}
    by_name = {d.name: d for d in available.values()}

    for mapping in ctx.config.shared_drives:
        drive = None
        if mapping.drive_id and mapping.drive_id in available:
            drive = available[mapping.drive_id]
        elif mapping.drive_name and mapping.drive_name in by_name:
            drive = by_name[mapping.drive_name]
        if drive is None:
            log.warning(
                "Shared drive not found (name=%s id=%s) — skipping",
                mapping.drive_name, mapping.drive_id,
            )
            continue

        display = mapping.display_name or mapping.drive_name or drive.name
        site_id, library_drive_id = ensure_site_for_drive(
            gc, drive.drive_id, mapping.target_site_alias, display
        )
        log.info(
            "Shared drive %s → SharePoint site %s (drive %s)", drive.name, site_id, library_drive_id
        )

        drive_root = f"drives/{library_drive_id}"
        workload = f"shared_drive:{drive.drive_id}"
        folder_id_cache: dict[str, str] = {}
        for f in ctx.source.iter_shared_drive_files(ctx.user, drive):
            _process_file(
                ctx, gc, drive_root, library_drive_id, f, folder_id_cache, workload=workload
            )


def run_sharepoint_sites(ctx: JobContext) -> None:
    """Migrate SharePoint document libraries between Microsoft 365 tenants. Each
    configured source site maps to an existing destination site (`dest_site`) or
    an auto-provisioned one (`target_site_alias`). Tenant-level."""
    if ctx.config.source.type != "microsoft365":
        raise RuntimeError("sharepoint site migration requires a microsoft365 source")
    ctx.require_capability("files")

    gc = ctx.dest_gc
    assert gc is not None, "GraphClient required outside whatif mode"

    for mapping in ctx.config.sharepoint_sites:
        source_site_id, source_drive_id = ctx.source.resolve_site_drive(mapping.source_site)

        if mapping.dest_site:
            _, dest_drive_id = resolve_existing_site_drive(gc, mapping.dest_site)
        elif mapping.target_site_alias:
            display = mapping.display_name or mapping.target_site_alias
            _, dest_drive_id = ensure_site_for_drive(
                gc, source_site_id, mapping.target_site_alias, display
            )
        else:
            log.warning(
                "sharepoint_sites entry %s has neither dest_site nor target_site_alias — skipping",
                mapping.source_site,
            )
            continue

        log.info("SharePoint %s → dest drive %s", mapping.source_site, dest_drive_id)
        drive_root = f"drives/{dest_drive_id}"
        workload = f"sharepoint_site:{source_site_id}"
        folder_id_cache: dict[str, str] = {}
        for f in ctx.source.iter_site_files(source_drive_id, None):
            _process_file(ctx, gc, drive_root, dest_drive_id, f, folder_id_cache, workload=workload)


def _process_file(
    ctx: JobContext,
    gc: GraphClient,
    drive_root: str,
    user_key: str,
    f: SourceFile,
    folder_id_cache: dict[str, str],
    workload: str,
) -> None:
    user = ctx.user

    if f.is_folder:
        parent_dest = folder_id_cache.get(f.parent_id) if f.parent_id else None
        dest_id = ensure_folder(gc, drive_root, user_key, parent_dest, f.name)
        folder_id_cache[f.source_id] = dest_id
        with session_scope() as s:
            upsert_folder(s, user.source_id, workload, f.source_id, dest_id, f.name)
            upsert_item(
                s, user.source_id, workload, f.source_id,
                source_hash=f.content_hash, dest_id=dest_id, status="done",
            )
        return

    with session_scope() as s:
        if ctx.mode == "full":
            if is_done(s, user.source_id, workload, f.source_id):
                return
        else:
            # Delta: skip only if the content hash is unchanged.
            stored_hash = get_item_hash(s, user.source_id, workload, f.source_id)
            if stored_hash is not None and stored_hash == f.content_hash:
                return

    if f.action == "skip":
        log.info("Skipping %s: %s", f.source_id, f.notes)
        with session_scope() as s:
            upsert_item(s, user.source_id, workload, f.source_id, status="skipped")
        return

    try:
        content, final_name = ctx.source.fetch_file(user, f)

        existing_dest_id: str | None = None
        if ctx.mode == "delta":
            with session_scope() as s:
                existing_dest_id = s.execute(
                    select(ItemMap.dest_id).where(
                        ItemMap.user_email == user.source_id,
                        ItemMap.workload == workload,
                        ItemMap.source_id == f.source_id,
                    )
                ).scalar_one_or_none()

        if existing_dest_id:
            update_file_content(gc, drive_root, user_key, existing_dest_id, content)
            dest_id = existing_dest_id
        else:
            parent_dest = folder_id_cache.get(f.parent_id) if f.parent_id else None
            if parent_dest is None:
                parent_dest = _get_root_id(gc, drive_root, user_key)
            if len(content) <= _SMALL_FILE_THRESHOLD:
                dest_id = upload_small_file(
                    gc, drive_root, user_key, parent_dest, final_name, content
                )
            else:
                dest_id = upload_large_file(
                    gc, drive_root, user_key, parent_dest, final_name, content
                )

        with session_scope() as s:
            upsert_item(
                s, user.source_id, workload, f.source_id,
                source_hash=f.content_hash, dest_id=dest_id, status="done",
            )
    except Exception as exc:
        log.error("Failed file %s (%s): %s", f.source_id, f.name, exc)
        with session_scope() as s:
            upsert_item(
                s, user.source_id, workload, f.source_id, status="failed", last_error=str(exc)
            )


def _get_root_id(gc: GraphClient, drive_root: str, user_key: str) -> str:
    root = gc.get(f"/{drive_root}/root", params={"$select": "id"}, user_key=user_key)
    return str(root["id"])


def _whatif_files(ctx: JobContext) -> None:
    import migrator as _pkg

    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"
    user = ctx.user

    for f in ctx.source.inventory_files(user):
        if f.action == "create-folder":
            action = "create-folder"
        elif f.action == "export":
            action = f"export-as-{(f.export_ext or '').lstrip('.')}"
        elif f.action == "skip":
            action = "skip"
        else:
            action = "migrate"
        manifest.add(
            source_user=user.source_id,
            dest_user=user.dest_id,
            workload="files",
            source_id=f.source_id,
            source_path=f.source_path,
            name=f.name,
            size_bytes=f.size,
            mime_type=f.mime_type,
            modified_time=f.modified_time,
            action=action,
            notes=f.notes,
        )
