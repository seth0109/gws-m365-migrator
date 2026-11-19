"""Shared-mailbox mappings (`users[].mailbox_type: shared`).

Graph addresses a shared mailbox exactly like a user mailbox (/users/{upn}), so
mail and contacts migrate through the unchanged jobs. The one difference is
that a shared mailbox is an unlicensed account with no OneDrive: the files
workload must skip it outright instead of waiting ~2.5 minutes for a drive that
will never be provisioned and then failing the run.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from migrator.config import Config, UserMapping
from migrator.connectors.base import BaseSource, SourceContact, SourceFile, SourceMessage
from migrator.context import JobContext
from migrator.reporting import generate_report
from migrator.state.db import init_db, session_scope
from migrator.state.models import ItemMap
from migrator.workloads.contacts_job import run_contacts
from migrator.workloads.files_job import run_files
from migrator.workloads.mail_job import run_mail

_SHARED = UserMapping(source_id="info@old.com", dest_id="info@new.com", mailbox_type="shared")


def _config(db: Path | None = None) -> Config:
    raw: dict[str, Any] = {
        "source": {"type": "imap", "host": "h"},
        "destination": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        "users": [
            {"source_id": "a@old.com", "dest_id": "a@new.com"},
            {"source_id": "info@old.com", "dest_id": "info@new.com", "mailbox_type": "shared"},
        ],
    }
    if db is not None:
        raw["state_db"] = str(db)
    return Config.model_validate(raw)


class _Src(BaseSource):
    capabilities = {"mail", "contacts", "files"}

    def __init__(self) -> None:
        super().__init__()
        self.file_reads = 0

    def iter_messages(self, user: UserMapping, since: str | None) -> Iterator[SourceMessage]:
        raw = b"From: x@old.com\r\nTo: info@old.com\r\nSubject: hi\r\n\r\nbody\r\n"
        yield SourceMessage(source_id="m1", raw_mime=raw, folder_paths=["Inbox"])

    def iter_contacts(self, user: UserMapping, since: str | None) -> Iterator[SourceContact]:
        yield SourceContact(source_id="c1", graph_body={"displayName": "Ann"})

    def iter_files(self, user: UserMapping, since: str | None) -> Iterator[SourceFile]:
        self.file_reads += 1
        yield SourceFile("f1", "a.pdf", "application/pdf", None, False)

    def inventory_files(self, user: UserMapping) -> Iterator[SourceFile]:
        self.file_reads += 1
        yield SourceFile("f1", "a.pdf", "application/pdf", None, False)

    def fetch_file(self, user: UserMapping, f: SourceFile) -> tuple[bytes, str]:
        return b"bytes", f.name


class _GC:
    """Records every call so a test can assert on what reached the destination."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("GET", path))
        return {"id": "shared-mbx-id"}

    def paginate(self, path: str, **kw: Any) -> Iterator[list[dict[str, Any]]]:
        self.calls.append(("LIST", path))
        yield [{"id": "folder-1", "displayName": "Imported Contacts"}]

    def post(self, path: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("POST", path))
        return {"id": f"dest-{len(self.calls)}"}

    def put_raw(self, url: str, data: bytes, **kw: Any) -> dict[str, Any]:
        self.calls.append(("PUT", url))
        return {"id": f"dest-{len(self.calls)}"}


def _ctx(gc: _GC | None, mode: str = "full", src: _Src | None = None) -> JobContext:
    return JobContext(_SHARED, src or _Src(), gc, mode, _config())  # type: ignore[arg-type]


def _done(workload: str) -> list[str]:
    from sqlalchemy import select

    with session_scope() as s:
        return list(s.execute(
            select(ItemMap.source_id).where(
                ItemMap.user_email == "info@old.com",
                ItemMap.workload == workload,
                ItemMap.status == "done",
            )
        ).scalars())


# ── config ────────────────────────────────────────────────────────────────────


def test_mailbox_type_defaults_to_user_and_normalizes() -> None:
    assert UserMapping(source_id="a", dest_id="b").mailbox_type == "user"
    assert UserMapping(source_id="a", dest_id="b", mailbox_type=" Shared ").mailbox_type == "shared"  # type: ignore[arg-type]
    assert _config().users[1].mailbox_type == "shared"


def test_unknown_mailbox_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        UserMapping(source_id="a", dest_id="b", mailbox_type="room")  # type: ignore[arg-type]


# ── mail + contacts go through the normal mailbox path ───────────────────────


def test_mail_migrates_into_the_shared_mailbox(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    gc = _GC()
    run_mail(_ctx(gc))
    assert ("GET", "/users/info@new.com") in gc.calls  # resolved like any mailbox
    assert ("POST", "/users/shared-mbx-id/mailFolders/inbox/messages") in gc.calls
    assert _done("mail") == ["m1"]


def test_contacts_migrate_into_the_shared_mailbox(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    gc = _GC()
    run_contacts(_ctx(gc))
    assert ("POST", "/users/shared-mbx-id/contactFolders/folder-1/contacts") in gc.calls
    assert _done("contacts") == ["c1"]


# ── files are skipped: there is no OneDrive ──────────────────────────────────


@pytest.mark.parametrize("mode", ["full", "delta"])
def test_files_workload_skips_a_shared_mailbox(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    init_db(tmp_path / "state.db")
    gc, src = _GC(), _Src()
    with caplog.at_level("WARNING"):
        run_files(_ctx(gc, mode, src))
    assert gc.calls == []  # no /users lookup, no OneDrive provisioning poll, no uploads
    assert src.file_reads == 0
    assert _done("files") == []
    assert any(
        "shared mailbox" in r.message and "info@new.com" in r.message for r in caplog.records
    )


def test_whatif_skips_files_for_a_shared_mailbox(tmp_path: Path) -> None:
    init_db(tmp_path / "state.db")
    src = _Src()
    # No ManifestWriter is installed: reaching the inventory branch would assert.
    run_files(_ctx(None, "whatif", src))
    assert src.file_reads == 0


def test_files_still_run_for_a_regular_user(tmp_path: Path) -> None:
    # Control: the same source and fake client do upload for a user mailbox.
    init_db(tmp_path / "state.db")
    gc, src = _GC(), _Src()
    user = UserMapping(source_id="a@old.com", dest_id="a@new.com")
    run_files(JobContext(user, src, gc, "full", _config()))  # type: ignore[arg-type]
    assert ("GET", "/users/a@new.com") in gc.calls
    assert src.file_reads == 1
    assert any(method == "PUT" and ":/a.pdf:/content" in url for method, url in gc.calls)


# ── validation report ────────────────────────────────────────────────────────


def test_report_does_not_expect_a_files_row_for_a_shared_mailbox(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    out = tmp_path / "report.html"
    generate_report(_config(db), out)
    html = out.read_text(encoding="utf-8")
    assert "<td>a@old.com</td><td>files</td>" in html
    assert "<td>info@old.com</td><td>mail</td>" in html
    assert "<td>info@old.com</td><td>contacts</td>" in html
    assert "<td>info@old.com</td><td>files</td>" not in html
