from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from ..config import Microsoft365SourceConfig, UserMapping
from ..microsoft.graph_client import GraphClient
from .base import (
    BaseSource,
    CalendarRef,
    SourceContact,
    SourceEvent,
    SourceFile,
    SourceMessage,
)

# Fields safe to copy when re-creating a contact/event in the destination tenant
# (Graph rejects server-owned fields like id/changeKey/@odata.* on create).
_CONTACT_FIELDS = (
    "givenName", "surname", "displayName", "middleName", "nickName", "title",
    "emailAddresses", "businessPhones", "homePhones", "mobilePhone",
    "companyName", "jobTitle", "department", "officeLocation",
    "businessAddress", "homeAddress", "otherAddress", "personalNotes",
)
_EVENT_FIELDS = (
    "subject", "body", "start", "end", "location", "locations", "attendees",
    "recurrence", "isAllDay", "isReminderOn", "reminderMinutesBeforeStart",
    "importance", "sensitivity", "showAs", "responseRequested",
)


_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Graph wellKnownName → canonical token shared with the destination writer
# (microsoft/mail.py:WELL_KNOWN_FOLDER_IDS) and the other sources, so a system
# folder maps to the real well-known destination folder rather than a duplicate.
_WELLKNOWN_TO_TOKEN = {
    "inbox": "Inbox",
    "sentitems": "SentItems",
    "drafts": "Drafts",
    "deleteditems": "DeletedItems",
    "junkemail": "JunkEmail",
    "archive": "Archive",
}


def _strip_base(url: str) -> str:
    """Turn an absolute Graph URL (next/deltaLink) into a path GraphClient accepts."""
    return url[len(_GRAPH_BASE):] if url.startswith(_GRAPH_BASE) else url


def _now_z() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _whitelist(src: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: src[k] for k in keys if k in src and src[k] is not None}


class M365Source(BaseSource):
    """Microsoft 365 (tenant-to-tenant) source. Reads from the source tenant via
    its own Graph client; the destination keeps its separate client."""

    capabilities = {"mail", "files", "contacts", "calendar"}

    def __init__(
        self, cfg: Microsoft365SourceConfig, gc_factory: Callable[[], GraphClient]
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self._gc_factory = gc_factory
        self._gc: GraphClient | None = None
        self._user_ids: dict[str, str] = {}

    def _client(self) -> GraphClient:
        if self._gc is None:
            self._gc = self._gc_factory()
        return self._gc

    def close(self) -> None:
        if self._gc is not None:
            self._gc.close()
            self._gc = None

    def _uid(self, user: UserMapping) -> str:
        if user.source_id not in self._user_ids:
            info = self._client().get(f"/users/{user.source_id}", params={"$select": "id"})
            self._user_ids[user.source_id] = info["id"]
        return self._user_ids[user.source_id]

    def _paginate(self, path: str, user_key: str | None = None) -> Iterator[dict[str, Any]]:
        for page in self._client().paginate(path, user_key=user_key):
            yield from page

    # -- mail --------------------------------------------------------------- #
    def _folder_paths(self, uid: str) -> dict[str, str]:
        """Map mailFolder id → destination folder path ("\\"-separated)."""
        paths: dict[str, str] = {}

        def walk(parent_path: str, container: str) -> None:
            for f in self._paginate(
                f"/users/{uid}/{container}?$top=100&$select=id,displayName,wellKnownName",
                user_key=uid,
            ):
                # Top-level system folders map to a canonical token the destination
                # writer routes to the real well-known folder; everything else keeps
                # its display name.
                token = _WELLKNOWN_TO_TOKEN.get(f.get("wellKnownName") or "")
                name = token if (token and not parent_path) else f.get("displayName", f["id"])
                full = f"{parent_path}\\{name}" if parent_path else name
                paths[f["id"]] = full
                walk(full, f"mailFolders/{f['id']}/childFolders")

        walk("", "mailFolders")
        return paths

    def iter_messages(self, user: UserMapping, since: str | None) -> Iterator[SourceMessage]:
        uid = self._uid(user)
        self._set_cursor("mail", _now_z())
        folder_paths = self._folder_paths(uid)
        select = "id,parentFolderId,isRead,flag,categories,subject"
        path = f"/users/{uid}/messages?$select={select}&$top=50"
        if since:
            path += f"&$filter=receivedDateTime ge {since}"
        for msg in self._paginate(path, user_key=uid):
            raw = self._client().get_bytes(
                f"/users/{uid}/messages/{msg['id']}/$value", user_key=uid
            )
            folder = folder_paths.get(msg.get("parentFolderId", ""), "Inbox")
            yield SourceMessage(
                source_id=msg["id"],
                raw_mime=raw,
                folder_paths=[folder],
                categories=msg.get("categories", []),
                is_read=msg.get("isRead", True),
                is_flagged=(msg.get("flag", {}) or {}).get("flagStatus") == "flagged",
                dedup_hash=msg["id"],
                subject=msg.get("subject", ""),
            )

    def inventory_messages(self, user: UserMapping) -> Iterator[SourceMessage]:
        uid = self._uid(user)
        folder_paths = self._folder_paths(uid)
        select = "id,parentFolderId,isRead,subject,from,receivedDateTime"
        for msg in self._paginate(
            f"/users/{uid}/messages?$select={select}&$top=50", user_key=uid
        ):
            sender = ((msg.get("from", {}) or {}).get("emailAddress", {}) or {}).get("address", "")
            yield SourceMessage(
                source_id=msg["id"],
                raw_mime=b"",
                folder_paths=[folder_paths.get(msg.get("parentFolderId", ""), "Inbox")],
                is_read=msg.get("isRead", True),
                subject=msg.get("subject", "(no subject)"),
                date=msg.get("receivedDateTime", ""),
                sender=sender,
            )

    # -- files (OneDrive + SharePoint, drive-generic) ----------------------- #
    def _iter_drive(
        self, drive_root: str, user_key: str, cursor_key: str, since: str | None
    ) -> Iterator[SourceFile]:
        """Walk a drive's delta feed. `drive_root` is the Graph prefix to the drive
        (e.g. "users/<id>/drive" or "drives/<id>"); it is stamped on each SourceFile
        so fetch_file knows where to read content from."""
        gc = self._client()
        url: str | None = _strip_base(since) if since else f"/{drive_root}/root/delta"
        delta_link: str | None = None
        while url:
            data = gc.get(url, user_key=user_key)
            for item in data.get("value", []):
                if "root" in item:
                    continue  # skip the drive root pseudo-item
                yield self._to_file(item, drive_root)
            next_link = data.get("@odata.nextLink")
            delta_link = data.get("@odata.deltaLink") or delta_link
            url = _strip_base(next_link) if next_link else None
        if delta_link:
            self._set_cursor(cursor_key, delta_link)

    def iter_files(self, user: UserMapping, since: str | None) -> Iterator[SourceFile]:
        uid = self._uid(user)
        yield from self._iter_drive(f"users/{uid}/drive", uid, "files", since)

    def iter_site_files(self, drive_id: str, since: str | None) -> Iterator[SourceFile]:
        yield from self._iter_drive(f"drives/{drive_id}", drive_id, f"sharepoint:{drive_id}", since)

    def resolve_site_drive(self, site_ref: str) -> tuple[str, str]:
        site = self._client().get(f"/sites/{site_ref}", params={"$select": "id"})
        drive = self._client().get(f"/sites/{site['id']}/drive", params={"$select": "id"})
        return site["id"], drive["id"]

    def _to_file(self, item: dict[str, Any], drive_root: str) -> SourceFile:
        is_folder = "folder" in item
        parent = (item.get("parentReference", {}) or {}).get("id")
        file_info = item.get("file", {}) or {}
        content_hash = (file_info.get("hashes", {}) or {}).get("quickXorHash") or item.get("eTag")
        return SourceFile(
            source_id=item["id"],
            name=item.get("name", item["id"]),
            mime_type=file_info.get("mimeType", ""),
            parent_id=parent,
            is_folder=is_folder,
            size=item.get("size", ""),
            modified_time=item.get("lastModifiedDateTime", ""),
            content_hash=content_hash,
            action="create-folder" if is_folder else "migrate",
            drive_root=drive_root,
        )

    def fetch_file(self, user: UserMapping, f: SourceFile) -> tuple[bytes, str]:
        drive_root = f.drive_root or f"users/{self._uid(user)}/drive"
        content = self._client().get_bytes(
            f"/{drive_root}/items/{f.source_id}/content", user_key=drive_root
        )
        return content, f.name

    def inventory_files(self, user: UserMapping) -> Iterator[SourceFile]:
        yield from self.iter_files(user, None)

    # -- contacts ----------------------------------------------------------- #
    def iter_contacts(self, user: UserMapping, since: str | None) -> Iterator[SourceContact]:
        uid = self._uid(user)
        self._set_cursor("contacts", _now_z())
        path = f"/users/{uid}/contacts?$top=100"
        if since:
            path += f"&$filter=lastModifiedDateTime ge {since}"
        for c in self._paginate(path, user_key=uid):
            yield self._to_contact(c)

    def inventory_contacts(self, user: UserMapping) -> Iterator[SourceContact]:
        uid = self._uid(user)
        for c in self._paginate(f"/users/{uid}/contacts?$top=100", user_key=uid):
            yield self._to_contact(c)

    def _to_contact(self, c: dict[str, Any]) -> SourceContact:
        emails = c.get("emailAddresses", []) or []
        return SourceContact(
            source_id=c["id"],
            graph_body=_whitelist(c, _CONTACT_FIELDS),
            display_name=c.get("displayName", ""),
            primary_email=emails[0].get("address", "") if emails else "",
        )

    # -- calendar ----------------------------------------------------------- #
    def list_calendars(self, user: UserMapping) -> list[CalendarRef]:
        uid = self._uid(user)
        return [
            CalendarRef(cal_id=c["id"], name=c.get("name", c["id"]))
            for c in self._paginate(f"/users/{uid}/calendars?$top=100", user_key=uid)
        ]

    def iter_events(
        self, user: UserMapping, cal: CalendarRef, since: str | None
    ) -> Iterator[SourceEvent]:
        uid = self._uid(user)
        self._set_cursor(f"calendar:{cal.cal_id}", _now_z())
        path = f"/users/{uid}/calendars/{cal.cal_id}/events?$top=50"
        if since:
            path += f"&$filter=lastModifiedDateTime ge {since}"
        for e in self._paginate(path, user_key=uid):
            cancelled = e.get("isCancelled", False)
            yield SourceEvent(
                source_id=e["id"],
                graph_body={} if cancelled else _whitelist(e, _EVENT_FIELDS),
                is_cancelled=cancelled,
                subject=e.get("subject", "(No title)"),
                modified_time=e.get("lastModifiedDateTime", ""),
                notes="recurring" if e.get("recurrence") else "",
            )
