from __future__ import annotations

import json
import threading
from pathlib import Path

import msal

_GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


class MSTokenProvider:
    """Thread-safe MSAL confidential-client token provider with disk cache."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        certificate_path: Path | None = None,
        certificate_thumbprint: str | None = None,
        client_secret: str | None = None,
        token_cache_file: Path | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._cache = msal.SerializableTokenCache()

        if token_cache_file and token_cache_file.exists():
            self._cache.deserialize(token_cache_file.read_text())

        self._token_cache_file = token_cache_file

        authority = f"https://login.microsoftonline.com/{tenant_id}"

        if certificate_path and certificate_thumbprint:
            cert_data = certificate_path.read_bytes()
            self._app = msal.ConfidentialClientApplication(
                client_id,
                authority=authority,
                client_credential={
                    "thumbprint": certificate_thumbprint,
                    "private_key": cert_data,
                },
                token_cache=self._cache,
            )
        elif client_secret:
            self._app = msal.ConfidentialClientApplication(
                client_id,
                authority=authority,
                client_credential=client_secret,
                token_cache=self._cache,
            )
        else:
            raise ValueError("Provide either (certificate_path + certificate_thumbprint) or client_secret")

    def get_token(self) -> str:
        with self._lock:
            result = self._app.acquire_token_silent(_GRAPH_SCOPE, account=None)
            if not result:
                result = self._app.acquire_token_for_client(_GRAPH_SCOPE)
            if "access_token" not in result:
                raise RuntimeError(
                    f"Failed to acquire MS token: {result.get('error_description', result)}"
                )
            self._persist_cache()
            return result["access_token"]

    def _persist_cache(self) -> None:
        if self._token_cache_file and self._cache.has_state_changed:
            self._token_cache_file.write_text(self._cache.serialize())
