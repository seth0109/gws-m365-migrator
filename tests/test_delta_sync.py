"""Full pass → mailbox keeps changing → delta, for a Microsoft 365 source.

The source mailbox here is a small stateful model of Exchange behind a real
GraphClient (so the request headers are the ones that would go on the wire):

- an item's *regular* id changes when it is moved to another folder; its
  immutable id — returned only for requests carrying `Prefer:
  IdType="ImmutableId"` — does not;
- `/messages` honours `$filter=receivedDateTime ge <timestamp>` and reports each
  message's current read state, follow-up flag and categories;
- folders are addressable by well-known name.

What a delta has to guarantee, and what these tests pin: mail that arrived
since the full pass is migrated exactly once; nothing already migrated is
downloaded or imported again (even after the user has filed it elsewhere);
read/flag/category changes made at the source since are replayed onto the
migrated copy and nothing else is touched; a clock that runs ahead cannot open
a gap.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from migrator.config import Config, UserMapping
from migrator.connectors import m365
from migrator.connectors.factory import build_source
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
        self.messages: dict[int, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def deliver(
        self, serial: int, received: str, folder: str = "F-INBOX", read: bool = False
    ) -> None:
        self.messages[serial] = {
            "folder": folder, "received": received, "moves": 0,
            "read": read, "flagged": False, "categories": [],
        }

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

    def listings(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/messages")]

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
                {
                    "id": self._id(serial, immutable), "parentFolderId": m["folder"],
                    "subject": f"msg {serial}", "isRead": m["read"],
                    "flag": {"flagStatus": "flagged" if m["flagged"] else "notFlagged"},
                    "categories": list(m["categories"]),
                }
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
    """Destination tenant: records every message create and every update."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []  # (folder, subject)
        self.created_read: dict[str, bool] = {}  # subject → isRead it was created with
        self.patches: list[tuple[str, dict[str, Any]]] = []  # (dest message id, body)

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        return {"id": "D-UID"}

    def paginate(self, path: str, **kw: Any) -> Any:
        yield [{"id": "D-PROJ", "displayName": "Projects"}]

    def post(self, path: str, **kw: Any) -> dict[str, Any]:
        folder = path.split("/mailFolders/")[1].split("/")[0]
        self.created.append((folder, kw["json"]["subject"]))
        self.created_read[kw["json"]["subject"]] = kw["json"]["isRead"]
        return {"id": f"dest-{len(self.created)}"}

    def patch(self, path: str, **kw: Any) -> None:
        self.patches.append((path.rsplit("/", 1)[1], kw["json"]))


class _Tokens:
    def get_token(self) -> str:
        return "tok"


def _cfg(replay: bool = True, **source_extra: Any) -> Config:
    return Config.model_validate({
        "source": {
            "type": "microsoft365", "tenant_id": "st", "client_id": "sc",
            "client_secret": "s", **source_extra,
        },
        "destination": {"tenant_id": "dt", "client_id": "dc", "client_secret": "s"},
        "users": [{"source_id": "adam@src", "dest_id": "adam@dst"}],
        "workloads": {"mail": {"replay_read_state": replay}},
    })


def _pass(
    mailbox: _Mailbox, dest: _Dest, mode: str, now: str, monkeypatch: pytest.MonkeyPatch,
    immutable_ids: bool = True, replay: bool = True,
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
    cfg = _cfg(replay)
    with build_source(cfg, lambda: gc) as source:  # the connector as the factory wires it
        run_mail(JobContext(_USER, source, dest, mode, cfg))  # type: ignore[arg-type]


def _seeded_mailbox(tmp_path: Path) -> _Mailbox:
    init_db(tmp_path / "state.db")
    mailbox = _Mailbox()
    mailbox.deliver(1, "2026-11-18T09:00:00Z")
    mailbox.deliver(2, "2026-11-18T10:00:00Z")
    mailbox.deliver(3, "2026-11-19T08:00:00Z", folder="F-SENT", read=True)
    return mailbox


def test_full_then_delta_migrates_new_mail_once_and_nothing_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox, dest = _seeded_mailbox(tmp_path), _Dest()

    # ── full pass at 12:00 ────────────────────────────────────────────────────
    _pass(mailbox, dest, "full", "2026-11-19T12:00:00Z", monkeypatch)
    assert dest.created == [("inbox", "msg 1"), ("inbox", "msg 2"), ("sentitems", "msg 3")]
    assert dest.created_read == {"msg 1": False, "msg 2": False, "msg 3": True}
    with session_scope() as s:
        assert get_cursor(s, "adam@src", "mail") == "2026-11-19T12:00:00Z"

    # ── the user keeps working in the old mailbox ────────────────────────────
    mailbox.messages[1]["read"] = True            # reads one
    mailbox.messages[3]["flagged"] = True         # flags another for follow-up
    mailbox.messages[3]["categories"] = ["Customer"]
    mailbox.move(2, "F-PROJ")                     # files one away, still unread
    mailbox.deliver(4, "2026-11-19T12:30:00Z")    # and receives two more
    mailbox.deliver(5, "2026-11-19T14:00:00Z", folder="F-PROJ", read=True)
    mailbox.requests.clear()

    # ── delta at 15:00 ───────────────────────────────────────────────────────
    _pass(mailbox, dest, "delta", "2026-11-19T15:00:00Z", monkeypatch)
    # New mail, once, in the right folders, with its current read state:
    assert dest.created[3:] == [("inbox", "msg 4"), ("D-PROJ", "msg 5")]
    assert dest.created_read["msg 4"] is False and dest.created_read["msg 5"] is True
    assert mailbox.mime_downloads() == ["imm-4", "imm-5"]  # only the new mail was read
    # Changes to mail that was already migrated, and only what changed:
    assert dest.patches == [
        ("dest-1", {"isRead": True}),
        ("dest-3", {"flag": {"flagStatus": "flagged"}, "categories": ["Customer"]}),
    ]  # msg 2 was only moved: its state is the same, so it is not touched
    # State is compared across the whole mailbox, not filtered for by date.
    assert all("$filter" not in r.url.params for r in mailbox.listings())
    with session_scope() as s:
        assert get_cursor(s, "adam@src", "mail") == "2026-11-19T15:00:00Z"

    # ── a second delta with nothing new is a no-op ───────────────────────────
    mailbox.requests.clear()
    dest.patches.clear()
    _pass(mailbox, dest, "delta", "2026-11-19T16:00:00Z", monkeypatch)
    assert len(dest.created) == 5 and dest.patches == [] and mailbox.mime_downloads() == []

    # ── a full re-run (the catch-all) lists the mailbox, downloads nothing ──
    mailbox.requests.clear()
    _pass(mailbox, dest, "full", "2026-11-19T17:00:00Z", monkeypatch)
    assert len(dest.created) == 5  # the message filed into Projects is not migrated again
    assert mailbox.mime_downloads() == [] and dest.patches == []
    assert all(r.headers["Prefer"] == 'IdType="ImmutableId"' for r in mailbox.requests)


def test_after_cutover_a_frozen_source_never_rewrites_the_new_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user now reads and flags mail in the *new* mailbox; the old one no
    # longer changes. Its state still matches what was recorded at migration, so
    # later deltas send nothing — they cannot undo what the user did.
    mailbox, dest = _seeded_mailbox(tmp_path), _Dest()
    _pass(mailbox, dest, "full", "2026-11-19T12:00:00Z", monkeypatch)
    for now in ("2026-11-20T08:00:00Z", "2026-11-21T08:00:00Z"):
        _pass(mailbox, dest, "delta", now, monkeypatch)
    assert dest.patches == [] and len(dest.created) == 3


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


def test_without_replay_the_delta_reads_recent_mail_only_with_an_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # workloads.mail.replay_read_state: false — the cheaper delta that only looks
    # for new mail, filtered by received date from 10 minutes before the cursor.
    mailbox, dest = _seeded_mailbox(tmp_path), _Dest()
    # This machine's clock is 4 minutes fast: it stamps the cursor 12:00 when
    # Exchange's time is 11:56.
    _pass(mailbox, dest, "full", "2026-11-19T12:00:00Z", monkeypatch, replay=False)
    # Mail delivered at 11:58 Exchange time: after the full pass listed the
    # mailbox, but "before" the cursor. And the user reads an old message.
    mailbox.deliver(4, "2026-11-19T11:58:00Z")
    mailbox.messages[1]["read"] = True
    mailbox.requests.clear()
    _pass(mailbox, dest, "delta", "2026-11-19T15:00:00Z", monkeypatch, replay=False)
    assert dest.created[3:] == [("inbox", "msg 4")]  # the overlap caught it
    assert dest.patches == []  # read state is not replayed in this mode
    (listing,) = mailbox.listings()
    assert listing.url.params["$filter"] == "receivedDateTime ge 2026-11-19T11:50:00Z"


def test_orchestrator_source_client_asks_for_immutable_ids_unless_opted_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from migrator.auth.ms_auth import MSTokenProvider

    monkeypatch.setattr(MSTokenProvider, "from_config", classmethod(lambda cls, cfg: _Tokens()))

    def clients(**source_extra: Any) -> tuple[GraphClient, GraphClient]:
        cfg = _cfg(**source_extra).model_copy(
            update={"state_db": tmp_path / "state.db", "log_file": None}
        )
        orch = Orchestrator(cfg)
        return orch.source_graph_client(), orch.dest_graph_client()

    src, dst = clients()
    assert src._headers()["Prefer"] == 'IdType="ImmutableId"'
    assert "Prefer" not in dst._headers()  # destination ids are left as they are
    src, _ = clients(immutable_ids=False)
    assert "Prefer" not in src._headers()
