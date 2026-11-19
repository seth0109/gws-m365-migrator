"""Sync-cursor rules shared by the per-user workload jobs.

A cursor marks how far a source has been read: a full pass captures one, and a
delta pass reads only what changed after it. Two rules keep deltas dependable:

- A delta with no cursor is an error, not a skip. It means no full pass ever
  finished reading that mailbox/drive/calendar, and a quiet "nothing to do"
  would let a cutover go by with new items left behind.
- An item that failed never traps the cursor. If a cursor is already stored it
  is held, so the failed items stay inside the next delta's window and are
  retried. If none is stored yet (the first full pass), it is seeded anyway:
  those failed items are older than any cursor, so only a full re-run can retry
  them whether it is seeded or not — and refusing to seed would make deltas
  impossible for as long as one item keeps failing.
"""
from __future__ import annotations

import logging

from ..context import JobContext
from ..state.db import get_cursor, save_cursor, session_scope

log = logging.getLogger(__name__)


class NoDeltaCursor(RuntimeError):
    """A delta pass was asked for something no full pass has finished reading."""


def delta_since(ctx: JobContext, key: str, what: str, command: str) -> str | None:
    """The `since` to hand the connector for cursor `key`.

    None on a full pass. On a delta pass, the stored cursor — or NoDeltaCursor
    (which fails the user's run, so the CLI exits 1) when there is none."""
    if ctx.mode != "delta":
        return None
    user = ctx.user.source_id
    with session_scope() as s:
        cursor = get_cursor(s, user, key)
    if not cursor:
        raise NoDeltaCursor(
            f"no sync cursor for {what} of {user}: no full pass has finished reading it, "
            f"so a delta has nowhere to start. Run `migrator {command} --user {user}` first."
        )
    return cursor


def advance_cursor(ctx: JobContext, key: str, failures: int, what: str) -> None:
    """Persist the cursor the connector captured during this pass (see module doc).

    `what` names the items for the log line, e.g. "message(s)"."""
    new_cursor = ctx.source.get_last_cursor(key)
    if not new_cursor:
        return
    user = ctx.user.source_id
    with session_scope() as s:
        held = bool(failures) and get_cursor(s, user, key) is not None
        if not held:
            save_cursor(s, user, key, new_cursor)
    if not failures:
        return
    if held:
        log.warning(
            "%d %s failed for %s — sync cursor not advanced; they will be retried on "
            "the next run", failures, what, user,
        )
    else:
        log.warning(
            "%d %s failed for %s. The sync cursor was seeded anyway so delta passes can "
            "run; the failed items are behind it and are retried only by re-running the "
            "full pass.", failures, what, user,
        )
