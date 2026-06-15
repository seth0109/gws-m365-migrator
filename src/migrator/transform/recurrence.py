from __future__ import annotations

import re
from typing import Any


def rrule_to_graph_recurrence(rrule_str: str, start_dt: str) -> dict[str, Any] | None:
    """Convert an RRULE string to a Microsoft Graph recurrence object.

    Returns None if the rule cannot be mapped (e.g. unsupported FREQ).
    Handles DAILY, WEEKLY, MONTHLY (BYMONTHDAY), YEARLY (BYMONTH+BYMONTHDAY).
    """
    props = _parse_rrule(rrule_str)
    freq = props.get("FREQ", "")

    pattern: dict[str, Any] = {}
    range_: dict[str, Any] = {"startDate": start_dt[:10]}

    if freq == "DAILY":
        pattern["type"] = "daily"
        pattern["interval"] = int(props.get("INTERVAL", 1))

    elif freq == "WEEKLY":
        pattern["type"] = "weekly"
        pattern["interval"] = int(props.get("INTERVAL", 1))
        byday = props.get("BYDAY", "")
        pattern["daysOfWeek"] = _map_days(byday) if byday else [_weekday_from_dt(start_dt)]

    elif freq == "MONTHLY":
        if "BYMONTHDAY" in props:
            pattern["type"] = "absoluteMonthly"
            pattern["interval"] = int(props.get("INTERVAL", 1))
            pattern["dayOfMonth"] = int(props["BYMONTHDAY"])
        elif "BYDAY" in props:
            pattern["type"] = "relativeMonthly"
            pattern["interval"] = int(props.get("INTERVAL", 1))
            day_str = props["BYDAY"]
            pattern["daysOfWeek"] = _map_days(re.sub(r"[+-]?\d", "", day_str))
            pattern["index"] = _map_week_index(day_str)
        else:
            return None

    elif freq == "YEARLY":
        if "BYMONTH" in props and "BYMONTHDAY" in props:
            pattern["type"] = "absoluteYearly"
            pattern["interval"] = int(props.get("INTERVAL", 1))
            pattern["dayOfMonth"] = int(props["BYMONTHDAY"])
            pattern["month"] = int(props["BYMONTH"])
        else:
            return None
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


def _map_week_index(byday: str) -> str:
    m = re.match(r"([+-]?\d)", byday)
    if m:
        return _INDEX_MAP.get(int(m.group(1)), "first")
    return "first"
