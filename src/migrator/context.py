from __future__ import annotations

from dataclasses import dataclass

from .config import Config, UserMapping
from .connectors.base import BaseSource
from .microsoft.graph_client import GraphClient


@dataclass
class JobContext:
    """Everything a workload job needs for one user.

    `source` is the configured source connector (already bound to source
    credentials). `dest_gc` is the destination Microsoft Graph client, or None
    in whatif (inventory-only) mode.
    """

    user: UserMapping
    source: BaseSource
    dest_gc: GraphClient | None
    mode: str
    config: Config

    def require_capability(self, workload: str) -> None:
        if workload not in self.source.capabilities:
            raise RuntimeError(
                f"Configured source ({self.config.source.type}) does not support the "
                f"'{workload}' workload (supports: {sorted(self.source.capabilities)})."
            )
