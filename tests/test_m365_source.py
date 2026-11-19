"""M365 source connector against a fake source-tenant Graph client."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from migrator.config import Microsoft365SourceConfig, UserMapping
from migrator.connectors.base import SourceFile
from migrator.connectors.m365 import M365Source, _drive_key
from migrator.microsoft.graph_client import GraphClient

_USER = UserMapping(source_id="u@src", dest_id="u@dst")


def _status_error(code: int, text: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://graph.microsoft.com/v1.0/x")
    resp = httpx.Response(code, text=text, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


class _GC:
    # Well-known folders this mailbox has, by the name v1.0 addresses them with.
    # The display names in the listing below are German on purpose: the system
    # folders must be recognised by id, never by what the mailbox calls them.
    well_known = {"inbox": "f1", "sentitems": "f2"}

    def __init__(self) -> None:
        self.byte_calls: list[tuple[str, dict[str, Any]]] = []
        self.pages: list[str] = []
        self.gets: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.gets.append((path, kwargs))
        prefix = "/users/UID/mailFolders/"
        if path.startswith(prefix):
            name = path[len(prefix):]
            if name not in self.well_known:
                raise _status_error(404, "ErrorItemNotFound")
            return {"id": self.well_known[name]}
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
            # v1.0 mailFolder: no wellKnownName property is ever returned.
            "/users/UID/mailFolders": [
                {"id": "f1", "displayName": "Posteingang"},
                {"id": "f2", "displayName": "Gesendete Elemente"},
                {"id": "f3", "displayName": "Projekte"},
            ],
            "/users/UID/mailFolders/f1/childFolders": [{"id": "f4", "displayName": "Kunden"}],
            "/users/UID/mailFolders/f2/childFolders": [],
            "/users/UID/mailFolders/f3/childFolders": [],
            "/users/UID/mailFolders/f4/childFolders": [],
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


def test_iter_messages_lists_without_downloading_and_loads_on_demand() -> None:
    gc = _GC()
    msgs = list(_source(gc).iter_messages(_USER, None))
    assert [m.source_id for m in msgs] == ["m1", "m404", "m500"]
    assert msgs[0].folder_paths == ["Inbox"]
    # Listing downloads nothing: the job reads a body only for a message it is
    # about to import, so a re-run over a migrated mailbox costs no MIME fetches.
    assert gc.byte_calls == [] and all(m.raw_mime == b"" for m in msgs)

    loaders = {m.source_id: m.load_mime for m in msgs}
    assert all(callable(fn) for fn in loaders.values())
    assert loaders["m1"]() == b"raw"  # type: ignore[misc]
    assert loaders["m404"]() is None  # type: ignore[misc]  # deleted since the listing
    with pytest.raises(RuntimeError, match="^500: InternalServerError"):
        loaders["m500"]()  # type: ignore[misc]
    assert [p for p, _ in gc.byte_calls] == [
        f"/users/UID/messages/{mid}/$value" for mid in ("m1", "m404", "m500")
    ]
    # the expected-404 probe is quieted rather than logged as a Graph error
    assert all(kw.get("quiet_statuses") == (404,) for _p, kw in gc.byte_calls)


def test_mail_delta_filter_overlaps_the_cursor() -> None:
    # The cursor is this machine's clock at the start of the previous pass; the
    # filter reaches 10 minutes before it so clock skew cannot open a gap.
    gc = _GC()
    list(_source(gc).iter_messages(_USER, "2026-11-19T20:05:00Z"))
    page = next(p for p in gc.pages if p.startswith("/users/UID/messages"))
    assert "$filter=receivedDateTime ge 2026-11-19T19:55:00Z" in page


def test_system_folders_are_found_by_well_known_name_not_display_name() -> None:
    gc = _GC()
    paths = _source(gc)._folder_paths("UID")
    assert paths == {
        "f1": "Inbox",                # "Posteingang": the token, not the local name
        "f2": "SentItems",
        "f3": "Projekte",             # an ordinary folder keeps its display name
        "f4": "Inbox\\Kunden",        # children hang off the token
    }
    # Each well-known folder is addressed by name; one this mailbox lacks is a
    # quiet 404, not an error.
    lookups = [(p, kw) for p, kw in gc.gets if "/mailFolders/" in p]
    assert [p.rsplit("/", 1)[1] for p, _ in lookups] == [
        "inbox", "sentitems", "drafts", "deleteditems", "junkemail", "archive",
    ]
    assert all(
        kw["params"] == {"$select": "id"} and kw["quiet_statuses"] == (404,)
        for _, kw in lookups
    )
    # wellKnownName is beta-only: selecting it on v1.0 is a 400 for the whole mailbox.
    assert all("wellKnownName" not in page for page in gc.pages)


def test_folder_lookup_failure_other_than_404_is_not_swallowed() -> None:
    class _Denied(_GC):
        def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
            if "/mailFolders/" in path:
                raise _status_error(403, "ErrorAccessDenied")
            return super().get(path, **kwargs)

    with pytest.raises(httpx.HTTPStatusError) as err:
        _source(_Denied())._folder_paths("UID")
    assert err.value.response.status_code == 403


def test_mail_folder_requests_are_valid_against_v1_mailfolder() -> None:
    """Real GraphClient against a transport that answers like Graph v1.0: an
    unknown $select property is a 400 (which is how the beta-only wellKnownName
    took down the first live run), and folders are addressable by well-known name."""
    v1_properties = {
        "id", "displayName", "parentFolderId", "childFolderCount",
        "totalItemCount", "unreadItemCount", "isHidden",
    }
    by_name = {"inbox": "f1", "deleteditems": "f9"}
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        unknown = set(req.url.params.get("$select", "id").split(",")) - v1_properties
        if unknown:
            return httpx.Response(400, json={"error": {
                "code": "BadRequest",
                "message": f"Could not find a property named '{unknown.pop()}' on type "
                           "'microsoft.graph.mailFolder'.",
            }})
        path = req.url.path.removeprefix("/v1.0/users/UID/mailFolders")
        if path == "":
            return httpx.Response(200, json={"value": [
                {"id": "f1", "displayName": "Boîte de réception"},
                {"id": "f9", "displayName": "Éléments supprimés"},
            ]})
        if path.endswith("/childFolders"):
            return httpx.Response(200, json={"value": []})
        name = path.lstrip("/")
        if name in by_name:
            return httpx.Response(200, json={"id": by_name[name]})
        return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})

    class _Tokens:
        def get_token(self) -> str:
            return "tok"

    gc = GraphClient(_Tokens())  # type: ignore[arg-type]
    gc._client.close()
    gc._client = httpx.Client(transport=httpx.MockTransport(handler))
    src = M365Source(Microsoft365SourceConfig(tenant_id="t", client_id="c"), lambda: gc)

    assert src._folder_paths("UID") == {"f1": "Inbox", "f9": "DeletedItems"}
    assert all(r.method == "GET" for r in seen)  # a source is only ever read


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
    # ...from 10 minutes before the cursor (see test_mail_delta_filter_overlaps_the_cursor)
    assert all("$filter=lastModifiedDateTime ge 2025-12-31T23:50:00Z" in p for p in contact_pages)


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
