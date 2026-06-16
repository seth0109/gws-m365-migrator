from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..config import UserMapping

# Canonical workload names a source may advertise in `capabilities`.
WORKLOADS = frozenset({"mail", "files", "contacts", "calendar"})


# --------------------------------------------------------------------------- #
# Normalized, destination-ready items.
#
# A source connector is responsible for translating its native data model into
# these shapes (including the Microsoft Graph request bodies for contacts and
# events). Workload jobs are therefore source-agnostic: they iterate items,
# check idempotency, and hand bodies/MIME straight to the Graph writers.
# --------------------------------------------------------------------------- #
@dataclass
class SourceMessage:
    source_id: str
    raw_mime: bytes  # RFC822 bytes; empty in inventory mode
    folder_paths: list[str]  # destination folder placement(s), "\\"-separated paths
    categories: list[str] = field(default_factory=list)
    is_read: bool = True
    is_flagged: bool = False
    dedup_hash: str = ""  # Message-ID or equivalent, used as source_hash
    # Inventory/reporting metadata:
    subject: str = ""
    size_bytes: int | str = ""
    date: str = ""
    sender: str = ""


@dataclass
class SourceFile:
    source_id: str
    name: str
    mime_type: str
    parent_id: str | None
    is_folder: bool
    size: int | str = ""
    modified_time: str = ""
    content_hash: str | None = None
    # Resolved by the connector so jobs/inventory need no source-specific logic:
    action: str = "migrate"  # migrate | export | skip | create-folder
    export_ext: str | None = None  # appended to name when action == "export"
    notes: str = ""
    source_path: str = ""  # human-readable path, for inventory only
    drive_root: str = ""  # connector-internal: which source drive to fetch content from


@dataclass
class SourceContact:
    source_id: str
    graph_body: dict[str, Any]  # ready for microsoft.contacts.create_contact
    folder_name: str = "Imported Contacts"
    # Inventory metadata:
    display_name: str = ""
    primary_email: str = ""


@dataclass
class CalendarRef:
    cal_id: str
    name: str


@dataclass
class SourceEvent:
    source_id: str
    graph_body: dict[str, Any]  # ready for microsoft.calendar.create_event
    is_cancelled: bool = False
    # Inventory metadata:
    subject: str = ""
    modified_time: str = ""
    notes: str = ""


@dataclass
class SharedDriveRef:
    drive_id: str
    name: str


class BaseSource:
    """Base class for source connectors.

    Concrete sources advertise supported workloads via `capabilities` and
    implement only the corresponding methods. Cursor state captured during
    iteration is exposed via `get_last_cursor()` so jobs can persist it for the
    next delta pass. Methods left unimplemented raise NotImplementedError, which
    only happens if a job runs a workload the source did not advertise.
    """

    capabilities: set[str] = set()

    def __init__(self) -> None:
        self._cursors: dict[str, str] = {}

    # -- cursor plumbing ---------------------------------------------------- #
    def _set_cursor(self, key: str, value: str | None) -> None:
        if value:
            self._cursors[key] = value

    def get_last_cursor(self, key: str) -> str | None:
        """Cursor captured during the most recent iteration for `key`.

        `key` is the workload name (e.g. "mail") or a workload-scoped key such
        as "calendar:<cal_id>".
        """
        return self._cursors.get(key)

    def close(self) -> None:  # noqa: B027 - optional override
        """Release any connections/clients. No-op by default."""

    def __enter__(self) -> BaseSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- mail --------------------------------------------------------------- #
    def iter_messages(self, user: UserMapping, since: str | None) -> Iterator[SourceMessage]:
        raise NotImplementedError

    def inventory_messages(self, user: UserMapping) -> Iterator[SourceMessage]:
        raise NotImplementedError

    # -- files -------------------------------------------------------------- #
    def iter_files(self, user: UserMapping, since: str | None) -> Iterator[SourceFile]:
        raise NotImplementedError

    def fetch_file(self, user: UserMapping, f: SourceFile) -> tuple[bytes, str]:
        """Return (content_bytes, final_name). final_name may differ from f.name
        when a native document is exported (extension appended)."""
        raise NotImplementedError

    def inventory_files(self, user: UserMapping) -> Iterator[SourceFile]:
        raise NotImplementedError

    def list_shared_drives(self, user: UserMapping) -> list[SharedDriveRef]:
        raise NotImplementedError

    def iter_shared_drive_files(
        self, user: UserMapping, drive: SharedDriveRef
    ) -> Iterator[SourceFile]:
        raise NotImplementedError

    def resolve_site_drive(self, site_ref: str) -> tuple[str, str]:
        """Resolve a SharePoint site address to (site_id, default_library_drive_id)."""
        raise NotImplementedError

    def iter_site_files(self, drive_id: str, since: str | None) -> Iterator[SourceFile]:
        """Iterate files in a SharePoint document-library drive."""
        raise NotImplementedError

    # -- contacts ----------------------------------------------------------- #
    def iter_contacts(self, user: UserMapping, since: str | None) -> Iterator[SourceContact]:
        raise NotImplementedError

    def inventory_contacts(self, user: UserMapping) -> Iterator[SourceContact]:
        raise NotImplementedError

    # -- calendar ----------------------------------------------------------- #
    def list_calendars(self, user: UserMapping) -> list[CalendarRef]:
        raise NotImplementedError

    def iter_events(
        self, user: UserMapping, cal: CalendarRef, since: str | None
    ) -> Iterator[SourceEvent]:
        raise NotImplementedError
