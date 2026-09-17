from __future__ import annotations

import email as email_lib
import imaplib
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass

from ..config import ImapSourceConfig, UserMapping
from .base import BaseSource, SourceMessage

log = logging.getLogger(__name__)

# IMAP special-use attribute → Outlook well-known folder name. Mirrors the tokens
# the Gmail path produces (see transform/labels.SYSTEM_LABEL_FOLDER) so both
# sources land mail in the same destination folders.
_SPECIAL_USE = {
    "\\Sent": "SentItems",
    "\\Drafts": "Drafts",
    "\\Trash": "DeletedItems",
    "\\Junk": "JunkEmail",
}
# Fallback name heuristics (case-insensitive) when no special-use flag is present.
_NAME_HEURISTICS = {
    "inbox": "Inbox",
    "sent": "SentItems",
    "sent items": "SentItems",
    "drafts": "Drafts",
    "trash": "DeletedItems",
    "deleted": "DeletedItems",
    "deleted items": "DeletedItems",
    "junk": "JunkEmail",
    "spam": "JunkEmail",
}

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) "?(?P<delim>[^"]*)"? (?P<name>.*)$')


@dataclass
class _Folder:
    raw_name: str  # IMAP mailbox name (as used in SELECT/STATUS)
    mapped_path: str  # destination folder path ("\\"-separated)


class ImapSource(BaseSource):
    """Generic IMAP mail source (mail only). Per-user credentials come from the
    user mapping (imap_user + resolved password)."""

    capabilities = {"mail"}

    def __init__(self, cfg: ImapSourceConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self._conn: imaplib.IMAP4 | None = None

    # -- connection --------------------------------------------------------- #
    def _connect(self, user: UserMapping) -> imaplib.IMAP4:
        if self._conn is not None:
            return self._conn
        username = user.imap_user or user.source_id
        password = user.resolve_imap_password()
        if not password:
            raise RuntimeError(
                f"No IMAP password for {user.source_id} "
                f"(set imap_password_env or imap_password in the user mapping)"
            )
        conn: imaplib.IMAP4
        if self.cfg.use_ssl:
            conn = imaplib.IMAP4_SSL(self.cfg.host, self.cfg.port)
        else:
            conn = imaplib.IMAP4(self.cfg.host, self.cfg.port)
        conn.login(username, password)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            self._conn = None

    @staticmethod
    def _quote(name: str) -> str:
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _folders(self, conn: imaplib.IMAP4) -> list[_Folder]:
        typ, data = conn.list()
        if typ != "OK":
            raise RuntimeError(f"IMAP LIST failed: {typ}")
        folders: list[_Folder] = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            m = _LIST_RE.match(line)
            if not m:
                continue
            flags = m.group("flags").decode("ascii", "replace")
            if "\\Noselect" in flags:
                continue
            delim = m.group("delim").decode("ascii", "replace") or "/"
            raw = m.group("name").decode("utf-8", "replace").strip().strip('"')
            if raw in self.cfg.exclude_folders:
                continue
            folders.append(_Folder(raw_name=raw, mapped_path=self._map_folder(raw, flags, delim)))
        return folders

    def _map_folder(self, raw: str, flags: str, delim: str) -> str:
        for attr, dest in _SPECIAL_USE.items():
            if attr in flags:
                return dest
        leaf = raw.split(delim)[-1]
        heuristic = _NAME_HEURISTICS.get(leaf.lower())
        if heuristic:
            return heuristic
        # Preserve hierarchy, normalising the IMAP delimiter to Outlook's "\\".
        return raw.replace(delim, "\\")

    @staticmethod
    def _status(conn: imaplib.IMAP4, raw_name: str) -> tuple[int, int]:
        typ, data = conn.status(ImapSource._quote(raw_name), "(UIDVALIDITY UIDNEXT)")
        if typ != "OK" or not data or not data[0]:
            return (0, 1)
        text = data[0].decode("ascii", "replace")
        uv = re.search(r"UIDVALIDITY (\d+)", text)
        un = re.search(r"UIDNEXT (\d+)", text)
        return (int(uv.group(1)) if uv else 0, int(un.group(1)) if un else 1)

    # -- mail --------------------------------------------------------------- #
    def iter_messages(self, user: UserMapping, since: str | None) -> Iterator[SourceMessage]:
        conn = self._connect(user)
        since_map: dict[str, dict[str, int]] = json.loads(since) if since else {}
        new_cursor: dict[str, dict[str, int]] = {}

        for folder in self._folders(conn):
            uidvalidity, uidnext = self._status(conn, folder.raw_name)
            new_cursor[folder.raw_name] = {"uidvalidity": uidvalidity, "uidnext": uidnext}

            start_uid = 1
            prev = since_map.get(folder.raw_name)
            if prev and prev.get("uidvalidity") == uidvalidity:
                start_uid = prev.get("uidnext", 1)
                if start_uid >= uidnext:
                    continue  # no new messages in this folder

            conn.select(self._quote(folder.raw_name), readonly=True)
            # A bare set in `UID SEARCH <set>` is *message sequence numbers*
            # (RFC 3501 §6.4.8); only a UID-prefixed set is a UID range. Without
            # the prefix, mail that arrived after expunges (sequence numbers
            # below the stored UIDNEXT) is skipped while the cursor advances.
            typ, data = conn.uid("search", "UID", f"{start_uid}:*")
            if typ != "OK" or not data or not data[0]:
                continue
            for uid_b in data[0].split():
                uid = int(uid_b)
                if uid < start_uid:  # IMAP returns the last msg when start > highest UID
                    continue
                yield self._fetch_message(conn, folder, uid, uidvalidity)

        self._set_cursor("mail", json.dumps(new_cursor))

    def _fetch_message(
        self, conn: imaplib.IMAP4, folder: _Folder, uid: int, uidvalidity: int
    ) -> SourceMessage:
        source_id = f"{folder.raw_name}:{uidvalidity}:{uid}"
        try:
            typ, data = conn.uid("fetch", str(uid), "(FLAGS RFC822)")
        except imaplib.IMAP4.abort:
            raise  # the connection is unusable; let the run fail and reconnect next time
        except imaplib.IMAP4.error as exc:
            return self._fetch_failed(source_id, folder, f"IMAP FETCH failed: {exc}")
        if typ != "OK":
            return self._fetch_failed(source_id, folder, f"IMAP FETCH returned {typ}")
        raw_bytes = b""
        flag_parts: list[bytes] = []
        # Servers return FETCH items in their own order: FLAGS may sit in the
        # tuple prefix ("1 (UID 5 FLAGS (\\Seen) RFC822 {n}") or, when RFC822 is
        # answered first, in a trailing bytes element after the literal
        # (" FLAGS (\\Seen))"). Scan every part or read state is lost.
        for part in data:
            if isinstance(part, tuple):
                flag_parts.append(part[0])
                raw_bytes = part[1] or b""
            elif isinstance(part, bytes):
                flag_parts.append(part)
        if not raw_bytes:
            return self._fetch_failed(source_id, folder, "IMAP FETCH returned no RFC822 body")
        flags = tuple(f for p in flag_parts for f in imaplib.ParseFlags(p))
        flag_str = " ".join(f.decode("ascii", "replace") for f in flags)
        parsed = email_lib.message_from_bytes(raw_bytes)
        return SourceMessage(
            source_id=source_id,
            raw_mime=raw_bytes,
            folder_paths=[folder.mapped_path],
            is_read="\\Seen" in flag_str,
            is_flagged="\\Flagged" in flag_str,
            dedup_hash=parsed.get("Message-ID", source_id),
            subject=parsed.get("Subject", ""),
        )

    @staticmethod
    def _fetch_failed(source_id: str, folder: _Folder, error: str) -> SourceMessage:
        log.warning("Could not fetch %s: %s", source_id, error)
        return SourceMessage(
            source_id=source_id, raw_mime=b"", folder_paths=[folder.mapped_path],
            fetch_error=error,
        )

    def inventory_messages(self, user: UserMapping) -> Iterator[SourceMessage]:
        conn = self._connect(user)
        fields = "(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
        for folder in self._folders(conn):
            uidvalidity, _ = self._status(conn, folder.raw_name)
            conn.select(self._quote(folder.raw_name), readonly=True)
            typ, data = conn.uid("search", "ALL")
            if typ != "OK" or not data or not data[0]:
                continue
            for uid_b in data[0].split():
                uid = int(uid_b)
                typ, msgdata = conn.uid("fetch", str(uid), fields)
                size: int | str = ""
                header_bytes = b""
                flags: tuple[bytes, ...] = ()
                for part in msgdata:
                    if isinstance(part, tuple):
                        meta = part[0].decode("ascii", "replace")
                        flags = imaplib.ParseFlags(part[0])
                        sm = re.search(r"RFC822.SIZE (\d+)", meta)
                        if sm:
                            size = int(sm.group(1))
                        header_bytes = part[1] or b""
                flag_str = " ".join(f.decode("ascii", "replace") for f in flags)
                headers = email_lib.message_from_bytes(header_bytes)
                yield SourceMessage(
                    source_id=f"{folder.raw_name}:{uidvalidity}:{uid}",
                    raw_mime=b"",
                    folder_paths=[folder.mapped_path],
                    is_read="\\Seen" in flag_str,
                    subject=headers.get("Subject", "(no subject)"),
                    size_bytes=size,
                    date=headers.get("Date", ""),
                    sender=headers.get("From", ""),
                )
