from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler

from .config import load_config
from .orchestrator import Orchestrator

app = typer.Typer(help="Google Workspace → Microsoft 365 migration tool", no_args_is_help=True)
console = Console()

_CONFIG_OPT = typer.Option("--config", "-c", help="Path to YAML config file")
_USER_OPT = typer.Option("--user", "-u", help="Limit run to a single Google email address")


def _setup_logging(level: str, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [RichHandler(rich_tracebacks=True)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=level, handlers=handlers, format="%(message)s", datefmt="[%X]")


def _build_orchestrator(config_path: Path) -> Orchestrator:
    cfg = load_config(config_path)
    _setup_logging(cfg.log_level, cfg.log_file)
    return Orchestrator(cfg)


@app.command()
def contacts(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[Optional[str], _USER_OPT] = None,
) -> None:
    """Migrate contacts (Google People → Outlook)."""
    from .workloads.contacts_job import run_contacts
    orch = _build_orchestrator(config)
    users = [u for u in orch.config.users if u.google_email == user] if user else None
    orch.run_workload(
        "contacts", run_contacts,
        max_workers=orch.config.workloads.contacts.concurrency,
        users=users,
    )


@app.command()
def calendar(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[Optional[str], _USER_OPT] = None,
) -> None:
    """Migrate calendars and events."""
    from .workloads.calendar_job import run_calendar
    orch = _build_orchestrator(config)
    users = [u for u in orch.config.users if u.google_email == user] if user else None
    orch.run_workload(
        "calendar", run_calendar,
        max_workers=orch.config.workloads.calendar.concurrency,
        users=users,
    )


@app.command()
def files(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[Optional[str], _USER_OPT] = None,
) -> None:
    """Migrate Drive files to OneDrive / SharePoint."""
    from .workloads.files_job import run_files
    orch = _build_orchestrator(config)
    users = [u for u in orch.config.users if u.google_email == user] if user else None
    orch.run_workload(
        "files", run_files,
        max_workers=orch.config.workloads.files.concurrency,
        users=users,
    )


@app.command()
def mail(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[Optional[str], _USER_OPT] = None,
) -> None:
    """Migrate Gmail to Outlook."""
    from .workloads.mail_job import run_mail
    orch = _build_orchestrator(config)
    users = [u for u in orch.config.users if u.google_email == user] if user else None
    orch.run_workload(
        "mail", run_mail,
        max_workers=orch.config.workloads.mail.concurrency,
        users=users,
    )


@app.command("run-all")
def run_all(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[Optional[str], _USER_OPT] = None,
) -> None:
    """Run all enabled workloads in sequence: contacts → calendar → files → mail."""
    from .workloads.calendar_job import run_calendar
    from .workloads.contacts_job import run_contacts
    from .workloads.files_job import run_files
    from .workloads.mail_job import run_mail

    orch = _build_orchestrator(config)
    cfg = orch.config
    users = [u for u in cfg.users if u.google_email == user] if user else None

    if cfg.workloads.contacts.enabled:
        orch.run_workload("contacts", run_contacts, max_workers=cfg.workloads.contacts.concurrency, users=users)
    if cfg.workloads.calendar.enabled:
        orch.run_workload("calendar", run_calendar, max_workers=cfg.workloads.calendar.concurrency, users=users)
    if cfg.workloads.files.enabled:
        orch.run_workload("files", run_files, max_workers=cfg.workloads.files.concurrency, users=users)
    if cfg.workloads.mail.enabled:
        orch.run_workload("mail", run_mail, max_workers=cfg.workloads.mail.concurrency, users=users)


@app.command()
def delta(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[Optional[str], _USER_OPT] = None,
) -> None:
    """Run a delta pass using stored sync cursors (post-cutover sync)."""
    from .workloads.calendar_job import run_calendar
    from .workloads.contacts_job import run_contacts
    from .workloads.files_job import run_files
    from .workloads.mail_job import run_mail

    orch = _build_orchestrator(config)
    cfg = orch.config
    users = [u for u in cfg.users if u.google_email == user] if user else None

    for name, fn, concurrency in [
        ("contacts", run_contacts, cfg.workloads.contacts.concurrency),
        ("calendar", run_calendar, cfg.workloads.calendar.concurrency),
        ("files", run_files, cfg.workloads.files.concurrency),
        ("mail", run_mail, cfg.workloads.mail.concurrency),
    ]:
        orch.run_workload(name, fn, mode="delta", max_workers=concurrency, users=users)


@app.command()
def whatif(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    user: Annotated[Optional[str], _USER_OPT] = None,
    output: Annotated[Path, typer.Option("--output", "-o", help="CSV output path")] = Path("whatif_manifest.csv"),
) -> None:
    """Dry-run inventory: enumerate every item that would be moved and write a CSV.

    Does not connect to Microsoft Graph or modify any state. Useful for pre-cutover
    sizing, change-management approval, and verifying user mappings.
    """
    import migrator as _pkg
    from .whatif import ManifestWriter
    from .workloads.calendar_job import run_calendar
    from .workloads.contacts_job import run_contacts
    from .workloads.files_job import run_files
    from .workloads.mail_job import run_mail

    orch = _build_orchestrator(config)
    cfg = orch.config
    users = [u for u in cfg.users if u.google_email == user] if user else None

    with ManifestWriter(output) as manifest:
        _pkg._current_manifest = manifest
        try:
            if cfg.workloads.contacts.enabled:
                orch.run_workload("contacts", run_contacts, mode="whatif",
                                  max_workers=cfg.workloads.contacts.concurrency, users=users)
            if cfg.workloads.calendar.enabled:
                orch.run_workload("calendar", run_calendar, mode="whatif",
                                  max_workers=cfg.workloads.calendar.concurrency, users=users)
            if cfg.workloads.files.enabled:
                orch.run_workload("files", run_files, mode="whatif",
                                  max_workers=cfg.workloads.files.concurrency, users=users)
            if cfg.workloads.mail.enabled:
                orch.run_workload("mail", run_mail, mode="whatif",
                                  max_workers=cfg.workloads.mail.concurrency, users=users)
        finally:
            count = manifest.count
            _pkg._current_manifest = None

    console.print(f"[green]Whatif manifest written: {output} ({count} items)")


@app.command()
def validate(
    config: Annotated[Path, _CONFIG_OPT] = Path("config.yaml"),
    output: Annotated[Path, typer.Option(help="Report output path")] = Path("migration_report.html"),
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
    """Phase 0 smoke test: read one Gmail label list, create+delete a test MS folder."""
    import json
    cfg = load_config(config)
    _setup_logging(cfg.log_level, cfg.log_file)

    if not cfg.users:
        console.print("[red]No users configured.")
        raise typer.Exit(1)

    pilot = cfg.users[0]
    console.print(f"[bold]Smoke test: pilot user = {pilot.google_email}")

    # Google read-only check
    from .google.gmail import list_labels
    labels = list_labels(cfg.google, pilot.google_email)
    console.print(f"[green]Google OK — found {len(labels)} Gmail labels")

    # Microsoft write check
    token_provider_kwargs = {
        "tenant_id": cfg.microsoft.tenant_id,
        "client_id": cfg.microsoft.client_id,
        "certificate_path": cfg.microsoft.certificate_path,
        "certificate_thumbprint": cfg.microsoft.certificate_thumbprint,
        "client_secret": cfg.microsoft.client_secret,
        "token_cache_file": cfg.microsoft.token_cache_file,
    }
    from .auth.ms_auth import MSTokenProvider
    from .microsoft.graph_client import GraphClient
    tp = MSTokenProvider(**token_provider_kwargs)
    with GraphClient(tp) as gc:
        # Resolve the MS user
        user_info = gc.get(f"/users/{pilot.ms_upn}")
        ms_id = user_info["id"]

        folder = gc.post(
            f"/users/{ms_id}/mailFolders",
            json={"displayName": "_migrator_smoke_test"},
        )
        folder_id = folder["id"]
        console.print(f"[green]Microsoft OK — created test folder id={folder_id}")
        gc.delete(f"/users/{ms_id}/mailFolders/{folder_id}")
        console.print("[green]Microsoft OK — deleted test folder")

    # Write a job_runs row to prove DB works
    from .state.db import init_db, session_scope
    from .state.models import JobRun
    init_db(cfg.state_db)
    with session_scope() as s:
        run = JobRun(user_email=pilot.google_email, workload="smoke_test", mode="smoke", status="done")
        s.add(run)
    console.print("[bold green]Smoke test passed.")
