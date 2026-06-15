from __future__ import annotations

import logging

from ..context import JobContext
from ..microsoft.mail import ensure_mail_folder, import_mime_message, patch_message_flags
from ..state.db import get_cursor, is_done, save_cursor, session_scope, upsert_folder, upsert_item

log = logging.getLogger(__name__)


def run_mail(ctx: JobContext) -> None:
    ctx.require_capability("mail")
    user = ctx.user

    if ctx.mode == "whatif":
        _whatif_mail(ctx)
        return

    gc = ctx.dest_gc
    assert gc is not None, "GraphClient required outside whatif mode"

    ms_user = gc.get(f"/users/{user.dest_id}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    since: str | None = None
    if ctx.mode == "delta":
        with session_scope() as s:
            since = get_cursor(s, user.source_id, "mail")

    folder_cache: dict[str, str] = {}  # folder_path → graph_folder_id

    def _get_folder(path: str) -> str:
        if path in folder_cache:
            return folder_cache[path]
        parts = path.split("\\")
        parent_id: str | None = None
        for i, part in enumerate(parts):
            current_path = "\\".join(parts[: i + 1])
            if current_path in folder_cache:
                parent_id = folder_cache[current_path]
            else:
                fid = ensure_mail_folder(gc, ms_user_id, part, parent_id)
                folder_cache[current_path] = fid
                with session_scope() as s:
                    upsert_folder(s, user.source_id, "mail", current_path, fid, current_path)
                parent_id = fid
        return folder_cache[path]

    for msg in ctx.source.iter_messages(user, since):
        with session_scope() as s:
            if is_done(s, user.source_id, "mail", msg.source_id):
                continue

        folder_paths = msg.folder_paths or ["Inbox"]
        try:
            dest_id: str | None = None
            for folder_path in folder_paths:
                folder_id = _get_folder(folder_path)
                dest_id = import_mime_message(gc, ms_user_id, folder_id, msg.raw_mime)
                patch_message_flags(
                    gc, ms_user_id, dest_id,
                    is_read=msg.is_read, categories=msg.categories or None,
                )
            with session_scope() as s:
                upsert_item(
                    s, user.source_id, "mail", msg.source_id,
                    source_hash=msg.dedup_hash or None, dest_id=dest_id, status="done",
                )
        except Exception as exc:
            log.error("Failed message %s: %s", msg.source_id, exc)
            with session_scope() as s:
                upsert_item(
                    s, user.source_id, "mail", msg.source_id, status="failed", last_error=str(exc)
                )

    new_cursor = ctx.source.get_last_cursor("mail")
    if new_cursor:
        with session_scope() as s:
            save_cursor(s, user.source_id, "mail", new_cursor)


def _whatif_mail(ctx: JobContext) -> None:
    import migrator as _pkg

    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"
    user = ctx.user

    for msg in ctx.source.inventory_messages(user):
        folder_paths = msg.folder_paths or ["Inbox"]
        primary = folder_paths[0]
        notes_parts = []
        if len(folder_paths) > 1:
            notes_parts.append(f"folders: {primary}+{len(folder_paths) - 1} more")
        if msg.categories:
            notes_parts.append(f"categories: {','.join(msg.categories)}")
        if not msg.is_read:
            notes_parts.append("unread")

        manifest.add(
            source_user=user.source_id,
            dest_user=user.dest_id,
            workload="mail",
            source_id=msg.source_id,
            source_path=primary,
            name=msg.subject or "(no subject)",
            size_bytes=msg.size_bytes,
            modified_time=msg.date,
            notes="; ".join(notes_parts) or (f"from={msg.sender}" if msg.sender else ""),
        )
