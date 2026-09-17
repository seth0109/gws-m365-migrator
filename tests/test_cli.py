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
