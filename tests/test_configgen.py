from __future__ import annotations

from pathlib import Path

import pytest

from migrator.config import Config
from migrator.configgen import (
    ConfigGenError,
    build_config_dict,
    discover_credentials,
    dump_config_yaml,
    parse_mapping_csv,
)


# --------------------------------------------------------------------------- #
# Credential discovery
# --------------------------------------------------------------------------- #
def _touch(p: Path) -> Path:
    p.write_text("x")
    return p


def test_discover_google_single_json_and_pem(tmp_path: Path) -> None:
    d = tmp_path / "credentials"
    d.mkdir()
    _touch(d / "google-service-account.json")
    _touch(d / "ms-cert.pem")
    creds = discover_credentials(d, "google_workspace")
    assert creds.service_account_key == d / "google-service-account.json"
    assert creds.dest_cert == d / "ms-cert.pem"
    assert creds.source_cert is None


def test_discover_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigGenError, match="not found"):
        discover_credentials(tmp_path / "nope", "imap")


def test_discover_imap_needs_only_dest_cert(tmp_path: Path) -> None:
    d = tmp_path / "credentials"
    d.mkdir()
    _touch(d / "ms-cert.pem")
    creds = discover_credentials(d, "imap")
    assert creds.dest_cert == d / "ms-cert.pem"
    assert creds.service_account_key is None


def test_discover_google_ambiguous_json_raises(tmp_path: Path) -> None:
    d = tmp_path / "credentials"
    d.mkdir()
    _touch(d / "a.json")
    _touch(d / "b.json")
    _touch(d / "ms.pem")
    with pytest.raises(ConfigGenError, match="Multiple"):
        discover_credentials(d, "google_workspace")
    # Override resolves the ambiguity.
    creds = discover_credentials(d, "google_workspace", service_account_key=d / "a.json")
    assert creds.service_account_key == d / "a.json"


def test_discover_m365_two_certs_by_keyword(tmp_path: Path) -> None:
    d = tmp_path / "credentials"
    d.mkdir()
    _touch(d / "source-cert.pem")
    _touch(d / "dest-cert.pem")
    creds = discover_credentials(d, "microsoft365")
    assert creds.source_cert == d / "source-cert.pem"
    assert creds.dest_cert == d / "dest-cert.pem"


def test_discover_m365_single_cert_raises(tmp_path: Path) -> None:
    d = tmp_path / "credentials"
    d.mkdir()
    _touch(d / "only.pem")
    with pytest.raises(ConfigGenError, match="two distinct certificates"):
        discover_credentials(d, "microsoft365")


def test_discover_missing_pem_raises(tmp_path: Path) -> None:
    d = tmp_path / "credentials"
    d.mkdir()
    with pytest.raises(ConfigGenError, match="No certificate"):
        discover_credentials(d, "imap")


# --------------------------------------------------------------------------- #
# Mapping CSV parsing
# --------------------------------------------------------------------------- #
def test_parse_mapping_basic(tmp_path: Path) -> None:
    csv_path = tmp_path / "users.csv"
    csv_path.write_text("source_id,dest_id\na@old.com,a@new.com\nb@old.com,b@new.com\n")
    users = parse_mapping_csv(csv_path)
    assert users == [
        {"source_id": "a@old.com", "dest_id": "a@new.com"},
        {"source_id": "b@old.com", "dest_id": "b@new.com"},
    ]


def test_parse_mapping_header_aliases_and_optional(tmp_path: Path) -> None:
    csv_path = tmp_path / "users.csv"
    csv_path.write_text(
        "From,To,imap_user,imap_password_env\n"
        "a@old.com,a@new.com,a@old.com,A_PW\n"
        "b@old.com,b@new.com,,\n"  # empty optional cells dropped
    )
    users = parse_mapping_csv(csv_path)
    assert users[0] == {
        "source_id": "a@old.com",
        "dest_id": "a@new.com",
        "imap_user": "a@old.com",
        "imap_password_env": "A_PW",
    }
    assert users[1] == {"source_id": "b@old.com", "dest_id": "b@new.com"}


def test_parse_mapping_missing_columns_raises(tmp_path: Path) -> None:
    csv_path = tmp_path / "users.csv"
    csv_path.write_text("source_id,foo\na@old.com,bar\n")
    with pytest.raises(ConfigGenError, match="source_id' and 'dest_id'"):
        parse_mapping_csv(csv_path)


def test_parse_mapping_empty_rows_raise(tmp_path: Path) -> None:
    csv_path = tmp_path / "users.csv"
    csv_path.write_text("source_id,dest_id\n")
    with pytest.raises(ConfigGenError, match="no user rows"):
        parse_mapping_csv(csv_path)


# --------------------------------------------------------------------------- #
# build_config_dict — produces a valid Config per source type
# --------------------------------------------------------------------------- #
def _creds(tmp_path: Path, *, json: bool = False, src: bool = False):
    from migrator.configgen import CredentialSet

    cs = CredentialSet(dest_cert=tmp_path / "dest.pem")
    if json:
        cs.service_account_key = tmp_path / "key.json"
    if src:
        cs.source_cert = tmp_path / "src.pem"
    return cs


def test_build_google_config_valid(tmp_path: Path) -> None:
    cfg = build_config_dict(
        source_type="google_workspace",
        creds=_creds(tmp_path, json=True),
        users=[{"source_id": "a@old.com", "dest_id": "a@new.com"}],
        dest_tenant_id="t",
        dest_client_id="c",
        dest_thumbprint="AABB",
        admin_email="admin@old.com",
    )
    model = Config.model_validate(cfg)
    assert model.source.type == "google_workspace"
    assert model.destination.certificate_thumbprint == "AABB"
    # Round-trips through YAML.
    assert "google_workspace" in dump_config_yaml(cfg)


def test_build_google_requires_admin_email(tmp_path: Path) -> None:
    with pytest.raises(ConfigGenError, match="admin-email"):
        build_config_dict(
            source_type="google_workspace",
            creds=_creds(tmp_path, json=True),
            users=[{"source_id": "a", "dest_id": "b"}],
            dest_tenant_id="t",
            dest_client_id="c",
            dest_thumbprint="x",
        )


def test_build_imap_config_valid(tmp_path: Path) -> None:
    cfg = build_config_dict(
        source_type="imap",
        creds=_creds(tmp_path),
        users=[{"source_id": "a@old.com", "dest_id": "a@new.com"}],
        dest_tenant_id="t",
        dest_client_id="c",
        dest_thumbprint="x",
        imap_host="imap.old.com",
        imap_port=143,
        imap_ssl=False,
    )
    model = Config.model_validate(cfg)
    assert model.source.type == "imap"
    assert model.source.port == 143
    assert model.source.use_ssl is False


def test_build_imap_requires_host(tmp_path: Path) -> None:
    with pytest.raises(ConfigGenError, match="imap-host"):
        build_config_dict(
            source_type="imap",
            creds=_creds(tmp_path),
            users=[{"source_id": "a", "dest_id": "b"}],
            dest_tenant_id="t",
            dest_client_id="c",
            dest_thumbprint="x",
        )


def test_build_m365_config_valid(tmp_path: Path) -> None:
    cfg = build_config_dict(
        source_type="microsoft365",
        creds=_creds(tmp_path, src=True),
        users=[{"source_id": "a@src.com", "dest_id": "a@dst.com"}],
        dest_tenant_id="t",
        dest_client_id="c",
        dest_thumbprint="x",
        source_tenant_id="st",
        source_client_id="sc",
        source_thumbprint="sx",
    )
    model = Config.model_validate(cfg)
    assert model.source.type == "microsoft365"
    assert model.source.tenant_id == "st"
    assert model.source.certificate_thumbprint == "sx"


def test_build_m365_requires_source_creds(tmp_path: Path) -> None:
    with pytest.raises(ConfigGenError, match="--source-tenant-id"):
        build_config_dict(
            source_type="microsoft365",
            creds=_creds(tmp_path, src=True),
            users=[{"source_id": "a", "dest_id": "b"}],
            dest_tenant_id="t",
            dest_client_id="c",
            dest_thumbprint="x",
        )


def test_build_unknown_source_type_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigGenError, match="Unknown source type"):
        build_config_dict(
            source_type="pop3",
            creds=_creds(tmp_path),
            users=[{"source_id": "a", "dest_id": "b"}],
            dest_tenant_id="t",
            dest_client_id="c",
            dest_thumbprint="x",
        )
