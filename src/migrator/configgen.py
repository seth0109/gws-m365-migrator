"""Config generation helper (`migrator init-config`).

Pure, network-free logic for scaffolding a `config.yaml` from a few CLI inputs
plus whatever credential files already sit in a `credentials/` folder:

- discover the service-account JSON / certificate PEM(s) on disk,
- read a user-mapping CSV (``source_id,dest_id[,imap_user,imap_password_env]``),
- assemble a `Config`-shaped dict, validate it, and dump it to YAML.

Keeping the assembly here (rather than in `cli.py`) mirrors the project's
transform layer: it is unit-testable without typer or any network client.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Config

# Source types that can be scaffolded.
SOURCE_TYPES = ("google_workspace", "imap", "microsoft365")

# Filename keywords used to disambiguate PEMs when more than one is present.
_SOURCE_HINTS = ("source", "src")
_DEST_HINTS = ("dest", "destination", "target", "graph", "ms")

# Accepted CSV header aliases (lower-cased) → canonical UserMapping field.
_CSV_ALIASES: dict[str, str] = {
    "source_id": "source_id",
    "source": "source_id",
    "from": "source_id",
    "dest_id": "dest_id",
    "dest": "dest_id",
    "destination": "dest_id",
    "to": "dest_id",
    "imap_user": "imap_user",
    "imap_password_env": "imap_password_env",
}


class ConfigGenError(ValueError):
    """Raised for any user-correctable problem while scaffolding the config."""


@dataclass
class CredentialSet:
    """Credential files resolved from a `credentials/` directory."""

    service_account_key: Path | None = None
    dest_cert: Path | None = None
    source_cert: Path | None = None


def _pick_cert(pems: list[Path], hints: tuple[str, ...]) -> Path | None:
    matches = [p for p in pems if any(h in p.name.lower() for h in hints)]
    return matches[0] if len(matches) == 1 else None


def discover_credentials(
    cred_dir: Path,
    source_type: str,
    *,
    service_account_key: Path | None = None,
    dest_cert: Path | None = None,
    source_cert: Path | None = None,
) -> CredentialSet:
    """Resolve the credential files needed for ``source_type``.

    Explicit overrides win; otherwise we glob ``*.json`` / ``*.pem`` in
    ``cred_dir``. A google_workspace source needs the JSON key; every flow
    needs a destination PEM; a microsoft365 source additionally needs a
    second (source-tenant) PEM, disambiguated by filename keyword.
    """
    if not cred_dir.is_dir():
        raise ConfigGenError(
            f"Credentials directory '{cred_dir}' not found. Create it and drop the "
            f"service-account JSON and/or certificate PEM there, or pass explicit paths."
        )

    jsons = sorted(p for p in cred_dir.glob("*.json"))
    pems = sorted(cred_dir.glob("*.pem"))
    found = CredentialSet()

    # --- service-account JSON (google_workspace only) --------------------- #
    if source_type == "google_workspace":
        if service_account_key is not None:
            found.service_account_key = service_account_key
        elif len(jsons) == 1:
            found.service_account_key = jsons[0]
        elif not jsons:
            raise ConfigGenError(
                f"No service-account *.json found in '{cred_dir}'. Add the Google "
                f"service-account key file (or pass --service-account-key)."
            )
        else:
            raise ConfigGenError(
                f"Multiple *.json files in '{cred_dir}': {[p.name for p in jsons]}. "
                f"Pass --service-account-key to choose one."
            )

    # --- destination certificate (always required) ------------------------ #
    if dest_cert is not None:
        found.dest_cert = dest_cert
    elif source_type == "microsoft365":
        # Two tenants → two certs; pick the destination one by keyword.
        found.dest_cert = _pick_cert(pems, _DEST_HINTS)
    elif len(pems) == 1:
        found.dest_cert = pems[0]
    elif not pems:
        raise ConfigGenError(
            f"No certificate *.pem found in '{cred_dir}'. Add the destination Entra "
            f"app certificate (or pass --cert)."
        )

    # --- source certificate (microsoft365 source only) -------------------- #
    if source_type == "microsoft365":
        if source_cert is not None:
            found.source_cert = source_cert
        else:
            found.source_cert = _pick_cert(pems, _SOURCE_HINTS)

        if found.dest_cert is None or found.source_cert is None or (
            found.dest_cert == found.source_cert
        ):
            raise ConfigGenError(
                f"A microsoft365 source needs two distinct certificates in '{cred_dir}' "
                f"(one per tenant). Found {[p.name for p in pems]}; name them with "
                f"'source'/'dest' keywords or pass --cert and --source-cert."
            )

    if found.dest_cert is None:
        raise ConfigGenError(
            f"Could not resolve a destination certificate in '{cred_dir}' "
            f"(found {[p.name for p in pems]}). Pass --cert to choose one."
        )

    return found


def parse_mapping_csv(path: Path) -> list[dict[str, str]]:
    """Parse a user-mapping CSV into UserMapping-shaped dicts.

    Required columns: ``source_id`` and ``dest_id`` (header aliases accepted,
    case-insensitive). Optional: ``imap_user``, ``imap_password_env``. Empty
    optional cells are dropped so they don't override model defaults.
    """
    if not path.is_file():
        raise ConfigGenError(f"Mapping CSV '{path}' not found.")

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ConfigGenError(f"Mapping CSV '{path}' is empty.")

        # Map the file's headers onto canonical field names.
        header_map: dict[str, str] = {}
        for raw in reader.fieldnames:
            key = _CSV_ALIASES.get(raw.strip().lower())
            if key:
                header_map[raw] = key
        if "source_id" not in header_map.values() or "dest_id" not in header_map.values():
            raise ConfigGenError(
                f"Mapping CSV '{path}' must have 'source_id' and 'dest_id' columns "
                f"(found: {reader.fieldnames})."
            )

        users: list[dict[str, str]] = []
        for lineno, row in enumerate(reader, start=2):
            entry: dict[str, str] = {}
            for raw, field in header_map.items():
                val = (row.get(raw) or "").strip()
                if val:
                    entry[field] = val
            if not entry:
                continue  # skip blank lines
            if "source_id" not in entry or "dest_id" not in entry:
                raise ConfigGenError(
                    f"Mapping CSV '{path}' line {lineno}: both source_id and dest_id required."
                )
            users.append(entry)

    if not users:
        raise ConfigGenError(f"Mapping CSV '{path}' contained no user rows.")
    return users


def _ms_block(
    tenant_id: str, client_id: str, cert: Path, thumbprint: str, cache: str
) -> dict[str, object]:
    return {
        "type": "microsoft365",
        "tenant_id": tenant_id,
        "client_id": client_id,
        "certificate_path": str(cert),
        "certificate_thumbprint": thumbprint,
        "token_cache_file": cache,
    }


def build_config_dict(
    *,
    source_type: str,
    creds: CredentialSet,
    users: list[dict[str, str]],
    dest_tenant_id: str,
    dest_client_id: str,
    dest_thumbprint: str,
    admin_email: str | None = None,
    imap_host: str | None = None,
    imap_port: int = 993,
    imap_ssl: bool = True,
    source_tenant_id: str | None = None,
    source_client_id: str | None = None,
    source_thumbprint: str | None = None,
) -> dict[str, object]:
    """Assemble (and validate) a `Config`-shaped dict ready to dump as YAML."""
    if source_type not in SOURCE_TYPES:
        raise ConfigGenError(f"Unknown source type '{source_type}'. Choose from {SOURCE_TYPES}.")

    # --- source block ----------------------------------------------------- #
    source: dict[str, object]
    if source_type == "google_workspace":
        if not admin_email:
            raise ConfigGenError("google_workspace source requires --admin-email.")
        assert creds.service_account_key is not None  # discover_credentials guarantees it
        source = {
            "type": "google_workspace",
            "service_account_key_file": str(creds.service_account_key),
            "admin_email": admin_email,
        }
    elif source_type == "imap":
        if not imap_host:
            raise ConfigGenError("imap source requires --imap-host.")
        source = {"type": "imap", "host": imap_host, "port": imap_port, "use_ssl": imap_ssl}
    else:  # microsoft365
        missing = [
            name
            for name, val in (
                ("--source-tenant-id", source_tenant_id),
                ("--source-client-id", source_client_id),
                ("--source-thumbprint", source_thumbprint),
            )
            if not val
        ]
        if missing:
            raise ConfigGenError(
                f"microsoft365 source requires: {', '.join(missing)}."
            )
        assert creds.source_cert is not None
        source = _ms_block(
            source_tenant_id,  # type: ignore[arg-type]
            source_client_id,  # type: ignore[arg-type]
            creds.source_cert,
            source_thumbprint,  # type: ignore[arg-type]
            ".ms_source_token_cache.json",
        )

    assert creds.dest_cert is not None
    destination = _ms_block(
        dest_tenant_id, dest_client_id, creds.dest_cert, dest_thumbprint, ".ms_token_cache.json"
    )

    cfg: dict[str, object] = {
        "state_db": "migration_state.db",
        "log_level": "INFO",
        "log_file": "migrator.log",
        "source": source,
        "destination": destination,
        "users": users,
        "workloads": {
            "contacts": {"enabled": True, "concurrency": 4},
            "calendar": {"enabled": True, "concurrency": 4},
            "files": {"enabled": True, "concurrency": 2},
            "mail": {"enabled": True, "concurrency": 2, "multi_label_policy": "categories"},
        },
        "rate_limits": {
            "google_requests_per_second": 10,
            "graph_requests_per_second": 4,
            "graph_requests_per_mailbox_per_minute": 120,
        },
    }

    # Validate the shape before anyone tries to run with it.
    Config.model_validate(cfg)
    return cfg


def dump_config_yaml(cfg: dict[str, object]) -> str:
    """Serialize a config dict to YAML, preserving key order."""
    return str(yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False))
