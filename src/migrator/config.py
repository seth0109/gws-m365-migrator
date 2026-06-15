from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class GoogleConfig(BaseModel):
    service_account_key_file: Path
    admin_email: str
    scopes: list[str] = Field(default_factory=lambda: [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    ])


class MicrosoftConfig(BaseModel):
    tenant_id: str
    client_id: str
    certificate_path: Path | None = None
    certificate_thumbprint: str | None = None
    client_secret: str | None = None
    token_cache_file: Path = Path(".ms_token_cache.json")


class UserMapping(BaseModel):
    google_email: str
    ms_upn: str


class WorkloadConfig(BaseModel):
    enabled: bool = True
    concurrency: int = 4


class FilesWorkloadConfig(WorkloadConfig):
    chunk_size_bytes: int = 10 * 1024 * 1024  # 10 MB


class MailWorkloadConfig(WorkloadConfig):
    multi_label_policy: Literal["categories", "duplicate"] = "categories"


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
    google: GoogleConfig
    microsoft: MicrosoftConfig
    users: list[UserMapping]
    workloads: WorkloadsConfig = Field(default_factory=WorkloadsConfig)
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)


def load_config(path: Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
