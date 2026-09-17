"""Tests for SharePoint site auto-provisioning.

Graph documents that an M365 group created app-only without an owner may never
get its SharePoint site provisioned, that visibility defaults to Public, and
that mailNickname has a restricted charset — all three are handled here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from migrator.microsoft.sharepoint import (
    _poll_group_site,
    _sanitize_mail_nickname,
    ensure_site_for_drive,
)
from migrator.state.db import init_db


def test_sanitize_mail_nickname_strips_illegal_chars() -> None:
    assert _sanitize_mail_nickname("Team Alias") == "Team-Alias"
    assert _sanitize_mail_nickname("a@b(c)d;e") == "a-b-c-d-e"
    assert _sanitize_mail_nickname("département") == "d-partement"  # non-ASCII replaced


def test_sanitize_mail_nickname_caps_length_and_never_empty() -> None:
    assert len(_sanitize_mail_nickname("x" * 100)) == 64
    assert _sanitize_mail_nickname("@@@") == "site"


class _ProvisionGC:
    def __init__(self) -> None:
        self.group_bodies: list[dict[str, Any]] = []

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        assert path == "/groups"
        self.group_bodies.append(kwargs["json"])
        return {"id": "grp-1"}

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if "sites/root" in path:
            return {"id": "site-1", "webUrl": "https://x"}
        return {"id": "lib-drive-1"}


def test_ensure_site_creates_private_group_with_owner(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    gc = _ProvisionGC()
    site_id, drive_id = ensure_site_for_drive(
        gc, "gdrive-1", "Finance Team", "Finance", owner="admin@x.com"  # type: ignore[arg-type]
    )
    assert (site_id, drive_id) == ("site-1", "lib-drive-1")
    body = gc.group_bodies[0]
    assert body["visibility"] == "Private"
    assert body["mailNickname"] == "Finance-Team"
    assert body["owners@odata.bind"] == [
        "https://graph.microsoft.com/v1.0/users/admin@x.com"
    ]


def test_ensure_site_without_owner_still_provisions_but_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    init_db(tmp_path / "state.db")
    gc = _ProvisionGC()
    with caplog.at_level("WARNING"):
        ensure_site_for_drive(gc, "gdrive-2", "Ops", "Ops")  # type: ignore[arg-type]
    assert "owners@odata.bind" not in gc.group_bodies[0]
    assert any("sharepoint_site_owner" in r.message for r in caplog.records)


def _status_error(code: int) -> httpx.HTTPStatusError:
    resp = httpx.Response(code, request=httpx.Request("GET", "https://g/x"))
    return httpx.HTTPStatusError("boom", request=resp.request, response=resp)


def test_poll_group_site_raises_immediately_on_permanent_error() -> None:
    class _ForbiddenGC:
        def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
            raise _status_error(403)

    with pytest.raises(httpx.HTTPStatusError):
        _poll_group_site(_ForbiddenGC(), "grp-1")  # type: ignore[arg-type]


def test_poll_group_site_waits_out_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("migrator.microsoft.sharepoint.time.sleep", lambda _s: None)

    class _SlowGC:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls < 3:
                raise _status_error(404)
            return {"id": "site-1"}

    gc = _SlowGC()
    assert _poll_group_site(gc, "grp-1")["id"] == "site-1"  # type: ignore[arg-type]
    assert gc.calls == 3


def test_provisioning_resumes_from_recorded_group_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll timeout must not leave the next run POSTing a second group with the
    same mailNickname (Graph rejects the duplicate) — it resumes polling the
    group the first run created."""
    init_db(tmp_path / "state.db")
    monkeypatch.setattr("migrator.microsoft.sharepoint.time.sleep", lambda _s: None)
    monkeypatch.setattr("migrator.microsoft.sharepoint._PROVISION_POLL_ATTEMPTS", 1)

    class _StalledGC(_ProvisionGC):
        def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
            if "sites/root" in path:
                raise _status_error(404)
            return super().get(path, **kwargs)

    stalled = _StalledGC()
    with pytest.raises(RuntimeError):
        ensure_site_for_drive(stalled, "gdrive-9", "Ops", "Ops")  # type: ignore[arg-type]
    assert len(stalled.group_bodies) == 1

    healthy = _ProvisionGC()
    site_id, drive_id = ensure_site_for_drive(healthy, "gdrive-9", "Ops", "Ops")  # type: ignore[arg-type]
    assert healthy.group_bodies == []  # resumed grp-1; no second group
    assert (site_id, drive_id) == ("site-1", "lib-drive-1")

    again = _ProvisionGC()
    assert ensure_site_for_drive(again, "gdrive-9", "Ops", "Ops") == ("site-1", "lib-drive-1")  # type: ignore[arg-type]
    assert again.group_bodies == []  # and the finished site is reused thereafter
