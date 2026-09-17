from __future__ import annotations

import logging
from typing import Any

from googleapiclient.errors import HttpError

from ..auth.google_auth import build_service
from ..config import GoogleConfig
from ..ratelimit import registry
from . import NUM_RETRIES

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
    else:
        # Without requestSyncToken the People API never returns nextSyncToken,
        # so incremental contact syncs would silently never work.
        params["requestSyncToken"] = True

    while True:
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        try:
            resp = svc.people().connections().list(**params).execute(num_retries=NUM_RETRIES)
        except HttpError as exc:
            if sync_token and exc.resp.status == 410:
                # EXPIRED_SYNC_TOKEN (tokens last ~7 days): re-baseline with a
                # fresh full sync rather than failing the contacts delta.
                log.warning(
                    "People sync token expired for %s — falling back to full sync",
                    user_email,
                )
                return iter_contacts(cfg, user_email, None)
            raise
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
    resp = svc.contactGroups().list().execute(num_retries=NUM_RETRIES)
    return resp.get("contactGroups", [])
