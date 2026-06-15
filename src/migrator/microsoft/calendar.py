from __future__ import annotations

import logging
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
