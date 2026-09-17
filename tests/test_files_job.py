"""End-to-end files_job placement against a fake source + fake Graph client.

Neither Drive's files.list nor Graph's delta feed guarantees that a folder is
listed before its children, and a delta pass re-emits only the changed child,
so folder placement must come from FolderMap + deferral rather than the order
items happen to arrive in.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from migrator.config import Config, UserMapping
from migrator.connectors.base import BaseSource, SourceFile
from migrator.context import JobContext
from migrator.state.db import init_db, save_cursor, session_scope
from migrator.state.models import ItemMap
from migrator.workloads.files_job import run_files

_FOLDER_MIME = "application/vnd.google-apps.folder"


class _DriveGC:
    """Records folder creates and uploads; folder ids are derived from the
    parent so a test can read the full placement off the upload URL."""

    def __init__(self, fail_folders: set[str] | None = None) -> None:
        self.uploads: list[str] = []  # "<parent-dest>:/<name>"
        self.folders: list[tuple[str | None, str]] = []  # (parent path segment, name)
        self.root_gets = 0
        self.fail_folders = fail_folders or set()

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if path.endswith("/root"):
            self.root_gets += 1
            return {"id": "ROOT"}
        return {"id": "ms-uid"}  # /users/{id} and /users/{id}/drive

    def paginate(self, path: str, **kwargs: Any) -> Iterator[list[dict[str, Any]]]:
        yield []  # no pre-existing children

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["json"]["name"]
        if name in self.fail_folders:
            raise RuntimeError(f"cannot create {name}")
        parent = path.split("/items/")[1].split("/")[0] if "/items/" in path else "ROOT"
        self.folders.append((parent, name))
        return {"id": f"{parent}>{name}"}

    def put_raw(self, url: str, data: bytes, **kwargs: Any) -> dict[str, Any]:
        self.uploads.append(url.split("/items/")[1].replace(":/content", ""))
        return {"id": f"file:{len(self.uploads)}"}


class _Src(BaseSource):
    capabilities = {"files"}

    def __init__(self, items: list[SourceFile]) -> None:
        super().__init__()
        self.items = items

    def iter_files(self, user: UserMapping, since: str | None) -> Iterator[SourceFile]:
        self._set_cursor("files", "tok-2")
        yield from self.items

    def fetch_file(self, user: UserMapping, f: SourceFile) -> tuple[bytes, str]:
        return b"bytes", f.name


def _folder(fid: str, name: str, parent: str | None = None) -> SourceFile:
    return SourceFile(fid, name, _FOLDER_MIME, parent, True, action="create-folder")


def _file(fid: str, name: str, parent: str | None, h: str = "h") -> SourceFile:
    return SourceFile(fid, name, "application/pdf", parent, False, content_hash=h)


_CFG = Config.model_validate({
    "source": {"type": "imap", "host": "h"},
    "destination": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
    "users": [{"source_id": "a@s", "dest_id": "a@d"}],
})
_USER = UserMapping(source_id="a@s", dest_id="a@d")


def _run(items: list[SourceFile], gc: _DriveGC, mode: str = "full") -> None:
    run_files(JobContext(_USER, _Src(items), gc, mode, _CFG))  # type: ignore[arg-type]


def _statuses() -> dict[str, str]:
    from sqlalchemy import select

    with session_scope() as s:
        return dict(s.execute(select(ItemMap.source_id, ItemMap.status)).all())


# ── tests ─────────────────────────────────────────────────────────────────────


def test_file_listed_before_its_folder_lands_in_the_folder(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    gc = _DriveGC()
    _run([_file("X1", "q3.pdf", "F1"), _folder("F1", "Reports")], gc)
    assert gc.uploads == ["ROOT>Reports:/q3.pdf"]
    assert gc.root_gets == 0  # never fell back to the root


def test_nested_folders_out_of_order_keep_their_hierarchy(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    gc = _DriveGC()
    _run(
        [
            _file("X1", "deep.pdf", "F2"),
            _folder("F2", "2026", parent="F1"),
            _folder("F1", "Reports"),
        ],
        gc,
    )
    assert gc.folders == [("ROOT", "Reports"), ("ROOT>Reports", "2026")]
    assert gc.uploads == ["ROOT>Reports>2026:/deep.pdf"]


def test_delta_new_file_in_previously_migrated_folder(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_folder("F1", "Reports"), _file("X1", "q3.pdf", "F1")], _DriveGC())
    with session_scope() as s:
        save_cursor(s, "a@s", "files", "tok-1")

    # Delta re-emits only the new child; the folder must come from FolderMap.
    gc = _DriveGC()
    _run([_file("X2", "q4.pdf", "F1")], gc, mode="delta")
    assert gc.uploads == ["ROOT>Reports:/q4.pdf"]
    assert gc.folders == []  # no folder re-created


def test_parent_outside_corpus_falls_back_to_root_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    init_db(tmp_path / "s.db")
    gc = _DriveGC()
    with caplog.at_level("WARNING"):
        _run([_file("X1", "shared.pdf", "EXTERNAL")], gc)
    assert gc.uploads == ["ROOT:/shared.pdf"]
    assert any("EXTERNAL" in r.message for r in caplog.records)
    assert _statuses()["X1"] == "done"


def test_orphan_subtree_is_rooted_but_keeps_internal_structure(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    gc = _DriveGC()
    # Folder under an external parent, with its own child: the folder is
    # rooted, the child must still land inside it (not loose at the root).
    _run([_file("X1", "a.pdf", "F1"), _folder("F1", "Sub", parent="EXTERNAL")], gc)
    assert gc.folders == [("ROOT", "Sub")]
    assert gc.uploads == ["ROOT>Sub:/a.pdf"]


def test_failed_folder_fails_its_children_instead_of_rooting_them(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    gc = _DriveGC(fail_folders={"Broken"})
    _run([_folder("F1", "Broken"), _file("X1", "a.pdf", "F1"), _file("X2", "ok.pdf", None)], gc)
    assert gc.uploads == ["ROOT:/ok.pdf"]  # the sibling at the root still migrated
    statuses = _statuses()
    assert statuses["F1"] == "failed"
    assert statuses["X1"] == "failed"
    assert statuses["X2"] == "done"
    with session_scope() as s:
        from migrator.state.db import get_cursor

        assert get_cursor(s, "a@s", "files") is None  # held back for the retry


def test_delta_update_failure_keeps_dest_id(tmp_path: Path) -> None:
    init_db(tmp_path / "s.db")
    _run([_file("X1", "a.pdf", None, h="h1")], _DriveGC())
    with session_scope() as s:
        save_cursor(s, "a@s", "files", "tok-1")

    class _Failing(_DriveGC):
        def put_raw(self, url: str, data: bytes, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("update boom")

    _run([_file("X1", "a.pdf", None, h="h2")], _Failing(), mode="delta")
    from sqlalchemy import select

    with session_scope() as s:
        row = s.execute(select(ItemMap.status, ItemMap.dest_id)).one()
    assert row == ("failed", "file:1")  # exists at the destination — no re-create on retry
