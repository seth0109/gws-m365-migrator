"""Live progress for a workload run.

One line per user being migrated, under a line for the workload as a whole: a
bar when the source could say how many items to expect, a running count when it
could not. Jobs report through the `ItemProgress` on their `JobContext`; the
base class only counts, so a job (or a test) running without a display needs
nothing special.

Totals are estimates supplied by the source connector (`BaseSource.
expected_total`). They size the bar and nothing else: what gets migrated is
decided by the listing and the state DB, never by a count.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import TypeVar

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
)
from rich.table import Column
from rich.text import Text

log = logging.getLogger(__name__)

T = TypeVar("T")

# How often a running job writes its position to the log. Next to a live
# display the lines are a record in the log file; without one (output piped or
# redirected, where rich draws nothing until the end) they are the only sign of
# life, so they come more often.
_NOTE_EVERY_LIVE = 300.0
_NOTE_EVERY_PIPED = 60.0


class ItemProgress:
    """Counts the items one job has dealt with, against an expected total when
    the source could give one. This base class only counts: it is what a
    JobContext carries when nothing is drawing (tests, the tenant-level flows)."""

    def __init__(self) -> None:
        self.completed = 0
        self.total: int | None = None
        self._open_ended = False

    def expect(self, count: int | None) -> None:
        """Add one collection's expected item count to the total.

        A job that walks several collections in turn (calendars) calls this
        once per collection. A single unknown count makes the whole total
        unknown: a bar measured against part of the work would read as nearly
        done while most of it is still ahead."""
        if count is None or self._open_ended:
            self._open_ended = True
            self.total = None
        else:
            self.total = (self.total or 0) + count
        self._changed()

    def advance(self, n: int = 1) -> None:
        self.completed += n
        if self.total is not None and self.completed > self.total:
            # The total was an estimate (mail arrived meanwhile, ...): grow it
            # rather than show more done than there is.
            self.total = self.completed
        self._changed()

    def track(
        self, items: Iterable[T], expected: Callable[[], int | None] | None = None
    ) -> Iterator[T]:
        """Yield `items`, counting each one when the caller comes back for the
        next, i.e. has finished with it (migrated, skipped or failed).

        `expected` is asked once, when the first item arrives: a connector
        learns its total while setting up the listing, and for a generator that
        is not before the first item is pulled."""
        asked = False
        for item in items:
            if not asked:
                asked = True
                if expected is not None:
                    self.expect(expected())
            yield item
            self.advance()

    def summary(self) -> str:
        if self.total:
            percent = _percent(self.completed, self.total)
            return f"{self.completed:,} of about {self.total:,} items ({percent}%)"
        return f"{self.completed:,} items so far"

    def _changed(self) -> None:
        """Hook for the drawn subclass."""


class _LiveItemProgress(ItemProgress):
    """An ItemProgress drawn as one row of a RunProgress."""

    def __init__(self, bars: Progress, task: TaskID, label: str, note_every: float) -> None:
        super().__init__()
        self._bars = bars
        self._task = task
        self._label = label
        self._note_every = note_every
        self._noted = time.monotonic()

    def _changed(self) -> None:
        if self.total is None:
            # Progress.update() reads total=None as "leave it alone", so going
            # back to an open-ended row means clearing it on the task itself.
            for task in self._bars.tasks:
                if task.id == self._task and task.total is not None:
                    task.total = None
        self._bars.update(self._task, completed=self.completed, total=self.total)
        now = time.monotonic()
        if now - self._noted >= self._note_every:
            self._noted = now
            log.info("%s: %s", self._label, self.summary())


def _percent(done: float, total: float) -> int:
    """Rounded down, so 100% is only ever shown for work that is all done."""
    return int(100 * done // total) if total else 0


def _clock(seconds: float | None) -> str:
    """h:mm:ss, with hours running past 24 (a large mailbox takes days)."""
    if seconds is None:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}"


def _fixed() -> Column:
    """A column that keeps its width on a narrow terminal: the bar gives way
    first, the numbers are never wrapped or cut."""
    return Column(no_wrap=True, justify="right")


class _CountColumn(ProgressColumn):
    """"12,400/~48,211  25%", or "1,020 items" when there is no total."""

    def render(self, task: Task) -> Text:
        done = f"{int(task.completed):,}"
        unit = str(task.fields.get("unit", "items"))
        if task.total is None:
            return Text(f"{done} {unit}", style="progress.download")
        total = f"{int(task.total):,}"
        if task.fields.get("overall"):
            return Text(f"{done}/{total} {unit}", style="progress.download")
        # "~": the total is the source's estimate, not a promise.
        text = Text(f"{done}/~{total}", style="progress.download")
        percent = _percent(task.completed, task.total)
        text.append(f"  {percent:3}%", style="progress.percentage")
        return text


class _RateColumn(ProgressColumn):
    """Items per second over the last couple of minutes (per minute when slow)."""

    def render(self, task: Task) -> Text:
        speed = task.speed
        if task.fields.get("overall") or not speed:
            return Text("")
        if speed >= 10:
            rate = f"{speed:.0f}/s"
        elif speed >= 1:
            rate = f"{speed:.1f}/s"
        else:
            rate = f"{speed * 60:.0f}/min"
        return Text(rate, style="progress.data.speed")


class _TimeColumn(ProgressColumn):
    """Time left when it can be estimated, time spent otherwise."""

    def render(self, task: Task) -> Text:
        remaining = None if task.fields.get("overall") else task.time_remaining
        if remaining is not None and not task.finished:
            return Text(f"eta {_clock(remaining)}", style="progress.remaining")
        return Text(_clock(task.elapsed), style="progress.elapsed")


class RunProgress:
    """The live display for one workload run across users.

    Rows are added as worker threads pick users up and removed as they finish,
    so only the users being migrated right now are on screen. rich's Progress
    is thread-safe, and it shares the global console with the RichHandler, so
    log lines scroll above the bars instead of tearing them."""

    def __init__(self, workload: str, users: int, console: Console | None = None) -> None:
        self._workload = workload
        self._bars = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", table_column=Column(no_wrap=True)),
            BarColumn(bar_width=None),
            _CountColumn(table_column=_fixed()),
            _RateColumn(table_column=_fixed()),
            _TimeColumn(table_column=_fixed()),
            console=console,
            expand=True,
            refresh_per_second=4,
            # A mailbox alternates between racing through what is already
            # migrated and importing at a message or two a second: a longer
            # window than rich's 30 s keeps the rate and the ETA from jumping.
            speed_estimate_period=120,
        )
        self._overall = self._bars.add_task(
            f"[bold blue]{escape(workload)}", total=users, unit="users", overall=True
        )
        console = self._bars.console
        self._note_every = _NOTE_EVERY_LIVE if console.is_terminal else _NOTE_EVERY_PIPED
        # Long sign-in names are cut so the numbers always fit beside them.
        self._label_width = max(16, console.width // 3)

    def __enter__(self) -> RunProgress:
        self._bars.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._bars.stop()

    @contextmanager
    def user(self, label: str) -> Iterator[ItemProgress]:
        """A row for one user's job, for as long as the job runs."""
        shown = label
        if len(shown) > self._label_width:
            shown = shown[: self._label_width - 1] + "…"
        task = self._bars.add_task(escape(shown), total=None, unit="items", overall=False)
        try:
            yield _LiveItemProgress(
                self._bars, task, f"{self._workload} for {label}", self._note_every
            )
        finally:
            self._bars.remove_task(task)

    def user_finished(self) -> None:
        self._bars.advance(self._overall)
