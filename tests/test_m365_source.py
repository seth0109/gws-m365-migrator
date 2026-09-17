"""M365 source connector against a fake source-tenant Graph client."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from migrator.config import Microsoft365SourceConfig, UserMapping
from migrator.connectors.base import SourceFile
from migrator.connectors.m365 import M365Source, _drive_key

_USER = UserMapping(source_id="u@src", dest_id="u@dst")


def _status_error(code: int, text: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://graph.microsoft.com/v1.0/x")
    resp = httpx.Response(code, text=text, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


class _GC:
    def __init__(self) -> None:
        self.byte_calls: list[tuple[str, dict[str, Any]]] = []
        self.pages: list[str] = []

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return {"id": "UID"}

    def get_bytes(self, path: str, **kwargs: Any) -> bytes:
        self.byte_calls.append((path, kwargs))
        if "/m404/" in path:
            raise _status_error(404)
        if "/m500/" in path:
            raise _status_error(500, "InternalServerError")
        return b"raw"

    def paginate(self, path: str, **kwargs: Any) -> Iterator[list[dict[str, Any]]]:
        self.pages.append(path)
        base = path.split("?")[0]
        routes: dict[str, list[dict[str, Any]]] = {
            # mail folders
            "/users/UID/mailFolders": [
                {"id": "f1", "displayName": "Inbox", "wellKnownName": "inbox"}
            ],
            "/users/UID/mailFolders/f1/childFolders": [],
            "/users/UID/messages": [
                {"id": "m1", "parentFolderId": "f1", "isRead": True},
                {"id": "m404", "parentFolderId": "f1"},
                {"id": "m500", "parentFolderId": "f1"},
            ],
            # contact folders: default + Clients > VIP
            "/users/UID/contactFolders": [{"id": "cf1", "displayName": "Clients"}],
            "/users/UID/contactFolders/cf1/childFolders": [{"id": "cf2", "displayName": "VIP"}],
            "/users/UID/contactFolders/cf2/childFolders": [],
            "/users/UID/contacts": [{"id": "c1", "displayName": "Ann", "changeKey": "k1"}],
            "/users/UID/contactFolders/cf1/contacts": [{"id": "c2", "displayName": "Bob"}],
            "/users/UID/contactFolders/cf2/contacts": [{"id": "c3", "displayName": "Cy"}],
        }
        yield routes[base]

    def close(self) -> None:
        pass


def _source(gc: _GC) -> M365Source:
    return M365Source(Microsoft365SourceConfig(tenant_id="t", client_id="c"), lambda: gc)  # type: ignore[arg-type, return-value]


# ── mail ──────────────────────────────────────────────────────────────────────


def test_iter_messages_skips_vanished_and_stubs_failed_fetches() -> None:
    gc = _GC()
    msgs = list(_source(gc).iter_messages(_USER, None))
    assert [m.source_id for m in msgs] == ["m1", "m500"]  # 404 skipped, not fatal
    assert msgs[0].raw_mime == b"raw" and msgs[0].folder_paths == ["Inbox"]
    assert msgs[1].raw_mime == b"" and msgs[1].fetch_error.startswith("500")
    # the expected-404 probe is quieted rather than logged as a Graph error
    assert all(kw.get("quiet_statuses") == (404,) for _p, kw in gc.byte_calls)


# ── contacts ──────────────────────────────────────────────────────────────────


def test_iter_contacts_covers_default_and_sub_folders() -> None:
    gc = _GC()
    contacts = list(_source(gc).iter_contacts(_USER, None))
    assert [(c.source_id, c.folder_name) for c in contacts] == [
        ("c1", "Imported Contacts"), ("c2", "Clients"), ("c3", "VIP"),
    ]
    assert contacts[0].source_hash == "k1"
    # photo refs are folder-qualified so sub-folder contacts resolve
    assert contacts[1].photo_ref == "/users/UID/contactFolders/cf1/contacts/c2"


def test_iter_contacts_delta_filters_every_collection() -> None:
    gc = _GC()
    list(_source(gc).iter_contacts(_USER, "2026-01-01T00:00:00Z"))
    contact_pages = [p for p in gc.pages if p.split("?")[0].endswith("/contacts")]
    assert len(contact_pages) == 3
    assert all("$filter=lastModifiedDateTime ge 2026-01-01T00:00:00Z" in p for p in contact_pages)


def test_fetch_contact_photo_uses_folder_qualified_path() -> None:
    gc = _GC()
    src = _source(gc)
    contact = next(c for c in src.iter_contacts(_USER, None) if c.source_id == "c2")
    assert src.fetch_contact_photo(_USER, contact) == b"raw"
    assert gc.byte_calls[-1][0] == "/users/UID/contactFolders/cf1/contacts/c2/photo/$value"


# ── files ─────────────────────────────────────────────────────────────────────


def test_drive_key_matches_iter_drive_key() -> None:
    assert _drive_key("users/UID/drive") == "UID"
    assert _drive_key("drives/D1") == "D1"


def test_fetch_file_rate_limits_on_drive_id() -> None:
    gc = _GC()
    f = SourceFile("i1", "a.txt", "text/plain", None, False, drive_root="drives/D1")
    _source(gc).fetch_file(_USER, f)
    path, kw = gc.byte_calls[-1]
    assert path == "/drives/D1/items/i1/content"
    assert kw["user_key"] == "D1"
