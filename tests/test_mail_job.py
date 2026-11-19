"""End-to-end mail_job runs against a fake source + fake Graph client.

Covers the job-loop invariants the writers can't test alone: idempotent
reruns, the done-before-extras ordering that prevents duplicate imports,
cursor seeding and holding around failures, unseeded-delta refusal, and lazy
message bodies.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from migrator.config import Config, UserMapping
from migrator.connectors.base import BaseSource, SourceMessage, mail_state_hash
from migrator.context import JobContext
from migrator.state.db import get_cursor, init_db, session_scope
from migrator.state.models import ItemMap
from migrator.workloads.cursors import NoDeltaCursor
from migrator.workloads.mail_job import run_mail

# ── fakes ─────────────────────────────────────────────────────────────────────


class _MailSource(BaseSource):
    capabilities = {"mail"}

    def __init__(self, messages: list[SourceMessage], cursor: str = "hist-42") -> None:
        super().__init__()
        self.messages = messages
        self.cursor = cursor
        self.last_since: str | None = "UNSET"

    def iter_messages(self, user: UserMapping, since: str | None) -> Iterator[SourceMessage]:
        self.last_since = since
        self._set_cursor("mail", self.cursor)
        yield from self.messages


class _DestGC:
    """Records message creates/patches; can fail selected post ordinals or
    every flags patch."""

    def __init__(self, fail_posts: set[int] | None = None, fail_patches: bool = False) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.post_count = 0
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.fail_posts = fail_posts or set()
        self.fail_patches = fail_patches

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return {"id": "ms-uid"}

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.post_count += 1
        if self.post_count in self.fail_posts:
            raise RuntimeError(f"post #{self.post_count} boom")
        self.posts.append((path, kwargs))
        return {"id": f"dest-{self.post_count}"}

    def patch(self, path: str, **kwargs: Any) -> None:
        if self.fail_patches:
            raise RuntimeError("patch boom")
        if self.patch_status:
            req = httpx.Request("PATCH", f"https://graph.microsoft.com/v1.0{path}")
            raise httpx.HTTPStatusError(
                "boom", request=req, response=httpx.Response(self.patch_status, request=req)
            )
        self.patches.append((path, kwargs))

    patch_status = 0  # set to an HTTP status to make every PATCH fail with it


def _config(import_mode: str = "json", replay: bool = True) -> Config:
    return Config.model_validate({
        "source": {
            "type": "google_workspace",
            "service_account_key_file": "k.json",
            "admin_email": "admin@old.com",
        },
        "destination": {
            "type": "microsoft365", "tenant_id": "t", "client_id": "c", "client_secret": "s",
        },
        "users": [{"source_id": "a@old.com", "dest_id": "a@new.com"}],
        "workloads": {"mail": {"import_mode": import_mode, "replay_read_state": replay}},
    })


def _msg(msg_id: str, unread: bool = False) -> SourceMessage:
    raw = (
        f"From: x@old.com\r\nTo: a@old.com\r\nSubject: {msg_id}\r\n"
        f"Date: Tue, 09 Dec 2025 13:42:55 +0000\r\n\r\nbody\r\n"
    ).encode()
    return SourceMessage(
        source_id=msg_id, raw_mime=raw, folder_paths=["Inbox"], is_read=not unread
    )


def _run(
    tmp_path: Path,
    messages: list[SourceMessage],
    gc: _DestGC,
    mode: str = "full",
    import_mode: str = "json",
    fresh_db: bool = True,
    cursor: str = "hist-42",
    replay: bool = True,
) -> _MailSource:
    if fresh_db:
        init_db(tmp_path / "state.db")
    source = _MailSource(messages, cursor)
    ctx = JobContext(
        user=UserMapping(source_id="a@old.com", dest_id="a@new.com"),
        source=source, dest_gc=gc, mode=mode,  # type: ignore[arg-type]
        config=_config(import_mode, replay),
    )
    run_mail(ctx)
    return source


def _item_statuses() -> dict[str, tuple[str, str | None]]:
    from sqlalchemy import select

    with session_scope() as s:
        rows = s.execute(select(ItemMap.source_id, ItemMap.status, ItemMap.dest_id)).all()
    return {r[0]: (r[1], r[2]) for r in rows}


# ── tests ─────────────────────────────────────────────────────────────────────


def test_full_run_imports_marks_done_and_saves_cursor(tmp_path: Path) -> None:
    gc = _DestGC()
    _run(tmp_path, [_msg("m1"), _msg("m2", unread=True)], gc)
    assert len(gc.posts) == 2
    statuses = _item_statuses()
    assert statuses["m1"] == ("done", "dest-1")
    assert statuses["m2"] == ("done", "dest-2")
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "mail") == "hist-42"
    # json mode carries flags on the create — no separate PATCH round-trips
    assert gc.patches == []
    assert gc.posts[1][1]["json"]["isRead"] is False


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    gc = _DestGC()
    _run(tmp_path, [_msg("m1")], gc)
    _run(tmp_path, [_msg("m1")], gc, fresh_db=False)
    assert len(gc.posts) == 1  # second run skipped the done item


def test_mime_patch_failure_never_duplicates_on_rerun(tmp_path: Path) -> None:
    gc = _DestGC(fail_patches=True)
    _run(tmp_path, [_msg("m1")], gc, import_mode="mime")
    # import succeeded, flags patch failed -> still done (patch is best-effort)
    assert _item_statuses()["m1"][0] == "done"
    _run(tmp_path, [_msg("m1")], gc, import_mode="mime", fresh_db=False)
    assert len(gc.posts) == 1  # no duplicate second import


def test_failure_in_the_first_full_pass_still_seeds_the_cursor(tmp_path: Path) -> None:
    # One message that keeps failing must not make delta passes impossible: the
    # first full pass seeds the cursor, the failed row stays failed, and a full
    # re-run retries exactly that item.
    gc = _DestGC(fail_posts={2})
    _run(tmp_path, [_msg("m1"), _msg("m2")], gc)
    statuses = _item_statuses()
    assert statuses["m1"][0] == "done"
    assert statuses["m2"][0] == "failed"
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "mail") == "hist-42"

    healthy = _DestGC()
    _run(tmp_path, [_msg("m1"), _msg("m2")], healthy, fresh_db=False, cursor="hist-50")
    assert len(healthy.posts) == 1  # only the failed item was retried
    assert _item_statuses()["m2"][0] == "done"
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "mail") == "hist-50"  # clean run advances


def test_failure_never_advances_an_existing_cursor(tmp_path: Path) -> None:
    # With a cursor stored, a failed item holds it — in a delta and in a full
    # re-run alike — so the item stays inside the next delta's window.
    _run(tmp_path, [_msg("m1")], _DestGC())  # clean first pass: cursor hist-42
    for mode in ("delta", "full"):
        gc = _DestGC(fail_posts={1})
        _run(tmp_path, [_msg("m1"), _msg("m2")], gc, mode=mode, fresh_db=False, cursor="hist-50")
        assert _item_statuses()["m2"][0] == "failed"
        with session_scope() as s:
            assert get_cursor(s, "a@old.com", "mail") == "hist-42"  # held back

    _run(tmp_path, [_msg("m2")], _DestGC(), mode="delta", fresh_db=False, cursor="hist-51")
    assert _item_statuses()["m2"][0] == "done"
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "mail") == "hist-51"


def test_unseeded_delta_fails_loudly_and_touches_nothing(tmp_path: Path) -> None:
    # A delta with no cursor used to warn and return: exit 0 with new mail left
    # behind. It is an error the operator has to see.
    gc = _DestGC()
    init_db(tmp_path / "state.db")
    source = _MailSource([_msg("m1")])
    ctx = JobContext(
        user=UserMapping(source_id="a@old.com", dest_id="a@new.com"),
        source=source, dest_gc=gc, mode="delta", config=_config(),  # type: ignore[arg-type]
    )
    with pytest.raises(NoDeltaCursor, match="migrator mail --user a@old.com"):
        run_mail(ctx)
    assert gc.posts == []
    assert source.last_since == "UNSET"  # iter_messages never called


def test_seeded_delta_passes_cursor(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    from migrator.state.db import save_cursor

    with session_scope() as s:
        save_cursor(s, "a@old.com", "mail", "hist-40")
    gc = _DestGC()
    source = _run(tmp_path, [_msg("m1")], gc, mode="delta", fresh_db=False)
    assert source.last_since == "hist-40"
    assert len(gc.posts) == 1


def test_fetch_error_fails_only_that_item(tmp_path: Path) -> None:
    from sqlalchemy import select

    gc = _DestGC()
    unreadable = SourceMessage(
        source_id="m2", raw_mime=b"", folder_paths=["Inbox"], fetch_error="503 from source"
    )
    _run(tmp_path, [_msg("m1"), unreadable, _msg("m3")], gc)
    assert len(gc.posts) == 2  # m1 and m3 imported; m2 never posted
    statuses = _item_statuses()
    assert statuses["m1"][0] == "done"
    assert statuses["m2"][0] == "failed"
    assert statuses["m3"][0] == "done"
    with session_scope() as s:
        err = s.execute(select(ItemMap.last_error).where(ItemMap.source_id == "m2")).scalar_one()
    assert err == "503 from source"  # the source error, not a misleading "empty MIME"


# ── lazy message bodies ──────────────────────────────────────────────────────


def _lazy(msg_id: str, loads: list[str], result: bytes | None | Exception) -> SourceMessage:
    """A message whose MIME is read on demand, recording each read."""

    def load() -> bytes | None:
        loads.append(msg_id)
        if isinstance(result, Exception):
            raise result
        return result

    return SourceMessage(source_id=msg_id, raw_mime=b"", folder_paths=["Inbox"], load_mime=load)


def test_lazy_body_is_read_only_for_messages_that_get_imported(tmp_path: Path) -> None:
    loads: list[str] = []
    raw = _msg("x").raw_mime
    messages = [_lazy("m1", loads, raw), _lazy("m2", loads, raw)]

    gc = _DestGC()
    _run(tmp_path, messages, gc)
    assert loads == ["m1", "m2"] and len(gc.posts) == 2

    # Re-run: both are done, so nothing is downloaded from the source again.
    loads.clear()
    _run(tmp_path, messages + [_lazy("m3", loads, raw)], gc, fresh_db=False)
    assert loads == ["m3"] and len(gc.posts) == 3


def test_lazy_body_vanished_is_skipped_and_unreadable_fails_only_that_item(
    tmp_path: Path,
) -> None:
    from sqlalchemy import select

    loads: list[str] = []
    raw = _msg("x").raw_mime
    gc = _DestGC()
    _run(tmp_path, [
        _lazy("gone", loads, None),                       # deleted since the listing
        _lazy("bad", loads, RuntimeError("503: ServiceUnavailable")),
        _lazy("ok", loads, raw),
    ], gc)
    assert len(gc.posts) == 1
    statuses = _item_statuses()
    assert "gone" not in statuses            # nothing to migrate, nothing recorded
    assert statuses["bad"][0] == "failed"
    assert statuses["ok"][0] == "done"
    with session_scope() as s:
        err = s.execute(select(ItemMap.last_error).where(ItemMap.source_id == "bad")).scalar_one()
    assert err == "503: ServiceUnavailable"


# ── delta replays read/flag/category changes onto migrated mail ──────────────


def _stateful(
    msg_id: str, *, read: bool = True, flagged: bool = False, categories: tuple[str, ...] = ()
) -> SourceMessage:
    """A message as a state-reporting connector (the M365 source) emits it."""
    m = _msg(msg_id, unread=not read)
    m.is_flagged = flagged
    m.categories = list(categories)
    m.state_hash = mail_state_hash(read, flagged, list(categories))
    return m


def _patch_bodies(gc: _DestGC) -> dict[str, dict[str, Any]]:
    return {path.rsplit("/", 1)[1]: kw["json"] for path, kw in gc.patches}


def test_delta_replays_only_what_changed_at_the_source(tmp_path: Path) -> None:
    gc = _DestGC()
    _run(tmp_path, [
        _stateful("m1", read=False),
        _stateful("m2", flagged=True),
        _stateful("m3", categories=("Red",)),
    ], gc)
    assert gc.patches == []  # json import carries the state on the create

    # Since the full pass the user read m1, cleared m2's flag and re-categorised
    # it, and left m3 alone.
    after = [
        _stateful("m1", read=True),
        _stateful("m2", flagged=False, categories=("Blue",)),
        _stateful("m3", categories=("Red",)),
    ]
    _run(tmp_path, after, gc, mode="delta", fresh_db=False)
    assert len(gc.posts) == 3  # nothing re-imported
    assert _patch_bodies(gc) == {
        "dest-1": {"isRead": True},  # only the aspect that changed is sent
        "dest-2": {"flag": {"flagStatus": "notFlagged"}, "categories": ["Blue"]},
    }
    assert all(p.startswith("/users/ms-uid/messages/") for p, _ in gc.patches)

    # The new state is recorded: the same listing again changes nothing.
    gc.patches.clear()
    _run(tmp_path, after, gc, mode="delta", fresh_db=False)
    assert gc.patches == []


def test_unchanged_source_never_overwrites_the_new_mailbox(tmp_path: Path) -> None:
    # After cutover the user works in the new mailbox. The source is frozen, so
    # its state equals what was recorded and no PATCH can clobber their changes.
    gc = _DestGC()
    _run(tmp_path, [_stateful("m1", read=False)], gc)
    for _ in range(2):
        _run(tmp_path, [_stateful("m1", read=False)], gc, mode="delta", fresh_db=False)
    assert gc.patches == []


def test_full_rerun_and_disabled_replay_leave_migrated_mail_alone(tmp_path: Path) -> None:
    gc = _DestGC()
    _run(tmp_path, [_stateful("m1", read=False)], gc)
    changed = [_stateful("m1", read=True)]
    _run(tmp_path, changed, gc, mode="full", fresh_db=False)  # full pass: pure skip
    _run(tmp_path, changed, gc, mode="delta", fresh_db=False, replay=False)
    assert gc.patches == [] and len(gc.posts) == 1


def test_copy_moved_or_deleted_in_the_new_mailbox_is_left_alone(tmp_path: Path) -> None:
    gc = _DestGC()
    _run(tmp_path, [_stateful("m1", read=False)], gc)
    gc.patch_status = 404  # the user moved/deleted it there: its id is gone
    _run(tmp_path, [_stateful("m1", read=True)], gc, mode="delta", fresh_db=False,
         cursor="hist-50")
    assert _item_statuses()["m1"] == ("done", "dest-1")  # not a failure
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "mail") == "hist-50"

    # ...and it is not attempted again on every later delta.
    gc.patch_status = 0
    _run(tmp_path, [_stateful("m1", read=True)], gc, mode="delta", fresh_db=False)
    assert gc.patches == []


def test_replay_error_is_retried_by_the_next_delta_and_never_fails_the_run(
    tmp_path: Path,
) -> None:
    gc = _DestGC()
    _run(tmp_path, [_stateful("m1", read=False)], gc)
    gc.patch_status = 503
    _run(tmp_path, [_stateful("m1", read=True)], gc, mode="delta", fresh_db=False,
         cursor="hist-50")
    assert _item_statuses()["m1"] == ("done", "dest-1")
    with session_scope() as s:
        # A fidelity extra never holds the cursor back.
        assert get_cursor(s, "a@old.com", "mail") == "hist-50"

    gc.patch_status = 0
    _run(tmp_path, [_stateful("m1", read=True)], gc, mode="delta", fresh_db=False)
    assert _patch_bodies(gc) == {"dest-1": {"isRead": True}}


def test_row_recorded_before_state_tracking_gets_the_whole_state_once(tmp_path: Path) -> None:
    gc = _DestGC()
    legacy = _msg("m1")
    legacy.dedup_hash = "<m1@old.com>"  # what such rows hold: a Message-ID
    _run(tmp_path, [legacy], gc)

    now = [_stateful("m1", read=True, flagged=True)]
    _run(tmp_path, now, gc, mode="delta", fresh_db=False)
    assert _patch_bodies(gc) == {
        "dest-1": {"isRead": True, "flag": {"flagStatus": "flagged"}, "categories": []},
    }
    gc.patches.clear()
    _run(tmp_path, now, gc, mode="delta", fresh_db=False)
    assert gc.patches == []
