"""End-to-end contacts_job runs against a fake source + fake Graph client.

A delta pass must apply *modifications* to already-migrated contacts (PATCH the
recorded dest_id) and honour People-API tombstones, not just create new ones.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import select

from migrator.config import Config, UserMapping
from migrator.connectors.base import BaseSource, SourceContact
from migrator.context import JobContext
from migrator.state.db import get_cursor, init_db, save_cursor, session_scope
from migrator.state.models import ItemMap
from migrator.workloads.contacts_job import run_contacts

_USER = UserMapping(source_id="a@old.com", dest_id="a@new.com")
_CFG = Config.model_validate({
    "source": {"type": "imap", "host": "h"},
    "destination": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
    "users": [{"source_id": "a@old.com", "dest_id": "a@new.com"}],
})


class _Src(BaseSource):
    capabilities = {"contacts"}

    def __init__(self, contacts: list[SourceContact]) -> None:
        super().__init__()
        self.contacts = contacts

    def iter_contacts(self, user: UserMapping, since: str | None) -> Iterator[SourceContact]:
        self._set_cursor("contacts", "tok-2")
        yield from self.contacts


class _GC:
    def __init__(self, fail_patch: bool = False) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[str] = []
        self.fail_patch = fail_patch

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        return {"id": "ms-uid"}

    def paginate(self, path: str, **kw: Any) -> Iterator[list[dict[str, Any]]]:
        yield [{"id": "folder-1", "displayName": "Imported Contacts"}]

    def post(self, path: str, **kw: Any) -> dict[str, Any]:
        self.posts.append((path, kw["json"]))
        return {"id": f"dest-{len(self.posts)}"}

    def patch(self, path: str, **kw: Any) -> None:
        if self.fail_patch:
            raise RuntimeError("patch boom")
        self.patches.append((path, kw["json"]))

    def delete(self, path: str, **kw: Any) -> None:
        self.deletes.append(path)


def _contact(cid: str, name: str, h: str, deleted: bool = False) -> SourceContact:
    return SourceContact(
        source_id=cid, graph_body={"displayName": name}, source_hash=h, is_deleted=deleted
    )


def _run(contacts: list[SourceContact], gc: _GC, mode: str = "full") -> None:
    run_contacts(JobContext(_USER, _Src(contacts), gc, mode, _CFG))  # type: ignore[arg-type]


def _seed_delta_cursor() -> None:
    with session_scope() as s:
        save_cursor(s, "a@old.com", "contacts", "tok-1")


def _row(cid: str) -> tuple[str, str | None, str | None]:
    with session_scope() as s:
        return s.execute(
            select(ItemMap.status, ItemMap.dest_id, ItemMap.source_hash)
            .where(ItemMap.source_id == cid)
        ).one()


def test_full_pass_creates_and_records_hash(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    gc = _GC()
    _run([_contact("c1", "Ann", "h1")], gc)
    assert len(gc.posts) == 1 and gc.posts[0][0].endswith("/contactFolders/folder-1/contacts")
    assert _row("c1") == ("done", "dest-1", "h1")


def test_full_rerun_skips_done_even_if_hash_changed(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_contact("c1", "Ann", "h1")], _GC())
    gc = _GC()
    _run([_contact("c1", "Ann v2", "h2")], gc)  # full pass = idempotent skip only
    assert gc.posts == [] and gc.patches == []


def test_delta_changed_contact_is_patched_not_recreated(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_contact("c1", "Ann", "h1")], _GC())
    _seed_delta_cursor()
    gc = _GC()
    _run([_contact("c1", "Ann v2", "h2")], gc, mode="delta")
    assert gc.posts == []
    assert gc.patches == [("/users/ms-uid/contacts/dest-1", {"displayName": "Ann v2"})]
    assert _row("c1") == ("done", "dest-1", "h2")
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "contacts") == "tok-2"


def test_delta_unchanged_contact_makes_no_graph_call(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_contact("c1", "Ann", "h1")], _GC())
    _seed_delta_cursor()
    gc = _GC()
    _run([_contact("c1", "Ann", "h1")], gc, mode="delta")
    assert gc.posts == [] and gc.patches == []


def test_delta_new_contact_is_created(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _seed_delta_cursor()
    gc = _GC()
    _run([_contact("c9", "New", "h9")], gc, mode="delta")
    assert len(gc.posts) == 1 and gc.patches == []


def test_delta_tombstone_deletes_migrated_copy(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_contact("c1", "Ann", "h1")], _GC())
    _seed_delta_cursor()
    gc = _GC()
    _run([_contact("c1", "", "h2", deleted=True)], gc, mode="delta")
    assert gc.deletes == ["/users/ms-uid/contacts/dest-1"]
    assert gc.posts == []  # an empty tombstone body is never created as a blank contact
    assert _row("c1")[0] == "skipped"


def test_delta_tombstone_for_never_migrated_contact_is_recorded(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _seed_delta_cursor()
    gc = _GC()
    _run([_contact("c7", "", "h", deleted=True)], gc, mode="delta")
    assert gc.deletes == [] and gc.posts == []
    assert _row("c7")[0] == "skipped"


def test_failed_update_keeps_dest_id_and_retries_as_patch(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_contact("c1", "Ann", "h1")], _GC())
    _seed_delta_cursor()
    _run([_contact("c1", "Ann v2", "h2")], _GC(fail_patch=True), mode="delta")
    assert _row("c1") == ("failed", "dest-1", "h1")  # still points at the destination copy
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "contacts") == "tok-1"  # held back

    gc = _GC()
    _run([_contact("c1", "Ann v2", "h2")], gc, mode="delta")
    assert gc.posts == []  # retried as a PATCH — no duplicate contact
    assert len(gc.patches) == 1
    assert _row("c1") == ("done", "dest-1", "h2")
