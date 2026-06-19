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
        # utf-8 so unicode in log messages (e.g. the "→" arrow) doesn't crash the
        # handler on Windows, where FileHandler otherwise defaults to cp1252.
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
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


@app.command("init-config")
def init_config(
    source_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            help="Source type: google_workspace | imap | microsoft365",
        ),
    ],
    tenant_id: Annotated[
        str, typer.Option("--tenant-id", help="Destination M365 tenant id")
    ],
    client_id: Annotated[
        str, typer.Option("--client-id", help="Destination Entra app client id")
    ],
    thumbprint: Annotated[
        str, typer.Option("--thumbprint", help="Destination certificate thumbprint")
    ],
    mapping: Annotated[
        Path | None,
        typer.Option("--mapping", "-m", help="User-mapping CSV (source_id,dest_id[,imap_*])"),
    ] = None,
    credentials_dir: Annotated[
        Path, typer.Option("--credentials-dir", help="Folder holding the JSON/PEM credentials")
    ] = Path("credentials"),
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Where to write the generated config")
    ] = Path("config.yaml"),
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite the output file if it exists")
    ] = False,
    # Source-specific options (only the relevant ones are required).
    admin_email: Annotated[
        str | None, typer.Option("--admin-email", help="[google_workspace] delegated admin email")
    ] = None,
    imap_host: Annotated[
        str | None, typer.Option("--imap-host", help="[imap] server hostname")
    ] = None,
    imap_port: Annotated[int, typer.Option("--imap-port", help="[imap] server port")] = 993,
    imap_ssl: Annotated[
        bool, typer.Option("--imap-ssl/--no-imap-ssl", help="[imap] use SSL")
    ] = True,
    source_tenant_id: Annotated[
        str | None, typer.Option("--source-tenant-id", help="[microsoft365] source tenant id")
    ] = None,
    source_client_id: Annotated[
        str | None, typer.Option("--source-client-id", help="[microsoft365] source app client id")
    ] = None,
    source_thumbprint: Annotated[
        str | None,
        typer.Option("--source-thumbprint", help="[microsoft365] source certificate thumbprint"),
    ] = None,
    # Explicit credential overrides (resolve ambiguity in --credentials-dir).
    service_account_key: Annotated[
        Path | None, typer.Option("--service-account-key", help="Override discovered JSON key")
    ] = None,
    cert: Annotated[
        Path | None, typer.Option("--cert", help="Override discovered destination PEM")
    ] = None,
    source_cert: Annotated[
        Path | None, typer.Option("--source-cert", help="Override discovered source PEM")
    ] = None,
) -> None:
    """Scaffold a config.yaml from CLI inputs, a credentials/ folder, and a mapping CSV.

    Auto-discovers the service-account JSON and certificate PEM(s) in
    --credentials-dir, reads the user mappings from --mapping, and writes a
    validated config to --output.
    """
    from .configgen import (
        ConfigGenError,
        build_config_dict,
        discover_credentials,
        dump_config_yaml,
        parse_mapping_csv,
    )

    if output.exists() and not force:
        console.print(f"[red]{output} already exists. Pass --force to overwrite.")
        raise typer.Exit(1)

    try:
        creds = discover_credentials(
            credentials_dir,
            source_type,
            service_account_key=service_account_key,
            dest_cert=cert,
            source_cert=source_cert,
        )

        if mapping is not None:
            users = parse_mapping_csv(mapping)
        else:
            console.print(
                "[yellow]No --mapping CSV given; writing a placeholder users[] entry to edit."
            )
            users = [{"source_id": "CHANGE_ME@source", "dest_id": "CHANGE_ME@dest"}]

        cfg = build_config_dict(
            source_type=source_type,
            creds=creds,
            users=users,
            dest_tenant_id=tenant_id,
            dest_client_id=client_id,
            dest_thumbprint=thumbprint,
            admin_email=admin_email,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_ssl=imap_ssl,
            source_tenant_id=source_tenant_id,
            source_client_id=source_client_id,
            source_thumbprint=source_thumbprint,
        )
    except ConfigGenError as exc:
        console.print(f"[red]{exc}")
        raise typer.Exit(1) from exc

    output.write_text(dump_config_yaml(cfg))
    console.print(
        f"[green]Wrote {output} — source '{source_type}', {len(users)} user mapping(s)."
    )
    console.print("[dim]Review it, then run: migrator smoke-test --config " + str(output))


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
