"""Tests for the OneDrive/SharePoint files writer.

Provisioning: a destination user's OneDrive is created lazily, so
/users/{id}/drive 404s with "User's mysite not found" until it comes online.
ensure_onedrive must trigger + wait for provisioning and fail clearly.

Folders: Graph's /children endpoint does not support $filter, so ensure_folder
must paginate and match client-side (case-insensitively) and recover from a 409
nameAlreadyExists. Simple upload must be PUT (POST is 405 per Graph docs).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from migrator.microsoft.files import (
    _is_mysite_missing,
    ensure_folder,
    ensure_onedrive,
    upload_large_file,
    upload_small_file,
)
from migrator.microsoft.graph_client import GraphClient


def _resp(code: int, text: str = "") -> httpx.Response:
    return httpx.Response(code, text=text, request=httpx.Request("GET", "https://g/x"))


def _status_error(code: int, text: str = "") -> httpx.HTTPStatusError:
    resp = _resp(code, text)
    return httpx.HTTPStatusError("boom", request=resp.request, response=resp)


def test_is_mysite_missing_detects_provisioning_404() -> None:
    body = '{"error":{"code":"ResourceNotFound","message":"User\'s mysite not found."}}'
    assert _is_mysite_missing(_resp(404, body)) is True


def test_is_mysite_missing_ignores_non_404() -> None:
    assert _is_mysite_missing(_resp(403, "mysite")) is False


class _FakeGC:
    """Minimal stand-in: raises a mysite-404 for the first `fail_times` GETs."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def get(self, path: str, **kwargs: object) -> dict[str, str]:
        self.calls += 1
        if self.calls <= self.fail_times:
            body = '{"error":{"code":"ResourceNotFound","message":"User\'s mysite not found."}}'
            raise _status_error(404, body)
        return {"id": "drive-id"}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Skip the real provisioning waits so tests run instantly.
    monkeypatch.setattr("migrator.microsoft.files.time.sleep", lambda _s: None)


def test_ensure_onedrive_returns_once_provisioned() -> None:
    gc = _FakeGC(fail_times=2)  # 404 twice, then succeeds
    ensure_onedrive(gc, "user-id")  # type: ignore[arg-type]
    assert gc.calls == 3


def test_ensure_onedrive_succeeds_immediately() -> None:
    gc = _FakeGC(fail_times=0)
    ensure_onedrive(gc, "user-id")  # type: ignore[arg-type]
    assert gc.calls == 1


def test_ensure_onedrive_raises_clear_error_if_never_provisioned() -> None:
    gc = _FakeGC(fail_times=999)
    with pytest.raises(RuntimeError, match="not provisioned"):
        ensure_onedrive(gc, "user-id")  # type: ignore[arg-type]


def test_never_provisioned_error_points_at_the_shared_mailbox_setting() -> None:
    # The usual cause of a OneDrive that never appears is an unflagged shared mailbox.
    with pytest.raises(RuntimeError, match="mailbox_type: shared"):
        ensure_onedrive(_FakeGC(fail_times=999), "user-id")  # type: ignore[arg-type]


def test_ensure_onedrive_reraises_unrelated_errors() -> None:
    class _BadGC:
        def get(self, path: str, **kwargs: object) -> dict[str, str]:
            raise _status_error(403, "Forbidden")

    with pytest.raises(httpx.HTTPStatusError):
        ensure_onedrive(_BadGC(), "user-id")  # type: ignore[arg-type]


class _DriveGC:
    """Fake GraphClient for folder/upload paths: paginated children listing,
    optional 409 on create, and a recording put_raw."""

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
        self.put_calls: list[tuple[str, bytes]] = []

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
        return {"id": "created-id"}

    def put_raw(self, url: str, data: bytes, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append((url, data))
        return {"id": "uploaded-id"}


def test_ensure_folder_finds_existing_beyond_first_page_case_insensitive() -> None:
    gc = _DriveGC(
        pages=[
            [{"id": "f1", "name": "Other", "folder": {}}],
            [{"id": "f2", "name": "REPORTS", "folder": {}}],
        ]
    )
    got = ensure_folder(gc, "users/u/drive", "u", None, "Reports")  # type: ignore[arg-type]
    assert got == "f2"
    assert gc.post_calls == 0


def test_ensure_folder_ignores_file_with_same_name() -> None:
    gc = _DriveGC(pages=[[{"id": "x1", "name": "Reports", "file": {}}]])
    got = ensure_folder(gc, "users/u/drive", "u", None, "Reports")  # type: ignore[arg-type]
    assert got == "created-id"
    assert gc.post_calls == 1


def test_ensure_folder_creates_when_absent() -> None:
    gc = _DriveGC(pages=[[]])
    got = ensure_folder(gc, "users/u/drive", "u", "parent1", "New Folder")  # type: ignore[arg-type]
    assert got == "created-id"


def test_ensure_folder_recovers_from_409_by_relisting() -> None:
    gc = _DriveGC(
        pages=[[]],
        post_error=_status_error(409, '{"error":{"code":"nameAlreadyExists"}}'),
        pages_after_conflict=[[{"id": "raced", "name": "Reports", "folder": {}}]],
    )
    got = ensure_folder(gc, "users/u/drive", "u", None, "Reports")  # type: ignore[arg-type]
    assert got == "raced"


def test_ensure_folder_reraises_non_409() -> None:
    gc = _DriveGC(pages=[[]], post_error=_status_error(403, "Forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        ensure_folder(gc, "users/u/drive", "u", None, "Reports")  # type: ignore[arg-type]


def test_upload_small_file_uses_put() -> None:
    gc = _DriveGC(pages=[[]])
    got = upload_small_file(gc, "users/u/drive", "u", "p1", "a.txt", b"data")  # type: ignore[arg-type]
    assert got == "uploaded-id"
    assert len(gc.put_calls) == 1
    url, data = gc.put_calls[0]
    assert url.endswith("/users/u/drive/items/p1:/a.txt:/content")
    assert data == b"data"


# ── file names in path-based addressing (real client, fake transport) ────────


class _Tokens:
    def get_token(self) -> str:
        return "tok"


def _recording_client(seen: list[httpx.Request]) -> GraphClient:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path.endswith("createUploadSession"):
            return httpx.Response(200, json={"uploadUrl": "https://up.example.com/s1"})
        return httpx.Response(201, json={"id": "new-item"})

    gc = GraphClient(_Tokens())  # type: ignore[arg-type]
    gc._client.close()
    gc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gc


def test_small_upload_encodes_hash_and_percent_in_the_name() -> None:
    # OneDrive allows "#" and "%" in names (an M365 source passes them through).
    # Unencoded, "#" starts a URL fragment: the request would go to
    # ".../items/p1:/Q3 " and never reach ":/content".
    seen: list[httpx.Request] = []
    gc = _recording_client(seen)
    assert upload_small_file(gc, "users/u/drive", "u", "p1", "Q3 #50%.pdf", b"x") == "new-item"
    assert seen[0].method == "PUT"
    assert seen[0].url.raw_path == b"/v1.0/users/u/drive/items/p1:/Q3%20%2350%25.pdf:/content"
    assert seen[0].url.fragment == ""


def test_large_upload_session_encodes_the_name_too() -> None:
    seen: list[httpx.Request] = []
    gc = _recording_client(seen)
    upload_large_file(gc, "drives/D1", "D1", "p1", "notes #1?.txt", b"x")
    assert seen[0].url.raw_path == (
        b"/v1.0/drives/D1/items/p1:/notes%20%231%3F.txt:/createUploadSession"
    )
    # The chunk goes to the pre-authenticated upload host without a bearer token.
    assert seen[1].url.host == "up.example.com"
    assert "Authorization" not in seen[1].headers
