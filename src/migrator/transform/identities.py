from __future__ import annotations

import logging
from typing import Any

from ..microsoft.graph_client import GraphClient

log = logging.getLogger(__name__)


class IdentityMap:
    """Resolves Google email addresses to Entra (Azure AD) user IDs, with a local cache."""

    def __init__(self, gc: GraphClient) -> None:
        self._gc = gc
        self._cache: dict[str, str | None] = {}  # google_email → ms_user_id or None

    def resolve(self, email: str) -> str | None:
        if email in self._cache:
            return self._cache[email]
        try:
            user = self._gc.get(f"/users/{email}", params={"$select": "id,mail,userPrincipalName"})
            ms_id: str = user["id"]
            self._cache[email] = ms_id
            return ms_id
        except Exception:
            log.warning("Could not resolve identity for %s", email)
            self._cache[email] = None
            return None
