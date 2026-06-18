from __future__ import annotations

from pathlib import Path

from migrator.config import Config
from migrator.reporting import generate_report
from migrator.state.db import init_db, session_scope, upsert_item


def _config(db_path: Path) -> Config:
    return Config.model_validate({
        "state_db": str(db_path),
        "source": {
            "type": "imap",
            "host": "imap.old.com",
        },
        "destination": {
            "type": "microsoft365",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
        },
        "users": [{"source_id": "alice@old.com", "dest_id": "alice@new.com"}],
    })


def test_report_includes_namespaced_and_unconfigured_workloads(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cfg = _config(db)
    init_db(db)
    with session_scope() as s:
        upsert_item(s, "alice@old.com", "mail", "m1", status="done")
        upsert_item(s, "alice@old.com", "mail", "m2", status="failed", last_error="boom")
        # Tenant-level namespaced workload keyed by a sentinel, not a config user.
        upsert_item(s, "__sharepoint__", "sharepoint_site:siteA", "f1", status="done")
        upsert_item(s, "admin@old.com", "shared_drive:d1", "f2", status="done")

    out = tmp_path / "report.html"
    generate_report(cfg, out)
    html = out.read_text()

    # Namespaced tenant-level workloads are now surfaced.
    assert "sharepoint_site:siteA" in html
    assert "shared_drive:d1" in html
    assert "__sharepoint__" in html
    # Configured per-user standard workloads still appear even with no rows.
    assert "contacts" in html
    assert "calendar" in html
    # Failure detail is reported.
    assert "boom" in html
