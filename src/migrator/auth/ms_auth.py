from __future__ import annotations

import threading
from pathlib import Path

import msal

from ..config import (
    CLOUD_ENDPOINTS,
    Microsoft365DestinationConfig,
    Microsoft365SourceConfig,
)
from .keyfile import KeyFileError, read_private_key


class MSTokenProvider:
    """Thread-safe MSAL confidential-client token provider with disk cache.

    `cloud` selects the national cloud (see `config.CLOUD_ENDPOINTS`): it sets
    the sign-in authority and the token scope, and `graph_base` exposes the
    matching Graph service root so a GraphClient built on this provider calls
    the cloud its token is valid for."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        certificate_path: Path | None = None,
        certificate_thumbprint: str | None = None,
        client_secret: str | None = None,
        token_cache_file: Path | None = None,
        cloud: str = "global",
    ) -> None:
        self._lock = threading.Lock()
        self._cache = msal.SerializableTokenCache()

        if token_cache_file and token_cache_file.exists():
            self._cache.deserialize(token_cache_file.read_text())

        self._token_cache_file = token_cache_file

        endpoints = CLOUD_ENDPOINTS[cloud]
        self.graph_base = f"{endpoints.graph}/v1.0"
        self._scope = [f"{endpoints.graph}/.default"]
        authority = f"{endpoints.login}/{tenant_id}"

        if certificate_path and certificate_thumbprint:
            # Parsed here, before any network call: MSAL only touches the key
            # at the first token request, and reports a bad one opaquely.
            cert_data = read_private_key(certificate_path)
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
            raise ValueError(
                "Provide either (certificate_path + certificate_thumbprint) or client_secret"
            )

    @classmethod
    def from_config(
        cls, cfg: Microsoft365SourceConfig | Microsoft365DestinationConfig
    ) -> MSTokenProvider:
        """Build the provider for one tenant block (source or destination)."""
        try:
            return cls(
                tenant_id=cfg.tenant_id,
                client_id=cfg.client_id,
                certificate_path=cfg.certificate_path,
                certificate_thumbprint=cfg.certificate_thumbprint,
                client_secret=cfg.client_secret,
                token_cache_file=cfg.token_cache_file,
                cloud=cfg.cloud,
            )
        except KeyFileError as exc:
            # Two tenants, two key files: say which block the bad one belongs to.
            block = "source" if isinstance(cfg, Microsoft365SourceConfig) else "destination"
            raise KeyFileError(f"{block}.{exc}") from exc.__cause__

    def get_token(self) -> str:
        with self._lock:
            result = self._app.acquire_token_silent(self._scope, account=None)
            if not result:
                result = self._app.acquire_token_for_client(self._scope)
            if "access_token" not in result:
                raise RuntimeError(
                    f"Failed to acquire MS token: {result.get('error_description', result)}"
                )
            self._persist_cache()
            return str(result["access_token"])

    def _persist_cache(self) -> None:
        if self._token_cache_file and self._cache.has_state_changed:
            self._token_cache_file.write_text(self._cache.serialize())
