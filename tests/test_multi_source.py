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
