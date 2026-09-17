"""Tests for RRULE → Graph recurrencePattern mapping.

Graph's model is narrower than RFC 5545, so the mapping is best-effort: bare
MONTHLY/YEARLY derive day/month from DTSTART, BYSETPOS supplies the relative
index, comma-lists collapse, and inexpressible rules return None (never raise).
"""
from __future__ import annotations

from migrator.transform.recurrence import rrule_to_graph_recurrence

_START = "2026-03-10T09:00:00"  # a Tuesday, March 10th


def test_daily_interval() -> None:
    rec = rrule_to_graph_recurrence("RRULE:FREQ=DAILY;INTERVAL=3", _START)
    assert rec is not None
    assert rec["pattern"] == {"type": "daily", "interval": 3}
    assert rec["range"] == {"startDate": "2026-03-10", "type": "noEnd"}


def test_weekly_sets_required_first_day_of_week() -> None:
    rec = rrule_to_graph_recurrence("RRULE:FREQ=WEEKLY;BYDAY=MO,WE", _START)
    assert rec is not None
    assert rec["pattern"]["daysOfWeek"] == ["monday", "wednesday"]
    assert rec["pattern"]["firstDayOfWeek"] == "monday"  # RFC 5545 WKST default


def test_weekly_honors_wkst() -> None:
    rec = rrule_to_graph_recurrence("RRULE:FREQ=WEEKLY;BYDAY=MO;WKST=SU", _START)
    assert rec is not None
    assert rec["pattern"]["firstDayOfWeek"] == "sunday"


def test_weekly_without_byday_uses_start_weekday() -> None:
    rec = rrule_to_graph_recurrence("RRULE:FREQ=WEEKLY", _START)
    assert rec is not None
    assert rec["pattern"]["daysOfWeek"] == ["tuesday"]


def test_bare_monthly_takes_day_from_dtstart() -> None:
    rec = rrule_to_graph_recurrence("RRULE:FREQ=MONTHLY", _START)
    assert rec is not None
    assert rec["pattern"] == {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 10}


def test_monthly_bymonthday_list_does_not_crash() -> None:
    # Graph can express only one dayOfMonth; the list collapses to its first.
    rec = rrule_to_graph_recurrence("RRULE:FREQ=MONTHLY;BYMONTHDAY=1,15", _START)
    assert rec is not None
    assert rec["pattern"]["dayOfMonth"] == 1


def test_monthly_negative_bymonthday_is_unmappable() -> None:
    # "Last day of month" has no Graph equivalent — None, not a crash.
    assert rrule_to_graph_recurrence("RRULE:FREQ=MONTHLY;BYMONTHDAY=-1", _START) is None


def test_relative_monthly_from_byday_ordinal() -> None:
    rec = rrule_to_graph_recurrence("RRULE:FREQ=MONTHLY;BYDAY=2TU", _START)
    assert rec is not None
    assert rec["pattern"] == {
        "type": "relativeMonthly", "interval": 1,
        "daysOfWeek": ["tuesday"], "index": "second",
    }


def test_relative_monthly_index_from_bysetpos() -> None:
    # "Last weekday of the month" — the ordinal lives in BYSETPOS, not BYDAY.
    rec = rrule_to_graph_recurrence(
        "RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", _START
    )
    assert rec is not None
    assert rec["pattern"]["index"] == "last"
    assert len(rec["pattern"]["daysOfWeek"]) == 5


def test_bare_yearly_takes_month_and_day_from_dtstart() -> None:
    rec = rrule_to_graph_recurrence("RRULE:FREQ=YEARLY", _START)
    assert rec is not None
    assert rec["pattern"] == {
        "type": "absoluteYearly", "interval": 1, "dayOfMonth": 10, "month": 3,
    }


def test_relative_yearly_fourth_thursday() -> None:
    # e.g. US Thanksgiving: 4th Thursday of November.
    rec = rrule_to_graph_recurrence("RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=4TH", _START)
    assert rec is not None
    assert rec["pattern"] == {
        "type": "relativeYearly", "interval": 1,
        "daysOfWeek": ["thursday"], "index": "fourth", "month": 11,
    }


def test_absolute_yearly() -> None:
    rec = rrule_to_graph_recurrence("RRULE:FREQ=YEARLY;BYMONTH=6;BYMONTHDAY=21", _START)
    assert rec is not None
    assert rec["pattern"]["type"] == "absoluteYearly"
    assert rec["pattern"]["month"] == 6
    assert rec["pattern"]["dayOfMonth"] == 21


def test_count_and_until_ranges() -> None:
    counted = rrule_to_graph_recurrence("RRULE:FREQ=DAILY;COUNT=10", _START)
    assert counted is not None
    assert counted["range"] == {
        "startDate": "2026-03-10", "type": "numbered", "numberOfOccurrences": 10,
    }
    until = rrule_to_graph_recurrence("RRULE:FREQ=DAILY;UNTIL=20261231T235959Z", _START)
    assert until is not None
    assert until["range"] == {
        "startDate": "2026-03-10", "type": "endDate", "endDate": "2026-12-31",
    }


def test_unsupported_freq_returns_none() -> None:
    assert rrule_to_graph_recurrence("RRULE:FREQ=HOURLY", _START) is None
