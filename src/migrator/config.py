from __future__ import annotations

import os
import string
import unicodedata
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

import yaml
from pydantic import BaseModel, BeforeValidator, Field

_GOOGLE_READONLY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def normalize_token(value: object) -> object:
    """Accept "GCC High" / "gcc-high" / "Shared" for the enum-like fields."""
    if isinstance(value, str):
        return value.strip().lower().replace("-", "_").replace(" ", "_")
    return value


def normalize_thumbprint(value: object) -> object:
    """Reduce a certificate thumbprint to 40 bare hex digits, or raise ValueError.

    MSAL hex-decodes the thumbprint exactly as given, so one pasted the way the
    tools print it only fails deep inside the token provider, as a bare
    `binascii.Error: Odd-length string`. Accepted here: `sha1 Fingerprint=AB:CD:..`
    (openssl), `ab cd ..` (the Windows certificate dialog, which also prepends an
    invisible left-to-right mark) and dashes. Blank means "not set"."""
    if not isinstance(value, str):
        return value
    digits = "".join(
        ch
        for ch in value.rpartition("=")[2]
        if not (ch.isspace() or ch in ":-" or unicodedata.category(ch) == "Cf")
    )
    if not digits:
        return None
    if any(ch not in string.hexdigits for ch in digits):
        raise ValueError(
            "certificate thumbprint must be hex digits (0-9, A-F), optionally "
            "separated by ':' or spaces"
        )
    if len(digits) != 40:
        sha256 = " (64 digits is the SHA-256 fingerprint)" if len(digits) == 64 else ""
        raise ValueError(
            "certificate thumbprint must be the certificate's SHA-1 thumbprint: 40 hex "
            f"digits, got {len(digits)}{sha256}. Print it with: "
            "openssl x509 -in <cert> -noout -fingerprint -sha1"
        )
    return digits.upper()


# SHA-1 thumbprint of the certificate uploaded to the app registration.
Thumbprint = Annotated[str | None, BeforeValidator(normalize_thumbprint)]


# --------------------------------------------------------------------------- #
# Microsoft national clouds
# --------------------------------------------------------------------------- #
# Each Microsoft 365 tenant block names the cloud its tenant lives in — source
# and destination independently, so a commercial tenant can migrate into GCC
# High. Tokens are not interchangeable between clouds, so the sign-in authority,
# the token scope and the Graph service root must all come from the same row.
# Endpoints per https://learn.microsoft.com/graph/deployments. Microsoft 365 GCC
# (not High) is served by the global endpoints; "gcc" exists so that is explicit.
M365Cloud = Annotated[
    Literal["global", "gcc", "gcc_high", "dod", "china"],
    BeforeValidator(normalize_token),
]


class CloudEndpoints(NamedTuple):
    login: str  # Microsoft Entra ID authority host
    graph: str  # Microsoft Graph service root (no version segment)


_GLOBAL = CloudEndpoints("https://login.microsoftonline.com", "https://graph.microsoft.com")

CLOUD_ENDPOINTS: dict[str, CloudEndpoints] = {
    "global": _GLOBAL,
    "gcc": _GLOBAL,
    "gcc_high": CloudEndpoints("https://login.microsoftonline.us", "https://graph.microsoft.us"),
    "dod": CloudEndpoints("https://login.microsoftonline.us", "https://dod-graph.microsoft.us"),
    "china": CloudEndpoints(
        "https://login.chinacloudapi.cn", "https://microsoftgraph.chinacloudapi.cn"
    ),
}


# --------------------------------------------------------------------------- #
# Source configs (discriminated on `type`)
# --------------------------------------------------------------------------- #
class GoogleWorkspaceSourceConfig(BaseModel):
    type: Literal["google_workspace"] = "google_workspace"
    service_account_key_file: Path
    admin_email: str
    scopes: list[str] = Field(default_factory=lambda: list(_GOOGLE_READONLY_SCOPES))


class ImapSourceConfig(BaseModel):
    type: Literal["imap"] = "imap"
    host: str
    port: int = 993
    use_ssl: bool = True
    # Folders to skip entirely (e.g. server-specific virtual folders).
    exclude_folders: list[str] = Field(default_factory=list)


class Microsoft365SourceConfig(BaseModel):
    type: Literal["microsoft365"] = "microsoft365"
    tenant_id: str
    client_id: str
    cloud: M365Cloud = "global"  # national cloud of the SOURCE tenant
    certificate_path: Path | None = None
    certificate_thumbprint: Thumbprint = None
    client_secret: str | None = None
    token_cache_file: Path = Path(".ms_source_token_cache.json")


SourceConfig = Annotated[
    GoogleWorkspaceSourceConfig | ImapSourceConfig | Microsoft365SourceConfig,
    Field(discriminator="type"),
]

# Backwards-compatible alias: the google connectors and google_auth type-hint
# against `GoogleConfig`. The shape (service_account_key_file/admin_email/scopes)
# is unchanged, so the alias keeps those modules working untouched.
GoogleConfig = GoogleWorkspaceSourceConfig


# --------------------------------------------------------------------------- #
# Destination configs (only Microsoft 365 today)
# --------------------------------------------------------------------------- #
class Microsoft365DestinationConfig(BaseModel):
    type: Literal["microsoft365"] = "microsoft365"
    tenant_id: str
    client_id: str
    cloud: M365Cloud = "global"  # national cloud of the DESTINATION tenant
    certificate_path: Path | None = None
    certificate_thumbprint: Thumbprint = None
    client_secret: str | None = None
    token_cache_file: Path = Path(".ms_token_cache.json")
    # Owner (UPN or object id) stamped on auto-provisioned M365 groups/sites.
    # Graph warns that groups created app-only *without* an owner may never get
    # their SharePoint site provisioned — set this when using shared-drives or
    # sharepoint auto-provisioning.
    sharepoint_site_owner: str | None = None


# Only one destination type exists today; alias kept for symmetry/extensibility.
DestinationConfig = Microsoft365DestinationConfig


# --------------------------------------------------------------------------- #
# User + shared-drive mappings
# --------------------------------------------------------------------------- #
class UserMapping(BaseModel):
    source_id: str  # source identity (Google email / IMAP address / source-tenant UPN)
    dest_id: str  # destination Microsoft 365 UPN

    # "shared" = the destination is a shared mailbox. Graph addresses it like any
    # other mailbox (/users/{upn}), so mail, contacts and calendar migrate
    # unchanged — but a shared mailbox is an unlicensed account with no OneDrive,
    # so the files workload is skipped for this mapping.
    mailbox_type: Annotated[
        Literal["user", "shared"], BeforeValidator(normalize_token)
    ] = "user"

    # IMAP source only — per-user credentials. Prefer imap_password_env (reads the
    # secret from an environment variable) over the inline imap_password.
    imap_user: str | None = None
    imap_password_env: str | None = None
    imap_password: str | None = None

    def resolve_imap_password(self) -> str | None:
        if self.imap_password_env:
            return os.environ.get(self.imap_password_env)
        return self.imap_password


class SharedDriveMapping(BaseModel):
    """A Google Shared Drive → SharePoint target. Identify the drive by name or id.

    `target_site_alias` is the mailNickname used to auto-provision (and later
    resolve) the SharePoint site backing this drive.
    """

    drive_name: str | None = None
    drive_id: str | None = None
    target_site_alias: str
    display_name: str | None = None  # site display name; defaults to drive_name


class SharePointSiteMapping(BaseModel):
    """A source SharePoint site → destination site (microsoft365 source only).

    `source_site` is a Graph site address: a site id, or a host:path form such as
    "contoso.sharepoint.com:/sites/Marketing". The destination is either an
    existing site (`dest_site`, same address forms) or auto-provisioned from
    `target_site_alias` (mailNickname). Provide exactly one of the two.
    """

    source_site: str
    dest_site: str | None = None
    target_site_alias: str | None = None
    display_name: str | None = None  # provisioned site display name; defaults to alias


# --------------------------------------------------------------------------- #
# Workloads + rate limits (unchanged shapes)
# --------------------------------------------------------------------------- #
class WorkloadConfig(BaseModel):
    enabled: bool = True
    concurrency: int = 4


class FilesWorkloadConfig(WorkloadConfig):
    chunk_size_bytes: int = 10 * 1024 * 1024  # 10 MB


class MailWorkloadConfig(WorkloadConfig):
    multi_label_policy: Literal["categories", "duplicate"] = "categories"
    # Include Gmail Spam/Trash in the migration (they route to JunkEmail /
    # DeletedItems). Off = those messages are silently left behind.
    include_spam_trash: bool = True
    # "json" (default) creates messages via the JSON API with MAPI extended
    # properties so they arrive as normal non-draft mail with the original
    # sent/received dates. "mime" posts raw MIME — byte-perfect content, but
    # Graph documents that path as creating *drafts* dated at import time.
    import_mode: Literal["json", "mime"] = "json"


class WorkloadsConfig(BaseModel):
    contacts: WorkloadConfig = Field(default_factory=WorkloadConfig)
    calendar: WorkloadConfig = Field(default_factory=WorkloadConfig)
    files: FilesWorkloadConfig = Field(default_factory=FilesWorkloadConfig)
    mail: MailWorkloadConfig = Field(default_factory=MailWorkloadConfig)


class RateLimitsConfig(BaseModel):
    google_requests_per_second: float = 10.0
    graph_requests_per_second: float = 4.0
    graph_requests_per_mailbox_per_minute: float = 120.0


class Config(BaseModel):
    state_db: Path = Path("migration_state.db")
    log_level: str = "INFO"
    log_file: Path | None = Path("migrator.log")
    source: SourceConfig
    destination: DestinationConfig
    users: list[UserMapping]
    shared_drives: list[SharedDriveMapping] = Field(default_factory=list)
    sharepoint_sites: list[SharePointSiteMapping] = Field(default_factory=list)
    workloads: WorkloadsConfig = Field(default_factory=WorkloadsConfig)
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)


def load_config(path: Path) -> Config:
    # YAML is UTF-8 by spec. Without an explicit encoding, Windows opens the file
    # as cp1252 and either garbles non-ASCII values (display names, comments) or
    # raises on bytes cp1252 cannot decode.
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
