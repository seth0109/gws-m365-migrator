from __future__ import annotations

import logging

from sqlalchemy import select

from ..context import JobContext
from ..microsoft.calendar import create_event, delete_event, ensure_calendar
from ..microsoft.graph_client import GraphClient
from ..state.db import get_cursor, is_done, save_cursor, session_scope, upsert_item
from ..state.models import ItemMap

log = logging.getLogger(__name__)


def run_calendar(ctx: JobContext) -> None:
    ctx.require_capability("calendar")
    user = ctx.user

    if ctx.mode == "whatif":
        _whatif_calendar(ctx)
        return

    gc = ctx.dest_gc
    assert gc is not None, "GraphClient required outside whatif mode"

    ms_user = gc.get(f"/users/{user.dest_id}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    for cal in ctx.source.list_calendars(user):
        ms_cal_id = ensure_calendar(gc, ms_user_id, cal.name)
        key = f"calendar:{cal.cal_id}"

        with session_scope() as s:
            since = get_cursor(s, user.source_id, key) if ctx.mode == "delta" else None

        for event in ctx.source.iter_events(user, cal, since):
            with session_scope() as s:
                if is_done(s, user.source_id, key, event.source_id):
                    continue
            try:
                if event.is_cancelled:
                    _handle_cancelled(gc, ms_user_id, user.source_id, key, event.source_id)
                    continue
                dest_id = create_event(gc, ms_user_id, ms_cal_id, event.graph_body)
                with session_scope() as s:
                    upsert_item(
                        s, user.source_id, key, event.source_id, dest_id=dest_id, status="done"
                    )
            except Exception as exc:
                log.error("Failed event %s for %s: %s", event.source_id, user.source_id, exc)
                with session_scope() as s:
                    upsert_item(
                        s, user.source_id, key, event.source_id,
                        status="failed", last_error=str(exc),
                    )

        new_cursor = ctx.source.get_last_cursor(key)
        if new_cursor:
            with session_scope() as s:
                save_cursor(s, user.source_id, key, new_cursor)


def _whatif_calendar(ctx: JobContext) -> None:
    import migrator as _pkg

    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"
    user = ctx.user

    for cal in ctx.source.list_calendars(user):
        for event in ctx.source.iter_events(user, cal, None):
            manifest.add(
                source_user=user.source_id,
                dest_user=user.dest_id,
                workload="calendar",
                source_id=event.source_id,
                source_path=cal.name,
                name=event.subject,
                modified_time=event.modified_time,
                action="skip" if event.is_cancelled else "migrate",
                notes="cancelled" if event.is_cancelled else event.notes,
            )


def _handle_cancelled(
    gc: GraphClient,
    ms_user_id: str,
    source_user: str,
    workload_key: str,
    event_id: str,
) -> None:
    with session_scope() as s:
        row = s.execute(
            select(ItemMap).where(
                ItemMap.user_email == source_user,
                ItemMap.workload == workload_key,
                ItemMap.source_id == event_id,
                ItemMap.status == "done",
            )
        ).scalar_one_or_none()
        if row and row.dest_id:
            try:
                delete_event(gc, ms_user_id, row.dest_id)
                row.status = "skipped"
            except Exception as exc:
                log.warning("Could not delete cancelled event %s: %s", event_id, exc)
