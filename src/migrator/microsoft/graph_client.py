from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..auth.ms_auth import MSTokenProvider
from ..ratelimit import PerUserRateLimiter, registry

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_MAX_RETRIES = 7


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    # RemoteProtocolError ("Server disconnected"/"connection closed") is the
    # face of a half-closed pooled connection under sustained load — retryable,
    # and the before-sleep hook drops the pool so the retry redials fresh.
    return isinstance(
        exc,
        (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )


class ThrottledError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Throttled — retry after {retry_after}s")
        self.retry_after = retry_after


def _parse_retry_after(value: str | None, default: int = 30) -> int:
    """Retry-After may be missing or an HTTP-date rather than delta-seconds."""
    if not value:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


_BACKOFF = wait_exponential(multiplier=1, min=2, max=60)


def _throttle(retry_state: Any) -> httpx.HTTPStatusError | None:
    """The 429 that failed this attempt, if that is what failed it."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return exc
    return None


def _wait(retry_state: Any) -> float:
    """Sleep Retry-After on a 429 (once — the wait is not also backed off);
    exponential backoff for everything else."""
    if (exc := _throttle(retry_state)) is not None:
        return float(_parse_retry_after(exc.response.headers.get("Retry-After")))
    return float(_BACKOFF(retry_state))


class GraphClient:
    """Thin httpx wrapper: auth injection, tenacity retry, 429/Retry-After, rate limiting."""

    def __init__(
        self,
        token_provider: MSTokenProvider,
        global_limiter_name: str = "graph_global",
        per_user_limiter: PerUserRateLimiter | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._token_provider = token_provider
        self._global_limiter_name = global_limiter_name
        self._per_user_limiter = per_user_limiter
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def _reset_transport(self) -> None:
        """Drop all pooled connections and start a fresh client.

        Called between retries: if a request failed because its keep-alive
        connection was half-closed by the server (the cause of a cascade of
        UnableToDeserializePostBody / RemoteProtocolError failures), reusing the
        pool would just hit the same dead socket. A new client redials clean.
        Safe per-thread: each GraphClient is owned by a single worker thread."""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - best-effort teardown of a bad client
            pass
        self._client = httpx.Client(timeout=self._timeout)

    def _headers(self, extra: dict[str, str] | None = None, *, auth: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {}
        if auth:
            headers["Authorization"] = f"Bearer {self._token_provider.get_token()}"
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update(extra)  # caller may override Content-Type, add Content-Range, etc.
        return headers

    def _apply_rate_limits(self, user_key: str | None) -> None:
        try:
            registry.acquire(self._global_limiter_name)
        except KeyError:
            pass
        if user_key and self._per_user_limiter:
            self._per_user_limiter.acquire(user_key)

    def _request(
        self,
        method: str,
        url: str,
        user_key: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        extra_headers = kwargs.pop("headers", None)
        # Statuses the caller expects as a normal outcome (e.g. 404 on a
        # contact photo probe) — still raised, but not logged as errors.
        quiet_statuses: tuple[int, ...] = tuple(kwargs.pop("quiet_statuses", ()))
        # Upload-session URLs (uploadUrl) are pre-authenticated; the Graph docs
        # direct apps NOT to send Authorization on those PUTs (it can 401).
        # Only requests to the Graph host get the bearer token + JSON default.
        is_graph = url.startswith(GRAPH_BASE)

        def _redial(retry_state: Any) -> None:
            # A 429 is the server's answer over a healthy socket — keep the pool.
            if (exc := _throttle(retry_state)) is not None:
                log.warning(
                    "Graph 429 on %s — retrying after %ss", url,
                    _parse_retry_after(exc.response.headers.get("Retry-After")),
                )
                return
            # Connection-level failure (server-disconnect, deserialize-400 from a
            # poisoned socket): discard the pool so the next attempt redials clean.
            self._reset_transport()

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(_MAX_RETRIES),
            wait=_wait,
            before_sleep=_redial,
            reraise=True,
        )
        def _do() -> httpx.Response:
            # Inside the retried closure so retries never bypass the limiter.
            self._apply_rate_limits(user_key)
            resp = self._client.request(
                method, url, headers=self._headers(extra_headers, auth=is_graph), **kwargs
            )
            if resp.status_code == 429:
                resp.raise_for_status()  # tenacity waits Retry-After (_wait) and retries
            if resp.status_code >= 400 and resp.status_code not in quiet_statuses:
                # Graph 4xx/5xx bodies carry the real reason (error.code/message);
                # raise_for_status() drops them, so surface it before re-raising.
                # Also echo the outgoing JSON body (truncated) — invaluable for
                # UnableToDeserializePostBody and other body-shape rejections.
                # Skip raw `content=` payloads (MIME / file bytes) to avoid dumping MBs,
                # but still report their size — a 0-byte body is the usual cause of
                # UnableToDeserializePostBody on MIME import.
                req_body = kwargs.get("json")
                if req_body is not None:
                    body_desc = repr(req_body)[:2000]
                elif (content := kwargs.get("content")) is not None:
                    body_desc = f"<non-JSON body, {len(content)} bytes>"
                else:
                    body_desc = "<no body>"
                log.error(
                    "Graph %s %s -> %s: %s | request body: %s",
                    method,
                    url,
                    resp.status_code,
                    resp.text,
                    body_desc,
                )
            resp.raise_for_status()
            return resp

        return _do()

    def get(self, path: str, user_key: str | None = None, **kwargs: Any) -> Any:
        resp = self._request("GET", f"{GRAPH_BASE}{path}", user_key=user_key, **kwargs)
        return resp.json()

    def get_bytes(self, path: str, user_key: str | None = None, **kwargs: Any) -> bytes:
        """GET raw response bytes (e.g. message MIME via /$value, file /content)."""
        resp = self._request("GET", f"{GRAPH_BASE}{path}", user_key=user_key, **kwargs)
        return resp.content

    def post(self, path: str, user_key: str | None = None, **kwargs: Any) -> Any:
        resp = self._request("POST", f"{GRAPH_BASE}{path}", user_key=user_key, **kwargs)
        if resp.content:
            return resp.json()
        return None

    def patch(self, path: str, user_key: str | None = None, **kwargs: Any) -> Any:
        resp = self._request("PATCH", f"{GRAPH_BASE}{path}", user_key=user_key, **kwargs)
        if resp.content:
            return resp.json()
        return None

    def delete(self, path: str, user_key: str | None = None, **kwargs: Any) -> None:
        self._request("DELETE", f"{GRAPH_BASE}{path}", user_key=user_key, **kwargs)

    def put_raw(self, url: str, data: bytes, user_key: str | None = None, **kwargs: Any) -> Any:
        """Used for chunked upload sessions (absolute URL, not path-relative).

        Routed through _request so chunk PUTs get rate limiting, tenacity retry,
        and 429/Retry-After handling — the throttle-prone large-upload path."""
        resp = self._request("PUT", url, user_key=user_key, content=data, **kwargs)
        if resp.content:
            return resp.json()
        return None

    def paginate(
        self, path: str, user_key: str | None = None, **kwargs: Any
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield pages from a Graph collection, following @odata.nextLink."""
        url: str | None = f"{GRAPH_BASE}{path}"
        first = True
        while url:
            kw = kwargs
            if not first:
                # nextLink already embeds the query ($top, skiptoken, ...) —
                # re-sending the original params would duplicate them.
                kw = {k: v for k, v in kwargs.items() if k != "params"}
            if url.startswith(GRAPH_BASE):
                data = self.get(url[len(GRAPH_BASE):], user_key=user_key, **kw)
            else:
                resp = self._request("GET", url, user_key=user_key, **kw)
                data = resp.json()
            yield data.get("value", [])
            url = data.get("@odata.nextLink")
            first = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
