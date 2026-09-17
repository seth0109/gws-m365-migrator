"""End-to-end mail_job runs against a fake source + fake Graph client.

Covers the job-loop invariants the writers can't test alone: idempotent
reruns, the done-before-extras ordering that prevents duplicate imports,
failure-gated cursor persistence, and unseeded-delta refusal.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from migrator.config import Config, UserMapping
from migrator.connectors.base import BaseSource, SourceMessage
from migrator.context import JobContext
from migrator.state.db import get_cursor, init_db, session_scope
from migrator.state.models import ItemMap
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
        self.patches.append((path, kwargs))


def _config(import_mode: str = "json") -> Config:
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
        "workloads": {"mail": {"import_mode": import_mode}},
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
) -> _MailSource:
    if fresh_db:
        init_db(tmp_path / "state.db")
    source = _MailSource(messages)
    ctx = JobContext(
        user=UserMapping(source_id="a@old.com", dest_id="a@new.com"),
        source=source, dest_gc=gc, mode=mode, config=_config(import_mode),  # type: ignore[arg-type]
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


def test_failed_import_holds_cursor_then_recovers(tmp_path: Path) -> None:
    gc = _DestGC(fail_posts={2})
    _run(tmp_path, [_msg("m1"), _msg("m2")], gc)
    statuses = _item_statuses()
    assert statuses["m1"][0] == "done"
    assert statuses["m2"][0] == "failed"
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "mail") is None  # held back

    healthy = _DestGC()
    _run(tmp_path, [_msg("m1"), _msg("m2")], healthy, fresh_db=False)
    assert len(healthy.posts) == 1  # only the failed item was retried
    assert _item_statuses()["m2"][0] == "done"
    with session_scope() as s:
        assert get_cursor(s, "a@old.com", "mail") == "hist-42"  # clean run advances


def test_unseeded_delta_refuses(tmp_path: Path) -> None:
    gc = _DestGC()
    source = _run(tmp_path, [_msg("m1")], gc, mode="delta")
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


def test_fetch_error_fails_only_that_item_and_holds_cursor(tmp_path: Path) -> None:
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
        assert get_cursor(s, "a@old.com", "mail") is None  # held for the retry
        err = s.execute(select(ItemMap.last_error).where(ItemMap.source_id == "m2")).scalar_one()
    assert err == "503 from source"  # the source error, not a misleading "empty MIME"
