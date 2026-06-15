from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .whatif import ManifestWriter

# Set by Orchestrator before dispatching any workload job so jobs can access config.
_current_config: "Config | None" = None

# Set by Orchestrator in whatif (dry-run) mode so jobs can record planned items.
# None outside whatif runs.
_current_manifest: "ManifestWriter | None" = None
