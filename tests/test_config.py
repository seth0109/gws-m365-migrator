"""Config loading must be locale-independent: YAML is UTF-8 by spec, and a
Windows default of cp1252 would garble or reject non-ASCII values."""
from __future__ import annotations

from pathlib import Path

from migrator.config import load_config

_YAML = """
# Überschrift → nicht ASCII
source:
  type: imap
  host: imap.example
destination:
  tenant_id: t
  client_id: c
  client_secret: s
users:
  - source_id: a@old.example
    dest_id: a@new.example
shared_drives:
  - drive_name: "Übergröße → Marketing"
    target_site_alias: marketing
"""


def test_load_config_reads_utf8_regardless_of_locale(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(_YAML.encode("utf-8"))
    cfg = load_config(path)
    assert cfg.shared_drives[0].drive_name == "Übergröße → Marketing"


def test_example_config_parses(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.source.type == "google_workspace"
    assert len(cfg.users) == 2
    # Template must stay ASCII so it parses under any tooling/locale.
    assert example.read_bytes().isascii()


# ── commented alternative blocks in the template ─────────────────────────────

import re  # noqa: E402

import yaml  # noqa: E402

from migrator.config import Config  # noqa: E402

_EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"
_YAML_LINE = re.compile(r"^\s*(-\s|[A-Za-z_][A-Za-z0-9_]*:(\s|$)|#|$)")


def _uncommented(start: str, end: str, strip: str = "# ") -> object:
    """Uncomment the block between two markers, dropping prose comment lines,
    and parse it as YAML — this is what a user does when they pick that option."""
    text = _EXAMPLE.read_text(encoding="utf-8")
    seg = text.split(start, 1)[1].split(end, 1)[0]
    lines = [ln[len(strip):] for ln in seg.splitlines() if ln.startswith(strip)]
    return yaml.safe_load("\n".join(ln for ln in lines if _YAML_LINE.match(ln)))


def test_example_imap_block_parses() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    imap = _uncommented("* Option 2", "* Option 3")
    assert isinstance(imap, dict) and imap["source"]["type"] == "imap"
    users = _uncommented("# IMAP source only", "# -- SHARED DRIVES", strip="  # ")
    cfg = Config.model_validate({**raw, "source": imap["source"], "users": users})
    assert cfg.users[0].imap_password_env == "CAROL_IMAP_PW"
    assert cfg.source.type == "imap" and cfg.source.exclude_folders == ["Public Folders", "Calendar"]


def test_example_m365_and_sharepoint_blocks_parse() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    m365 = _uncommented("* Option 3", "# -- DESTINATION")
    assert isinstance(m365, dict) and m365["source"]["type"] == "microsoft365"
    sites = _uncommented("# -- SHAREPOINT SITES", "# -- WORKLOADS")
    assert isinstance(sites, dict)
    cfg = Config.model_validate(
        {**raw, "source": m365["source"], "sharepoint_sites": sites["sharepoint_sites"]}
    )
    assert len(cfg.sharepoint_sites) == 2
    assert cfg.sharepoint_sites[0].dest_site and cfg.sharepoint_sites[1].target_site_alias


def test_example_documents_cloud_and_shared_mailbox() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    assert raw["destination"]["cloud"] == "global"  # the field is shown, at its default
    m365 = _uncommented("* Option 3", "# -- DESTINATION")
    assert isinstance(m365, dict) and m365["source"]["cloud"] == "global"
    shared = _uncommented("# Shared mailbox", "# IMAP source only", strip="  # ")
    cfg = Config.model_validate({**raw, "users": shared})
    assert cfg.users[0].mailbox_type == "shared"


def test_example_users_csv_parses() -> None:
    from migrator.configgen import parse_mapping_csv

    users = parse_mapping_csv(_EXAMPLE.with_name("users.example.csv"))
    assert [u.get("mailbox_type", "user") for u in users] == ["user", "user", "shared"]


# ── certificate thumbprint ───────────────────────────────────────────────────
# MSAL hex-decodes the thumbprint as given. One pasted with the separators the
# tools print (59 characters with colons) used to load fine and then fail the
# first token request with a bare `binascii.Error: Odd-length string`.

import binascii  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from migrator.auth import ms_auth  # noqa: E402
from migrator.auth.ms_auth import MSTokenProvider  # noqa: E402

_TP = "AABBCCDDEEFF00112233445566778899AABBCCDD"
_PAIRS = [_TP[i:i + 2] for i in range(0, 40, 2)]


def _m365(thumbprint: object, **extra: Any) -> dict[str, Any]:
    return {
        "tenant_id": "t", "client_id": "c", "certificate_path": "key.pem",
        "certificate_thumbprint": thumbprint, **extra,
    }


def _thumbprint_config(source_tp: object, dest_tp: object = _TP) -> Config:
    return Config.model_validate({
        "source": _m365(source_tp, type="microsoft365"),
        "destination": _m365(dest_tp, cloud="gcc_high"),
        "users": [{"source_id": "a@src", "dest_id": "a@dst"}],
    })


@pytest.mark.parametrize(
    "pasted",
    [
        _TP,
        _TP.lower(),
        ":".join(_PAIRS),                           # openssl x509 -fingerprint
        "sha1 Fingerprint=" + ":".join(_PAIRS),     # ...the whole output line
        " ".join(_PAIRS).lower(),                   # Windows certificate dialog
        "‎" + " ".join(_PAIRS) + "\n",         # ...with its invisible LRM mark
        "-".join(_PAIRS),
    ],
)
def test_thumbprint_is_reduced_to_bare_hex(pasted: str) -> None:
    cfg = _thumbprint_config(pasted, pasted)
    assert cfg.source.type == "microsoft365"
    for block in (cfg.source, cfg.destination):
        assert block.certificate_thumbprint == _TP
        assert len(binascii.a2b_hex(block.certificate_thumbprint)) == 20  # what MSAL does


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (_TP[:-1], "40 hex digits, got 39"),          # dropped a character
        (_TP + "0", "40 hex digits, got 41"),
        ("AB" * 32, "SHA-256"),                       # -sha256 fingerprint
        ("thumbprint-goes-here", "must be hex digits"),
    ],
)
def test_bad_thumbprint_is_rejected_at_load_naming_the_block(bad: str, message: str) -> None:
    with pytest.raises(ValidationError) as dest_err:
        _thumbprint_config(_TP, bad)
    assert message in str(dest_err.value)
    assert dest_err.value.errors()[0]["loc"] == ("destination", "certificate_thumbprint")

    with pytest.raises(ValidationError) as src_err:
        _thumbprint_config(bad)
    assert src_err.value.errors()[0]["loc"] == (
        "source", "microsoft365", "certificate_thumbprint"
    )


def test_blank_thumbprint_means_not_set() -> None:
    cfg = Config.model_validate({
        "source": {"type": "imap", "host": "h"},
        "destination": {
            "tenant_id": "t", "client_id": "c", "client_secret": "s",
            "certificate_thumbprint": "  ",
        },
        "users": [{"source_id": "a", "dest_id": "b"}],
    })
    assert cfg.destination.certificate_thumbprint is None


def test_colon_separated_thumbprint_reaches_msal_decodable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    class _DecodingMsalApp:
        """Does to the thumbprint what msal's JwtAssertionCreator does."""

        def __init__(self, client_id: str, client_credential: Any = None, **_kw: Any) -> None:
            binascii.a2b_hex(client_credential["thumbprint"])
            seen.append(client_credential["thumbprint"])

    monkeypatch.setattr(ms_auth.msal, "ConfidentialClientApplication", _DecodingMsalApp)
    # The provider parses the key file before it builds the MSAL app.
    monkeypatch.setattr(ms_auth, "read_private_key", lambda path: path.read_bytes())
    key = tmp_path / "key.pem"
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\n")
    block = {
        "tenant_id": "t", "client_id": "c", "certificate_path": str(key),
        "certificate_thumbprint": ":".join(_PAIRS),
        "token_cache_file": str(tmp_path / "cache.json"),
    }
    cfg = Config.model_validate({
        "source": {**block, "type": "microsoft365"},
        "destination": {**block, "cloud": "gcc_high"},
        "users": [{"source_id": "a@src", "dest_id": "a@dst"}],
    })
    MSTokenProvider.from_config(cfg.source)  # type: ignore[arg-type]
    MSTokenProvider.from_config(cfg.destination)
    assert seen == [_TP, _TP]
