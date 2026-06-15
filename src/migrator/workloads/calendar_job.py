from __future__ import annotations

import logging
from typing import Any

from ..config import UserMapping
from ..google.calendar import iter_events, list_calendars
from ..microsoft.calendar import create_event, delete_event, ensure_calendar, patch_event
from ..microsoft.graph_client import GraphClient
from ..state.db import get_cursor, is_done, save_cursor, session_scope, upsert_item
from ..transform.recurrence import rrule_to_graph_recurrence

log = logging.getLogger(__name__)


def run_calendar(user: UserMapping, gc: GraphClient | None, mode: str) -> None:
    import migrator as _pkg
    cfg = _pkg._current_config
    assert cfg is not None

    google_cfg = cfg.google

    if mode == "whatif":
        _whatif_calendar(user, google_cfg)
        return

    assert gc is not None, "GraphClient required outside whatif mode"

    ms_user = gc.get(f"/users/{user.ms_upn}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    calendars = list_calendars(google_cfg, user.google_email)
    for gcal in calendars:
        cal_id: str = gcal["id"]
        cal_name: str = gcal.get("summary", cal_id)

        # Skip system calendars that don't map well
        if cal_id == "contacts@group.v.calendar.google.com":
            continue

        ms_cal_id = ensure_calendar(gc, ms_user_id, cal_name)
        cursor_key = f"calendar:{cal_id}"

        with session_scope() as s:
            sync_token = get_cursor(s, user.google_email, cursor_key) if mode == "delta" else None

        events, new_sync_token = iter_events(google_cfg, user.google_email, cal_id, sync_token)

        for event in events:
            event_id: str = event.get("id", "")
            workload_key = f"calendar:{cal_id}"

            with session_scope() as s:
                if is_done(s, user.google_email, workload_key, event_id):
                    continue

            try:
                if event.get("status") == "cancelled":
                    # Try to delete if we previously imported it
                    _handle_cancelled(gc, ms_user_id, user.google_email, workload_key, event_id)
                    continue

                event_body = _map_event(event)
                dest_id = create_event(gc, ms_user_id, ms_cal_id, event_body)
                with session_scope() as s:
                    upsert_item(s, user.google_email, workload_key, event_id, dest_id=dest_id, status="done")
            except Exception as exc:
                log.error("Failed event %s for %s: %s", event_id, user.google_email, exc)
                with session_scope() as s:
                    upsert_item(s, user.google_email, workload_key, event_id, status="failed", last_error=str(exc))

        if new_sync_token:
            with session_scope() as s:
                save_cursor(s, user.google_email, cursor_key, new_sync_token)


def _whatif_calendar(user: UserMapping, google_cfg: Any) -> None:
    import migrator as _pkg
    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"

    calendars = list_calendars(google_cfg, user.google_email)
    for gcal in calendars:
        cal_id: str = gcal["id"]
        cal_name: str = gcal.get("summary", cal_id)

        if cal_id == "contacts@group.v.calendar.google.com":
            continue

        events, _ = iter_events(google_cfg, user.google_email, cal_id, None)
        for event in events:
            event_id = event.get("id", "")
            if not event_id:
                continue
            if event.get("status") == "cancelled":
                action = "skip"
                notes = "cancelled"
            else:
                action = "migrate"
                notes = "recurring" if event.get("recurrence") else ""

            start = event.get("start", {})
            modified = event.get("updated", "") or start.get("dateTime", "") or start.get("date", "")

            manifest.add(
                user_email=user.google_email,
                ms_upn=user.ms_upn,
                workload="calendar",
                source_id=event_id,
                source_path=cal_name,
                name=event.get("summary", "(No title)"),
                modified_time=modified,
                action=action,
                notes=notes,
            )


def _handle_cancelled(
    gc: GraphClient,
    ms_user_id: str,
    user_email: str,
    workload_key: str,
    event_id: str,
) -> None:
    from ..state.db import session_scope
    from sqlalchemy import select
    from ..state.models import ItemMap

    with session_scope() as s:
        row = s.execute(
            select(ItemMap).where(
                ItemMap.user_email == user_email,
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


def _map_event(event: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "subject": event.get("summary", "(No title)"),
        "body": {
            "contentType": "html" if event.get("description", "").startswith("<") else "text",
            "content": event.get("description", ""),
        },
    }

    start = event.get("start", {})
    end = event.get("end", {})

    if "dateTime" in start:
        body["start"] = {"dateTime": start["dateTime"], "timeZone": start.get("timeZone", "UTC")}
        body["end"] = {"dateTime": end["dateTime"], "timeZone": end.get("timeZone", "UTC")}
    else:
        # All-day event
        body["start"] = {"date": start["date"]}
        body["end"] = {"date": end["date"]}
        body["isAllDay"] = True

    location = event.get("location", "")
    if location:
        body["location"] = {"displayName": location}

    attendees = event.get("attendees", [])
    if attendees:
        body["attendees"] = [
            {
                "emailAddress": {"address": a["email"], "name": a.get("displayName", a["email"])},
                "type": "required" if a.get("optional") is not True else "optional",
            }
            for a in attendees
        ]

    recurrence_rules = event.get("recurrence", [])
    for rule in recurrence_rules:
        if rule.startswith("RRULE:"):
            start_dt = start.get("dateTime") or start.get("date", "")
            graph_rec = rrule_to_graph_recurrence(rule, start_dt)
            if graph_rec:
                body["recurrence"] = graph_rec
            break

    return body
