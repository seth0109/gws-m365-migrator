from __future__ import annotations

import email as email_lib
import logging
import re
from collections.abc import Iterator
from typing import Any

import httpx

from ..config import GoogleWorkspaceSourceConfig, UserMapping
from ..google.calendar import iter_events, list_calendars
from ..google.drive import (
    EXPORT_MIME_MAP,
    download_file,
    export_native_file,
    get_changes_start_token,
    iter_drive_changes,
    iter_my_drive_files,
    iter_shared_drive_files,
    list_shared_drives,
)
from ..google.gmail import (
    decode_raw_mime,
    get_history_id,
    iter_message_metadata,
    iter_messages,
    list_labels,
)
from ..google.people import iter_contacts, list_contact_groups
from ..transform.labels import MultiLabelPolicy, resolve_label_placement
from ..transform.paths import sanitize_segment
from ..transform.recurrence import rrule_to_graph_recurrence
from .base import (
    BaseSource,
    CalendarRef,
    SharedDriveRef,
    SourceContact,
    SourceEvent,
    SourceFile,
    SourceMessage,
)

log = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleWorkspaceSource(BaseSource):
    """Read-only Google Workspace source (Gmail / Drive / People / Calendar)."""

    capabilities = {"mail", "files", "contacts", "calendar"}

    def __init__(
        self,
        cfg: GoogleWorkspaceSourceConfig,
        mail_policy: MultiLabelPolicy,
        include_spam_trash: bool = True,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.mail_policy = mail_policy
        self.include_spam_trash = include_spam_trash

    # -- mail --------------------------------------------------------------- #
    def _label_map(self, user: UserMapping) -> dict[str, str]:
        return {lbl["id"]: lbl["name"] for lbl in list_labels(self.cfg, user.source_id)}

    def iter_messages(self, user: UserMapping, since: str | None) -> Iterator[SourceMessage]:
        label_map = self._label_map(user)
        if since is None:
            # Capture the history cursor before reading so the delta pass sees
            # everything that arrives during this run.
            self._set_cursor("mail", get_history_id(self.cfg, user.source_id))
        for msg in iter_messages(
            self.cfg, user.source_id, history_id=since,
            include_spam_trash=self.include_spam_trash,
        ):
            yield self._to_message(msg, label_map)

    def inventory_messages(self, user: UserMapping) -> Iterator[SourceMessage]:
        label_map = self._label_map(user)
        for msg in iter_message_metadata(
            self.cfg, user.source_id, include_spam_trash=self.include_spam_trash
        ):
            msg_id = msg.get("id", "")
            if not msg_id:
                continue
            label_ids = msg.get("labelIds", [])
            folder_paths, categories = resolve_label_placement(
                label_ids, label_map, self.mail_policy
            )
            if "IMPORTANT" in label_ids:
                categories = [*categories, "Important"]
            headers = msg.get("headers", {})
            yield SourceMessage(
                source_id=msg_id,
                raw_mime=b"",
                folder_paths=folder_paths,
                categories=categories,
                is_read="UNREAD" not in label_ids,
                is_flagged="STARRED" in label_ids,
                subject=headers.get("Subject", "(no subject)"),
                size_bytes=msg.get("sizeEstimate", ""),
                date=headers.get("Date", ""),
                sender=headers.get("From", ""),
            )

    def _to_message(self, msg: dict[str, Any], label_map: dict[str, str]) -> SourceMessage:
        msg_id = msg.get("id", "")
        if fetch_error := msg.get("fetch_error"):
            return SourceMessage(
                source_id=msg_id, raw_mime=b"", folder_paths=["Inbox"],
                fetch_error=str(fetch_error),
            )
        raw_bytes = decode_raw_mime(msg.get("raw", ""))
        parsed = email_lib.message_from_bytes(raw_bytes)
        label_ids = msg.get("labelIds", [])
        folder_paths, categories = resolve_label_placement(
            label_ids, label_map, self.mail_policy
        )
        # System-label state rides on the message, not the folder mapping:
        # STARRED → Outlook follow-up flag, IMPORTANT → a category (Outlook
        # `importance` means sender-set priority — a different concept).
        if "IMPORTANT" in label_ids:
            categories = [*categories, "Important"]
        return SourceMessage(
            source_id=msg_id,
            raw_mime=raw_bytes,
            folder_paths=folder_paths,
            categories=categories,
            is_read="UNREAD" not in label_ids,
            is_flagged="STARRED" in label_ids,
            dedup_hash=parsed.get("Message-ID", msg_id),
            subject=parsed.get("Subject", ""),
        )

    # -- files -------------------------------------------------------------- #
    def iter_files(self, user: UserMapping, since: str | None) -> Iterator[SourceFile]:
        if since is None:
            self._set_cursor("files", get_changes_start_token(self.cfg, user.source_id))
            for gfile in iter_my_drive_files(self.cfg, user.source_id):
                yield self._to_file(gfile)
        else:
            changed, new_token = iter_drive_changes(self.cfg, user.source_id, since)
            self._set_cursor("files", new_token)
            for gfile in changed:
                yield self._to_file(gfile)

    def list_shared_drives(self, user: UserMapping) -> list[SharedDriveRef]:
        return [
            SharedDriveRef(drive_id=d["id"], name=d.get("name", d["id"]))
            for d in list_shared_drives(self.cfg, user.source_id)
        ]

    def iter_shared_drive_files(
        self, user: UserMapping, drive: SharedDriveRef, since: str | None = None
    ) -> Iterator[SourceFile]:
        key = f"shared_drive:{drive.drive_id}"
        if since is None:
            # Capture the per-drive change cursor before the full enumeration so
            # the next delta pass sees anything that lands mid-run.
            self._set_cursor(
                key, get_changes_start_token(self.cfg, user.source_id, drive.drive_id)
            )
            for gfile in iter_shared_drive_files(self.cfg, user.source_id, drive.drive_id):
                yield self._to_file(gfile)
        else:
            changed, new_token = iter_drive_changes(
                self.cfg, user.source_id, since, drive.drive_id
            )
            self._set_cursor(key, new_token)
            for gfile in changed:
                yield self._to_file(gfile)

    def _to_file(self, gfile: dict[str, Any]) -> SourceFile:
        mime = gfile.get("mimeType", "")
        parents = gfile.get("parents", [])
        name = sanitize_segment(gfile.get("name", gfile["id"]))
        action = "migrate"
        export_ext: str | None = None
        notes = ""
        if mime == _FOLDER_MIME:
            action = "create-folder"
        else:
            export_info = EXPORT_MIME_MAP.get(mime)
            if export_info is None and mime.startswith("application/vnd.google-apps."):
                action = "skip"
                notes = f"unsupported native type: {mime}"
            elif export_info:
                action = "export"
                export_ext = export_info[1]
                notes = "native Google Doc"
        return SourceFile(
            source_id=gfile["id"],
            name=name,
            mime_type=mime,
            parent_id=parents[0] if parents else None,
            is_folder=mime == _FOLDER_MIME,
            size=gfile.get("size", ""),
            modified_time=gfile.get("modifiedTime", ""),
            content_hash=gfile.get("md5Checksum") or gfile.get("version"),
            action=action,
            export_ext=export_ext,
            notes=notes,
        )

    def fetch_file(self, user: UserMapping, f: SourceFile) -> tuple[bytes, str]:
        if f.action == "export":
            export_mime = EXPORT_MIME_MAP[f.mime_type][0]  # type: ignore[index]
            content = export_native_file(self.cfg, user.source_id, f.source_id, export_mime)
            name = f.name if f.name.endswith(f.export_ext or "") else f.name + (f.export_ext or "")
            return content, name
        return download_file(self.cfg, user.source_id, f.source_id), f.name

    def inventory_files(self, user: UserMapping) -> Iterator[SourceFile]:
        all_files = list(iter_my_drive_files(self.cfg, user.source_id))
        folders = {f["id"]: f for f in all_files if f.get("mimeType") == _FOLDER_MIME}

        def resolve_path(g: dict[str, Any], seen: set[str] | None = None) -> str:
            seen = seen or set()
            name = str(g.get("name", g["id"]))
            if g["id"] in seen:
                return name
            seen.add(g["id"])
            parents = g.get("parents", [])
            if not parents:
                return name
            parent = folders.get(parents[0])
            if parent is None:
                return f"(external:{parents[0]})/{name}"
            return f"{resolve_path(parent, seen)}/{name}"

        for gfile in all_files:
            sf = self._to_file(gfile)
            sf.source_path = resolve_path(gfile)
            sf.name = gfile.get("name", gfile["id"])  # raw name for the manifest
            yield sf

    # -- contacts ----------------------------------------------------------- #
    def iter_contacts(self, user: UserMapping, since: str | None) -> Iterator[SourceContact]:
        contacts, new_token = iter_contacts(self.cfg, user.source_id, since)
        self._set_cursor("contacts", new_token)
        group_names = self._group_names(user)
        for contact in contacts:
            yield self._to_contact(contact, group_names)

    def inventory_contacts(self, user: UserMapping) -> Iterator[SourceContact]:
        contacts, _ = iter_contacts(self.cfg, user.source_id, None)
        group_names = self._group_names(user)
        for contact in contacts:
            yield self._to_contact(contact, group_names)

    def _group_names(self, user: UserMapping) -> dict[str, str]:
        return {
            g["resourceName"]: g.get("name", "")
            for g in list_contact_groups(self.cfg, user.source_id)
            if g.get("resourceName")
        }

    def fetch_contact_photo(self, user: UserMapping, contact: SourceContact) -> bytes | None:
        if not contact.photo_ref:
            return None
        # People photo URLs are token-authenticated and directly fetchable, but
        # default to a small thumbnail (...=s100); ask for a usable size.
        url = re.sub(r"=s\d+(-c)?$", "=s512", contact.photo_ref)
        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        if resp.status_code != 200:
            return None
        if not resp.headers.get("content-type", "").startswith("image/"):
            return None
        return resp.content

    def _to_contact(self, contact: dict[str, Any], group_names: dict[str, str]) -> SourceContact:
        resource_name = contact.get("resourceName", "")
        source_id = resource_name.replace("people/", "")

        memberships = contact.get("memberships", [])
        group_resource = next(
            (
                m.get("contactGroupMembership", {}).get("contactGroupResourceName")
                for m in memberships
                if "contactGroupMembership" in m
            ),
            None,
        )
        folder = "Imported Contacts"
        if group_resource:
            gname = group_names.get(group_resource, "")
            if gname and not gname.startswith("contactGroups/"):
                folder = gname

        names = contact.get("names", [])
        display_name = names[0].get("displayName", "") if names else ""
        emails = contact.get("emailAddresses", [])
        primary_email = emails[0].get("value", "") if emails else ""
        # Real (user-set) photo only: `default: true` marks the generated
        # initials avatar, which is not worth migrating.
        photo_url = next(
            (p.get("url", "") for p in contact.get("photos", []) if not p.get("default")),
            "",
        )

        metadata = contact.get("metadata") or {}
        return SourceContact(
            source_id=source_id,
            graph_body=_map_contact(contact),
            folder_name=folder,
            photo_ref=photo_url,
            source_hash=contact.get("etag", ""),
            # Incremental sync returns deleted people as tombstones.
            is_deleted=bool(metadata.get("deleted")),
            display_name=display_name,
            primary_email=primary_email,
        )

    # -- calendar ----------------------------------------------------------- #
    def list_calendars(self, user: UserMapping) -> list[CalendarRef]:
        out = []
        for gcal in list_calendars(self.cfg, user.source_id):
            cal_id = gcal["id"]
            if cal_id == "contacts@group.v.calendar.google.com":
                continue
            out.append(CalendarRef(cal_id=cal_id, name=gcal.get("summary", cal_id)))
        return out

    def iter_events(
        self, user: UserMapping, cal: CalendarRef, since: str | None
    ) -> Iterator[SourceEvent]:
        events, new_token = iter_events(self.cfg, user.source_id, cal.cal_id, since)
        self._set_cursor(f"calendar:{cal.cal_id}", new_token)
        for event in events:
            event_id = event.get("id", "")
            if not event_id:
                continue
            cancelled = event.get("status") == "cancelled"
            start = event.get("start", {})
            modified = (
                event.get("updated", "") or start.get("dateTime", "") or start.get("date", "")
            )
            # A modified/cancelled single occurrence of a recurring series
            # (singleEvents=False still returns these alongside the master).
            master = event.get("recurringEventId", "")
            original = event.get("originalStartTime", {}) or {}
            if event.get("recurrence"):
                note = "recurring"
            elif master:
                note = "recurrence exception"
            else:
                note = ""
            yield SourceEvent(
                source_id=event_id,
                graph_body={} if cancelled else _map_event(event),
                is_cancelled=cancelled,
                master_source_id=master,
                original_start=original.get("dateTime") or original.get("date", ""),
                source_hash=event.get("etag", ""),
                subject=event.get("summary", "(No title)"),
                modified_time=modified,
                notes=note,
            )


# --------------------------------------------------------------------------- #
# Google → Graph body mappers (moved out of the workload jobs).
# --------------------------------------------------------------------------- #
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
            {"address": e["value"], "name": e.get("displayName", e["value"])} for e in emails
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
        # All-day events: Graph has no date-only shape — start/end must be
        # dateTimeTimeZone objects at midnight (with isAllDay=true), not Google's
        # bare {"date": ...}. Sending the latter fails with UnableToDeserializePostBody.
        body["start"] = {"dateTime": f"{start['date']}T00:00:00", "timeZone": "UTC"}
        body["end"] = {"dateTime": f"{end['date']}T00:00:00", "timeZone": "UTC"}
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

    for rule in event.get("recurrence", []):
        if rule.startswith("RRULE:"):
            start_dt = start.get("dateTime") or start.get("date", "")
            graph_rec = None
            try:
                graph_rec = rrule_to_graph_recurrence(rule, start_dt)
            except Exception as exc:  # noqa: BLE001 - one bad rule must not kill the run
                log.warning("Recurrence rule %r failed to convert: %s", rule, exc)
            if graph_rec:
                body["recurrence"] = graph_rec
            else:
                log.warning(
                    "Recurrence %r not expressible in Graph — event %r migrates "
                    "as a single occurrence", rule, event.get("summary", ""),
                )
            break

    return body
