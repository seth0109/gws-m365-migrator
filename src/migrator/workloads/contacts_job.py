from __future__ import annotations

import logging
from typing import Any

from ..config import UserMapping
from ..google.people import iter_contacts, list_contact_groups
from ..microsoft.contacts import create_contact, ensure_contact_folder
from ..microsoft.graph_client import GraphClient
from ..state.db import get_cursor, is_done, save_cursor, session_scope, upsert_item

log = logging.getLogger(__name__)


def run_contacts(user: UserMapping, gc: GraphClient | None, mode: str) -> None:
    import migrator as _pkg
    cfg = _pkg._current_config
    assert cfg is not None, "Orchestrator must set _current_config before dispatching"

    google_cfg = cfg.google

    if mode == "whatif":
        _whatif_contacts(user, google_cfg)
        return

    assert gc is not None, "GraphClient required outside whatif mode"

    with session_scope() as s:
        sync_token = get_cursor(s, user.google_email, "contacts") if mode == "delta" else None

    contacts, new_sync_token = iter_contacts(google_cfg, user.google_email, sync_token)

    # Resolve MS user ID
    ms_user = gc.get(f"/users/{user.ms_upn}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    # Build group → folder mapping
    groups = list_contact_groups(google_cfg, user.google_email)
    group_folder_cache: dict[str, str] = {}

    def _get_folder(group_name: str) -> str:
        if group_name not in group_folder_cache:
            folder_id = ensure_contact_folder(gc, ms_user_id, group_name)
            group_folder_cache[group_name] = folder_id
        return group_folder_cache[group_name]

    default_folder_id = _get_folder("Imported Contacts")

    for contact in contacts:
        resource_name: str = contact.get("resourceName", "")
        source_id = resource_name.replace("people/", "")

        with session_scope() as s:
            if is_done(s, user.google_email, "contacts", source_id):
                log.debug("Skipping already-done contact %s", source_id)
                continue

        memberships = contact.get("memberships", [])
        group_resource = next(
            (m.get("contactGroupMembership", {}).get("contactGroupResourceName")
             for m in memberships if "contactGroupMembership" in m),
            None,
        )

        folder_id = default_folder_id
        if group_resource:
            group_name = next(
                (g["name"] for g in groups
                 if g.get("resourceName") == group_resource),
                None,
            )
            if group_name and not group_name.startswith("contactGroups/"):
                folder_id = _get_folder(group_name)

        contact_body = _map_contact(contact)
        try:
            dest_id = create_contact(gc, ms_user_id, folder_id, contact_body)
            with session_scope() as s:
                upsert_item(s, user.google_email, "contacts", source_id, dest_id=dest_id, status="done")
        except Exception as exc:
            log.error("Failed to create contact %s: %s", source_id, exc)
            with session_scope() as s:
                upsert_item(s, user.google_email, "contacts", source_id, status="failed", last_error=str(exc))

    if new_sync_token:
        with session_scope() as s:
            save_cursor(s, user.google_email, "contacts", new_sync_token)


def _whatif_contacts(user: UserMapping, google_cfg: Any) -> None:
    import migrator as _pkg
    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"

    contacts, _ = iter_contacts(google_cfg, user.google_email, None)
    groups = list_contact_groups(google_cfg, user.google_email)
    group_name_by_resource = {
        g.get("resourceName"): g.get("name", "")
        for g in groups
        if g.get("resourceName")
    }

    for contact in contacts:
        resource_name = contact.get("resourceName", "")
        source_id = resource_name.replace("people/", "")
        names = contact.get("names", [])
        display_name = names[0].get("displayName", "") if names else ""
        emails = contact.get("emailAddresses", [])
        primary_email = emails[0].get("value", "") if emails else ""

        memberships = contact.get("memberships", [])
        group_resource = next(
            (m.get("contactGroupMembership", {}).get("contactGroupResourceName")
             for m in memberships if "contactGroupMembership" in m),
            None,
        )
        group_name = group_name_by_resource.get(group_resource, "") if group_resource else ""
        if group_name.startswith("contactGroups/"):
            group_name = ""
        folder = group_name or "Imported Contacts"

        manifest.add(
            user_email=user.google_email,
            ms_upn=user.ms_upn,
            workload="contacts",
            source_id=source_id,
            source_path=folder,
            name=display_name or primary_email or source_id,
            notes=f"email={primary_email}" if primary_email else "",
        )


def _map_contact(c: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}

    names = c.get("names", [])
    if names:
        n = names[0]
        body["givenName"] = n.get("givenName", "")
        body["surname"] = n.get("familyName", "")
        body["displayName"] = n.get("displayName", "")

    emails = c.get("emailAddresses", [])
    if emails:
        body["emailAddresses"] = [
            {"address": e["value"], "name": e.get("displayName", e["value"])}
            for e in emails
        ]

    phones = c.get("phoneNumbers", [])
    if phones:
        body["businessPhones"] = [p["value"] for p in phones if p.get("type", "") in ("work", "")]
        body["homePhones"] = [p["value"] for p in phones if p.get("type") == "home"]
        mobile = next((p["value"] for p in phones if p.get("type") == "mobile"), None)
        if mobile:
            body["mobilePhone"] = mobile

    orgs = c.get("organizations", [])
    if orgs:
        body["companyName"] = orgs[0].get("name", "")
        body["jobTitle"] = orgs[0].get("title", "")

    addresses = c.get("addresses", [])
    if addresses:
        a = addresses[0]
        body["businessAddress"] = {
            "street": a.get("streetAddress", ""),
            "city": a.get("city", ""),
            "state": a.get("region", ""),
            "postalCode": a.get("postalCode", ""),
            "countryOrRegion": a.get("country", ""),
        }

    return body
