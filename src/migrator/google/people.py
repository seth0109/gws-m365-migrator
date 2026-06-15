from __future__ import annotations

import logging
from typing import Any, Generator

from ..auth.google_auth import build_service
from ..config import GoogleConfig
from ..ratelimit import registry

log = logging.getLogger(__name__)

_PERSON_FIELDS = (
    "names,emailAddresses,phoneNumbers,addresses,organizations,"
    "birthdays,urls,biographies,memberships,photos"
)


def _svc(cfg: GoogleConfig, email: str):
    return build_service("people", "v1", cfg.service_account_key_file, email)


def iter_contacts(
    cfg: GoogleConfig,
    user_email: str,
    sync_token: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Return (contacts, new_sync_token).

    If *sync_token* is provided, performs an incremental sync (delta pass).
    """
    svc = _svc(cfg, user_email)
    contacts: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "resourceName": "people/me",
        "personFields": _PERSON_FIELDS,
        "pageSize": 1000,
    }
    if sync_token:
        params["syncToken"] = sync_token

    while True:
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.people().connections().list(**params).execute()
        contacts.extend(resp.get("connections", []))
        next_token = resp.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token

    new_sync_token: str = resp.get("nextSyncToken", "")
    return contacts, new_sync_token


def list_contact_groups(cfg: GoogleConfig, user_email: str) -> list[dict[str, Any]]:
    svc = _svc(cfg, user_email)
    try:
        registry.acquire("google_global")
    except KeyError:
        pass
    resp = svc.contactGroups().list().execute()
    return resp.get("contactGroups", [])
