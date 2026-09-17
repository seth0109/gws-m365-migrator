from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..state.db import get_folder_dest, session_scope, upsert_folder
from .graph_client import GRAPH_BASE, GraphClient

log = logging.getLogger(__name__)

# mailNickname allows ASCII 0-127 except these (per POST /groups docs), max 64.
_NICKNAME_ILLEGAL = set('@()\\[]";:<>, ')


def _sanitize_mail_nickname(alias: str) -> str:
    cleaned = "".join(
        c if ord(c) < 128 and c not in _NICKNAME_ILLEGAL else "-" for c in alias
    )
    cleaned = cleaned.strip("-.") or "site"
    return cleaned[:64]

# State namespace for the (shared drive → SharePoint site/drive) mapping. Keeps
# auto-provisioning idempotent: a re-run reuses the site created on the first run.
_STATE_USER = "__shared_drives__"
_STATE_WORKLOAD = "sharepoint_site"
# Group created but its site not yet resolved: lets a re-run resume polling
# instead of POSTing a second group with the same mailNickname (which 400s).
_GROUP_WORKLOAD = "sharepoint_group"

_PROVISION_POLL_ATTEMPTS = 30
_PROVISION_POLL_SECONDS = 10


def ensure_site_for_drive(
    gc: GraphClient,
    drive_id: str,
    alias: str,
    display_name: str,
    owner: str | None = None,
) -> tuple[str, str]:
    """Return (site_id, document_library_drive_id) for a shared drive, creating a
    connected SharePoint team site if one was not already provisioned.

    Idempotent via FolderMap (dest_id = SharePoint drive id, dest_path = site id),
    so re-runs reuse the existing site. Requires the destination app to hold
    Group.ReadWrite.All and Sites.ReadWrite.All (or Sites.FullControl.All).

    `owner` (UPN or user object id) is bound as the group owner: Graph documents
    that a group created app-only *without* owners may never get its SharePoint
    site provisioned. Visibility is Private — migrated drives must not default
    to org-wide visibility.
    """
    with session_scope() as s:
        existing_drive = get_folder_dest(s, _STATE_USER, _STATE_WORKLOAD, drive_id)
        if existing_drive:
            site_row = _lookup_site_id(s, drive_id)
            if site_row:
                log.info("Reusing SharePoint site for shared drive %s", drive_id)
                return site_row, existing_drive
        pending_group = get_folder_dest(s, _STATE_USER, _GROUP_WORKLOAD, drive_id)

    if pending_group:
        log.info(
            "Resuming site provisioning for shared drive %s (group %s)", drive_id, pending_group
        )
        group_id = pending_group
    else:
        body: dict[str, Any] = {
            "displayName": display_name,
            "mailNickname": _sanitize_mail_nickname(alias),
            "groupTypes": ["Unified"],
            "mailEnabled": True,
            "securityEnabled": False,
            "visibility": "Private",
        }
        if owner:
            body["owners@odata.bind"] = [f"{GRAPH_BASE}/users/{owner}"]
        else:
            log.warning(
                "Provisioning group %s app-only without an owner — the SharePoint "
                "site may never provision (set destination.sharepoint_site_owner)",
                display_name,
            )
        group = gc.post("/groups", json=body)
        group_id = str(group["id"])
        # Record the group *before* polling so a timeout leaves a resumable trail.
        with session_scope() as s:
            upsert_folder(s, _STATE_USER, _GROUP_WORKLOAD, drive_id, group_id, alias)
        log.info(
            "Created M365 group %s (%s) for shared drive %s", display_name, group_id, drive_id
        )

    site = _poll_group_site(gc, group_id)
    site_id = site["id"]
    library = gc.get(f"/sites/{site_id}/drive", params={"$select": "id"})
    library_drive_id = library["id"]

    with session_scope() as s:
        upsert_folder(s, _STATE_USER, _STATE_WORKLOAD, drive_id, library_drive_id, site_id)

    return site_id, library_drive_id


def resolve_existing_site_drive(gc: GraphClient, site_ref: str) -> tuple[str, str]:
    """Resolve an existing destination SharePoint site address to
    (site_id, default_library_drive_id). `site_ref` is a site id or a host:path
    address like "contoso.sharepoint.com:/sites/Marketing"."""
    site = gc.get(f"/sites/{site_ref}", params={"$select": "id"})
    drive = gc.get(f"/sites/{site['id']}/drive", params={"$select": "id"})
    return site["id"], drive["id"]


def _poll_group_site(gc: GraphClient, group_id: str) -> dict[str, Any]:
    """Poll until the group's connected SharePoint site has been provisioned."""
    last_exc: Exception | None = None
    for attempt in range(_PROVISION_POLL_ATTEMPTS):
        try:
            site: dict[str, Any] = gc.get(
                f"/groups/{group_id}/sites/root", params={"$select": "id,webUrl"}
            )
            return site
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404, 503):
                # Permanent failure (403 permissions, bad request, ...): waiting
                # through the whole poll window won't fix it — surface it now.
                raise
            last_exc = exc
            log.info("Waiting for site provisioning (attempt %d)…", attempt + 1)
            time.sleep(_PROVISION_POLL_SECONDS)
    raise RuntimeError(
        f"SharePoint site for group {group_id} was not provisioned in time. "
        "If the group was created without an owner, set "
        "destination.sharepoint_site_owner and re-run."
    ) from last_exc


def _lookup_site_id(session: Session, drive_id: str) -> str | None:
    from sqlalchemy import select

    from ..state.models import FolderMap

    site_id: str | None = session.execute(
        select(FolderMap.dest_path).where(
            FolderMap.user_email == _STATE_USER,
            FolderMap.workload == _STATE_WORKLOAD,
            FolderMap.source_path == drive_id,
        )
    ).scalar_one_or_none()
    return site_id
