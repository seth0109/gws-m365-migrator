"""End-to-end calendar_job main loop against a fake source + fake Graph client.

Delta passes must PATCH already-migrated events that changed at the source and
delete ones that were cancelled, instead of skipping every `done` item.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import select

from migrator.config import Config, UserMapping
from migrator.connectors.base import BaseSource, CalendarRef, SourceEvent
from migrator.context import JobContext
from migrator.state.db import get_cursor, init_db, save_cursor, session_scope
from migrator.state.models import ItemMap
from migrator.workloads.calendar_job import run_calendar

_USER = UserMapping(source_id="a@old.com", dest_id="a@new.com")
_CFG = Config.model_validate({
    "source": {"type": "imap", "host": "h"},
    "destination": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
    "users": [{"source_id": "a@old.com", "dest_id": "a@new.com"}],
})
_KEY = "calendar:cal-1"


class _Src(BaseSource):
    capabilities = {"calendar"}

    def __init__(self, events: list[SourceEvent]) -> None:
        super().__init__()
        self.events = events

    def list_calendars(self, user: UserMapping) -> list[CalendarRef]:
        return [CalendarRef(cal_id="cal-1", name="Work")]

    def iter_events(
        self, user: UserMapping, cal: CalendarRef, since: str | None
    ) -> Iterator[SourceEvent]:
        self._set_cursor(_KEY, "sync-2")
        yield from self.events


class _GC:
    def __init__(self, fail_patch: bool = False) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.deletes: list[str] = []
        self.fail_patch = fail_patch

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        if path.endswith("/calendars"):
            return {"value": [{"id": "mscal", "name": "Work"}]}
        return {"id": "ms-uid"}

    def post(self, path: str, **kw: Any) -> dict[str, Any]:
        self.posts.append((path, kw["json"]))
        return {"id": f"dest-{len(self.posts)}"}

    def patch(self, path: str, **kw: Any) -> None:
        if self.fail_patch:
            raise RuntimeError("patch boom")
        self.patches.append((path, kw["json"]))

    def delete(self, path: str, **kw: Any) -> None:
        self.deletes.append(path)


def _event(eid: str, subject: str, h: str, cancelled: bool = False) -> SourceEvent:
    return SourceEvent(
        source_id=eid, graph_body={} if cancelled else {"subject": subject},
        is_cancelled=cancelled, source_hash=h,
    )


def _run(events: list[SourceEvent], gc: _GC, mode: str = "full") -> None:
    run_calendar(JobContext(_USER, _Src(events), gc, mode, _CFG))  # type: ignore[arg-type]


def _seed_delta_cursor() -> None:
    with session_scope() as s:
        save_cursor(s, "a@old.com", _KEY, "sync-1")


def _row(eid: str) -> tuple[str, str | None, str | None]:
    with session_scope() as s:
        return s.execute(
            select(ItemMap.status, ItemMap.dest_id, ItemMap.source_hash)
            .where(ItemMap.source_id == eid)
        ).one()


def test_full_pass_creates_in_calendar_and_records_hash(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    gc = _GC()
    _run([_event("e1", "Standup", "h1")], gc)
    assert gc.posts == [("/users/ms-uid/calendars/mscal/events", {"subject": "Standup"})]
    assert _row("e1") == ("done", "dest-1", "h1")
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", _KEY) == "sync-2"


def test_full_rerun_skips_done(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_event("e1", "Standup", "h1")], _GC())
    gc = _GC()
    _run([_event("e1", "Standup moved", "h2")], gc)
    assert gc.posts == [] and gc.patches == []


def test_delta_changed_event_is_patched(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_event("e1", "Standup", "h1")], _GC())
    _seed_delta_cursor()
    gc = _GC()
    _run([_event("e1", "Standup moved", "h2")], gc, mode="delta")
    assert gc.posts == []
    assert gc.patches == [("/users/ms-uid/events/dest-1", {"subject": "Standup moved"})]
    assert _row("e1") == ("done", "dest-1", "h2")


def test_delta_unchanged_event_makes_no_graph_call(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_event("e1", "Standup", "h1")], _GC())
    _seed_delta_cursor()
    gc = _GC()
    _run([_event("e1", "Standup", "h1")], gc, mode="delta")
    assert gc.posts == [] and gc.patches == []


def test_cancelled_done_event_is_deleted_in_any_mode(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_event("e1", "Standup", "h1")], _GC())
    gc = _GC()
    _run([_event("e1", "", "h2", cancelled=True)], gc)  # full re-run, showDeleted=True
    assert gc.deletes == ["/users/ms-uid/events/dest-1"]
    assert _row("e1")[0] == "skipped"


def test_failed_update_keeps_dest_id_then_patches_on_retry(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_event("e1", "Standup", "h1")], _GC())
    _seed_delta_cursor()
    _run([_event("e1", "Moved", "h2")], _GC(fail_patch=True), mode="delta")
    assert _row("e1") == ("failed", "dest-1", "h1")
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", _KEY) == "sync-1"  # held back

    gc = _GC()
    _run([_event("e1", "Moved", "h2")], gc, mode="delta")
    assert gc.posts == [] and len(gc.patches) == 1  # no duplicate event
    assert _row("e1") == ("done", "dest-1", "h2")
