"""State-store invariants the CLI exit code depends on.

`count_failed_items_since` is how a scripted cutover learns a run left failures
behind, so both the re-run path (ON CONFLICT DO UPDATE) and the timestamp
comparison it relies on must behave.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from migrator.state.db import (
    count_failed_items_since,
    get_item_state,
    init_db,
    save_cursor,
    session_scope,
    upsert_item,
)
from migrator.state.models import ItemMap, SyncCursor


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def test_refailing_an_existing_item_is_counted(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    with session_scope() as s:
        upsert_item(s, "u", "mail", "m1", status="failed", last_error="first")
    with session_scope() as s:
        first_stamp = s.execute(select(ItemMap.updated_at)).scalar_one()

    time.sleep(1.1)  # CURRENT_TIMESTAMP has one-second resolution
    rerun_started = _now()
    with session_scope() as s:
        upsert_item(s, "u", "mail", "m1", status="failed", last_error="again")

    with session_scope() as s:
        # ON CONFLICT DO UPDATE does not fire Column.onupdate — the upsert must
        # stamp updated_at itself or a re-run's failures are invisible.
        assert s.execute(select(ItemMap.updated_at)).scalar_one() > first_stamp
        assert count_failed_items_since(s, rerun_started) == 1


def test_same_second_failure_is_counted(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    started = _now()  # carries microseconds; the stored stamp does not
    with session_scope() as s:
        upsert_item(s, "u", "mail", "m1", status="failed")
    with session_scope() as s:
        assert count_failed_items_since(s, started) == 1


def test_done_items_are_not_counted(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    started = _now()
    with session_scope() as s:
        upsert_item(s, "u", "mail", "m1", dest_id="d1", status="done")
    with session_scope() as s:
        assert count_failed_items_since(s, started) == 0


def test_save_cursor_refreshes_updated_at(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    with session_scope() as s:
        save_cursor(s, "u", "mail", "c1")
    with session_scope() as s:
        first = s.execute(select(SyncCursor.updated_at)).scalar_one()
    time.sleep(1.1)
    with session_scope() as s:
        save_cursor(s, "u", "mail", "c2")
    with session_scope() as s:
        assert s.execute(select(SyncCursor.updated_at)).scalar_one() > first
        assert s.execute(select(SyncCursor.cursor_value)).scalar_one() == "c2"


def test_get_item_state_round_trips(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    with session_scope() as s:
        assert get_item_state(s, "u", "contacts", "c1") is None
        upsert_item(s, "u", "contacts", "c1", dest_id="d1", status="done", source_hash="h1")
    with session_scope() as s:
        state = get_item_state(s, "u", "contacts", "c1")
    assert state is not None
    assert (state.status, state.dest_id, state.source_hash) == ("done", "d1", "h1")
