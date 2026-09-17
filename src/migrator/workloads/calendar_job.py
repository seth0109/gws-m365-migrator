from __future__ import annotations

import logging

from sqlalchemy import select

from ..connectors.base import SourceEvent
from ..context import JobContext
from ..microsoft.calendar import (
    create_event,
    delete_event,
    ensure_calendar,
    find_instance,
    patch_event,
)
from ..microsoft.graph_client import GraphClient
from ..state.db import (
    get_cursor,
    get_dest_id,
    get_item_state,
    save_cursor,
    session_scope,
    upsert_item,
)
from ..state.models import ItemMap
from ..transform.identities import IdentityMap

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

    # Rewrite source-tenant attendee/organizer addresses to destination UPNs so
    # migrated events don't reference dead source mailboxes.
    identities = IdentityMap(ctx.config.users)

    for cal in ctx.source.list_calendars(user):
        ms_cal_id = ensure_calendar(gc, ms_user_id, cal.name)
        key = f"calendar:{cal.cal_id}"

        since: str | None = None
        if ctx.mode == "delta":
            with session_scope() as s:
                since = get_cursor(s, user.source_id, key)
            if not since:
                log.warning("No cursor for %s calendar %s — run a full pass first; skipping",
                            user.source_id, cal.name)
                continue

        failures = 0
        exceptions: list[SourceEvent] = []
        events = iter(ctx.source.iter_events(user, cal, since))
        while True:
            # Advance the generator inside the try: a connector/transform error
            # on one event must not abort the remaining calendars. A generator
            # is dead after raising, so enumeration of *this* calendar stops,
            # but the failure is counted and the cursor stays put.
            try:
                event = next(events)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 - connector enumeration failed
                failures += 1
                log.error("Event enumeration failed for %s calendar %s: %s",
                          user.source_id, cal.name, exc)
                break

            with session_scope() as s:
                state = get_item_state(s, user.source_id, key, event.source_id)
            done = state is not None and state.status == "done"
            # Full pass: a done item is skipped (idempotency) unless the source
            # now reports it cancelled. Delta: the source says it changed, so it
            # flows on to the update-in-place path below.
            if done and ctx.mode != "delta" and not event.is_cancelled:
                continue
            if event.master_source_id:
                # Modified/cancelled single occurrence of a recurring series —
                # defer until after the pass so its master exists at the
                # destination, then apply it onto the migrated series.
                exceptions.append(event)
                continue
            # A recorded dest_id means the event exists at the destination (even
            # after a failed update): PATCH it, never create a duplicate.
            dest_existing = state.dest_id if state else None
            try:
                if event.is_cancelled:
                    _handle_cancelled(gc, ms_user_id, user.source_id, key, event.source_id)
                    continue
                if done and event.source_hash and state and state.source_hash == event.source_hash:
                    continue  # change marker unchanged since it was migrated
                identities.remap_event(event.graph_body)
                if dest_existing:
                    patch_event(gc, ms_user_id, dest_existing, event.graph_body)
                    dest_id = dest_existing
                else:
                    dest_id = create_event(gc, ms_user_id, ms_cal_id, event.graph_body)
                with session_scope() as s:
                    upsert_item(
                        s, user.source_id, key, event.source_id,
                        dest_id=dest_id, status="done", source_hash=event.source_hash or None,
                    )
            except Exception as exc:
                failures += 1
                log.error("Failed event %s for %s: %s", event.source_id, user.source_id, exc)
                with session_scope() as s:
                    upsert_item(
                        s, user.source_id, key, event.source_id,
                        dest_id=dest_existing, source_hash=state.source_hash if state else None,
                        status="failed", last_error=str(exc),
                    )

        for exc_event in exceptions:
            if not _apply_exception(gc, ms_user_id, user.source_id, key, exc_event, identities):
                failures += 1

        new_cursor = ctx.source.get_last_cursor(key)
        if new_cursor:
            if failures:
                log.warning("%d event(s) failed in %s calendar %s — cursor not advanced",
                            failures, user.source_id, cal.name)
            else:
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


def _apply_exception(
    gc: GraphClient,
    ms_user_id: str,
    source_user: str,
    workload_key: str,
    event: SourceEvent,
    identities: IdentityMap,
) -> bool:
    """Apply one modified/cancelled occurrence onto the migrated series.

    The destination occurrence is located via the series master's instances
    around the original start, then PATCHed (modified) or DELETEd (cancelled).
    Returns False on failure so the caller holds back the sync cursor."""
    with session_scope() as s:
        master_dest = get_dest_id(s, source_user, workload_key, event.master_source_id)
        state = get_item_state(s, source_user, workload_key, event.source_id)
    if (
        state is not None and state.status == "done"
        and event.source_hash and state.source_hash == event.source_hash
    ):
        return True  # already applied and unchanged since (delta re-emit)
    try:
        if not master_dest:
            raise RuntimeError(f"series master {event.master_source_id} is not migrated")
        if not event.original_start:
            raise RuntimeError("occurrence carries no originalStartTime to match on")
        instance_id = find_instance(gc, ms_user_id, master_dest, event.original_start)
        if not instance_id:
            if event.is_cancelled:
                # Nothing to delete — the occurrence is already gone (e.g. a
                # prior run deleted it but crashed before recording it).
                with session_scope() as s:
                    upsert_item(s, source_user, workload_key, event.source_id, status="skipped")
                return True
            raise RuntimeError("no matching occurrence in the destination series")
        if event.is_cancelled:
            delete_event(gc, ms_user_id, instance_id)
        else:
            identities.remap_event(event.graph_body)
            patch_event(gc, ms_user_id, instance_id, event.graph_body)
        with session_scope() as s:
            upsert_item(
                s, source_user, workload_key, event.source_id,
                dest_id=instance_id, status="done", source_hash=event.source_hash or None,
            )
        return True
    except Exception as exc:
        log.error("Failed recurrence exception %s: %s", event.source_id, exc)
        with session_scope() as s:
            upsert_item(
                s, source_user, workload_key, event.source_id,
                status="failed", last_error=str(exc),
            )
        return False


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
