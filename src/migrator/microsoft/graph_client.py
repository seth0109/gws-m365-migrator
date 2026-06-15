from __future__ import annotations

import logging
import time
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
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class ThrottledError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Throttled — retry after {retry_after}s")
        self.retry_after = retry_after


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
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider.get_token()}",
            "Content-Type": "application/json",
        }

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
        self._apply_rate_limits(user_key)

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(_MAX_RETRIES),
            wait=wait_exponential(multiplier=1, min=2, max=60),
            reraise=True,
        )
        def _do() -> httpx.Response:
            resp = self._client.request(method, url, headers=self._headers(), **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "30"))
                log.warning("Graph 429 on %s — sleeping %ss", url, retry_after)
                time.sleep(retry_after)
                resp.raise_for_status()  # trigger tenacity retry
            resp.raise_for_status()
            return resp

        return _do()

    def get(self, path: str, user_key: str | None = None, **kwargs: Any) -> Any:
        resp = self._request("GET", f"{GRAPH_BASE}{path}", user_key=user_key, **kwargs)
        return resp.json()

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
        """Used for chunked upload sessions (absolute URL, not path-relative)."""
        self._apply_rate_limits(user_key)
        resp = self._client.put(url, content=data, headers=self._headers(), **kwargs)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None

    def paginate(self, path: str, user_key: str | None = None, **kwargs: Any):
        """Yield pages from a Graph collection, following @odata.nextLink."""
        url: str | None = f"{GRAPH_BASE}{path}"
        while url:
            if url.startswith(GRAPH_BASE):
                data = self.get(url[len(GRAPH_BASE):], user_key=user_key, **kwargs)
            else:
                resp = self._request("GET", url, user_key=user_key, **kwargs)
                data = resp.json()
            yield data.get("value", [])
            url = data.get("@odata.nextLink")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GraphClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
