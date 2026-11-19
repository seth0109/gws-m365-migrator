"""CLI/orchestrator user targeting.

`--user` exists so an operator can run one mailbox. A value that matches
nothing must stop the run, never widen into every configured user.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer

from migrator.cli import _filter_users
from migrator.config import Config
from migrator.context import JobContext
from migrator.orchestrator import Orchestrator


def _config(tmp_path: Path) -> Config:
    return Config.model_validate({
        "state_db": str(tmp_path / "state.db"),
        "log_file": None,
        "source": {"type": "imap", "host": "imap.example"},
        "destination": {
            "tenant_id": "t", "client_id": "c", "client_secret": "s",
            "token_cache_file": str(tmp_path / "tc.json"),
        },
        "users": [
            {"source_id": "alice@src", "dest_id": "alice@dst"},
            {"source_id": "bob@src", "dest_id": "bob@dst"},
        ],
    })


def test_filter_users_none_means_all(tmp_path: Path) -> None:
    assert _filter_users(_config(tmp_path), None) is None


def test_filter_users_matches_one(tmp_path: Path) -> None:
    users = _filter_users(_config(tmp_path), "bob@src")
    assert users is not None and [u.source_id for u in users] == ["bob@src"]


def test_filter_users_unknown_id_exits(tmp_path: Path) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        _filter_users(_config(tmp_path), "typo@src")
    assert exc_info.value.exit_code == 1


def test_delta_without_a_cursor_counts_as_a_failed_run_without_a_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from migrator.auth.ms_auth import MSTokenProvider
    from migrator.workloads.cursors import delta_since

    class _Tokens:
        def get_token(self) -> str:
            return "tok"

    # A delta builds the destination client; keep MSAL (network) out of it.
    monkeypatch.setattr(MSTokenProvider, "from_config", classmethod(lambda cls, cfg: _Tokens()))
    orch = Orchestrator(_config(tmp_path))

    def job(ctx: JobContext) -> None:
        if ctx.user.source_id == "alice@src":
            delta_since(ctx, "mail", "mail", "mail")  # alice has no cursor

    # A delta that cannot start must not look like success (exit 0), and it is
    # an instruction to the operator, not a crash to dump a stack for.
    with caplog.at_level("ERROR"):
        failed = orch.run_workload("mail", job, mode="delta", max_workers=1)
    assert failed == 1
    records = [r for r in caplog.records if "alice@src" in r.getMessage()]
    assert len(records) == 1 and records[0].exc_info is None
    assert "migrator mail --user alice@src" in records[0].getMessage()


def test_clean_run_still_reports_items_left_failed_by_earlier_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy import text

    from migrator.cli import _exit_if_failures, _utc_now_naive
    from migrator.state.db import session_scope, upsert_item

    Orchestrator(_config(tmp_path))  # initialises the state DB
    with session_scope() as s:
        upsert_item(s, "alice@src", "mail", "m1", status="failed", last_error="boom")
        # ...in a run that ended long before this one started
        s.execute(text("UPDATE item_map SET updated_at = '2026-01-01 00:00:00'"))

    _exit_if_failures(0, _utc_now_naive())  # this run was clean: no exit
    out = " ".join(capsys.readouterr().out.split())
    assert "1 item(s) from earlier runs are still marked failed" in out
    assert "A delta does not retry them" in out


def test_run_workload_honours_explicit_empty_user_list(tmp_path: Path) -> None:
    orch = Orchestrator(_config(tmp_path))
    ran: list[str] = []

    def job(ctx: JobContext) -> None:
        ran.append(ctx.user.source_id)

    # whatif never builds a destination client, so no credentials are touched.
    orch.run_workload("mail", job, mode="whatif", max_workers=1, users=[])
    assert ran == []
    orch.run_workload("mail", job, mode="whatif", max_workers=1, users=None)
    assert sorted(ran) == ["alice@src", "bob@src"]
