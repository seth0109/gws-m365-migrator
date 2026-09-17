"""Gmail wrapper: per-message fetch failures must not kill the generator.

The raw-message GET runs inside the connector's generator, so an exception
there used to escape mail_job's per-item try and fail the whole mailbox run.
"""
from __future__ import annotations

from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from migrator.config import GoogleWorkspaceSourceConfig
from migrator.connectors.google import GoogleWorkspaceSource
from migrator.google import NUM_RETRIES, gmail


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"err")


class _Req:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None,
                 seen: list[int] | None = None) -> None:
        self.result, self.error, self.seen = result, error, seen

    def execute(self, num_retries: int = 0) -> dict[str, Any]:
        if self.seen is not None:
            self.seen.append(num_retries)
        if self.error:
            raise self.error
        return self.result or {}


class _Svc:
    """messages().list() returns a, b, c; get() 404s b and 500s c."""

    def __init__(self) -> None:
        self.retries: list[int] = []

    def users(self) -> _Svc:
        return self

    def messages(self) -> _Svc:
        return self

    def list(self, **params: Any) -> _Req:
        return _Req({"messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}, seen=self.retries)

    def get(self, *, userId: str, id: str, format: str) -> _Req:  # noqa: A002 - API kwarg
        errors = {"b": _http_error(404), "c": _http_error(500)}
        return _Req({"id": id, "raw": "QQ=="}, error=errors.get(id), seen=self.retries)


def _cfg() -> GoogleWorkspaceSourceConfig:
    return GoogleWorkspaceSourceConfig(service_account_key_file="k.json", admin_email="a@x")


def test_iter_messages_skips_404_and_stubs_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _Svc()
    monkeypatch.setattr(gmail, "_svc", lambda cfg, email: svc)
    out = list(gmail.iter_messages(_cfg(), "u@x"))
    assert [m["id"] for m in out] == ["a", "c"]  # b vanished → skipped, not fatal
    assert "raw" in out[0]
    assert out[1]["fetch_error"]  # c: transient error → stub for the job to fail
    # Every execute() carried the library-level retry budget.
    assert svc.retries and all(n == NUM_RETRIES for n in svc.retries)


def test_connector_turns_fetch_error_stub_into_failed_message() -> None:
    source = GoogleWorkspaceSource(_cfg(), "categories")
    msg = source._to_message({"id": "c", "fetch_error": "HttpError 500"}, {})
    assert msg.source_id == "c"
    assert msg.raw_mime == b""
    assert msg.fetch_error == "HttpError 500"
