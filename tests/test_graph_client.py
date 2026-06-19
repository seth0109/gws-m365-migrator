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


def test_deserialize_400_is_retryable() -> None:
    body = '{"error":{"code":"UnableToDeserializePostBody","message":"were unable to..."}}'
    assert _is_retryable(_status_error(400, body)) is True


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
