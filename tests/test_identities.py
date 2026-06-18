from __future__ import annotations

from migrator.config import UserMapping
from migrator.transform.identities import IdentityMap


def _users() -> list[UserMapping]:
    return [
        UserMapping(source_id="alice@old.com", dest_id="alice@new.com"),
        UserMapping(source_id="Bob@OLD.com", dest_id="bob@new.com"),
    ]


def test_map_address_rewrites_known_and_is_case_insensitive() -> None:
    im = IdentityMap(_users())
    assert im.map_address("alice@old.com") == "alice@new.com"
    # Lookup is case-insensitive on both the stored key and the query.
    assert im.map_address("ALICE@OLD.COM") == "alice@new.com"
    assert im.map_address("bob@old.com") == "bob@new.com"


def test_map_address_passes_through_unknown_and_empty() -> None:
    im = IdentityMap(_users())
    assert im.map_address("external@elsewhere.com") == "external@elsewhere.com"
    assert im.map_address("") == ""
    assert im.map_address(None) is None


def test_remap_event_rewrites_attendees_and_organizer() -> None:
    im = IdentityMap(_users())
    body = {
        "subject": "Sync",
        "attendees": [
            {"emailAddress": {"address": "alice@old.com", "name": "Alice"}, "type": "required"},
            {"emailAddress": {"address": "ext@partner.com", "name": "Ext"}, "type": "optional"},
        ],
        "organizer": {"emailAddress": {"address": "bob@old.com", "name": "Bob"}},
    }
    returned = im.remap_event(body)

    assert returned is body  # mutates in place and returns for chaining
    assert body["attendees"][0]["emailAddress"]["address"] == "alice@new.com"
    # External attendee left untouched.
    assert body["attendees"][1]["emailAddress"]["address"] == "ext@partner.com"
    # Names and other fields preserved.
    assert body["attendees"][0]["emailAddress"]["name"] == "Alice"
    assert body["organizer"]["emailAddress"]["address"] == "bob@new.com"


def test_remap_event_handles_missing_fields() -> None:
    im = IdentityMap(_users())
    # Cancelled-event sentinel / minimal bodies must not raise.
    assert im.remap_event({}) == {}
    assert im.remap_event({"attendees": None, "organizer": None}) == {
        "attendees": None,
        "organizer": None,
    }
    # Malformed attendee entries are tolerated.
    body = {"attendees": [{"type": "required"}, {"emailAddress": {}}]}
    im.remap_event(body)  # no exception
