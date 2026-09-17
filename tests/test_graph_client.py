"""Pure (no-network) tests for GraphClient retry classification + transport reset.

Covers the "works for a while, then every item 400s" cascade: a poisoned
keep-alive connection surfaces as either a deserialize-400 or a RemoteProtocolError,
both of which must be retried with a fresh connection.
"""
from __future__ import annotations

import httpx
import pytest

from migrator.microsoft.graph_client import _is_retryable


def _status_error(code: int, text: str = "") -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://graph.microsoft.com/v1.0/x")
    resp = httpx.Response(code, text=text, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_throttle_and_server_errors_retry(code: int) -> None:
    assert _is_retryable(_status_error(code)) is True


def test_deserialize_400_is_not_retryable() -> None:
    # Content-based, not transient — handled (logged + fallback) at the mail layer.
    body = '{"error":{"code":"UnableToDeserializePostBody","message":"were unable to..."}}'
    assert _is_retryable(_status_error(400, body)) is False


def test_plain_400_is_not_retryable() -> None:
    assert _is_retryable(_status_error(400, '{"error":{"code":"InvalidRequest"}}')) is False


def test_404_is_not_retryable() -> None:
    assert _is_retryable(_status_error(404)) is False


def test_remote_protocol_error_is_retryable() -> None:
    # Half-closed pooled connection ("Server disconnected without sending a response").
    assert _is_retryable(httpx.RemoteProtocolError("Server disconnected")) is True


def test_network_and_timeout_errors_retry() -> None:
    assert _is_retryable(httpx.ConnectTimeout("t")) is True
    assert _is_retryable(httpx.ConnectError("c")) is True


def test_reset_transport_swaps_the_client() -> None:
    from migrator.microsoft.graph_client import GraphClient

    # Construct without touching the network: __init__ only builds an httpx.Client.
    gc = GraphClient.__new__(GraphClient)
    gc._timeout = 60.0
    gc._client = httpx.Client(timeout=60.0)
    original = gc._client

    gc._reset_transport()

    assert gc._client is not original
    assert original.is_closed
    gc._client.close()


# ── request-level behavior (MockTransport, no network) ───────────────────────

from collections.abc import Callable  # noqa: E402

from migrator.microsoft.graph_client import (  # noqa: E402
    GRAPH_BASE,
    GraphClient,
    _parse_retry_after,
)


class _StubTokens:
    def get_token(self) -> str:
        return "tok"


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> GraphClient:
    gc = GraphClient(_StubTokens())  # type: ignore[arg-type]
    gc._client.close()
    gc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gc


def test_parse_retry_after() -> None:
    assert _parse_retry_after("45") == 45
    assert _parse_retry_after(None) == 30
    assert _parse_retry_after("Fri, 31 Dec 2027 23:59:59 GMT") == 30  # HTTP-date form
    assert _parse_retry_after("0") == 1


def test_graph_requests_carry_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True})

    gc = _client_with(handler)
    gc.get("/users/x")
    assert seen[0].headers["Authorization"] == "Bearer tok"
    assert seen[0].headers["Content-Type"] == "application/json"


def test_upload_session_put_omits_authorization() -> None:
    # Docs: uploadUrl is pre-authenticated; sending Authorization can 401.
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(202, json={"nextExpectedRanges": ["26-"]})

    gc = _client_with(handler)
    gc.put_raw(
        "https://up.example.com/session123",
        data=b"chunk",
        headers={"Content-Range": "bytes 0-4/5"},
    )
    assert "Authorization" not in seen[0].headers
    assert seen[0].headers.get("Content-Type") != "application/json"


def test_paginate_does_not_duplicate_params_on_nextlink() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={"value": [1], "@odata.nextLink": f"{GRAPH_BASE}/col?$skiptoken=xyz"},
            )
        return httpx.Response(200, json={"value": [2]})

    gc = _client_with(handler)
    pages = list(gc.paginate("/col", params={"$top": 2}))
    assert pages == [[1], [2]]
    assert seen[0].url.params["$top"] == "2"
    assert "$top" not in seen[1].url.params
    assert seen[1].url.params["$skiptoken"] == "xyz"


def test_rate_limits_reapplied_on_each_retry_attempt(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = {"limits": 0, "requests": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["requests"] += 1
        if calls["requests"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    gc = _client_with(handler)
    monkeypatch.setattr(gc, "_apply_rate_limits", lambda _uk: calls.__setitem__("limits", calls["limits"] + 1))
    monkeypatch.setattr(gc, "_reset_transport", lambda: None)  # keep the mock transport
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)  # skip backoff waits

    gc.get("/users/x")
    assert calls["requests"] == 2
    assert calls["limits"] == 2  # re-acquired on the retry, not just once up front


def test_429_waits_retry_after_once_and_keeps_the_pool(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []
    slept: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="throttled")
        return httpx.Response(200, json={"ok": True})

    gc = _client_with(handler)
    monkeypatch.setattr(
        gc, "_reset_transport", lambda: pytest.fail("pool must not be reset on a 429")
    )
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: slept.append(s))

    assert gc.get("/users/x") == {"ok": True}
    assert len(seen) == 2
    assert slept == [7.0]  # Retry-After honoured exactly once, no extra backoff


def test_5xx_still_backs_off_and_redials(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = {"requests": 0, "resets": 0}
    slept: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls["requests"] += 1
        return httpx.Response(503 if calls["requests"] == 1 else 200, json={"ok": True})

    gc = _client_with(handler)
    monkeypatch.setattr(gc, "_reset_transport", lambda: calls.__setitem__("resets", calls["resets"] + 1))
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: slept.append(s))

    gc.get("/users/x")
    assert calls == {"requests": 2, "resets": 1}
    assert slept and slept[0] >= 2.0  # exponential backoff floor
