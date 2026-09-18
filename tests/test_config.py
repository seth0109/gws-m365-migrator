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
