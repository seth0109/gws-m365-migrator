"""Tests for recurring-event exception reconciliation.

Modified/cancelled single occurrences of a recurring series (Google:
recurringEventId + originalStartTime) must be applied onto the migrated
destination series — located via /events/{master}/instances — instead of being
created as standalone duplicate events.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from migrator.microsoft.calendar import _instance_matches, _to_utc, find_instance
from migrator.state.db import init_db, session_scope, upsert_item
from migrator.transform.identities import IdentityMap
from migrator.workloads.calendar_job import _apply_exception

# ── instance matching ─────────────────────────────────────────────────────────


def test_to_utc_handles_graph_and_source_formats() -> None:
    # Graph instance start: naive UTC with 7 fractional digits
    graph = _to_utc("2026-03-10T16:00:00.0000000")
    # Graph originalStart: Z-suffixed
    zulu = _to_utc("2026-03-10T16:00:00Z")
    # Google originalStartTime: offset form
    offset = _to_utc("2026-03-10T09:00:00-07:00")
    assert graph == zulu == offset


def test_instance_matches_on_original_start_or_start() -> None:
    target = "2026-03-10T09:00:00-07:00"
    # Unmodified occurrence: start == original occurrence time
    assert _instance_matches({"start": {"dateTime": "2026-03-10T16:00:00.0000000"}}, target)
    # Previously-patched occurrence: start moved, originalStart still matches
    assert _instance_matches(
        {"originalStart": "2026-03-10T16:00:00Z",
         "start": {"dateTime": "2026-03-11T10:00:00.0000000"}},
        target,
    )
    assert not _instance_matches(
        {"start": {"dateTime": "2026-03-11T16:00:00.0000000"}}, target
    )


def test_instance_matches_all_day_by_date() -> None:
    assert _instance_matches({"start": {"dateTime": "2026-03-10T00:00:00.0000000"}}, "2026-03-10")
    assert not _instance_matches(
        {"start": {"dateTime": "2026-03-11T00:00:00.0000000"}}, "2026-03-10"
    )


class _InstancesGC:
    def __init__(self, instances: list[dict[str, Any]]) -> None:
        self.instances = instances
        self.params: dict[str, Any] | None = None
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []

    def get(self, path: str, user_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self.params = kwargs.get("params")
        return {"value": self.instances}

    def patch(self, path: str, user_key: str | None = None, **kwargs: Any) -> None:
        self.patched.append((path, kwargs["json"]))

    def delete(self, path: str, user_key: str | None = None) -> None:
        self.deleted.append(path)


def test_find_instance_queries_window_and_matches() -> None:
    gc = _InstancesGC([
        {"id": "occ-1", "start": {"dateTime": "2026-03-10T16:00:00.0000000"}},
        {"id": "occ-2", "start": {"dateTime": "2026-03-11T16:00:00.0000000"}},
    ])
    got = find_instance(gc, "u", "master-1", "2026-03-10T09:00:00-07:00")  # type: ignore[arg-type]
    assert got == "occ-1"
    assert gc.params is not None
    assert gc.params["startDateTime"] < "2026-03-10T16:00:00Z" < gc.params["endDateTime"]


# ── job-level application (state + fake Graph) ───────────────────────────────


def _seed_master(tmp_path: Path, dest_id: str = "master-dest") -> None:
    init_db(tmp_path / "state.db")
    with session_scope() as s:
        upsert_item(
            s, "a@old.com", "calendar:cal1", "master-src", dest_id=dest_id, status="done"
        )


def _exception_event(cancelled: bool = False) -> Any:
    from migrator.connectors.base import SourceEvent

    return SourceEvent(
        source_id="master-src_20260310T160000Z",
        graph_body={} if cancelled else {"subject": "Moved meeting"},
        is_cancelled=cancelled,
        master_source_id="master-src",
        original_start="2026-03-10T09:00:00-07:00",
    )


def test_apply_exception_patches_matching_instance(tmp_path: Path) -> None:
    _seed_master(tmp_path)
    gc = _InstancesGC([{"id": "occ-1", "start": {"dateTime": "2026-03-10T16:00:00.0000000"}}])
    ok = _apply_exception(
        gc, "ms-uid", "a@old.com", "calendar:cal1", _exception_event(), IdentityMap([])  # type: ignore[arg-type]
    )
    assert ok
    path, body = gc.patched[0]
    assert path.endswith("/events/occ-1")
    assert body == {"subject": "Moved meeting"}


def test_apply_exception_deletes_cancelled_occurrence(tmp_path: Path) -> None:
    _seed_master(tmp_path)
    gc = _InstancesGC([{"id": "occ-1", "start": {"dateTime": "2026-03-10T16:00:00.0000000"}}])
    ok = _apply_exception(
        gc, "ms-uid", "a@old.com", "calendar:cal1", _exception_event(cancelled=True),  # type: ignore[arg-type]
        IdentityMap([]),
    )
    assert ok
    assert gc.deleted == ["/users/ms-uid/events/occ-1"]


def test_apply_exception_cancelled_missing_occurrence_is_success(tmp_path: Path) -> None:
    # Already deleted (e.g. prior run crashed after the DELETE): not a failure.
    _seed_master(tmp_path)
    gc = _InstancesGC([])
    ok = _apply_exception(
        gc, "ms-uid", "a@old.com", "calendar:cal1", _exception_event(cancelled=True),  # type: ignore[arg-type]
        IdentityMap([]),
    )
    assert ok
    assert gc.deleted == []


def test_apply_exception_fails_without_migrated_master(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")  # no master row seeded
    gc = _InstancesGC([])
    ok = _apply_exception(
        gc, "ms-uid", "a@old.com", "calendar:cal1", _exception_event(), IdentityMap([])  # type: ignore[arg-type]
    )
    assert not ok  # counted as failure -> cursor held back
