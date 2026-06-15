from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

import migrator as _pkg

from .auth.ms_auth import MSTokenProvider
from .config import (
    Config,
    GoogleWorkspaceSourceConfig,
    Microsoft365SourceConfig,
    UserMapping,
)
from .connectors.base import BaseSource
from .connectors.factory import build_source
from .context import JobContext
from .microsoft.graph_client import GraphClient
from .ratelimit import PerUserRateLimiter, registry
from .state.db import init_db, session_scope
from .state.models import JobRun

log = logging.getLogger(__name__)

JobFn = Callable[[JobContext], None]


class Orchestrator:
    def __init__(self, config: Config) -> None:
        self.config = config
        _pkg._current_config = config
        init_db(config.state_db)
        self._setup_rate_limiters()
        # Token providers are built lazily so whatif (inventory-only) runs do not
        # require valid Microsoft credentials or a readable certificate.
        self._dest_token_provider: MSTokenProvider | None = None
        self._source_token_provider: MSTokenProvider | None = None
        self._per_user_limiter = PerUserRateLimiter(
            rate=config.rate_limits.graph_requests_per_mailbox_per_minute / 60.0
        )

    def _setup_rate_limiters(self) -> None:
        rl = self.config.rate_limits
        registry.register("google_global", rl.google_requests_per_second)
        registry.register("graph_global", rl.graph_requests_per_second)

    # -- destination Graph -------------------------------------------------- #
    def _get_dest_token_provider(self) -> MSTokenProvider:
        if self._dest_token_provider is None:
            d = self.config.destination
            self._dest_token_provider = MSTokenProvider(
                tenant_id=d.tenant_id,
                client_id=d.client_id,
                certificate_path=d.certificate_path,
                certificate_thumbprint=d.certificate_thumbprint,
                client_secret=d.client_secret,
                token_cache_file=d.token_cache_file,
            )
        return self._dest_token_provider

    def dest_graph_client(self) -> GraphClient:
        return GraphClient(
            token_provider=self._get_dest_token_provider(),
            per_user_limiter=self._per_user_limiter,
        )

    # -- source Graph (microsoft365 source only) ---------------------------- #
    def _get_source_token_provider(self) -> MSTokenProvider:
        if self._source_token_provider is None:
            s = self.config.source
            assert isinstance(s, Microsoft365SourceConfig)
            self._source_token_provider = MSTokenProvider(
                tenant_id=s.tenant_id,
                client_id=s.client_id,
                certificate_path=s.certificate_path,
                certificate_thumbprint=s.certificate_thumbprint,
                client_secret=s.client_secret,
                token_cache_file=s.token_cache_file,
            )
        return self._source_token_provider

    def source_graph_client(self) -> GraphClient:
        return GraphClient(
            token_provider=self._get_source_token_provider(),
            per_user_limiter=self._per_user_limiter,
        )

    def _build_source(self) -> BaseSource:
        factory = (
            self.source_graph_client
            if isinstance(self.config.source, Microsoft365SourceConfig)
            else None
        )
        return build_source(self.config, source_graph_client_factory=factory)

    def run_shared_drives(self) -> None:
        """Migrate configured Google Shared Drives → SharePoint (tenant-level)."""
        from .workloads.files_job import run_shared_drives as job

        src_cfg = self.config.source
        if not isinstance(src_cfg, GoogleWorkspaceSourceConfig):
            raise RuntimeError("shared-drives migration requires a google_workspace source")
        if not self.config.shared_drives:
            log.warning("No shared_drives configured — nothing to do")
            return
        # Enumerate/download Drive content by impersonating the Workspace admin.
        impersonation = UserMapping(source_id=src_cfg.admin_email, dest_id="")
        with self._build_source() as source, self.dest_graph_client() as gc:
            ctx = JobContext(
                user=impersonation, source=source, dest_gc=gc, mode="full", config=self.config
            )
            job(ctx)

    def run_workload(
        self,
        workload_name: str,
        job_fn: JobFn,
        mode: str = "full",
        max_workers: int = 4,
        users: list[UserMapping] | None = None,
    ) -> None:
        target_users = users or self.config.users
        log.info("Starting workload=%s mode=%s users=%d", workload_name, mode, len(target_users))

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task(f"[cyan]{workload_name}", total=len(target_users))

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(self._run_user, workload_name, job_fn, user, mode): user
                    for user in target_users
                }
                for future in as_completed(futures):
                    user = futures[future]
                    try:
                        future.result()
                        log.info("Completed %s for %s", workload_name, user.source_id)
                    except Exception:
                        log.exception("Failed %s for %s", workload_name, user.source_id)
                    finally:
                        progress.advance(task)

    def _run_user(
        self,
        workload_name: str,
        job_fn: JobFn,
        user: UserMapping,
        mode: str,
    ) -> None:
        with session_scope() as session:
            run = JobRun(
                user_email=user.source_id,
                workload=workload_name,
                mode=mode,
                status="running",
            )
            session.add(run)
            session.flush()
            run_id = run.id

        try:
            with self._build_source() as source:
                if mode == "whatif":
                    # Inventory-only: never touch the destination. Jobs read from
                    # the source and write rows to the package-level ManifestWriter.
                    ctx = JobContext(
                        user=user, source=source, dest_gc=None, mode=mode, config=self.config
                    )
                    job_fn(ctx)
                else:
                    with self.dest_graph_client() as client:
                        ctx = JobContext(
                            user=user, source=source, dest_gc=client, mode=mode, config=self.config
                        )
                        job_fn(ctx)
            with session_scope() as session:
                done = session.get(JobRun, run_id)
                if done:
                    done.status = "done"
        except Exception as exc:
            with session_scope() as session:
                failed = session.get(JobRun, run_id)
                if failed:
                    failed.status = "failed"
                    failed.error_count += 1
            raise exc
