"""`migrator smoke-test` findings.

The smoke test is the first command run against a new pair of tenants. What it
is there to find — the pilot account is not in the tenant under that name, a
permission was never consented, the account has no mailbox, sign-in is refused
— must come out as one line that says what to fix, not as an httpx traceback.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from migrator.auth.ms_auth import MSTokenProvider, TokenError
from migrator.cli import app
from migrator.config import CLOUD_ENDPOINTS
from migrator.microsoft import graph_client

SRC_USER = "adamb@src.example"
DST_USER = "adamb@dst.onmicrosoft.us"
Handler = Callable[[httpx.Request], httpx.Response]


class _StubTokens:
    """Cloud-aware like the real provider, so each client keeps its own host."""

    def __init__(self, cfg: Any, refuse: str | None = None) -> None:
        self.graph_base = f"{CLOUD_ENDPOINTS[cfg.cloud].graph}/v1.0"
        self._refuse = refuse if cfg.cloud == "gcc_high" else None

    def get_token(self) -> str:
        if self._refuse:
            raise TokenError(f"Failed to acquire MS token: {self._refuse}")
        return "tok"


def _error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


def _not_found(user: str) -> httpx.Response:
    return _error(404, "Request_ResourceNotFound", f"Resource '{user}' does not exist.")


def _tenants(dest: Handler, source: Handler | None = None) -> Handler:
    """Route by Graph host: commercial source, GCC High destination."""

    def source_ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "S1", "displayName": "Adam B"})

    def route(req: httpx.Request) -> httpx.Response:
        if req.url.host == "graph.microsoft.com":
            return (source or source_ok)(req)
        assert req.url.host == "graph.microsoft.us"
        return dest(req)

    return route


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler,
    refuse_token: str | None = None,
    source_id: str = SRC_USER,
) -> tuple[int, str, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    real_client = httpx.Client
    monkeypatch.setattr(
        graph_client.httpx, "Client",
        lambda **_kw: real_client(transport=httpx.MockTransport(recording)),
    )
    monkeypatch.setattr(
        MSTokenProvider, "from_config",
        classmethod(lambda cls, cfg: _StubTokens(cfg, refuse_token)),
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "state_db": str(tmp_path / "state.db"),
        "log_file": None,
        "source": {
            "type": "microsoft365", "tenant_id": "src-tenant", "client_id": "c",
            "client_secret": "s",
        },
        "destination": {
            "tenant_id": "dst-tenant", "client_id": "c", "client_secret": "s",
            "cloud": "gcc_high",
        },
        "users": [{"source_id": source_id, "dest_id": DST_USER}],
    }), encoding="utf-8")
    result = CliRunner().invoke(
        app, ["smoke-test", "--config", str(config)], env={"COLUMNS": "200"}
    )
    return result.exit_code, " ".join(result.output.split()), seen


def _dest_calls(seen: list[httpx.Request]) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in seen if r.url.host == "graph.microsoft.us"]


# ── the pilot account is not in the tenant under that name ───────────────────


def test_missing_destination_user_is_explained_with_similar_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dest(req: httpx.Request) -> httpx.Response:
        if req.url.path == f"/v1.0/users/{DST_USER}":
            return _not_found(DST_USER)
        assert req.url.path == "/v1.0/users"
        assert req.url.params["$filter"] == "startswith(userPrincipalName,'adamb')"
        return httpx.Response(200, json={"value": [{"userPrincipalName": "adamb@dst.us"}]})

    code, text, seen = _run(tmp_path, monkeypatch, _tenants(dest))
    assert code == 1
    assert "M365 source OK" in text  # the source side passed; only the destination is at fault
    assert (
        f"destination: no user '{DST_USER}' in tenant dst-tenant (gcc_high cloud, "
        "https://graph.microsoft.us/v1.0)" in text
    )
    assert "users[].dest_id must be the account's sign-in name" in text
    assert "Similar sign-in names there: adamb@dst.us." in text
    assert "Traceback" not in text and "Smoke test passed" not in text
    # Nothing was written, and nothing left the destination's own cloud.
    assert _dest_calls(seen) == [("GET", f"/v1.0/users/{DST_USER}"), ("GET", "/v1.0/users")]


def test_no_similar_name_shows_what_sign_in_names_look_like(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dest(req: httpx.Request) -> httpx.Response:
        if req.url.path == f"/v1.0/users/{DST_USER}":
            return _not_found(DST_USER)
        if "$filter" in req.url.params:
            return httpx.Response(200, json={"value": []})
        return httpx.Response(200, json={"value": [
            {"userPrincipalName": "admin@dst.onmicrosoft.us"},
            {"userPrincipalName": "adam.b@dst.us"},
        ]})

    code, text, _ = _run(tmp_path, monkeypatch, _tenants(dest))
    assert code == 1
    assert (
        "Sign-in names in that tenant look like: admin@dst.onmicrosoft.us, adam.b@dst.us."
        in text
    )


def test_empty_tenant_listing_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def dest(req: httpx.Request) -> httpx.Response:
        if req.url.path == f"/v1.0/users/{DST_USER}":
            return _not_found(DST_USER)
        return httpx.Response(200, json={"value": []})

    code, text, _ = _run(tmp_path, monkeypatch, _tenants(dest))
    assert code == 1 and "The app could not list any user in that tenant." in text


def test_hint_lookup_failure_does_not_hide_the_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dest(req: httpx.Request) -> httpx.Response:
        if req.url.path == f"/v1.0/users/{DST_USER}":
            return _not_found(DST_USER)
        return _error(403, "Authorization_RequestDenied", "Insufficient privileges.")

    code, text, _ = _run(tmp_path, monkeypatch, _tenants(dest))
    assert code == 1
    assert f"destination: no user '{DST_USER}'" in text and "Traceback" not in text
    assert "sign-in names there" not in text and "look like" not in text


def test_missing_source_user_names_the_source_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def source(req: httpx.Request) -> httpx.Response:
        if req.url.path == f"/v1.0/users/{SRC_USER}":
            return _not_found(SRC_USER)
        return httpx.Response(200, json={"value": []})

    def dest(req: httpx.Request) -> httpx.Response:
        raise AssertionError("destination must not be contacted after a source finding")

    code, text, _ = _run(tmp_path, monkeypatch, _tenants(dest, source))
    assert code == 1
    assert f"source: no user '{SRC_USER}' in tenant src-tenant (global cloud" in text
    assert "users[].source_id must be" in text


def test_odata_quote_in_the_sign_in_name_is_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filters: list[str] = []

    def source(req: httpx.Request) -> httpx.Response:
        if "$filter" in req.url.params:
            filters.append(req.url.params["$filter"])
        if req.url.path == "/v1.0/users":
            return httpx.Response(200, json={"value": []})
        return _not_found("x")

    dest: Handler = lambda req: _not_found("x")  # noqa: E731 - never reached
    code, _, _ = _run(
        tmp_path, monkeypatch, _tenants(dest, source), source_id="o'brien@src.example"
    )
    assert code == 1
    assert filters == ["startswith(userPrincipalName,'o''brien')"]


# ── other findings: Graph's own code and message, plus what it means here ────


def _dest_with_user(folder_response: httpx.Response) -> Handler:
    def dest(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json={"id": "D1", "displayName": "Adam B"})
        if req.method == "POST":
            return folder_response
        return httpx.Response(204)

    return dest


def test_missing_permission_is_reported_with_the_consent_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = _error(403, "ErrorAccessDenied", "Access is denied. Check credentials and try again.")
    code, text, _ = _run(tmp_path, monkeypatch, _tenants(_dest_with_user(denied)))
    assert code == 1
    assert (
        "destination: Graph answered 403 ErrorAccessDenied to POST "
        "https://graph.microsoft.us/v1.0/users/D1/mailFolders: Access is denied." in text
    )
    assert "admin consent has not been granted" in text and "Traceback" not in text


def test_account_without_a_mailbox_gets_the_license_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_mailbox = _error(
        404, "MailboxNotEnabledForRESTAPI",
        "The mailbox is either inactive, soft-deleted, or is hosted on-premise.",
    )
    code, text, _ = _run(tmp_path, monkeypatch, _tenants(_dest_with_user(no_mailbox)))
    assert code == 1
    assert "404 MailboxNotEnabledForRESTAPI" in text
    assert "has no Exchange Online mailbox: assign it a license" in text


def test_refused_sign_in_names_the_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refused = "AADSTS700027: The certificate used to sign the client assertion is not registered."
    code, text, seen = _run(
        tmp_path, monkeypatch, _tenants(_dest_with_user(httpx.Response(201))), refused
    )
    assert code == 1
    assert f"destination: Failed to acquire MS token: {refused}" in text
    assert "Traceback" not in text and _dest_calls(seen) == []


# ── and the path that works ──────────────────────────────────────────────────


def test_passing_smoke_test_creates_and_deletes_the_probe_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = httpx.Response(201, json={"id": "F1"})
    code, text, seen = _run(tmp_path, monkeypatch, _tenants(_dest_with_user(created)))
    assert code == 0, text
    assert "Destination cloud: gcc_high (https://graph.microsoft.us/v1.0)" in text
    assert "Smoke test passed." in text
    assert _dest_calls(seen) == [
        ("GET", f"/v1.0/users/{DST_USER}"),
        ("POST", "/v1.0/users/D1/mailFolders"),
        ("DELETE", "/v1.0/users/D1/mailFolders/F1"),
    ]
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
