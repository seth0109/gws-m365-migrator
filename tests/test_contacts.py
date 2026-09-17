"""Tests for the contacts writer and contact-photo plumbing."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from migrator.config import GoogleWorkspaceSourceConfig, UserMapping
from migrator.connectors.base import SourceContact
from migrator.connectors.google import GoogleWorkspaceSource
from migrator.microsoft.contacts import ensure_contact_folder, set_contact_photo

# ── Google photo selection ────────────────────────────────────────────────────


def _google_source() -> GoogleWorkspaceSource:
    cfg = GoogleWorkspaceSourceConfig(
        type="google_workspace",
        service_account_key_file="k.json",
        admin_email="admin@old.com",
    )
    return GoogleWorkspaceSource(cfg, "categories")


def test_to_contact_picks_real_photo_not_default_avatar() -> None:
    person = {
        "resourceName": "people/c1",
        "names": [{"displayName": "Alice"}],
        "photos": [
            {"url": "https://lh3/avatar=s100", "default": True},
            {"url": "https://lh3/real=s100"},
        ],
    }
    contact = _google_source()._to_contact(person, {})
    assert contact.photo_ref == "https://lh3/real=s100"


def test_to_contact_ignores_generated_avatar() -> None:
    person = {
        "resourceName": "people/c2",
        "photos": [{"url": "https://lh3/avatar=s100", "default": True}],
    }
    assert _google_source()._to_contact(person, {}).photo_ref == ""


def test_fetch_contact_photo_upsizes_and_checks_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        seen["url"] = url
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"JPG")

    monkeypatch.setattr("migrator.connectors.google.httpx.get", fake_get)
    user = UserMapping(source_id="a@old.com", dest_id="a@new.com")
    contact = SourceContact(source_id="c1", graph_body={}, photo_ref="https://lh3/p=s100")
    assert _google_source().fetch_contact_photo(user, contact) == b"JPG"
    assert seen["url"].endswith("=s512")  # thumbnail upsized


def test_fetch_contact_photo_rejects_non_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "migrator.connectors.google.httpx.get",
        lambda url, **kw: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>login</html>"
        ),
    )
    user = UserMapping(source_id="a@old.com", dest_id="a@new.com")
    contact = SourceContact(source_id="c1", graph_body={}, photo_ref="https://lh3/p=s100")
    assert _google_source().fetch_contact_photo(user, contact) is None


# ── destination writer ────────────────────────────────────────────────────────


def _status_error(code: int) -> httpx.HTTPStatusError:
    resp = httpx.Response(code, request=httpx.Request("POST", "https://g/x"))
    return httpx.HTTPStatusError("boom", request=resp.request, response=resp)


class _FolderGC:
    def __init__(
        self,
        pages: list[list[dict[str, Any]]],
        post_error: Exception | None = None,
        pages_after_conflict: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self._pages = pages
        self._post_error = post_error
        self._pages_after_conflict = pages_after_conflict
        self.paginate_calls = 0
        self.post_calls = 0

    def paginate(self, path: str, **kwargs: Any) -> Iterator[list[dict[str, Any]]]:
        self.paginate_calls += 1
        if self.paginate_calls > 1 and self._pages_after_conflict is not None:
            yield from self._pages_after_conflict
        else:
            yield from self._pages

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.post_calls += 1
        if self._post_error is not None:
            raise self._post_error
        return {"id": "created-cf"}


def test_ensure_contact_folder_finds_beyond_first_page() -> None:
    gc = _FolderGC(pages=[
        [{"id": "f1", "displayName": "Other"}],
        [{"id": "f2", "displayName": "Clients"}],
    ])
    assert ensure_contact_folder(gc, "u", "Clients") == "f2"  # type: ignore[arg-type]
    assert gc.post_calls == 0


def test_ensure_contact_folder_recovers_from_409() -> None:
    gc = _FolderGC(
        pages=[[]],
        post_error=_status_error(409),
        pages_after_conflict=[[{"id": "raced", "displayName": "Clients"}]],
    )
    assert ensure_contact_folder(gc, "u", "Clients") == "raced"  # type: ignore[arg-type]


def test_set_contact_photo_puts_jpeg() -> None:
    class _GC:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes, dict[str, str] | None]] = []

        def put_raw(self, url: str, data: bytes, **kwargs: Any) -> None:
            self.calls.append((url, data, kwargs.get("headers")))

    gc = _GC()
    set_contact_photo(gc, "u", "c1", b"IMG")  # type: ignore[arg-type]
    url, data, headers = gc.calls[0]
    assert url.endswith("/users/u/contacts/c1/photo/$value")
    assert data == b"IMG"
    assert headers is not None and headers["Content-Type"] == "image/jpeg"
