from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..config import UserMapping

log = logging.getLogger(__name__)


class IdentityMap:
    """Rewrites source identities (Google emails / source-tenant UPNs) to their
    destination Microsoft 365 UPNs.

    The configured user mapping (``source_id`` → ``dest_id``) is the source of
    truth, so this is pure and deterministic — no Graph calls and it works in
    whatif mode. Addresses with no mapping (external attendees, distribution
    lists, etc.) are left untouched, which is the correct behaviour: only known
    migrated mailboxes are rewritten.
    """

    def __init__(self, users: Iterable[UserMapping]) -> None:
        self._map: dict[str, str] = {
            u.source_id.lower(): u.dest_id
            for u in users
            if u.source_id and u.dest_id
        }

    def map_address(self, email: str | None) -> str | None:
        """Return the destination address for `email`, or the original (unchanged)
        when there is no mapping. ``None``/empty pass through unchanged."""
        if not email:
            return email
        return self._map.get(email.lower(), email)

    def _remap_email_holder(self, holder: dict[str, Any] | None) -> None:
        """Rewrite the ``emailAddress.address`` of a Graph recipient object in place."""
        if not holder:
            return
        ea = holder.get("emailAddress")
        if isinstance(ea, dict) and ea.get("address"):
            ea["address"] = self.map_address(ea["address"])

    def remap_event(self, body: dict[str, Any]) -> dict[str, Any]:
        """Rewrite attendee and organizer addresses on a Graph event body in place.

        Returns the same dict for convenient chaining. Safe to call on bodies
        with no attendees/organizer (e.g. cancelled-event sentinels).
        """
        for attendee in body.get("attendees", []) or []:
            self._remap_email_holder(attendee)
        self._remap_email_holder(body.get("organizer"))
        return body
