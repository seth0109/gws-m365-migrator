from __future__ import annotations

from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build

# Google Workspace is ALWAYS read-only — these scopes must never be widened.
_READ_ONLY_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def _base_credentials(key_file: Path, scopes: list[str]) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_file(
        str(key_file), scopes=scopes
    )


def impersonated_credentials(
    key_file: Path,
    user_email: str,
    scopes: list[str] | None = None,
) -> service_account.Credentials:
    """Return read-only service-account credentials impersonating *user_email*."""
    resolved_scopes = scopes or _READ_ONLY_SCOPES
    # Ensure we never accidentally request write scopes.
    for scope in resolved_scopes:
        if not scope.endswith(".readonly"):
            raise ValueError(
                f"Google credentials must be read-only. Refused scope: {scope}"
            )
    base = _base_credentials(key_file, resolved_scopes)
    return base.with_subject(user_email)


def build_service(
    service_name: str,
    version: str,
    key_file: Path,
    user_email: str,
    scopes: list[str] | None = None,
) -> Resource:
    creds = impersonated_credentials(key_file, user_email, scopes)
    return build(service_name, version, credentials=creds, cache_discovery=False)
