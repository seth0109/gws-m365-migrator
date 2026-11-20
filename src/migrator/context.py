from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TypeVar

from .config import Config, UserMapping
from .connectors.base import BaseSource
from .microsoft.graph_client import GraphClient
from .progress import ItemProgress

T = TypeVar("T")


@dataclass
class JobContext:
    """Everything a workload job needs for one user.

    `source` is the configured source connector (already bound to source
    credentials). `dest_gc` is the destination Microsoft Graph client, or None
    in whatif (inventory-only) mode. `progress` is where the job reports how
    far it is; the default only counts (nothing is drawn).
    """

    user: UserMapping
    source: BaseSource
    dest_gc: GraphClient | None
    mode: str
    config: Config
    progress: ItemProgress = field(default_factory=ItemProgress)

    def track(self, items: Iterable[T], key: str) -> Iterator[T]:
        """Iterate a source listing while reporting progress on it. `key` is the
        one the connector files that listing's cursor and expected item count
        under ("mail", "contacts", "calendar:<id>", ...)."""
        return self.progress.track(items, expected=lambda: self.source.expected_total(key))

    def require_capability(self, workload: str) -> None:
        if workload not in self.source.capabilities:
            raise RuntimeError(
                f"Configured source ({self.config.source.type}) does not support the "
                f"'{workload}' workload (supports: {sorted(self.source.capabilities)})."
            )
