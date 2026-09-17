from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .graph_client import GraphClient

log = logging.getLogger(__name__)


def ensure_calendar(gc: GraphClient, ms_user_id: str, name: str) -> str:
    """Return Graph calendar ID, creating it if absent."""
    cals = gc.get(f"/users/{ms_user_id}/calendars")
    for cal in cals.get("value", []):
        if cal["name"] == name:
            return cal["id"]
    created = gc.post(
        f"/users/{ms_user_id}/calendars",
        user_key=ms_user_id,
        json={"name": name},
    )
    return created["id"]


def create_event(
    gc: GraphClient,
    ms_user_id: str,
    calendar_id: str,
    event_body: dict[str, Any],
) -> str:
    result = gc.post(
        f"/users/{ms_user_id}/calendars/{calendar_id}/events",
        user_key=ms_user_id,
        json=event_body,
    )
    return result["id"]


def patch_event(
    gc: GraphClient,
    ms_user_id: str,
    event_id: str,
    patch_body: dict[str, Any],
) -> None:
    gc.patch(
        f"/users/{ms_user_id}/events/{event_id}",
        user_key=ms_user_id,
        json=patch_body,
    )


def delete_event(gc: GraphClient, ms_user_id: str, event_id: str) -> None:
    gc.delete(f"/users/{ms_user_id}/events/{event_id}", user_key=ms_user_id)


def find_instance(
    gc: GraphClient,
    ms_user_id: str,
    master_event_id: str,
    original_start: str,
) -> str | None:
    """Locate the occurrence of a recurring series that started at
    `original_start` (source-side originalStartTime — full ISO with offset for
    timed events, bare "YYYY-MM-DD" for all-day). Used to apply modified /
    cancelled single occurrences onto the migrated series."""
    if len(original_start) == 10:  # all-day
        window_start = f"{original_start}T00:00:00Z"
        window_end = f"{original_start}T23:59:59Z"
    else:
        target = _to_utc(original_start)
        if target is None:
            return None
        window_start = (target - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        window_end = (target + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = gc.get(
        f"/users/{ms_user_id}/events/{master_event_id}/instances",
        user_key=ms_user_id,
        params={
            "startDateTime": window_start,
            "endDateTime": window_end,
            "$select": "id,start,originalStart",
            "$top": 50,
        },
    )
    for inst in data.get("value", []):
        if _instance_matches(inst, original_start):
            return str(inst["id"])
    return None


def _instance_matches(inst: dict[str, Any], original_start: str) -> bool:
    start = (inst.get("start") or {}).get("dateTime", "")
    if len(original_start) == 10:  # all-day: date equality is enough
        return start[:10] == original_start
    target = _to_utc(original_start)
    if target is None:
        return False
    # Prefer originalStart (stable even if the occurrence was already patched
    # to a new time on a prior run); fall back to the current start.
    for candidate in (inst.get("originalStart", ""), start):
        if candidate and _to_utc(candidate) == target:
            return True
    return False


def _to_utc(value: str) -> datetime | None:
    """Graph emits instance starts as naive-UTC with 7 fractional digits
    ("2026-03-10T16:00:00.0000000") and originalStart as "...Z"; the source
    side carries an offset. Normalize all three to aware-UTC for comparison."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
