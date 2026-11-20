"""Progress reporting: the counting handle jobs report through, the live
display the orchestrator draws, and what each job loop reports.

A bar is only as good as its total, and the totals here are estimates from the
source. What these tests pin: an item is counted once, when the job has
finished with it; a total that turns out too small grows instead of showing
more done than there is; a partly unknown total is shown as unknown rather than
as a bar measured against part of the work; and nothing about the display can
change what a job migrates.
"""
from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from migrator import progress as progress_mod
from migrator.config import Config, UserMapping
from migrator.connectors.base import (
    BaseSource,
    CalendarRef,
    SourceContact,
    SourceEvent,
    SourceFile,
    SourceMessage,
)
from migrator.context import JobContext
from migrator.orchestrator import Orchestrator
from migrator.progress import ItemProgress, RunProgress, _clock
from migrator.state.db import init_db
from migrator.workloads.calendar_job import run_calendar
from migrator.workloads.contacts_job import run_contacts
from migrator.workloads.files_job import run_files
from migrator.workloads.mail_job import run_mail

_USER = UserMapping(source_id="a@old.com", dest_id="a@new.com")
_CFG = Config.model_validate({
    "source": {"type": "imap", "host": "h"},
    "destination": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
    "users": [{"source_id": "a@old.com", "dest_id": "a@new.com"}],
})


# ── the counting handle ───────────────────────────────────────────────────────


def test_track_counts_an_item_once_the_caller_is_done_with_it() -> None:
    p = ItemProgress()
    seen: list[tuple[str, int]] = []
    for item in p.track(["a", "b", "c"]):
        seen.append((item, p.completed))  # not counted while it is being worked on
    assert seen == [("a", 0), ("b", 1), ("c", 2)]
    assert p.completed == 3 and p.total is None


def test_track_asks_for_the_total_once_the_listing_has_started() -> None:
    # A connector learns its total inside the generator, i.e. not before the
    # first item is pulled: asking up front would always get "unknown".
    known: dict[str, int] = {}
    asked: list[int] = []

    def listing() -> Iterator[str]:
        known["n"] = 2
        yield from ("a", "b")

    def expected() -> int | None:
        asked.append(1)
        return known.get("n")

    p = ItemProgress()
    assert list(p.track(listing(), expected=expected)) == ["a", "b"]
    assert p.total == 2 and len(asked) == 1


def test_totals_of_several_collections_add_up() -> None:
    p = ItemProgress()
    p.expect(840)
    p.expect(15)
    assert p.total == 855


def test_one_unknown_collection_makes_the_whole_total_unknown() -> None:
    # 840/840 while a second, uncounted calendar is still ahead would read as done.
    p = ItemProgress()
    p.expect(840)
    p.expect(None)
    assert p.total is None
    p.expect(15)  # ...and it stays unknown
    assert p.total is None


def test_a_total_that_was_too_small_grows_with_the_count() -> None:
    p = ItemProgress()
    p.expect(2)
    p.advance(3)  # mail arrived while the mailbox was being read
    assert (p.completed, p.total) == (3, 3)


def test_summary_reads_with_and_without_a_total() -> None:
    p = ItemProgress()
    p.advance(1234)
    assert p.summary() == "1,234 items so far"
    p.expect(48211)
    assert p.summary() == "1,234 of about 48,211 items (2%)"
    p.advance(48211 - 1234 - 1)
    assert p.summary().endswith("(99%)")  # rounded down: 100% means all of it


def test_clock_keeps_counting_hours_past_a_day() -> None:
    assert _clock(None) == ""
    assert _clock(59) == "0:00:59"
    assert _clock(26 * 3600 + 3 * 60 + 4) == "26:03:04"


# ── the live display ──────────────────────────────────────────────────────────


def _display(width: int = 120, terminal: bool = True, users: int = 2) -> RunProgress:
    console = Console(file=io.StringIO(), force_terminal=terminal, width=width, color_system=None)
    return RunProgress("mail", users, console=console)


def _frame(display: RunProgress, width: int = 120) -> str:
    out = Console(file=io.StringIO(), width=width, color_system=None)
    out.print(display._bars.get_renderable())
    assert isinstance(out.file, io.StringIO)
    return out.file.getvalue()


def test_a_user_has_a_row_only_while_their_job_runs() -> None:
    display = _display()
    with display.user("alice@src") as p:
        p.expect(48211)
        p.advance(12400)
        frame = _frame(display)
        assert "alice@src" in frame
        assert "12,400/~48,211" in frame and "25%" in frame
    display.user_finished()
    frame = _frame(display)
    assert "alice@src" not in frame
    assert "1/2 users" in frame


def test_a_row_is_removed_even_when_the_job_raises() -> None:
    display = _display()
    with pytest.raises(RuntimeError), display.user("alice@src"):
        raise RuntimeError("job failed")
    assert "alice@src" not in _frame(display)


def test_a_row_without_a_total_shows_a_count_and_no_percentage() -> None:
    display = _display()
    with display.user("alice@src") as p:
        p.advance(1020)
        frame = _frame(display)
    assert "1,020 items" in frame and "%" not in frame


def test_a_row_goes_back_to_a_plain_count_when_the_total_becomes_unknown() -> None:
    display = _display()
    with display.user("alice@src") as p:
        p.expect(840)
        p.advance(840)
        p.expect(None)  # the next calendar could not be counted
        p.advance(5)
        frame = _frame(display)
    assert "845 items" in frame and "%" not in frame


def test_numbers_are_never_cut_on_a_narrow_terminal() -> None:
    display = _display(width=80)
    with display.user("a.very.long.sign.in.name@a-long-company-name.example.com") as p:
        p.expect(148211)
        p.advance(112201)
        frame = _frame(display, width=80)
    assert "112,201/~148,211" in frame and "75%" in frame
    assert "a.very.long.sign.in.name@" in frame  # the name is what gives way
    assert "example.com" not in frame
    assert all(len(line) <= 80 for line in frame.splitlines())


def test_a_row_stays_on_one_line_however_narrow_the_terminal() -> None:
    # A row that wraps makes a live display jump around; cut it instead.
    display = _display(width=50)
    with display.user("alice@src") as p:
        p.expect(148211)
        p.advance(112201)
        frame = _frame(display, width=50)
    assert len(frame.splitlines()) == 2  # the workload line and alice's


def test_position_is_written_to_the_log_every_so_often(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1000.0]
    monkeypatch.setattr(progress_mod.time, "monotonic", lambda: now[0])
    display = _display(terminal=False)  # piped output: the log is the only sign of life
    with caplog.at_level(logging.INFO, logger="migrator.progress"), display.user("alice@src") as p:
        p.expect(200)
        p.advance(10)
        assert caplog.records == []  # not on every item
        now[0] += 61
        p.advance(10)
        now[0] += 5
        p.advance(10)
    assert [r.getMessage() for r in caplog.records] == [
        "mail for alice@src: 20 of about 200 items (10%)"
    ]


def test_log_notes_are_rarer_next_to_a_live_display(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _display(terminal=False)._note_every == 60
    assert _display(terminal=True)._note_every == 300


# ── what the jobs report ──────────────────────────────────────────────────────


class _Dest:
    """Destination that accepts everything."""

    def __init__(self) -> None:
        self.creates = 0

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        if path.endswith("/calendars"):
            return {"value": []}
        return {"id": "ms-uid"}

    def paginate(self, path: str, **kw: Any) -> Iterator[list[dict[str, Any]]]:
        yield []

    def post(self, path: str, **kw: Any) -> dict[str, Any]:
        self.creates += 1
        return {"id": f"dest-{self.creates}"}

    def put_raw(self, url: str, data: bytes, **kw: Any) -> dict[str, Any]:
        self.creates += 1
        return {"id": f"dest-{self.creates}"}

    def patch(self, path: str, **kw: Any) -> None:
        pass


def _mime(subject: str) -> bytes:
    return (
        f"From: x@old.com\r\nTo: a@old.com\r\nSubject: {subject}\r\n"
        "Date: Tue, 09 Dec 2025 13:42:55 +0000\r\n\r\nbody\r\n"
    ).encode()


class _Source(BaseSource):
    capabilities = {"mail", "contacts", "calendar", "files"}

    def __init__(self, expected: dict[str, int | None] | None = None) -> None:
        super().__init__()
        self.expect = expected or {}

    def _announce(self, key: str) -> None:
        if key in self.expect:
            self._set_expected(key, self.expect[key])

    def iter_messages(self, user: UserMapping, since: str | None) -> Iterator[SourceMessage]:
        self._set_cursor("mail", "c1")
        self._announce("mail")
        for i in (1, 2, 3):
            yield SourceMessage(source_id=f"m{i}", raw_mime=_mime(f"m{i}"), folder_paths=["Inbox"])

    def iter_contacts(self, user: UserMapping, since: str | None) -> Iterator[SourceContact]:
        self._set_cursor("contacts", "c1")
        self._announce("contacts")
        for i in (1, 2):
            yield SourceContact(source_id=f"c{i}", graph_body={"givenName": f"c{i}"})

    def list_calendars(self, user: UserMapping) -> list[CalendarRef]:
        return [CalendarRef("cal-1", "Work"), CalendarRef("cal-2", "Home")]

    def iter_events(
        self, user: UserMapping, cal: CalendarRef, since: str | None
    ) -> Iterator[SourceEvent]:
        key = f"calendar:{cal.cal_id}"
        self._set_cursor(key, "c1")
        self._announce(key)
        for i in range(2 if cal.cal_id == "cal-1" else 1):
            yield SourceEvent(source_id=f"{cal.cal_id}-e{i}", graph_body={"subject": "s"})

    def iter_files(self, user: UserMapping, since: str | None) -> Iterator[SourceFile]:
        self._set_cursor("files", "c1")
        self._announce("files")
        # The child is listed before its folder, so it is deferred and retried.
        yield SourceFile("f1", "a.txt", "text/plain", parent_id="d1", is_folder=False)
        yield SourceFile("d1", "Docs", "", parent_id=None, is_folder=True)

    def fetch_file(self, user: UserMapping, f: SourceFile) -> tuple[bytes, str]:
        return b"data", f.name


def _ctx(source: _Source, mode: str = "full") -> JobContext:
    return JobContext(_USER, source, _Dest(), mode, _CFG)  # type: ignore[arg-type]


def test_mail_job_reports_every_message_against_the_expected_total(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    ctx = _ctx(_Source({"mail": 3}))
    run_mail(ctx)
    assert (ctx.progress.completed, ctx.progress.total) == (3, 3)

    # A re-run goes through the same three messages (all already migrated): the
    # bar measures the mailbox gone through, not only what was newly imported.
    again = _ctx(_Source({"mail": 3}))
    run_mail(again)
    assert (again.progress.completed, again.progress.total) == (3, 3)
    assert again.dest_gc.creates == 0  # type: ignore[union-attr]


def test_a_source_without_a_count_still_gets_a_running_count(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    ctx = _ctx(_Source())
    run_mail(ctx)
    assert (ctx.progress.completed, ctx.progress.total) == (3, None)


def test_contacts_job_reports_its_contacts(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    ctx = _ctx(_Source({"contacts": 2}))
    run_contacts(ctx)
    assert (ctx.progress.completed, ctx.progress.total) == (2, 2)


def test_calendar_job_adds_up_its_calendars(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    ctx = _ctx(_Source({"calendar:cal-1": 2, "calendar:cal-2": 1}))
    run_calendar(ctx)
    assert (ctx.progress.completed, ctx.progress.total) == (3, 3)


def test_calendar_total_is_unknown_when_one_calendar_could_not_be_counted(
    tmp_path: Path,
) -> None:
    init_db(tmp_path / "state.db")
    ctx = _ctx(_Source({"calendar:cal-1": 2}))  # cal-2 reports nothing
    run_calendar(ctx)
    assert (ctx.progress.completed, ctx.progress.total) == (3, None)


def test_files_job_counts_a_deferred_item_once(tmp_path: Path, monkeypatch: Any) -> None:
    from migrator.workloads import files_job

    init_db(tmp_path / "state.db")
    monkeypatch.setattr(files_job, "ensure_onedrive", lambda gc, uid: None)
    monkeypatch.setattr(files_job, "ensure_folder", lambda *a, **k: "dest-folder")
    ctx = _ctx(_Source())
    run_files(ctx)
    assert ctx.dest_gc.creates == 1  # type: ignore[union-attr]  # the file was uploaded...
    assert (ctx.progress.completed, ctx.progress.total) == (2, None)  # ...and counted once


# ── the orchestrator ──────────────────────────────────────────────────────────


def test_run_workload_hands_each_job_a_drawn_progress_and_logs_the_count(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = Config.model_validate({
        "state_db": str(tmp_path / "state.db"),
        "source": {"type": "imap", "host": "imap.example"},
        "destination": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        "users": [
            {"source_id": "alice@src", "dest_id": "alice@dst"},
            {"source_id": "bob@src", "dest_id": "bob@dst"},
        ],
    })
    orch = Orchestrator(cfg)
    handed: list[type] = []

    def job(ctx: JobContext) -> None:
        handed.append(type(ctx.progress))
        ctx.progress.expect(5)
        if ctx.user.source_id == "bob@src":
            ctx.progress.advance(2)
            raise RuntimeError("bob's mailbox is on fire")
        ctx.progress.advance(5)

    # whatif never builds a destination client, so no credentials are touched.
    with caplog.at_level(logging.INFO, logger="migrator.orchestrator"):
        failed = orch.run_workload("mail", job, mode="whatif", max_workers=2)

    assert failed == 1  # a failing job is still a failed run, display or not
    assert all(issubclass(t, ItemProgress) and t is not ItemProgress for t in handed)
    messages = [r.getMessage() for r in caplog.records]
    assert "Completed mail for alice@src (5 item(s) processed)" in messages
    assert not any("Completed mail for bob@src" in m for m in messages)
