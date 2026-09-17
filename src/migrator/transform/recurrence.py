from __future__ import annotations

import re
from typing import Any


def rrule_to_graph_recurrence(rrule_str: str, start_dt: str) -> dict[str, Any] | None:
    """Convert an RRULE string to a Microsoft Graph recurrence object.

    Graph's recurrencePattern is strictly narrower than RRULE, so this is a
    best-effort mapping: a bare MONTHLY/YEARLY takes its day/month from DTSTART
    (RFC 5545 semantics), BYMONTHDAY comma-lists collapse to their first
    positive day, and the relative index comes from a BYDAY ordinal or
    BYSETPOS. Rules Graph cannot express at all (negative BYMONTHDAY,
    BYWEEKNO, unsupported FREQ, ...) return None — callers should log the loss
    and migrate the event as a single occurrence."""
    props = _parse_rrule(rrule_str)
    freq = props.get("FREQ", "")
    interval = _first_int(props.get("INTERVAL", "")) or 1

    pattern: dict[str, Any] = {}
    range_: dict[str, Any] = {"startDate": start_dt[:10]}

    if freq == "DAILY":
        pattern = {"type": "daily", "interval": interval}

    elif freq == "WEEKLY":
        byday = props.get("BYDAY", "")
        pattern = {
            "type": "weekly",
            "interval": interval,
            "daysOfWeek": _map_days(byday) if byday else [_weekday_from_dt(start_dt)],
            # Required by Graph for weekly; RFC 5545 defaults WKST to Monday.
            "firstDayOfWeek": _DAY_MAP.get(props.get("WKST", "MO"), "monday"),
        }

    elif freq == "MONTHLY":
        byday = props.get("BYDAY") or ""
        if byday and "BYMONTHDAY" not in props:
            pattern = {
                "type": "relativeMonthly",
                "interval": interval,
                "daysOfWeek": _map_days(byday),
                "index": _week_index(byday, props),
            }
        else:
            day = _day_of_month(props, start_dt)
            if day is None:
                return None
            pattern = {"type": "absoluteMonthly", "interval": interval, "dayOfMonth": day}

    elif freq == "YEARLY":
        month = _first_int(props["BYMONTH"]) if "BYMONTH" in props else int(start_dt[5:7])
        if not month:
            return None
        byday = props.get("BYDAY") or ""
        if byday and "BYMONTHDAY" not in props:
            pattern = {
                "type": "relativeYearly",
                "interval": interval,
                "daysOfWeek": _map_days(byday),
                "index": _week_index(byday, props),
                "month": month,
            }
        else:
            day = _day_of_month(props, start_dt)
            if day is None:
                return None
            pattern = {
                "type": "absoluteYearly",
                "interval": interval,
                "dayOfMonth": day,
                "month": month,
            }
    else:
        return None

    # Recurrence range
    if "COUNT" in props:
        range_["type"] = "numbered"
        range_["numberOfOccurrences"] = int(props["COUNT"])
    elif "UNTIL" in props:
        range_["type"] = "endDate"
        until = props["UNTIL"]
        range_["endDate"] = f"{until[:4]}-{until[4:6]}-{until[6:8]}"
    else:
        range_["type"] = "noEnd"

    return {"pattern": pattern, "range": range_}


# ── helpers ───────────────────────────────────────────────────────────────────

_DAY_MAP = {
    "MO": "monday", "TU": "tuesday", "WE": "wednesday",
    "TH": "thursday", "FR": "friday", "SA": "saturday", "SU": "sunday",
}

_WEEKDAY_IDX = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

_INDEX_MAP = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}


def _parse_rrule(rrule: str) -> dict[str, str]:
    if rrule.startswith("RRULE:"):
        rrule = rrule[6:]
    return dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)


def _map_days(byday: str) -> list[str]:
    return [_DAY_MAP[d] for d in re.findall(r"[A-Z]{2}", byday) if d in _DAY_MAP]


def _weekday_from_dt(iso_dt: str) -> str:
    from datetime import datetime
    dt = datetime.fromisoformat(iso_dt[:10])
    return list(_DAY_MAP.values())[dt.weekday()]


def _first_int(value: str | None) -> int | None:
    if not value:
        return None
    m = re.match(r"\s*([+-]?\d+)", value)
    return int(m.group(1)) if m else None


def _day_of_month(props: dict[str, str], start_dt: str) -> int | None:
    """Graph's dayOfMonth is a single positive day. No BYMONTHDAY means the
    DTSTART day (RFC 5545); a comma list collapses to its first positive entry;
    all-negative values ("last day of month") are inexpressible -> None."""
    raw = props.get("BYMONTHDAY")
    if raw is None:
        return int(start_dt[8:10])
    days = [d for d in (_first_int(p) for p in raw.split(",")) if d is not None]
    positive = [d for d in days if d > 0]
    return positive[0] if positive else None


def _week_index(byday: str, props: dict[str, str]) -> str:
    """Relative-pattern index: a BYDAY ordinal ("-1FR") wins, else BYSETPOS
    (e.g. BYDAY=MO,...,FR;BYSETPOS=-1 = "last weekday"), else "first"."""
    m = re.match(r"([+-]?\d+)", byday)
    if m:
        return _INDEX_MAP.get(int(m.group(1)), "first")
    if (pos := _first_int(props.get("BYSETPOS"))) is not None:
        return _INDEX_MAP.get(pos, "first")
    return "first"
