from __future__ import annotations

import logging

from ..context import JobContext
from ..microsoft.contacts import create_contact, ensure_contact_folder
from ..state.db import get_cursor, is_done, save_cursor, session_scope, upsert_item

log = logging.getLogger(__name__)


def run_contacts(ctx: JobContext) -> None:
    ctx.require_capability("contacts")
    user = ctx.user

    if ctx.mode == "whatif":
        _whatif_contacts(ctx)
        return

    gc = ctx.dest_gc
    assert gc is not None, "GraphClient required outside whatif mode"

    with session_scope() as s:
        since = get_cursor(s, user.source_id, "contacts") if ctx.mode == "delta" else None

    ms_user = gc.get(f"/users/{user.dest_id}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    folder_cache: dict[str, str] = {}

    def _get_folder(name: str) -> str:
        if name not in folder_cache:
            folder_cache[name] = ensure_contact_folder(gc, ms_user_id, name)
        return folder_cache[name]

    # Touch the default folder so it exists even for an empty source.
    _get_folder("Imported Contacts")

    for contact in ctx.source.iter_contacts(user, since):
        with session_scope() as s:
            if is_done(s, user.source_id, "contacts", contact.source_id):
                continue

        folder_id = _get_folder(contact.folder_name)
        try:
            dest_id = create_contact(gc, ms_user_id, folder_id, contact.graph_body)
            with session_scope() as s:
                upsert_item(
                    s, user.source_id, "contacts", contact.source_id,
                    dest_id=dest_id, status="done",
                )
        except Exception as exc:
            log.error("Failed to create contact %s: %s", contact.source_id, exc)
            with session_scope() as s:
                upsert_item(
                    s, user.source_id, "contacts", contact.source_id,
                    status="failed", last_error=str(exc),
                )

    new_cursor = ctx.source.get_last_cursor("contacts")
    if new_cursor:
        with session_scope() as s:
            save_cursor(s, user.source_id, "contacts", new_cursor)


def _whatif_contacts(ctx: JobContext) -> None:
    import migrator as _pkg

    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"
    user = ctx.user

    for contact in ctx.source.inventory_contacts(user):
        manifest.add(
            source_user=user.source_id,
            dest_user=user.dest_id,
            workload="contacts",
            source_id=contact.source_id,
            source_path=contact.folder_name,
            name=contact.display_name or contact.primary_email or contact.source_id,
            notes=f"email={contact.primary_email}" if contact.primary_email else "",
        )
