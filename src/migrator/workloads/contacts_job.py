from __future__ import annotations

import logging

from ..context import JobContext
from ..microsoft.contacts import (
    create_contact,
    delete_contact,
    ensure_contact_folder,
    set_contact_photo,
    update_contact,
)
from ..state.db import get_item_state, session_scope, upsert_item
from .cursors import advance_cursor, delta_since

log = logging.getLogger(__name__)


def run_contacts(ctx: JobContext) -> None:
    ctx.require_capability("contacts")
    user = ctx.user

    if ctx.mode == "whatif":
        _whatif_contacts(ctx)
        return

    gc = ctx.dest_gc
    assert gc is not None, "GraphClient required outside whatif mode"

    # No seeded cursor: a "delta" would silently re-scan everything — this raises.
    since = delta_since(ctx, "contacts", "contacts", "contacts")

    ms_user = gc.get(f"/users/{user.dest_id}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    folder_cache: dict[str, str] = {}

    def _get_folder(name: str) -> str:
        if name not in folder_cache:
            folder_cache[name] = ensure_contact_folder(gc, ms_user_id, name)
        return folder_cache[name]

    # Touch the default folder so it exists even for an empty source.
    _get_folder("Imported Contacts")

    failures = 0
    for contact in ctx.track(ctx.source.iter_contacts(user, since), "contacts"):
        with session_scope() as s:
            state = get_item_state(s, user.source_id, "contacts", contact.source_id)
        # A recorded dest_id means the contact exists at the destination (even
        # after a failed update), so it is PATCHed rather than re-created.
        dest_existing = state.dest_id if state else None

        if contact.is_deleted:
            # Tombstone from an incremental sync: remove the migrated copy.
            if dest_existing:
                try:
                    delete_contact(gc, ms_user_id, dest_existing)
                except Exception as exc:
                    failures += 1
                    log.error("Failed to delete contact %s: %s", contact.source_id, exc)
                    with session_scope() as s:
                        upsert_item(
                            s, user.source_id, "contacts", contact.source_id,
                            dest_id=dest_existing, status="failed", last_error=str(exc),
                        )
                    continue
            with session_scope() as s:
                upsert_item(s, user.source_id, "contacts", contact.source_id, status="skipped")
            continue

        if state and state.status == "done":
            # Full pass: idempotent skip. Delta: the source reports a change —
            # apply it unless the change marker says otherwise.
            unchanged = bool(contact.source_hash) and state.source_hash == contact.source_hash
            if ctx.mode != "delta" or unchanged:
                continue

        try:
            if dest_existing:
                update_contact(gc, ms_user_id, dest_existing, contact.graph_body)
                dest_id = dest_existing
            else:
                folder_id = _get_folder(contact.folder_name)
                dest_id = create_contact(gc, ms_user_id, folder_id, contact.graph_body)
            with session_scope() as s:
                upsert_item(
                    s, user.source_id, "contacts", contact.source_id,
                    dest_id=dest_id, status="done", source_hash=contact.source_hash or None,
                )
            # Photo is best-effort fidelity: never fail (or re-create) a
            # migrated contact over it.
            if contact.photo_ref:
                try:
                    photo = ctx.source.fetch_contact_photo(user, contact)
                    if photo:
                        set_contact_photo(gc, ms_user_id, dest_id, photo)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Photo migration failed for %s: %s", contact.source_id, exc)
        except Exception as exc:
            failures += 1
            log.error("Failed to migrate contact %s: %s", contact.source_id, exc)
            with session_scope() as s:
                # Keep dest_id + the hash the destination copy still reflects, so the
                # retry PATCHes it rather than creating a duplicate.
                upsert_item(
                    s, user.source_id, "contacts", contact.source_id,
                    dest_id=dest_existing, source_hash=state.source_hash if state else None,
                    status="failed", last_error=str(exc),
                )

    advance_cursor(ctx, "contacts", failures, "contact(s)")


def _whatif_contacts(ctx: JobContext) -> None:
    import migrator as _pkg

    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"
    user = ctx.user

    for contact in ctx.track(ctx.source.inventory_contacts(user), "contacts"):
        manifest.add(
            source_user=user.source_id,
            dest_user=user.dest_id,
            workload="contacts",
            source_id=contact.source_id,
            source_path=contact.folder_name,
            name=contact.display_name or contact.primary_email or contact.source_id,
            notes=f"email={contact.primary_email}" if contact.primary_email else "",
        )
