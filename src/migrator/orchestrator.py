from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .auth.ms_auth import MSTokenProvider
from .config import Config, UserMapping
from .microsoft.graph_client import GraphClient
from .ratelimit import PerUserRateLimiter, registry
from .state.db import init_db, session_scope
from .state.models import JobRun
import migrator as _pkg

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: Config) -> None:
        self.config = config
        _pkg._current_config = config
        init_db(config.state_db)
        self._setup_rate_limiters()
        # Lazy: built on first call so whatif (inventory-only) runs do not require
        # valid MS credentials or a readable certificate file.
        self._token_provider: MSTokenProvider | None = None
        self._per_user_limiter = PerUserRateLimiter(
            rate=config.rate_limits.graph_requests_per_mailbox_per_minute / 60.0
        )

    def _setup_rate_limiters(self) -> None:
        rl = self.config.rate_limits
        registry.register("google_global", rl.google_requests_per_second)
        registry.register("graph_global", rl.graph_requests_per_second)

    def _get_token_provider(self) -> MSTokenProvider:
        if self._token_provider is None:
            mc = self.config.microsoft
            self._token_provider = MSTokenProvider(
                tenant_id=mc.tenant_id,
                client_id=mc.client_id,
                certificate_path=mc.certificate_path,
                certificate_thumbprint=mc.certificate_thumbprint,
                client_secret=mc.client_secret,
                token_cache_file=mc.token_cache_file,
            )
        return self._token_provider

    def graph_client(self) -> GraphClient:
        return GraphClient(
            token_provider=self._get_token_provider(),
            per_user_limiter=self._per_user_limiter,
        )

    def run_workload(
        self,
        workload_name: str,
        job_fn: Callable[[UserMapping, GraphClient | None, str], None],
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
                        log.info("Completed %s for %s", workload_name, user.google_email)
                    except Exception:
                        log.exception("Failed %s for %s", workload_name, user.google_email)
                    finally:
                        progress.advance(task)

    def _run_user(
        self,
        workload_name: str,
        job_fn: Callable[[UserMapping, GraphClient | None, str], None],
        user: UserMapping,
        mode: str,
    ) -> None:
        with session_scope() as session:
            run = JobRun(
                user_email=user.google_email,
                workload=workload_name,
                mode=mode,
                status="running",
            )
            session.add(run)
            session.flush()
            run_id = run.id

        try:
            if mode == "whatif":
                # Inventory-only: never touch Microsoft. Jobs branch on mode and
                # write rows to the package-level ManifestWriter instead.
                job_fn(user, None, mode)
            else:
                with self.graph_client() as client:
                    job_fn(user, client, mode)
            with session_scope() as session:
                run = session.get(JobRun, run_id)
                if run:
                    run.status = "done"
        except Exception as exc:
            with session_scope() as session:
                run = session.get(JobRun, run_id)
                if run:
                    run.status = "failed"
                    run.error_count += 1
            raise exc
