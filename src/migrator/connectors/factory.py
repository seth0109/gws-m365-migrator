from __future__ import annotations

from collections.abc import Callable

from ..config import (
    Config,
    GoogleWorkspaceSourceConfig,
    ImapSourceConfig,
    Microsoft365SourceConfig,
)
from ..microsoft.graph_client import GraphClient
from .base import BaseSource

# Workloads each source type can handle, without constructing network clients.
_CAPABILITIES: dict[str, set[str]] = {
    "google_workspace": {"mail", "files", "contacts", "calendar"},
    "imap": {"mail"},
    "microsoft365": {"mail", "files", "contacts", "calendar"},
}


def source_capabilities(cfg: Config) -> set[str]:
    return set(_CAPABILITIES.get(cfg.source.type, set()))


def build_source(
    cfg: Config,
    source_graph_client_factory: Callable[[], GraphClient] | None = None,
) -> BaseSource:
    """Construct the configured source connector.

    `source_graph_client_factory` builds a Graph client bound to the *source*
    tenant; it is required only for the microsoft365 source.
    """
    src = cfg.source
    if isinstance(src, GoogleWorkspaceSourceConfig):
        from .google import GoogleWorkspaceSource

        return GoogleWorkspaceSource(src, cfg.workloads.mail.multi_label_policy)

    if isinstance(src, ImapSourceConfig):
        from .imap import ImapSource

        return ImapSource(src)

    if isinstance(src, Microsoft365SourceConfig):
        from .m365 import M365Source

        if source_graph_client_factory is None:
            raise ValueError("microsoft365 source requires a source Graph client factory")
        return M365Source(src, source_graph_client_factory)

    raise ValueError(f"Unsupported source type: {cfg.source.type!r}")
