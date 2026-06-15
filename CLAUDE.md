# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Install Dependencies
```bash
pip install -e ".[dev]"
```
Uses `hatchling` build backend. Dev extras include pytest, mypy, ruff, and pytest plugins.

### Run Tests
```bash
pytest                          # Run all tests
pytest tests/ -v               # Verbose output
pytest tests/test_foo.py -k test_name  # Single test
```
pytest configured in pyproject.toml with testpaths=["tests"]; currently tests/ is mostly empty.

### Linting & Formatting
```bash
ruff check src/                # Check violations (E, F, I, UP rules)
ruff check --fix src/          # Auto-fix imports and formatting
```
Line length is 100. Source root is src/.

### Type Checking
```bash
mypy src/
```
Strict mode enabled (strict = true). Python 3.11+.

### Run the CLI Tool
```bash
migrator --help                              # Show all commands
migrator contacts --config config.yaml      # Migrate contacts
migrator calendar --config config.yaml      # Migrate calendar
migrator files --config config.yaml         # Migrate Drive → OneDrive
migrator mail --config config.yaml          # Migrate Gmail → Outlook
migrator run-all --config config.yaml       # Run all enabled workloads sequentially
migrator delta --config config.yaml         # Delta sync (post-cutover) using stored cursors
migrator whatif --config config.yaml --output whatif_manifest.csv  # Dry-run inventory CSV
migrator smoke-test --config config.yaml    # Phase 0 test: read Gmail labels, write test folder to MS
migrator validate --config config.yaml --output report.html  # Generate validation report
```
Use `--user user@domain.com` to limit any run to a single user. Config is YAML with Google service account, Microsoft tenant/client, user mappings, workload settings, and rate limits.

## Architecture

### Configuration Flow

1. **YAML → Config model**: `load_config(path)` parses YAML and validates using Pydantic v2 models (GoogleConfig, MicrosoftConfig, WorkloadsConfig, RateLimitsConfig).
2. **Orchestrator singleton**: Receives Config in `__init__()`, stores as `self.config`.
3. **Global injection**: `migrator._current_config = config` set by Orchestrator before dispatching any job. Jobs import `migrator` and read `migrator._current_config` to access config. This avoids passing config through deeply nested function calls.

**Why this pattern:** Workload jobs are called with signature `fn(user: UserMapping, gc: GraphClient, mode: str)`. Config cannot be threaded through without changing the interface. Global injection keeps job signatures clean.

### Rate Limiting (Three-Layer System)

1. **Global Registry** (singleton at `migrator.ratelimit.registry`):
   - Orchestrator registers two named token buckets at startup: `"google_global"` and `"graph_global"` from config.rate_limits.
   - TokenBucket implements token-bucket algorithm with thread-safe locking.
   - Jobs never interact with registry directly.

2. **Per-User Limiter** (PerUserRateLimiter instance):
   - Passed to every GraphClient instance created by Orchestrator.
   - Maintains a dict of TokenBuckets keyed by user (e.g., mailbox ID).
   - Used for Graph API per-mailbox throttling (graph_requests_per_mailbox_per_minute).

3. **GraphClient Integration**:
   - Every GraphClient.get/post/patch/delete/paginate call invokes `_apply_rate_limits(user_key)` before the request.
   - Acquires from global registry first (may pass silently if not registered).
   - If user_key provided, also acquires from per-user limiter.
   - Throttles by sleeping if capacity exhausted.

**Key invariant:** Rate limiters are checked BEFORE every API call, including retries. Tenacity retry decorator wraps after rate limiting, so a retry doesn't bypass the limiter.

### Idempotency & State Store

**Invariant:** Before processing any item (contact, event, email, file), job checks `is_done(session, user_email, workload, source_id)` against ItemMap table.

**ItemMap table:**
- Unique constraint: (user_email, workload, source_id)
- Fields: status (pending/done/failed/skipped), dest_id, source_hash, attempts, last_error, updated_at
- Accessed via helpers: `is_done()`, `upsert_item()` (using SQLite INSERT OR REPLACE with conflict handling)

**How it works:**
```python
with session_scope() as s:
    if is_done(s, user.google_email, "contacts", source_id):
        continue  # Skip already-done items
    # ... do migration ...
    upsert_item(s, user.google_email, "contacts", source_id, dest_id=dest_id, status="done")
```

If a job is interrupted and re-run, it resumes from the first `pending` item. This is true re-entrancy: no duplicate writes.

**Other tables:**
- FolderMap: source folder path → destination folder ID (used by files, mail, calendar)
- SyncCursor: Stores pagination tokens per (user, workload) for delta syncs
- JobRun: Audit log of workload runs (user, workload, mode, status, started_at, error_count)

### Threading & Job Dispatch

1. **Orchestrator.run_workload()** creates a ThreadPoolExecutor with concurrency = config.workloads.{workload}.concurrency (typically 2–4 per workload).
2. Each thread calls `_run_user(workload_name, job_fn, user, mode)`:
   - Writes JobRun row to DB with status="running"
   - Calls `job_fn(user, GraphClient, mode)`
   - Updates JobRun.status to "done" or "failed"
3. Each job gets its own GraphClient instance (ephemeral HTTP client + token provider).
4. **State access:** All state reads/writes use `session_scope()` context manager, which creates a fresh session per scope. Session is committed on exit (exception triggers rollback). This is SQLAlchemy's recommended pattern for thread-safe concurrent access.

**Why separate GraphClient per thread:** MS token provider may refresh tokens. Using one shared client would require synchronization; separate clients per thread are simpler and each thread maintains its own token state.

### Google is Read-Only

All Google OAuth scopes are `.readonly`:
```
gmail.readonly
drive.readonly
contacts.readonly
calendar.readonly
```

Jobs NEVER write to Google. All writes go to Microsoft Graph API (Outlook, OneDrive, SharePoint). This is a safety invariant: if the code ever accidentally calls a write method on Google services, it will fail with permission denied.

### Job Execution Model

**Signature:** `fn(user: UserMapping, gc: GraphClient, mode: str) → None`

**Parameters:**
- `user`: Mapping of google_email → ms_upn for one user in this thread's batch
- `gc`: GraphClient instance bound to this thread (not thread-safe across threads)
- `mode`: "full" (initial migration) or "delta" (incremental post-cutover sync)

**Mode behavior:**
- `"full"`: Process all items from source. On delta-capable workloads, fetch and store sync cursor for next run.
- `"delta"`: Retrieve stored sync cursor, fetch only changed items from source since that cursor, process them.
- `"whatif"`: Inventory only. Jobs branch to a `_whatif_<workload>` path that enumerates source items and writes rows to `_pkg._current_manifest` (a `ManifestWriter`). No Microsoft Graph calls, no `ItemMap`/`SyncCursor` writes. `gc` is passed as `None`; jobs must guard with `assert gc is not None` for non-whatif paths.

Example (contacts_job.py):
```python
sync_token = get_cursor(s, user.google_email, "contacts") if mode == "delta" else None
contacts, new_sync_token = iter_contacts(google_cfg, user.google_email, sync_token)
# ... process contacts ...
if new_sync_token:
    save_cursor(s, user.google_email, "contacts", new_sync_token)
```

### Error Handling & Retries

**Graph API retries:** GraphClient wraps all requests with tenacity retry (up to 7 attempts, exponential backoff 2–60s). Retryable: 429 (throttled), 500–504 (server errors), timeouts, network errors.

**Migration job errors:** If a single item fails (e.g., create_contact() throws), the job catches it, logs, and upsets ItemMap with status="failed". The job continues to the next item. Workload-level errors bubble up and mark JobRun.status="failed"; the orchestrator logs but does not re-run.

**Idempotency recovery:** If a job crashes mid-run, re-running the same command resumes from the first non-done item (via is_done() check). Completed items will be skipped.

### Config Propagation to Jobs

Jobs retrieve config via:
```python
import migrator as _pkg
cfg = _pkg._current_config
assert cfg is not None, "Orchestrator must set _current_config before dispatching"
```

The assertion documents the dependency. Orchestrator sets `_pkg._current_config = config` in `__init__()` before any job runs. This happens once per CLI invocation.

### Workload Structure

Each workload (contacts, calendar, files, mail) is isolated:
- Separate job function (contacts_job.run_contacts, etc.)
- Separate state tables (ItemMap/FolderMap scoped by workload name)
- Separate sync cursors (SyncCursor scoped by workload name)
- Separate concurrency and feature configs (e.g., MailWorkloadConfig.multi_label_policy)

This allows workloads to run independently, be enabled/disabled, and be re-run without affecting others.
