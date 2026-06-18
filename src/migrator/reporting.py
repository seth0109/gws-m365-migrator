from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .config import Config
from .state.db import init_db, session_scope
from .state.models import ItemMap

log = logging.getLogger(__name__)

# Per-user workloads we expect for every configured user. Listing one with zero
# rows is itself a signal (workload never ran / nothing enumerated), so these are
# always shown even when no ItemMap rows exist. Tenant-level workloads
# (shared_drive:*, sharepoint_site:*) are namespaced and keyed by an
# impersonation/sentinel id rather than a configured user, so those are
# discovered from the DB instead of assumed.
_STANDARD_WORKLOADS = ("contacts", "calendar", "files", "mail")


def generate_report(cfg: Config, output_path: Path) -> None:
    init_db(cfg.state_db)

    rows: list[dict[str, Any]] = []
    with session_scope() as s:
        # Every (user, workload) pair that actually has state, so namespaced
        # tenant-level workloads (shared_drive:<id> / sharepoint_site:<id>) and
        # any user not in the config are included rather than silently dropped.
        observed = {
            (user_email, workload)
            for user_email, workload in s.execute(
                select(ItemMap.user_email, ItemMap.workload).distinct()
            ).all()
        }
        # Union with the standard per-user workloads so a configured user whose
        # workload produced no rows still surfaces (a possible data-loss signal).
        expected = {
            (user.source_id, workload)
            for user in cfg.users
            for workload in _STANDARD_WORKLOADS
        }

        for user_email, workload in sorted(observed | expected):
            counts = s.execute(
                select(ItemMap.status, func.count(ItemMap.id))
                .where(
                    ItemMap.user_email == user_email,
                    ItemMap.workload == workload,
                )
                .group_by(ItemMap.status)
            ).all()

            status_map = {status: count for status, count in counts}
            rows.append({
                "user": user_email,
                "workload": workload,
                "done": status_map.get("done", 0),
                "failed": status_map.get("failed", 0),
                "skipped": status_map.get("skipped", 0),
                "pending": status_map.get("pending", 0),
            })

        failures: list[dict[str, Any]] = []
        fail_rows = s.execute(
            select(ItemMap)
            .where(ItemMap.status == "failed")
            .order_by(ItemMap.user_email, ItemMap.workload)
        ).scalars().all()
        for row in fail_rows:
            failures.append({
                "user": row.user_email,
                "workload": row.workload,
                "source_id": row.source_id,
                "error": row.last_error or "",
            })

    _write_html(rows, failures, output_path)


def _write_html(
    rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    output_path: Path,
) -> None:
    html_rows = "\n".join(
        f"<tr><td>{r['user']}</td><td>{r['workload']}</td>"
        f"<td class='done'>{r['done']}</td>"
        f"<td class='fail'>{r['failed']}</td>"
        f"<td>{r['skipped']}</td>"
        f"<td>{r['pending']}</td></tr>"
        for r in rows
    )

    fail_rows = "\n".join(
        f"<tr><td>{f['user']}</td><td>{f['workload']}</td>"
        f"<td>{f['source_id']}</td><td>{f['error']}</td></tr>"
        for f in failures
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Migration Report</title>
<style>
body {{ font-family: sans-serif; margin: 2em; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 2em; }}
th, td {{ border: 1px solid #ccc; padding: 6px 12px; text-align: left; }}
th {{ background: #f4f4f4; }}
.done {{ color: green; font-weight: bold; }}
.fail {{ color: red; font-weight: bold; }}
</style>
</head>
<body>
<h1>GWS → M365 Migration Report</h1>
<h2>Summary</h2>
<table>
<tr><th>User</th><th>Workload</th><th>Done</th><th>Failed</th><th>Skipped</th><th>Pending</th></tr>
{html_rows}
</table>
<h2>Failures requiring manual review</h2>
<table>
<tr><th>User</th><th>Workload</th><th>Source ID</th><th>Error</th></tr>
{fail_rows}
</table>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    log.info("Report written to %s", output_path)
