"""Tests for OneDrive provisioning handling in the files writer.

A destination user's OneDrive is created lazily, so /users/{id}/drive 404s with
"User's mysite not found" until it comes online. ensure_onedrive must trigger +
wait for provisioning and fail with a clear error if it never appears.
"""
from __future__ import annotations

import httpx
import pytest

from migrator.microsoft.files import _is_mysite_missing, ensure_onedrive


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


def test_ensure_onedrive_reraises_unrelated_errors() -> None:
    class _BadGC:
        def get(self, path: str, **kwargs: object) -> dict[str, str]:
            raise _status_error(403, "Forbidden")

    with pytest.raises(httpx.HTTPStatusError):
        ensure_onedrive(_BadGC(), "user-id")  # type: ignore[arg-type]
