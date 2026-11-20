from __future__ import annotations

import logging
from typing import Any

import httpx

from ..connectors.base import SourceMessage, parse_mail_state
from ..context import JobContext
from ..microsoft.mail import (
    ensure_mail_folder,
    import_message,
    patch_message_flags,
    resolve_folder_segment,
    set_message_state,
)
from ..state.db import get_item_state, session_scope, upsert_folder, upsert_item
from .cursors import advance_cursor, delta_since

log = logging.getLogger(__name__)


def run_mail(ctx: JobContext) -> None:
    ctx.require_capability("mail")
    user = ctx.user

    if ctx.mode == "whatif":
        _whatif_mail(ctx)
        return

    gc = ctx.dest_gc
    assert gc is not None, "GraphClient required outside whatif mode"

    # Without a seeded cursor a "delta" would silently re-scan the whole
    # mailbox; this raises instead (before any destination call).
    since = delta_since(ctx, "mail", "mail", "mail")

    ms_user = gc.get(f"/users/{user.dest_id}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

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

    # Delta only: a message that is already migrated and whose read/flag/category
    # state has changed at the source since it was recorded gets that change
    # re-applied. A full pass stays a pure idempotent skip.
    replay = ctx.mode == "delta" and ctx.config.workloads.mail.replay_read_state
    replayed = gone = 0

    def _replay_state(msg: SourceMessage, dest_id: str, recorded: str | None) -> None:
        """Best-effort: never fails the item or holds the cursor."""
        nonlocal replayed, gone
        changes: dict[str, Any] = {
            "is_read": msg.is_read,
            "is_flagged": msg.is_flagged,
            "categories": sorted(msg.categories),
        }
        before = parse_mail_state(recorded)
        if before is not None:
            # Send only what changed at the source, so anything the user has
            # since set on the other aspects in the new mailbox is left alone.
            was_read, was_flagged, had_categories = before
            if was_read == msg.is_read:
                del changes["is_read"]
            if was_flagged == msg.is_flagged:
                del changes["is_flagged"]
            if sorted(had_categories) == changes["categories"]:
                del changes["categories"]
        try:
            set_message_state(gc, ms_user_id, dest_id, **changes)
            replayed += 1
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                log.warning("Could not update read/flag state of %s (retried on the next "
                            "delta): %s", msg.source_id, exc)
                return
            # Moved or deleted in the new mailbox: its id there changed or it is
            # gone. That was the user's doing; record the state and move on.
            gone += 1
        except Exception as exc:  # noqa: BLE001 - fidelity extra, not a migration failure
            log.warning("Could not update read/flag state of %s (retried on the next "
                        "delta): %s", msg.source_id, exc)
            return
        with session_scope() as s:
            upsert_item(
                s, user.source_id, "mail", msg.source_id,
                source_hash=msg.state_hash, dest_id=dest_id, status="done",
            )

    failures = 0
    for msg in ctx.track(ctx.source.iter_messages(user, since), "mail"):
        with session_scope() as s:
            state = get_item_state(s, user.source_id, "mail", msg.source_id)
        if state is not None and state.status == "done":
            if (
                replay and msg.state_hash and state.dest_id
                and state.source_hash != msg.state_hash
            ):
                _replay_state(msg, state.dest_id, state.source_hash)
            continue

        if msg.load_mime is not None and not msg.fetch_error:
            # Lazy body: read from the source only now that the message is known
            # to need importing, so a re-run does not download migrated mail.
            try:
                raw = msg.load_mime()
            except Exception as exc:  # noqa: BLE001 - source read failed for this item
                msg.fetch_error = str(exc)
            else:
                if raw is None:
                    # Deleted between list and fetch — nothing to migrate.
                    log.warning("Message %s vanished before fetch — skipping", msg.source_id)
                    continue
                msg.raw_mime = raw

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
                # The state just written at the destination, when the connector
                # can report it again later (see _replay_state); else the
                # Message-ID.
                source_hash=msg.state_hash or msg.dedup_hash or None,
                dest_id=dest_id, status="done",
            )

        # Best-effort extra copies (multi_label_policy="duplicate"): the primary
        # copy is safe, so log-and-continue rather than flip the item to failed.
        for folder_path in folder_paths[1:]:
            try:
                _import_into(_get_folder(folder_path), msg)
            except Exception as exc:  # noqa: BLE001 - primary copy already imported
                log.warning("Extra folder copy %s failed for %s (primary imported): %s",
                            folder_path, msg.source_id, exc)

    if replayed or gone:
        left_alone = (
            f"; {gone} more were moved or deleted in the new mailbox and left alone"
            if gone else ""
        )
        log.info(
            "Read/flag state replayed onto %d already-migrated message(s) for %s%s",
            replayed, user.source_id, left_alone,
        )

    advance_cursor(ctx, "mail", failures, "message(s)")


def _whatif_mail(ctx: JobContext) -> None:
    import migrator as _pkg

    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"
    user = ctx.user

    for msg in ctx.track(ctx.source.inventory_messages(user), "mail"):
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
