"""IMAP connector against a fake connection: UID search semantics, FLAGS
parsing regardless of server item order, and per-message fetch failures.

RFC 3501 §6.4.8: a bare set in `UID SEARCH <set>` is message *sequence numbers*;
only `UID SEARCH UID <set>` is a UID range. The difference is invisible on a
folder that has never been expunged and silently drops mail on one that has.
"""
from __future__ import annotations

import imaplib
import json
from typing import Any

import pytest

from migrator.config import ImapSourceConfig, UserMapping
from migrator.connectors.imap import ImapSource

_RAW = b"From: a@x\r\nSubject: hi\r\nMessage-ID: <m1@x>\r\n\r\nbody\r\n"
_USER = UserMapping(source_id="u@x", dest_id="u@y", imap_password="pw")


class _FakeImap:
    """Just enough of imaplib.IMAP4 for iter_messages: one INBOX folder with
    UIDVALIDITY 7 / UIDNEXT 200 and a scripted FETCH response."""

    def __init__(
        self,
        search_uids: bytes = b"150 151",
        fetch: tuple[str, list[Any]] | None = None,
        fetch_exc: Exception | None = None,
    ) -> None:
        self.commands: list[tuple[str, tuple[str, ...]]] = []
        self.search_uids = search_uids
        self.fetch = fetch or ("OK", [(b"1 (UID 150 FLAGS (\\Seen) RFC822 {56}", _RAW), b")"])
        self.fetch_exc = fetch_exc

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [b'(\\HasNoChildren) "/" INBOX']

    def status(self, name: str, items: str) -> tuple[str, list[bytes]]:
        return "OK", [b"INBOX (UIDVALIDITY 7 UIDNEXT 200)"]

    def select(self, name: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly is True  # sources never write back
        return "OK", [b"2"]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.commands.append((command.upper(), args))
        if command.upper() == "SEARCH":
            return "OK", [self.search_uids]
        if self.fetch_exc is not None:
            raise self.fetch_exc
        return self.fetch

    def logout(self) -> None:
        pass


def _source(fake: _FakeImap) -> ImapSource:
    src = ImapSource(ImapSourceConfig(host="imap.example"))
    src._conn = fake  # type: ignore[assignment]
    return src


def _search_args(fake: _FakeImap) -> tuple[str, ...]:
    return next(args for cmd, args in fake.commands if cmd == "SEARCH")


def test_full_pass_searches_uid_range_from_one() -> None:
    fake = _FakeImap()
    list(_source(fake).iter_messages(_USER, None))
    assert _search_args(fake) == ("UID", "1:*")


def test_delta_pass_searches_uid_range_from_stored_uidnext() -> None:
    fake = _FakeImap()
    since = json.dumps({"INBOX": {"uidvalidity": 7, "uidnext": 150}})
    msgs = list(_source(fake).iter_messages(_USER, since))
    assert _search_args(fake) == ("UID", "150:*")
    assert [m.source_id for m in msgs] == ["INBOX:7:150", "INBOX:7:151"]


def test_delta_skips_folder_with_no_new_mail() -> None:
    fake = _FakeImap()
    since = json.dumps({"INBOX": {"uidvalidity": 7, "uidnext": 200}})
    assert list(_source(fake).iter_messages(_USER, since)) == []
    assert not any(cmd == "SEARCH" for cmd, _ in fake.commands)


def test_flags_in_tuple_prefix_are_read() -> None:
    msgs = list(_source(_FakeImap()).iter_messages(_USER, None))
    assert msgs[0].is_read is True
    assert msgs[0].raw_mime == _RAW


def test_flags_after_literal_are_read() -> None:
    # Request-order servers (e.g. Dovecot) answer RFC822 first; FLAGS then sits
    # in a trailing bytes element, not in the tuple prefix.
    fake = _FakeImap(fetch=("OK", [(b"1 (UID 150 RFC822 {56}", _RAW), b" FLAGS (\\Seen \\Flagged))"]))
    msgs = list(_source(fake).iter_messages(_USER, None))
    assert msgs[0].is_read is True
    assert msgs[0].is_flagged is True


def test_fetch_protocol_error_becomes_fetch_error_stub() -> None:
    fake = _FakeImap(fetch_exc=imaplib.IMAP4.error("FETCH command error: BAD"))
    msgs = list(_source(fake).iter_messages(_USER, None))
    assert len(msgs) == 2
    assert all(m.fetch_error and m.raw_mime == b"" for m in msgs)
    assert msgs[0].folder_paths == ["Inbox"]


def test_fetch_non_ok_becomes_fetch_error_stub() -> None:
    fake = _FakeImap(fetch=("NO", [None]))
    msgs = list(_source(fake).iter_messages(_USER, None))
    assert msgs[0].fetch_error.startswith("IMAP FETCH returned NO")


def test_connection_abort_propagates() -> None:
    fake = _FakeImap(fetch_exc=imaplib.IMAP4.abort("socket error"))
    with pytest.raises(imaplib.IMAP4.abort):
        list(_source(fake).iter_messages(_USER, None))


def test_cursor_records_uidvalidity_and_uidnext() -> None:
    src = _source(_FakeImap())
    list(src.iter_messages(_USER, None))
    assert json.loads(src.get_last_cursor("mail") or "{}") == {
        "INBOX": {"uidvalidity": 7, "uidnext": 200}
    }
