from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CSV_COLUMNS = [
    "timestamp",
    "user_email",
    "ms_upn",
    "workload",
    "source_id",
    "source_path",
    "name",
    "size_bytes",
    "mime_type",
    "modified_time",
    "action",
    "notes",
]


class ManifestWriter:
    """Thread-safe streaming CSV writer for whatif (dry-run) inventory.

    Each workload job calls `add(...)` once per source item it would migrate.
    Rows are flushed immediately so a long inventory survives an interrupt.
    """

    def __init__(self, output_path: Path) -> None:
        self._path = output_path
        self._lock = threading.Lock()
        self._count = 0
        # Open in write mode and emit header. We intentionally truncate any prior file —
        # the manifest reflects a single planning snapshot.
        self._fh = open(output_path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_COLUMNS)
        self._writer.writeheader()
        self._fh.flush()

    def add(
        self,
        *,
        user_email: str,
        ms_upn: str,
        workload: str,
        source_id: str,
        source_path: str = "",
        name: str = "",
        size_bytes: int | str = "",
        mime_type: str = "",
        modified_time: str = "",
        action: str = "migrate",
        notes: str = "",
    ) -> None:
        row: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user_email": user_email,
            "ms_upn": ms_upn,
            "workload": workload,
            "source_id": source_id,
            "source_path": source_path,
            "name": name,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "modified_time": modified_time,
            "action": action,
            "notes": notes,
        }
        with self._lock:
            self._writer.writerow(row)
            self._fh.flush()
            self._count += 1

    @property
    def count(self) -> int:
        return self._count

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> ManifestWriter:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
