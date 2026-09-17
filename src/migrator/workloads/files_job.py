from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select

from ..connectors.base import SourceFile
from ..context import JobContext
from ..microsoft.files import (
    ensure_folder,
    ensure_onedrive,
    update_file_content,
    upload_large_file,
    upload_small_file,
)
from ..microsoft.graph_client import GraphClient
from ..microsoft.sharepoint import ensure_site_for_drive, resolve_existing_site_drive
from ..state.db import (
    get_cursor,
    get_folder_dest,
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

# _process_file outcomes
_OK = "ok"
_FAILED = "failed"
_DEFERRED = "deferred"  # parent folder not migrated yet — retried after the pass


@dataclass
class _FolderIndex:
    """Source folder id → destination folder id for one drive.

    Backed by FolderMap, so a parent migrated by an earlier run (a delta pass
    re-emits only the changed child) or earlier in this run resolves. Neither
    Drive's files.list nor Graph's delta feed guarantees parents before
    children, so the caller defers items whose parent is still unknown."""

    user: str
    workload: str
    ids: dict[str, str] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)  # folders whose create failed
    rooted: set[str] = field(default_factory=set)  # unknown parents already warned about
    root_id: str | None = None

    def resolve(self, parent_id: str | None) -> tuple[bool, str | None]:
        """(resolved, dest_folder_id); a resolved `None` means the drive root."""
        if not parent_id:
            return True, None
        if parent_id in self.ids:
            return True, self.ids[parent_id]
        with session_scope() as s:
            dest = get_folder_dest(s, self.user, self.workload, parent_id)
        if dest:
            self.ids[parent_id] = dest
            return True, dest
        return False, None


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

    # OneDrive is provisioned lazily; make sure it exists before writing files so
    # an unprovisioned mailbox fails fast with a clear message rather than an
    # opaque 404 on the first upload. Skips just this user (orchestrator continues).
    ensure_onedrive(gc, ms_user_id)

    drive_root = f"users/{ms_user_id}/drive"  # personal files → OneDrive

    since: str | None = None
    if ctx.mode == "delta":
        with session_scope() as s:
            since = get_cursor(s, user.source_id, "files")
        if not since:
            log.warning("No files cursor found for %s — run a full pass first", user.source_id)
            return

    failures = _migrate(
        ctx, gc, drive_root, ms_user_id, ctx.source.iter_files(user, since), workload="files"
    )

    new_cursor = ctx.source.get_last_cursor("files")
    if new_cursor:
        if failures:
            # Advancing past failed items would drop them from every future
            # delta; keep the old cursor so the next run retries them.
            log.warning("%d file(s) failed for %s — files cursor not advanced",
                        failures, user.source_id)
        else:
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
            gc, drive.drive_id, mapping.target_site_alias, display,
            owner=ctx.config.destination.sharepoint_site_owner,
        )
        log.info(
            "Shared drive %s → SharePoint site %s (drive %s)", drive.name, site_id, library_drive_id
        )

        drive_root = f"drives/{library_drive_id}"
        workload = f"shared_drive:{drive.drive_id}"

        run, since = _delta_cursor(ctx, workload, label=drive.name)
        if not run:
            continue

        failures = _migrate(
            ctx, gc, drive_root, library_drive_id,
            ctx.source.iter_shared_drive_files(ctx.user, drive, since), workload=workload,
        )

        if failures:
            log.warning("%d file(s) failed in shared drive %s — cursor not advanced",
                        failures, drive.name)
        else:
            # Connector cursor key for shared drives matches the workload string.
            _persist_cursor(ctx, workload, cursor_key=workload)


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
                gc, source_site_id, mapping.target_site_alias, display,
                owner=ctx.config.destination.sharepoint_site_owner,
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
        # The connector stamps the deltaLink under a key derived from the *source*
        # drive id; persist it per source site.
        cursor_key = f"sharepoint:{source_drive_id}"

        run, since = _delta_cursor(ctx, workload, label=mapping.source_site)
        if not run:
            continue

        failures = _migrate(
            ctx, gc, drive_root, dest_drive_id,
            ctx.source.iter_site_files(source_drive_id, since), workload=workload,
        )

        if failures:
            log.warning("%d file(s) failed in site %s — cursor not advanced",
                        failures, mapping.source_site)
        else:
            _persist_cursor(ctx, workload, cursor_key=cursor_key)


def _migrate(
    ctx: JobContext,
    gc: GraphClient,
    drive_root: str,
    user_key: str,
    items: Iterable[SourceFile],
    workload: str,
) -> int:
    """One pass over `items`; returns the number of failed items.

    Items whose parent folder is not known yet are deferred and retried once
    the pass has seen everything else, so a child listed before its folder still
    lands in that folder. When a round makes no progress, the items whose parent
    is not part of the batch are orphans relative to the migrated corpus (e.g.
    a shared-with-me folder) and are placed at the drive root; the loop then
    continues so their own children resolve under them."""
    folders = _FolderIndex(ctx.user.source_id, workload)
    failures = 0

    def _run(batch: Iterable[SourceFile], defer: bool) -> list[SourceFile]:
        nonlocal failures
        still: list[SourceFile] = []
        for f in batch:
            outcome = _process_file(
                ctx, gc, drive_root, user_key, f, folders, workload, defer=defer
            )
            if outcome == _DEFERRED:
                still.append(f)
            elif outcome == _FAILED:
                failures += 1
        return still

    deferred = _run(items, defer=True)
    while deferred:
        remaining = _run(deferred, defer=True)
        if len(remaining) == len(deferred):
            pending = {f.source_id for f in remaining}
            orphans = [f for f in remaining if f.parent_id not in pending] or remaining
            _run(orphans, defer=False)
            orphan_ids = {f.source_id for f in orphans}
            remaining = [f for f in remaining if f.source_id not in orphan_ids]
        deferred = remaining
    return failures


def _parent_for(
    f: SourceFile, folders: _FolderIndex, defer: bool, user: str, workload: str
) -> tuple[str | None, str | None]:
    """Resolve the destination parent for `f`.

    Returns (stop_outcome, parent_dest): a non-None stop_outcome means the
    caller returns it (_DEFERRED while the parent is still unknown and
    deferral is allowed; _FAILED when the parent folder itself failed). A
    resolved None parent means the drive root."""
    resolved, parent_dest = folders.resolve(f.parent_id)
    if resolved:
        return None, parent_dest
    parent_id = f.parent_id or ""
    if parent_id in folders.failed:
        err = f"parent folder {parent_id} failed to migrate"
        log.error("Failed %s (%s): %s", f.source_id, f.name, err)
        with session_scope() as s:
            upsert_item(s, user, workload, f.source_id, status="failed", last_error=err)
        return _FAILED, None
    if defer:
        return _DEFERRED, None
    if parent_id not in folders.rooted:
        folders.rooted.add(parent_id)
        log.warning(
            "Parent folder %s is not part of the migrated corpus — placing its items "
            "at the drive root", parent_id,
        )
    return None, None


def _process_file(
    ctx: JobContext,
    gc: GraphClient,
    drive_root: str,
    user_key: str,
    f: SourceFile,
    folders: _FolderIndex,
    workload: str,
    *,
    defer: bool = True,
) -> str:
    """Migrate one file/folder. Returns _OK, _FAILED (callers hold back the
    sync cursor so the item is retried next run) or _DEFERRED (parent folder
    not migrated yet; only while `defer` is set)."""
    user = ctx.user

    if f.is_folder:
        stop, parent_dest = _parent_for(f, folders, defer, user.source_id, workload)
        if stop:
            return stop
        try:
            dest_id = ensure_folder(gc, drive_root, user_key, parent_dest, f.name)
        except Exception as exc:
            # Children must not silently fall through to the root: remember the
            # failure so they fail with a clear reason and are retried next run.
            log.error("Failed folder %s (%s): %s", f.source_id, f.name, exc)
            folders.failed.add(f.source_id)
            with session_scope() as s:
                upsert_item(
                    s, user.source_id, workload, f.source_id, status="failed", last_error=str(exc)
                )
            return _FAILED
        folders.ids[f.source_id] = dest_id
        with session_scope() as s:
            upsert_folder(s, user.source_id, workload, f.source_id, dest_id, f.name)
            upsert_item(
                s, user.source_id, workload, f.source_id,
                source_hash=f.content_hash, dest_id=dest_id, status="done",
            )
        return _OK

    with session_scope() as s:
        if ctx.mode == "full":
            if is_done(s, user.source_id, workload, f.source_id):
                return _OK
        else:
            # Delta: skip only if the content hash is unchanged.
            stored_hash = get_item_hash(s, user.source_id, workload, f.source_id)
            if stored_hash is not None and stored_hash == f.content_hash:
                return _OK

    if f.action == "skip":
        log.info("Skipping %s: %s", f.source_id, f.notes)
        with session_scope() as s:
            upsert_item(s, user.source_id, workload, f.source_id, status="skipped")
        return _OK

    # An item that already exists at the destination is updated in place; its
    # dest_id is kept even when the update fails so the retry never re-creates it.
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

    parent_dest = None
    if not existing_dest_id:
        stop, parent_dest = _parent_for(f, folders, defer, user.source_id, workload)
        if stop:
            return stop

    try:
        content, final_name = ctx.source.fetch_file(user, f)

        if existing_dest_id:
            update_file_content(gc, drive_root, user_key, existing_dest_id, content)
            dest_id = existing_dest_id
        else:
            if parent_dest is None:
                parent_dest = _root_id(gc, drive_root, user_key, folders)
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
        return _OK
    except Exception as exc:
        log.error("Failed file %s (%s): %s", f.source_id, f.name, exc)
        with session_scope() as s:
            upsert_item(
                s, user.source_id, workload, f.source_id,
                dest_id=existing_dest_id, status="failed", last_error=str(exc),
            )
        return _FAILED


def _root_id(gc: GraphClient, drive_root: str, user_key: str, folders: _FolderIndex) -> str:
    if folders.root_id is None:
        folders.root_id = _get_root_id(gc, drive_root, user_key)
    return folders.root_id


def _get_root_id(gc: GraphClient, drive_root: str, user_key: str) -> str:
    root = gc.get(f"/{drive_root}/root", params={"$select": "id"}, user_key=user_key)
    return str(root["id"])


def _delta_cursor(ctx: JobContext, workload: str, label: str) -> tuple[bool, str | None]:
    """Resolve the `since` value for one drive/site in the tenant-level SharePoint
    flows. A full pass returns ``(True, None)``. A delta pass returns the stored
    cursor, or ``(False, None)`` when none exists yet (so the caller skips it and
    waits for a full pass to seed one)."""
    if ctx.mode != "delta":
        return True, None
    with session_scope() as s:
        cursor = get_cursor(s, ctx.user.source_id, workload)
    if not cursor:
        log.warning("No delta cursor for %s — run a full pass first; skipping", label)
        return False, None
    return True, cursor


def _persist_cursor(ctx: JobContext, workload: str, cursor_key: str) -> None:
    """Save the cursor the connector captured during iteration under `cursor_key`,
    namespaced in SyncCursor by `workload`."""
    new_cursor = ctx.source.get_last_cursor(cursor_key)
    if new_cursor:
        with session_scope() as s:
            save_cursor(s, ctx.user.source_id, workload, new_cursor)


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
