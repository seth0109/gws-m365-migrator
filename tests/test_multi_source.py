from __future__ import annotations

import csv
from pathlib import Path

import pytest

from migrator.config import (
    Config,
    GoogleWorkspaceSourceConfig,
    ImapSourceConfig,
    Microsoft365SourceConfig,
    UserMapping,
)
from migrator.connectors.base import BaseSource
from migrator.connectors.factory import build_source, source_capabilities
from migrator.connectors.google import GoogleWorkspaceSource
from migrator.connectors.imap import ImapSource
from migrator.context import JobContext
from migrator.whatif import CSV_COLUMNS, ManifestWriter


def _base_config(source: dict) -> dict:
    return {
        "source": source,
        "destination": {
            "type": "microsoft365",
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
        },
        "users": [{"source_id": "a@old.com", "dest_id": "a@new.com"}],
    }


# --------------------------------------------------------------------------- #
# Config discrimination
# --------------------------------------------------------------------------- #
def test_source_discriminator_google() -> None:
    cfg = Config.model_validate(
        _base_config({
            "type": "google_workspace",
            "service_account_key_file": "k.json",
            "admin_email": "admin@old.com",
        })
    )
    assert isinstance(cfg.source, GoogleWorkspaceSourceConfig)


def test_source_discriminator_imap() -> None:
    cfg = Config.model_validate(_base_config({"type": "imap", "host": "imap.old.com"}))
    assert isinstance(cfg.source, ImapSourceConfig)
    assert cfg.source.port == 993  # default


def test_source_discriminator_m365() -> None:
    cfg = Config.model_validate(
        _base_config({"type": "microsoft365", "tenant_id": "x", "client_id": "y"})
    )
    assert isinstance(cfg.source, Microsoft365SourceConfig)


def test_imap_password_resolution_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALICE_PW", "secret-from-env")
    user = UserMapping(
        source_id="a@old.com", dest_id="a@new.com",
        imap_user="a@old.com", imap_password_env="ALICE_PW", imap_password="inline",
    )
    assert user.resolve_imap_password() == "secret-from-env"


def test_imap_password_falls_back_to_inline() -> None:
    user = UserMapping(source_id="a", dest_id="b", imap_password="inline")
    assert user.resolve_imap_password() == "inline"


# --------------------------------------------------------------------------- #
# Capabilities + factory dispatch
# --------------------------------------------------------------------------- #
def test_capabilities_per_source_type() -> None:
    google = Config.model_validate(
        _base_config({
            "type": "google_workspace",
            "service_account_key_file": "k.json",
            "admin_email": "admin@old.com",
        })
    )
    imap = Config.model_validate(_base_config({"type": "imap", "host": "h"}))
    m365 = Config.model_validate(
        _base_config({"type": "microsoft365", "tenant_id": "x", "client_id": "y"})
    )
    assert source_capabilities(google) == {"mail", "files", "contacts", "calendar"}
    assert source_capabilities(imap) == {"mail"}
    assert source_capabilities(m365) == {"mail", "files", "contacts", "calendar"}


def test_build_source_dispatch() -> None:
    google = Config.model_validate(
        _base_config({
            "type": "google_workspace",
            "service_account_key_file": "k.json",
            "admin_email": "admin@old.com",
        })
    )
    imap = Config.model_validate(_base_config({"type": "imap", "host": "h"}))
    assert isinstance(build_source(google), GoogleWorkspaceSource)
    assert isinstance(build_source(imap), ImapSource)


def test_build_source_m365_requires_factory() -> None:
    m365 = Config.model_validate(
        _base_config({"type": "microsoft365", "tenant_id": "x", "client_id": "y"})
    )
    with pytest.raises(ValueError, match="source Graph client factory"):
        build_source(m365)


# --------------------------------------------------------------------------- #
# Capability gating in JobContext
# --------------------------------------------------------------------------- #
def test_require_capability_raises_for_unsupported() -> None:
    cfg = Config.model_validate(_base_config({"type": "imap", "host": "h"}))
    assert isinstance(cfg.source, ImapSourceConfig)
    source = ImapSource(cfg.source)
    ctx = JobContext(user=cfg.users[0], source=source, dest_gc=None, mode="full", config=cfg)
    ctx.require_capability("mail")  # supported — no raise
    with pytest.raises(RuntimeError, match="does not support the 'contacts'"):
        ctx.require_capability("contacts")


# --------------------------------------------------------------------------- #
# IMAP folder mapping
# --------------------------------------------------------------------------- #
@pytest.fixture()
def imap_source() -> ImapSource:
    return ImapSource(ImapSourceConfig(host="imap.old.com"))


def test_imap_folder_mapping_special_use(imap_source: ImapSource) -> None:
    assert imap_source._map_folder("Sent Mail", "\\Sent \\HasNoChildren", "/") == "SentItems"
    assert imap_source._map_folder("Bin", "\\Trash", "/") == "DeletedItems"


def test_imap_folder_mapping_name_heuristics(imap_source: ImapSource) -> None:
    assert imap_source._map_folder("INBOX", "\\HasNoChildren", "/") == "Inbox"
    assert imap_source._map_folder("Spam", "", "/") == "JunkEmail"


def test_imap_folder_mapping_preserves_hierarchy(imap_source: ImapSource) -> None:
    # Custom nested folder: IMAP delimiter normalised to Outlook's backslash.
    assert imap_source._map_folder("Clients/AcmeCorp", "", "/") == "Clients\\AcmeCorp"


# --------------------------------------------------------------------------- #
# Whatif manifest columns (source/dest rename)
# --------------------------------------------------------------------------- #
def test_manifest_uses_source_dest_columns(tmp_path: Path) -> None:
    out = tmp_path / "manifest.csv"
    with ManifestWriter(out) as m:
        m.add(source_user="a@old.com", dest_user="a@new.com", workload="mail", source_id="1")
    rows = list(csv.DictReader(out.open()))
    assert "source_user" in CSV_COLUMNS and "dest_user" in CSV_COLUMNS
    assert "user_email" not in CSV_COLUMNS and "ms_upn" not in CSV_COLUMNS
    assert rows[0]["source_user"] == "a@old.com"
    assert rows[0]["dest_user"] == "a@new.com"


def test_base_source_unimplemented_raises() -> None:
    src = BaseSource()
    user = UserMapping(source_id="a", dest_id="b")
    with pytest.raises(NotImplementedError):
        next(src.iter_messages(user, None))
    with pytest.raises(NotImplementedError):
        src.resolve_site_drive("contoso.sharepoint.com:/sites/X")


def test_sharepoint_sites_config_parses() -> None:
    cfg = Config.model_validate({
        **_base_config({"type": "microsoft365", "tenant_id": "x", "client_id": "y"}),
        "sharepoint_sites": [
            {"source_site": "contoso.sharepoint.com:/sites/Marketing",
             "dest_site": "fabrikam.sharepoint.com:/sites/Marketing"},
            {"source_site": "contoso.sharepoint.com:/sites/Eng",
             "target_site_alias": "eng"},
        ],
    })
    assert len(cfg.sharepoint_sites) == 2
    assert cfg.sharepoint_sites[0].dest_site == "fabrikam.sharepoint.com:/sites/Marketing"
    assert cfg.sharepoint_sites[1].target_site_alias == "eng"
    assert cfg.sharepoint_sites[1].dest_site is None


def test_sourcefile_carries_drive_root() -> None:
    from migrator.connectors.base import SourceFile

    f = SourceFile(source_id="1", name="x", mime_type="", parent_id=None, is_folder=False,
                   drive_root="drives/abc")
    assert f.drive_root == "drives/abc"


# --------------------------------------------------------------------------- #
# Delta cursor persistence for the tenant-level SharePoint flows
# --------------------------------------------------------------------------- #
def test_sharepoint_sites_delta_persists_and_reads_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from migrator.state.db import get_cursor, init_db, session_scope
    from migrator.workloads import files_job

    init_db(tmp_path / "state.db")
    monkeypatch.setattr(
        files_job, "resolve_existing_site_drive", lambda gc, site: ("dest-site", "dest-drive")
    )

    cfg = Config.model_validate({
        **_base_config({"type": "microsoft365", "tenant_id": "x", "client_id": "y"}),
        "sharepoint_sites": [
            {"source_site": "contoso.sharepoint.com:/sites/Eng",
             "dest_site": "fabrikam.sharepoint.com:/sites/Eng"},
        ],
    })

    class FakeSite(BaseSource):
        capabilities = {"files"}

        def __init__(self) -> None:
            super().__init__()
            self.seen_since: object = "UNSET"

        def resolve_site_drive(self, site_ref: str) -> tuple[str, str]:
            return "src-site", "src-drive"

        def iter_site_files(self, drive_id, since):  # type: ignore[no-untyped-def]
            self.seen_since = since
            self._set_cursor(f"sharepoint:{drive_id}", "DELTA-LINK-2")
            return iter(())

    sentinel = UserMapping(source_id="__sharepoint__", dest_id="")

    full = FakeSite()
    files_job.run_sharepoint_sites(
        JobContext(user=sentinel, source=full, dest_gc=object(), mode="full", config=cfg)  # type: ignore[arg-type]
    )
    assert full.seen_since is None
    with session_scope() as s:
        assert get_cursor(s, "__sharepoint__", "sharepoint_site:src-site") == "DELTA-LINK-2"

    nxt = FakeSite()
    files_job.run_sharepoint_sites(
        JobContext(user=sentinel, source=nxt, dest_gc=object(), mode="delta", config=cfg)  # type: ignore[arg-type]
    )
    assert nxt.seen_since == "DELTA-LINK-2"


def test_sharepoint_sites_delta_skips_when_no_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from migrator.state.db import init_db
    from migrator.workloads import files_job

    init_db(tmp_path / "state.db")
    monkeypatch.setattr(
        files_job, "resolve_existing_site_drive", lambda gc, site: ("dest-site", "dest-drive")
    )

    cfg = Config.model_validate({
        **_base_config({"type": "microsoft365", "tenant_id": "x", "client_id": "y"}),
        "sharepoint_sites": [
            {"source_site": "contoso.sharepoint.com:/sites/Eng",
             "dest_site": "fabrikam.sharepoint.com:/sites/Eng"},
        ],
    })

    iterated = False

    class FakeSite(BaseSource):
        capabilities = {"files"}

        def resolve_site_drive(self, site_ref: str) -> tuple[str, str]:
            return "src-site", "src-drive"

        def iter_site_files(self, drive_id, since):  # type: ignore[no-untyped-def]
            nonlocal iterated
            iterated = True
            return iter(())

    sentinel = UserMapping(source_id="__sharepoint__", dest_id="")
    files_job.run_sharepoint_sites(
        JobContext(user=sentinel, source=FakeSite(), dest_gc=object(), mode="delta", config=cfg)  # type: ignore[arg-type]
    )
    assert iterated is False  # no seeded cursor → site skipped


# --------------------------------------------------------------------------- #
# Well-known mail folder mapping (system folders → real Outlook folders)
# --------------------------------------------------------------------------- #
def test_resolve_folder_segment_top_level_wellknown() -> None:
    from migrator.microsoft.mail import resolve_folder_segment

    assert resolve_folder_segment("Inbox", is_top_level=True) == "inbox"
    assert resolve_folder_segment("SentItems", is_top_level=True) == "sentitems"
    assert resolve_folder_segment("DeletedItems", is_top_level=True) == "deleteditems"


def test_resolve_folder_segment_only_top_level() -> None:
    from migrator.microsoft.mail import resolve_folder_segment

    # A user folder literally named "Inbox" nested under another folder is custom.
    assert resolve_folder_segment("Inbox", is_top_level=False) is None
    # Unknown names are never well-known, even at top level.
    assert resolve_folder_segment("Clients", is_top_level=True) is None


# --------------------------------------------------------------------------- #
# GraphClient header merge (caller headers override the JSON default)
# --------------------------------------------------------------------------- #
def test_graph_headers_merge_and_override() -> None:
    from migrator.microsoft.graph_client import GraphClient

    class _FakeTP:
        def get_token(self) -> str:
            return "TKN"

    gc = GraphClient(token_provider=_FakeTP())  # type: ignore[arg-type]
    base = gc._headers()
    assert base["Authorization"] == "Bearer TKN"
    assert base["Content-Type"] == "application/json"

    merged = gc._headers({"Content-Type": "text/plain", "Content-Range": "bytes 0-9/10"})
    assert merged["Authorization"] == "Bearer TKN"  # auth preserved
    assert merged["Content-Type"] == "text/plain"   # caller overrides default
    assert merged["Content-Range"] == "bytes 0-9/10"
    gc.close()


# --------------------------------------------------------------------------- #
# Large MIME handling: split attachments + chunked re-upload
# --------------------------------------------------------------------------- #
def _build_mime_with_attachments() -> bytes:
    from email.message import EmailMessage

    em = EmailMessage()
    em["Subject"] = "Big one"
    em["From"] = "a@old.com"
    em["To"] = "b@old.com"
    em.set_content("body text")
    # Inline part must survive stripping (cid image referenced by the body).
    em.add_attachment(
        b"img-bytes", maintype="image", subtype="png", cid="<logo>", disposition="inline"
    )
    em.add_attachment(
        b"X" * (4 * 1024 * 1024), maintype="application", subtype="octet-stream",
        filename="big.bin",
    )
    em.add_attachment(
        b"Y" * 1000, maintype="application", subtype="octet-stream", filename="small.bin"
    )
    return em.as_bytes()


def test_split_large_attachments_strips_only_attachments() -> None:
    from migrator.microsoft.mail import _split_large_attachments

    raw = _build_mime_with_attachments()
    stripped, attachments = _split_large_attachments(raw)

    names = sorted(name for name, _ct, _data in attachments)
    assert names == ["big.bin", "small.bin"]
    assert len(stripped) < len(raw)  # bulk attachments removed
    # Inline image (Content-Disposition: inline) is retained, not extracted.
    assert b"Content-ID" in stripped
    assert b"big.bin" not in stripped and b"small.bin" not in stripped
    big = next(data for name, _ct, data in attachments if name == "big.bin")
    assert len(big) == 4 * 1024 * 1024


class _RecordingGC:
    """Captures Graph calls so import_mime_message branching can be asserted."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.puts: list[tuple[str, int, dict]] = []
        self._n = 0

    def post(self, path: str, user_key: str | None = None, **kw: object) -> dict:
        self.posts.append((path, kw))
        if path.endswith("/createUploadSession"):
            return {"uploadUrl": "https://upload.example/session"}
        self._n += 1
        return {"id": f"msg-{self._n}"}

    def put_raw(self, url: str, data: bytes, user_key: str | None = None, **kw: object) -> dict:
        self.puts.append((url, len(data), kw.get("headers", {})))  # type: ignore[arg-type]
        return {"id": "chunk"}


def test_import_small_message_single_post() -> None:
    from migrator.microsoft.mail import import_mime_message

    gc = _RecordingGC()
    msg_id = import_mime_message(gc, "user-1", "folder-1", b"From: a\r\n\r\nhi")  # type: ignore[arg-type]

    assert msg_id == "msg-1"
    assert len(gc.posts) == 1  # single MIME import, no attachment calls
    assert gc.posts[0][0].endswith("/mailFolders/folder-1/messages")
    assert gc.posts[0][1]["headers"]["Content-Type"] == "text/plain"
    assert gc.puts == []


def test_import_large_message_strips_and_reuploads() -> None:
    from migrator.microsoft.mail import _ATTACHMENT_CHUNK, import_mime_message

    gc = _RecordingGC()
    raw = _build_mime_with_attachments()
    msg_id = import_mime_message(gc, "user-1", "folder-1", raw)  # type: ignore[arg-type]

    assert msg_id == "msg-1"  # returns the imported message id, not an attachment
    paths = [p for p, _ in gc.posts]
    # MIME import first.
    assert paths[0].endswith("/mailFolders/folder-1/messages")
    # Large attachment → upload session; small attachment → single POST.
    assert any(p.endswith("/messages/msg-1/attachments/createUploadSession") for p in paths)
    assert any(p.endswith("/messages/msg-1/attachments") for p in paths)

    # Chunks cover exactly the 4 MB attachment, each within the chunk cap.
    assert sum(size for _u, size, _h in gc.puts) == 4 * 1024 * 1024
    assert all(size <= _ATTACHMENT_CHUNK for _u, size, _h in gc.puts)
    assert all("Content-Range" in headers for _u, _s, headers in gc.puts)


def test_shared_drives_delta_persists_and_reads_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from migrator.connectors.base import SharedDriveRef
    from migrator.state.db import get_cursor, init_db, session_scope
    from migrator.workloads import files_job

    init_db(tmp_path / "state.db")
    monkeypatch.setattr(
        files_job, "ensure_site_for_drive",
        lambda gc, drive_id, alias, display: ("site-1", "lib-drive-1"),
    )

    cfg = Config.model_validate({
        **_base_config({
            "type": "google_workspace",
            "service_account_key_file": "k.json",
            "admin_email": "admin@old.com",
        }),
        "shared_drives": [{"drive_id": "d1", "target_site_alias": "eng"}],
    })

    class FakeDrive(BaseSource):
        capabilities = {"files"}

        def __init__(self) -> None:
            super().__init__()
            self.seen_since: object = "UNSET"

        def list_shared_drives(self, user: UserMapping) -> list[SharedDriveRef]:
            return [SharedDriveRef(drive_id="d1", name="Eng")]

        def iter_shared_drive_files(self, user, drive, since=None):  # type: ignore[no-untyped-def]
            self.seen_since = since
            self._set_cursor(f"shared_drive:{drive.drive_id}", "PAGE-TOKEN-2")
            return iter(())

    impersonation = UserMapping(source_id="admin@old.com", dest_id="")

    full = FakeDrive()
    files_job.run_shared_drives(
        JobContext(user=impersonation, source=full, dest_gc=object(), mode="full", config=cfg)  # type: ignore[arg-type]
    )
    assert full.seen_since is None
    with session_scope() as s:
        assert get_cursor(s, "admin@old.com", "shared_drive:d1") == "PAGE-TOKEN-2"

    nxt = FakeDrive()
    files_job.run_shared_drives(
        JobContext(user=impersonation, source=nxt, dest_gc=object(), mode="delta", config=cfg)  # type: ignore[arg-type]
    )
    assert nxt.seen_since == "PAGE-TOKEN-2"
