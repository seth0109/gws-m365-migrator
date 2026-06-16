from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from .config import Config, UserMapping, load_config
from .connectors.factory import source_capabilities
from .orchestrator import JobFn, Orchestrator

app = typer.Typer(help="Multi-source → Microsoft 365 migration tool", no_args_is_help=True)
console = Console()

_CONFIG_OPT = typer.Option("--config", "-c", help="Path to YAML config file")
_USER_OPT = typer.Option("--user", "-u", help="Limit run to a single source identity")

# Order workloads run in run-all / delta / whatif.
_WORKLOAD_ORDER = ("contacts", "calendar", "files", "mail")


def _setup_logging(level: str, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [RichHandler(rich_tracebacks=True)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=level, handlers=handlers, format="%(message)s", datefmt="[%X]")


def _build_orchestrator(config_path: Path) -> Orchestrator:
    cfg = load_config(config_path)
    _setup_logging(cfg.log_level, cfg.log_file)
    return Orchestrator(cfg)


def _filter_users(cfg: Config, user: str | None) -> list[UserMapping] | None:
    return [u for u in cfg.users if u.source_id == user] if user else None


def _require_capability(cfg: Config, workload: str) -> None:
    caps = source_capabilities(cfg)
    if workload not in caps:
        console.print(
            f"[red]Source '{cfg.source.type}' does not support the '{workload}' workload "
            f"(supports: {', '.join(sorted(caps))})."
        )
        raise typer.Exit(1)


def _job_fn(workload: str) -> JobFn:
    from .workloads.calendar_job import run_calendar
    from .workloads.contacts_job import run_contacts
    from .workloads.files_job import run_files
    from .workloads.mail_job import run_mail

    fns: dict[str, JobFn] = {
        "contacts": run_contacts,
        "calendar": run_calendar,
        "files": run_files,
        "mail": run_mail,
    }
    return fns[workload]


def _run_single(workload: str, config: Path, user: str | None, mode: str = "full") -> None:
    orch = _build_orchestrator(config)
    _require_capability(orch.config, workload)
    orch.run_workload(
        workload,
        _job_fn(workload),
        mode=mode,
        max_workers=getattr(orch.config.workloads, workload).concurrency,
        users=_filter_users(orch.config, user),
    )


@app.command()
def contacts(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[str | None, _USER_OPT] = None,
) -> None:
    """Migrate contacts to Outlook."""
    _run_single("contacts", config, user)


@app.command()
def calendar(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[str | None, _USER_OPT] = None,
) -> None:
    """Migrate calendars and events."""
    _run_single("calendar", config, user)


@app.command()
def files(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[str | None, _USER_OPT] = None,
) -> None:
    """Migrate personal files to OneDrive."""
    _run_single("files", config, user)


@app.command()
def mail(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[str | None, _USER_OPT] = None,
) -> None:
    """Migrate mail to Outlook (Gmail / IMAP / Microsoft 365 source)."""
    _run_single("mail", config, user)


@app.command("shared-drives")
def shared_drives(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    delta: Annotated[
        bool, typer.Option("--delta", help="Migrate only files changed since the last run.")
    ] = False,
) -> None:
    """Migrate Google Shared Drives → SharePoint (auto-provisions a site per drive)."""
    orch = _build_orchestrator(config)
    if orch.config.source.type != "google_workspace":
        console.print("[red]shared-drives is only available for a google_workspace source.")
        raise typer.Exit(1)
    orch.run_shared_drives(mode="delta" if delta else "full")


@app.command()
def sharepoint(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    delta: Annotated[
        bool, typer.Option("--delta", help="Migrate only files changed since the last run.")
    ] = False,
) -> None:
    """Migrate SharePoint sites between Microsoft 365 tenants (microsoft365 source)."""
    orch = _build_orchestrator(config)
    if orch.config.source.type != "microsoft365":
        console.print("[red]sharepoint is only available for a microsoft365 source.")
        raise typer.Exit(1)
    orch.run_sharepoint_sites(mode="delta" if delta else "full")


@app.command("run-all")
def run_all(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[str | None, _USER_OPT] = None,
) -> None:
    """Run all enabled + supported workloads in sequence."""
    orch = _build_orchestrator(config)
    cfg = orch.config
    caps = source_capabilities(cfg)
    users = _filter_users(cfg, user)
    for workload in _WORKLOAD_ORDER:
        wcfg = getattr(cfg.workloads, workload)
        if not wcfg.enabled:
            continue
        if workload not in caps:
            console.print(
                f"[yellow]Skipping '{workload}' — unsupported by source '{cfg.source.type}'."
            )
            continue
        orch.run_workload(workload, _job_fn(workload), max_workers=wcfg.concurrency, users=users)


@app.command()
def delta(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[str | None, _USER_OPT] = None,
) -> None:
    """Run a delta pass using stored sync cursors (post-cutover sync)."""
    orch = _build_orchestrator(config)
    cfg = orch.config
    caps = source_capabilities(cfg)
    users = _filter_users(cfg, user)
    for workload in _WORKLOAD_ORDER:
        wcfg = getattr(cfg.workloads, workload)
        if not wcfg.enabled or workload not in caps:
            continue
        orch.run_workload(
            workload, _job_fn(workload), mode="delta", max_workers=wcfg.concurrency, users=users
        )


@app.command()
def whatif(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[str | None, _USER_OPT] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="CSV output path")
    ] = Path("whatif_manifest.csv"),
) -> None:
    """Dry-run inventory: enumerate every item that would be moved and write a CSV.

    Does not connect to the destination or modify state.
    """
    import migrator as _pkg

    from .whatif import ManifestWriter

    orch = _build_orchestrator(config)
    cfg = orch.config
    caps = source_capabilities(cfg)
    users = _filter_users(cfg, user)

    with ManifestWriter(output) as manifest:
        _pkg._current_manifest = manifest
        try:
            for workload in _WORKLOAD_ORDER:
                wcfg = getattr(cfg.workloads, workload)
                if not wcfg.enabled or workload not in caps:
                    continue
                orch.run_workload(
                    workload, _job_fn(workload), mode="whatif",
                    max_workers=wcfg.concurrency, users=users,
                )
        finally:
            count = manifest.count
            _pkg._current_manifest = None

    console.print(f"[green]Whatif manifest written: {output} ({count} items)")


@app.command()
def validate(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    output: Annotated[
        Path, typer.Option(help="Report output path")
    ] = Path("migration_report.html"),
) -> None:
    """Generate a validation report: source vs destination counts."""
    from .reporting import generate_report

    cfg = load_config(config)
    _setup_logging(cfg.log_level, cfg.log_file)
    generate_report(cfg, output)
    console.print(f"[green]Report written to {output}")


@app.command("smoke-test")
def smoke_test(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
) -> None:
    """Phase 0 smoke test: probe the configured source, then create+delete a test
    folder in the destination mailbox."""
    cfg = load_config(config)
    _setup_logging(cfg.log_level, cfg.log_file)

    if not cfg.users:
        console.print("[red]No users configured.")
        raise typer.Exit(1)

    pilot = cfg.users[0]
    console.print(f"[bold]Smoke test: pilot user = {pilot.source_id} → {pilot.dest_id}")

    _probe_source(cfg, pilot)
    _probe_destination(cfg, pilot)

    from .state.db import init_db, session_scope
    from .state.models import JobRun

    init_db(cfg.state_db)
    with session_scope() as s:
        s.add(JobRun(
            user_email=pilot.source_id, workload="smoke_test", mode="smoke", status="done",
        ))
    console.print("[bold green]Smoke test passed.")


def _probe_source(cfg: Config, pilot: UserMapping) -> None:
    stype = cfg.source.type
    if stype == "google_workspace":
        from .config import GoogleWorkspaceSourceConfig
        from .google.gmail import list_labels

        assert isinstance(cfg.source, GoogleWorkspaceSourceConfig)
        labels = list_labels(cfg.source, pilot.source_id)
        console.print(f"[green]Google OK — found {len(labels)} Gmail labels")
    elif stype == "imap":
        from .config import ImapSourceConfig
        from .connectors.imap import ImapSource

        assert isinstance(cfg.source, ImapSourceConfig)
        src = ImapSource(cfg.source)
        try:
            conn = src._connect(pilot)
            typ, data = conn.list()
            console.print(
                f"[green]IMAP OK — login succeeded, {len(data)} folders listed (status={typ})"
            )
        finally:
            src.close()
    elif stype == "microsoft365":
        orch = Orchestrator(cfg)
        with orch.source_graph_client() as gc:
            info = gc.get(f"/users/{pilot.source_id}", params={"$select": "id,displayName"})
            name = info.get("displayName", pilot.source_id)
            console.print(f"[green]M365 source OK — resolved {name}")
    else:
        console.print(f"[yellow]No source probe implemented for type '{stype}'")


def _probe_destination(cfg: Config, pilot: UserMapping) -> None:
    from .auth.ms_auth import MSTokenProvider
    from .microsoft.graph_client import GraphClient

    d = cfg.destination
    tp = MSTokenProvider(
        tenant_id=d.tenant_id,
        client_id=d.client_id,
        certificate_path=d.certificate_path,
        certificate_thumbprint=d.certificate_thumbprint,
        client_secret=d.client_secret,
        token_cache_file=d.token_cache_file,
    )
    with GraphClient(tp) as gc:
        user_info = gc.get(f"/users/{pilot.dest_id}")
        ms_id = user_info["id"]
        folder = gc.post(
            f"/users/{ms_id}/mailFolders", json={"displayName": "_migrator_smoke_test"}
        )
        folder_id = folder["id"]
        console.print(f"[green]Destination OK — created test folder id={folder_id}")
        gc.delete(f"/users/{ms_id}/mailFolders/{folder_id}")
        console.print("[green]Destination OK — deleted test folder")
