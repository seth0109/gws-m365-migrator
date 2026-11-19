"""National-cloud routing (GCC High / DoD / China).

A token is only valid for the cloud that issued it, so the sign-in authority,
the token scope and the Graph service root must all follow the tenant block's
`cloud` — for the source and the destination independently (a commercial tenant
migrating into GCC High talks to two different clouds in one run). Next/delta
links and upload URLs come back on the cloud's own hosts and must be routed and
authenticated accordingly.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from migrator.auth import ms_auth
from migrator.auth.ms_auth import MSTokenProvider
from migrator.config import CLOUD_ENDPOINTS, Config, Microsoft365SourceConfig, UserMapping
from migrator.connectors.m365 import M365Source
from migrator.microsoft.graph_client import GRAPH_BASE, GraphClient
from migrator.orchestrator import Orchestrator

GOV = "https://graph.microsoft.us/v1.0"


class _FakeMsalApp:
    """Stands in for msal.ConfidentialClientApplication: records the authority
    it was built with and the scopes tokens were requested for."""

    built: list[_FakeMsalApp] = []

    def __init__(self, client_id: str, authority: str = "", **_kwargs: Any) -> None:
        self.client_id = client_id
        self.authority = authority
        self.scopes: list[list[str]] = []
        _FakeMsalApp.built.append(self)

    def acquire_token_silent(self, scopes: list[str], account: object = None) -> None:
        self.scopes.append(list(scopes))
        return None

    def acquire_token_for_client(self, scopes: list[str]) -> dict[str, str]:
        self.scopes.append(list(scopes))
        return {"access_token": f"tok-for-{self.client_id}"}


@pytest.fixture
def fake_msal(monkeypatch: pytest.MonkeyPatch) -> list[_FakeMsalApp]:
    _FakeMsalApp.built = []
    monkeypatch.setattr(ms_auth.msal, "ConfidentialClientApplication", _FakeMsalApp)
    return _FakeMsalApp.built


class _StubTokens:
    def get_token(self) -> str:
        return "tok"


def _gov_client(handler: Callable[[httpx.Request], httpx.Response]) -> GraphClient:
    gc = GraphClient(_StubTokens(), base_url=GOV)  # type: ignore[arg-type]
    gc._client.close()
    gc._client = httpx.Client(transport=httpx.MockTransport(handler))
    return gc


def _config(dest: dict[str, Any], source: dict[str, Any] | None = None) -> Config:
    return Config.model_validate({
        "source": source or {"type": "imap", "host": "imap.example"},
        "destination": {"tenant_id": "dst", "client_id": "dst-app", "client_secret": "s", **dest},
        "users": [{"source_id": "a@src", "dest_id": "a@dst"}],
    })


# ── config ────────────────────────────────────────────────────────────────────


def test_cloud_defaults_to_global() -> None:
    assert _config({}).destination.cloud == "global"


@pytest.mark.parametrize("spelling", ["gcc_high", "GCC High", "gcc-high", " GCC_HIGH "])
def test_cloud_accepts_common_spellings(spelling: str) -> None:
    assert _config({"cloud": spelling}).destination.cloud == "gcc_high"


def test_unknown_cloud_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _config({"cloud": "gcchigh-typo"})


def test_gcc_is_served_by_the_global_endpoints() -> None:
    # Microsoft 365 GCC (not High) keeps the worldwide endpoints.
    assert CLOUD_ENDPOINTS["gcc"] == CLOUD_ENDPOINTS["global"]


# ── token provider ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cloud", "authority", "graph_root"),
    [
        ("global", "https://login.microsoftonline.com/tid", "https://graph.microsoft.com"),
        ("gcc", "https://login.microsoftonline.com/tid", "https://graph.microsoft.com"),
        ("gcc_high", "https://login.microsoftonline.us/tid", "https://graph.microsoft.us"),
        ("dod", "https://login.microsoftonline.us/tid", "https://dod-graph.microsoft.us"),
        (
            "china",
            "https://login.chinacloudapi.cn/tid",
            "https://microsoftgraph.chinacloudapi.cn",
        ),
    ],
)
def test_token_provider_authority_scope_and_root_follow_cloud(
    fake_msal: list[_FakeMsalApp], cloud: str, authority: str, graph_root: str
) -> None:
    tp = MSTokenProvider("tid", "app", client_secret="s", cloud=cloud)
    assert fake_msal[0].authority == authority
    assert tp.graph_base == f"{graph_root}/v1.0"
    tp.get_token()
    # Both the silent lookup and the client-credential request use the cloud's scope.
    assert fake_msal[0].scopes == [[f"{graph_root}/.default"]] * 2


# ── graph client ──────────────────────────────────────────────────────────────


def test_client_takes_its_service_root_from_the_token_provider(
    fake_msal: list[_FakeMsalApp],
) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"id": "x"})

    tp = MSTokenProvider.from_config(_config({"cloud": "gcc_high"}).destination)
    gc = GraphClient(tp)
    gc._client.close()
    gc._client = httpx.Client(transport=httpx.MockTransport(handler))

    assert gc.base_url == GOV
    gc.get("/users/a@dst")
    assert str(seen[0].url) == f"{GOV}/users/a@dst"
    assert seen[0].headers["Authorization"] == "Bearer tok-for-dst-app"


def test_client_without_a_cloud_aware_provider_stays_global() -> None:
    assert GraphClient(_StubTokens()).base_url == GRAPH_BASE  # type: ignore[arg-type]


def test_gov_nextlink_is_followed_with_auth_and_without_duplicate_params() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(
                200, json={"value": [1], "@odata.nextLink": f"{GOV}/col?$skiptoken=xyz"}
            )
        return httpx.Response(200, json={"value": [2]})

    gc = _gov_client(handler)
    assert list(gc.paginate("/col", params={"$top": 2})) == [[1], [2]]
    assert [r.url.host for r in seen] == ["graph.microsoft.us"] * 2
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
    assert "$top" not in seen[1].url.params
    assert seen[1].url.params["$skiptoken"] == "xyz"


def test_gov_upload_url_gets_no_token_but_a_relative_put_does() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"id": "f"})

    gc = _gov_client(handler)
    # Upload-session URL on the GCC High SharePoint host: pre-authenticated.
    gc.put_raw("https://contoso-my.sharepoint.us/_api/v2.0/up/session1", data=b"chunk")
    # Simple upload addressed relative to the client's own service root.
    gc.put_raw("/users/u/drive/items/p:/a.txt:/content", data=b"data")

    assert "Authorization" not in seen[0].headers
    assert str(seen[1].url) == f"{GOV}/users/u/drive/items/p:/a.txt:/content"
    assert seen[1].headers["Authorization"] == "Bearer tok"


def test_token_is_never_sent_to_another_clouds_graph_host() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"value": []})

    gc = _gov_client(handler)
    # e.g. a delta cursor stored before the tenant block's cloud was changed.
    gc.get(f"{GRAPH_BASE}/users/u/drive/root/delta?token=old")
    assert seen[0].url.host == "graph.microsoft.com"
    assert "Authorization" not in seen[0].headers


# ── orchestrator: two tenants, two clouds ─────────────────────────────────────


def test_commercial_source_and_gcc_high_destination_use_their_own_clouds(
    tmp_path: Path, fake_msal: list[_FakeMsalApp]
) -> None:
    cfg = Config.model_validate({
        "state_db": str(tmp_path / "state.db"),
        "log_file": None,
        "source": {
            "type": "microsoft365", "tenant_id": "src", "client_id": "src-app",
            "client_secret": "s", "token_cache_file": str(tmp_path / "src-cache.json"),
        },
        "destination": {
            "tenant_id": "dst", "client_id": "dst-app", "client_secret": "s",
            "cloud": "gcc_high", "token_cache_file": str(tmp_path / "dst-cache.json"),
        },
        "users": [{"source_id": "a@src", "dest_id": "a@dst"}],
    })
    orch = Orchestrator(cfg)

    with orch.source_graph_client() as src, orch.dest_graph_client() as dst:
        assert src.base_url == GRAPH_BASE
        assert dst.base_url == GOV

    by_app = {app.client_id: app.authority for app in fake_msal}
    assert by_app == {
        "src-app": "https://login.microsoftonline.com/src",
        "dst-app": "https://login.microsoftonline.us/dst",
    }


# ── M365 source on a national cloud: delta links carry that cloud's host ───────


def test_m365_source_drive_delta_follows_gov_next_and_delta_links() -> None:
    seen: list[httpx.Request] = []
    next_link = f"{GOV}/users/UID/drive/root/delta?token=page2"
    delta_link = f"{GOV}/users/UID/drive/root/delta?token=d1"

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1.0/users/a@src":
            return httpx.Response(200, json={"id": "UID"})
        token = req.url.params.get("token")
        if token is None:
            return httpx.Response(200, json={
                "value": [{"id": "i1", "name": "a.txt", "file": {}}],
                "@odata.nextLink": next_link,
            })
        if token == "page2":
            return httpx.Response(200, json={
                "value": [{"id": "i2", "name": "b.txt", "file": {}}],
                "@odata.deltaLink": delta_link,
            })
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": delta_link})

    gc = _gov_client(handler)
    src = M365Source(
        Microsoft365SourceConfig(tenant_id="t", client_id="c", cloud="gcc_high"), lambda: gc
    )
    user = UserMapping(source_id="a@src", dest_id="a@dst")

    assert [f.source_id for f in src.iter_files(user, None)] == ["i1", "i2"]
    assert src.get_last_cursor("files") == delta_link

    # The stored cursor (an absolute gov deltaLink) drives the next delta pass.
    assert list(src.iter_files(user, delta_link)) == []
    assert str(seen[-1].url) == delta_link

    assert all(r.url.host == "graph.microsoft.us" for r in seen)
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
