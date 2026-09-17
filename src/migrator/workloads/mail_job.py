from __future__ import annotations

import logging

from ..connectors.base import SourceMessage
from ..context import JobContext
from ..microsoft.mail import (
    ensure_mail_folder,
    import_message,
    patch_message_flags,
    resolve_folder_segment,
)
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
        if not since:
            # Without a seeded cursor a "delta" would silently re-scan the whole
            # mailbox; refuse instead (matches the files workload).
            log.warning("No mail cursor for %s — run a full pass first; skipping", user.source_id)
            return

    import_mode: str = ctx.config.workloads.mail.import_mode
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
                continue
            # Route a top-level system folder (Inbox/SentItems/...) to its
            # well-known Graph folder id instead of creating a duplicate.
            wk = resolve_folder_segment(part, is_top_level=(i == 0))
            fid = wk if wk is not None else ensure_mail_folder(gc, ms_user_id, part, parent_id)
            folder_cache[current_path] = fid
            with session_scope() as s:
                upsert_folder(s, user.source_id, "mail", current_path, fid, current_path)
            parent_id = fid
        return folder_cache[path]

    def _import_into(folder_id: str, msg: SourceMessage) -> str:
        dest = import_message(
            gc, ms_user_id, folder_id, msg.raw_mime,
            is_read=msg.is_read, is_flagged=msg.is_flagged,
            categories=msg.categories or None, mode=import_mode,
        )
        if import_mode == "mime":
            # MIME creates can't carry flags; patch after the fact. A patch
            # failure must not fail the item — the message is already imported,
            # and a "failed" status would re-import it as a duplicate on rerun.
            try:
                patch_message_flags(
                    gc, ms_user_id, dest,
                    is_read=msg.is_read, categories=msg.categories or None,
                    is_flagged=msg.is_flagged,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort flag fidelity
                log.warning("Flags patch failed for %s (message imported): %s",
                            msg.source_id, exc)
        return dest

    failures = 0
    for msg in ctx.source.iter_messages(user, since):
        with session_scope() as s:
            if is_done(s, user.source_id, "mail", msg.source_id):
                continue

        if msg.fetch_error:
            # The connector could not read this message from the source. Fail
            # just this item (holding the cursor) rather than the whole mailbox.
            failures += 1
            log.error("Failed to fetch message %s from source: %s", msg.source_id, msg.fetch_error)
            with session_scope() as s:
                upsert_item(
                    s, user.source_id, "mail", msg.source_id,
                    status="failed", last_error=msg.fetch_error,
                )
            continue

        folder_paths = msg.folder_paths or ["Inbox"]
        try:
            dest_id = _import_into(_get_folder(folder_paths[0]), msg)
        except Exception as exc:
            failures += 1
            log.error("Failed message %s: %s", msg.source_id, exc)
            with session_scope() as s:
                upsert_item(
                    s, user.source_id, "mail", msg.source_id, status="failed", last_error=str(exc)
                )
            continue

        # The message now exists at the destination: record done immediately so
        # nothing after this point can re-import it as a duplicate on rerun.
        with session_scope() as s:
            upsert_item(
                s, user.source_id, "mail", msg.source_id,
                source_hash=msg.dedup_hash or None, dest_id=dest_id, status="done",
            )

        # Best-effort extra copies (multi_label_policy="duplicate"): the primary
        # copy is safe, so log-and-continue rather than flip the item to failed.
        for folder_path in folder_paths[1:]:
            try:
                _import_into(_get_folder(folder_path), msg)
            except Exception as exc:  # noqa: BLE001 - primary copy already imported
                log.warning("Extra folder copy %s failed for %s (primary imported): %s",
                            folder_path, msg.source_id, exc)

    new_cursor = ctx.source.get_last_cursor("mail")
    if new_cursor:
        if failures:
            # Advancing the cursor past failed items would drop them from every
            # future delta; keep the old cursor so the next run retries them.
            log.warning(
                "%d message(s) failed for %s — mail cursor not advanced; "
                "they will be retried on the next run", failures, user.source_id,
            )
        else:
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
        if msg.is_flagged:
            notes_parts.append("starred")

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
