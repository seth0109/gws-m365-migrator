"""Full pass → mailbox keeps changing → delta, for a Microsoft 365 source.

The source mailbox here is a small stateful model of Exchange behind a real
GraphClient (so the request headers are the ones that would go on the wire):

- an item's *regular* id changes when it is moved to another folder; its
  immutable id — returned only for requests carrying `Prefer:
  IdType="ImmutableId"` — does not;
- `/messages` honours `$filter=receivedDateTime ge <timestamp>`;
- folders are addressable by well-known name.

What a delta has to guarantee, and what these tests pin: mail that arrived
since the full pass is migrated exactly once, nothing already migrated is
downloaded or imported again (even after the user has filed it elsewhere), and
a clock that runs ahead cannot open a gap.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from migrator.config import Config, Microsoft365SourceConfig, UserMapping
from migrator.connectors import m365
from migrator.connectors.m365 import M365Source
from migrator.context import JobContext
from migrator.microsoft.graph_client import GraphClient
from migrator.orchestrator import Orchestrator
from migrator.state.db import get_cursor, init_db, session_scope
from migrator.workloads.mail_job import run_mail

_USER = UserMapping(source_id="adam@src", dest_id="adam@dst")
_FOLDERS = {"inbox": ("F-INBOX", "Inbox"), "sentitems": ("F-SENT", "Sent Items")}
_CUSTOM = {"F-PROJ": "Projects"}


class _Mailbox:
    """The source mailbox. `handler` answers Graph requests against it."""

    def __init__(self) -> None:
        self.messages: dict[int, dict[str, Any]] = {}  # serial → {folder, received, moves}
        self.requests: list[httpx.Request] = []

    def deliver(self, serial: int, received: str, folder: str = "F-INBOX") -> None:
        self.messages[serial] = {"folder": folder, "received": received, "moves": 0}

    def move(self, serial: int, folder: str) -> None:
        self.messages[serial]["folder"] = folder
        self.messages[serial]["moves"] += 1  # ...which changes its regular id

    def _id(self, serial: int, immutable: bool) -> str:
        m = self.messages[serial]
        return f"imm-{serial}" if immutable else f"reg-{serial}-v{m['moves']}"

    def _serial(self, item_id: str) -> int | None:
        for serial in self.messages:
            if item_id in (self._id(serial, True), self._id(serial, False)):
                return serial
        return None  # e.g. a regular id from before the item was moved

    def mime_downloads(self) -> list[str]:
        return [r.url.path.split("/")[-2] for r in self.requests if r.url.path.endswith("$value")]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.method == "GET", "a source is only ever read"
        immutable = req.headers.get("Prefer") == 'IdType="ImmutableId"'
        path = req.url.path.removeprefix("/v1.0")
        if path == "/users/adam@src":
            return httpx.Response(200, json={"id": "UID"})
        if path == "/users/UID/mailFolders":
            folders = [{"id": fid, "displayName": name} for fid, name in _FOLDERS.values()]
            folders += [{"id": fid, "displayName": name} for fid, name in _CUSTOM.items()]
            return httpx.Response(200, json={"value": folders})
        if path.endswith("/childFolders"):
            return httpx.Response(200, json={"value": []})
        if path.startswith("/users/UID/mailFolders/"):
            name = path.rsplit("/", 1)[1]
            if name in _FOLDERS:
                return httpx.Response(200, json={"id": _FOLDERS[name][0]})
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        if path == "/users/UID/messages":
            since = req.url.params.get("$filter", "").removeprefix("receivedDateTime ge ")
            rows = [
                {"id": self._id(serial, immutable), "parentFolderId": m["folder"],
                 "isRead": True, "subject": f"msg {serial}"}
                for serial, m in sorted(self.messages.items())
                if m["received"] >= since  # ISO-8601 UTC strings order correctly
            ]
            return httpx.Response(200, json={"value": rows})
        if path.endswith("/$value"):
            serial = self._serial(path.split("/")[-2])
            if serial is None:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
            raw = (
                f"From: x@src\r\nTo: adam@src\r\nSubject: msg {serial}\r\n"
                f"Message-ID: <{serial}@src>\r\n"
                "Date: Thu, 19 Nov 2026 11:00:00 +0000\r\n\r\nbody\r\n"
            )
            return httpx.Response(200, content=raw.encode())
        raise AssertionError(f"unexpected source request: {req.url}")


class _Dest:
    """Destination tenant: records every message create."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []  # (folder, subject)

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        return {"id": "D-UID"}

    def paginate(self, path: str, **kw: Any) -> Any:
        yield [{"id": "D-PROJ", "displayName": "Projects"}]

    def post(self, path: str, **kw: Any) -> dict[str, Any]:
        folder = path.split("/mailFolders/")[1].split("/")[0]
        self.created.append((folder, kw["json"]["subject"]))
        return {"id": f"dest-{len(self.created)}"}


class _Tokens:
    def get_token(self) -> str:
        return "tok"


_CFG = Config.model_validate({
    "source": {"type": "microsoft365", "tenant_id": "st", "client_id": "sc", "client_secret": "s"},
    "destination": {"tenant_id": "dt", "client_id": "dc", "client_secret": "s"},
    "users": [{"source_id": "adam@src", "dest_id": "adam@dst"}],
})


def _pass(
    mailbox: _Mailbox, dest: _Dest, mode: str, now: str, monkeypatch: pytest.MonkeyPatch,
    immutable_ids: bool = True,
) -> None:
    """One `migrator mail` run at wall-clock `now` (as this machine sees it)."""
    monkeypatch.setattr(m365, "_now_z", lambda: now)
    gc = GraphClient(
        _Tokens(),  # type: ignore[arg-type]
        immutable_ids=immutable_ids,
        # Not the shared "graph_global" bucket: another test may have registered
        # it at 4 requests/s, which would only slow this file down.
        global_limiter_name="test_delta_sync_unthrottled",
    )
    gc._client.close()
    gc._client = httpx.Client(transport=httpx.MockTransport(mailbox.handler))
    assert isinstance(_CFG.source, Microsoft365SourceConfig)
    with M365Source(_CFG.source, lambda: gc) as source:
        run_mail(JobContext(_USER, source, dest, mode, _CFG))  # type: ignore[arg-type]


def _seeded_mailbox(tmp_path: Path) -> _Mailbox:
    init_db(tmp_path / "state.db")
    mailbox = _Mailbox()
    mailbox.deliver(1, "2026-11-18T09:00:00Z")
    mailbox.deliver(2, "2026-11-18T10:00:00Z")
    mailbox.deliver(3, "2026-11-19T08:00:00Z", folder="F-SENT")
    return mailbox


def test_full_then_delta_migrates_new_mail_once_and_nothing_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox, dest = _seeded_mailbox(tmp_path), _Dest()

    # ── full pass at 12:00 ────────────────────────────────────────────────────
    _pass(mailbox, dest, "full", "2026-11-19T12:00:00Z", monkeypatch)
    assert dest.created == [("inbox", "msg 1"), ("inbox", "msg 2"), ("sentitems", "msg 3")]
    with session_scope() as s:
        assert get_cursor(s, "adam@src", "mail") == "2026-11-19T12:00:00Z"

    # ── the user keeps working: files one message away, receives two more ────
    mailbox.move(2, "F-PROJ")
    mailbox.deliver(4, "2026-11-19T12:30:00Z")
    mailbox.deliver(5, "2026-11-19T14:00:00Z", folder="F-PROJ")
    mailbox.requests.clear()

    # ── delta at 15:00 ───────────────────────────────────────────────────────
    _pass(mailbox, dest, "delta", "2026-11-19T15:00:00Z", monkeypatch)
    assert dest.created[3:] == [("inbox", "msg 4"), ("D-PROJ", "msg 5")]
    assert mailbox.mime_downloads() == ["imm-4", "imm-5"]  # only the new mail was read
    listing = next(r for r in mailbox.requests if r.url.path.endswith("/messages"))
    assert listing.url.params["$filter"] == "receivedDateTime ge 2026-11-19T11:50:00Z"
    with session_scope() as s:
        assert get_cursor(s, "adam@src", "mail") == "2026-11-19T15:00:00Z"

    # ── a second delta with nothing new is a no-op ───────────────────────────
    mailbox.requests.clear()
    _pass(mailbox, dest, "delta", "2026-11-19T16:00:00Z", monkeypatch)
    assert len(dest.created) == 5 and mailbox.mime_downloads() == []

    # ── a full re-run (the catch-all) lists the mailbox, downloads nothing ──
    mailbox.requests.clear()
    _pass(mailbox, dest, "full", "2026-11-19T17:00:00Z", monkeypatch)
    assert len(dest.created) == 5  # the message filed into Projects is not migrated again
    assert mailbox.mime_downloads() == []
    assert all(r.headers["Prefer"] == 'IdType="ImmutableId"' for r in mailbox.requests)


def test_regular_ids_would_duplicate_a_moved_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Why the source client asks for immutable ids: with regular ids, filing a
    # migrated message into another folder gives it a new id, and the next full
    # pass imports it a second time.
    mailbox, dest = _seeded_mailbox(tmp_path), _Dest()
    _pass(mailbox, dest, "full", "2026-11-19T12:00:00Z", monkeypatch, immutable_ids=False)
    mailbox.move(2, "F-PROJ")
    _pass(mailbox, dest, "full", "2026-11-19T13:00:00Z", monkeypatch, immutable_ids=False)
    assert dest.created[3:] == [("D-PROJ", "msg 2")]  # the duplicate


def test_delta_overlap_recovers_mail_a_fast_clock_would_have_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox, dest = _seeded_mailbox(tmp_path), _Dest()
    # This machine's clock is 4 minutes fast: it stamps the cursor 12:00 when
    # Exchange's time is 11:56.
    _pass(mailbox, dest, "full", "2026-11-19T12:00:00Z", monkeypatch)
    # Mail delivered at 11:58 Exchange time: after the full pass listed the
    # mailbox, but "before" the cursor.
    mailbox.deliver(4, "2026-11-19T11:58:00Z")
    _pass(mailbox, dest, "delta", "2026-11-19T15:00:00Z", monkeypatch)
    assert dest.created[3:] == [("inbox", "msg 4")]


def test_orchestrator_source_client_asks_for_immutable_ids_unless_opted_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from migrator.auth.ms_auth import MSTokenProvider

    monkeypatch.setattr(MSTokenProvider, "from_config", classmethod(lambda cls, cfg: _Tokens()))

    def clients(source_extra: dict[str, Any]) -> tuple[GraphClient, GraphClient]:
        cfg = Config.model_validate({
            "state_db": str(tmp_path / "state.db"), "log_file": None,
            "source": {
                "type": "microsoft365", "tenant_id": "st", "client_id": "sc",
                "client_secret": "s", **source_extra,
            },
            "destination": {"tenant_id": "dt", "client_id": "dc", "client_secret": "s"},
            "users": [{"source_id": "adam@src", "dest_id": "adam@dst"}],
        })
        orch = Orchestrator(cfg)
        return orch.source_graph_client(), orch.dest_graph_client()

    src, dst = clients({})
    assert src._headers()["Prefer"] == 'IdType="ImmutableId"'
    assert "Prefer" not in dst._headers()  # destination ids are left as they are
    src, _ = clients({"immutable_ids": False})
    assert "Prefer" not in src._headers()
