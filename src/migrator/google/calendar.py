from __future__ import annotations

import logging
from typing import Any, Generator

from ..auth.google_auth import build_service
from ..config import GoogleConfig
from ..ratelimit import registry

log = logging.getLogger(__name__)


def _svc(cfg: GoogleConfig, email: str):
    return build_service("calendar", "v3", cfg.service_account_key_file, email)


def list_calendars(cfg: GoogleConfig, user_email: str) -> list[dict[str, Any]]:
    svc = _svc(cfg, user_email)
    try:
        registry.acquire("google_global")
    except KeyError:
        pass
    resp = svc.calendarList().list().execute()
    return resp.get("items", [])


def iter_events(
    cfg: GoogleConfig,
    user_email: str,
    calendar_id: str,
    sync_token: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Return (events, new_sync_token).

    Handles both full and incremental (delta) passes.
    """
    svc = _svc(cfg, user_email)
    events: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "calendarId": calendar_id,
        "maxResults": 2500,
        "showDeleted": True,
        "singleEvents": False,  # keep recurring series intact
    }
    if sync_token:
        params["syncToken"] = sync_token

    while True:
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.events().list(**params).execute()
        events.extend(resp.get("items", []))
        next_token = resp.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token
        params.pop("syncToken", None)

    new_sync_token: str = resp.get("nextSyncToken", "")
    return events, new_sync_token
