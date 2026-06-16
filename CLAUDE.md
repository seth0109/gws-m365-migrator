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
pytest configured in pyproject.toml with testpaths=["tests"]. `tests/test_multi_source.py` covers the pure (no-network) logic: config discrimination, source factory dispatch, capability gating, IMAP folder mapping, and manifest columns.

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
migrator files --config config.yaml         # Migrate personal files → OneDrive
migrator mail --config config.yaml          # Migrate mail (Gmail / IMAP / M365 source) → Outlook
migrator shared-drives --config config.yaml # Google Shared Drives → SharePoint (auto-provision sites); --delta for changed-only
migrator sharepoint --config config.yaml    # M365 SharePoint sites → M365 (tenant-to-tenant); --delta for changed-only
migrator run-all --config config.yaml       # Run all enabled + source-supported workloads sequentially
migrator delta --config config.yaml         # Delta sync (post-cutover) using stored cursors (per-user workloads)
migrator whatif --config config.yaml --output whatif_manifest.csv  # Dry-run inventory CSV
migrator smoke-test --config config.yaml    # Phase 0 test: probe source, write+delete a dest test folder
migrator validate --config config.yaml --output report.html  # Generate validation report
```
Use `--user <source_id>` to limit any run to a single user. Per-workload commands and `run-all`/`delta`/`whatif` skip (or error on) workloads the configured source does not support — see Source Connectors. Config is YAML with a `source:` block (type `google_workspace` | `imap` | `microsoft365`), a `destination:` Microsoft 365 block, user mappings (`source_id` → `dest_id`), optional `shared_drives`, workload settings, and rate limits. See `config.example.yaml`.

## Architecture

### Source/Destination model (the core abstraction)

The tool migrates from a pluggable **source** to a Microsoft 365 **destination**. The source is generalized behind connector classes (`src/migrator/connectors/`); the destination is always Microsoft Graph. This is what lets the same workload jobs serve Google Workspace, generic IMAP, and tenant-to-tenant M365 migrations.

- **Config**: `load_config(path)` validates a Pydantic v2 `Config` with a discriminated `source` union (`GoogleWorkspaceSourceConfig` | `ImapSourceConfig` | `Microsoft365SourceConfig`, discriminated on `type`) and a `Microsoft365DestinationConfig`. `UserMapping` is identity-neutral: `source_id` → `dest_id` (plus optional `imap_user`/`imap_password_env`). `GoogleConfig` is kept as a backwards-compat alias of `GoogleWorkspaceSourceConfig` so the untouched `google/*` connectors still type-check.
- **Source connectors** (`connectors/base.py`): each concrete source subclasses `BaseSource`, advertises a `capabilities` set, and emits **destination-ready** normalized items (`SourceMessage` carries raw RFC822 MIME + folder placement + flags; `SourceContact`/`SourceEvent` carry ready-to-POST Graph bodies; `SourceFile` carries metadata + a lazy `fetch_file`). `connectors/factory.py:build_source()` dispatches on `source.type`; `source_capabilities()` answers gating questions without constructing network clients.
- **JobContext** (`context.py`): replaces the old global-config injection for per-job data. The Orchestrator builds `JobContext(user, source, dest_gc, mode, config)` per user and calls `fn(ctx)`. `ctx.require_capability(workload)` raises if the source can't do that workload. (`_current_config`/`_current_manifest` globals remain only for whatif manifest access.)

**Key leverage:** every mail source emits raw MIME and `microsoft/files.py` takes a `drive_root` prefix — so the destination writers (`microsoft/{mail,files,contacts,calendar}.py`) are shared unchanged across all source types and across OneDrive vs SharePoint.

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
   - Writes JobRun row to DB with status="running" (`JobRun.user_email` holds `source_id`)
   - Builds a per-thread source connector + destination GraphClient, wraps them in a `JobContext`, and calls `job_fn(ctx)`
   - Updates JobRun.status to "done" or "failed"
3. Each job gets its own destination GraphClient (ephemeral HTTP client + token provider). For a `microsoft365` source, a second per-thread **source** GraphClient is built from the source-tenant token provider and injected into `M365Source`.
4. **State access:** All state reads/writes use `session_scope()` context manager, which creates a fresh session per scope. Session is committed on exit (exception triggers rollback). This is SQLAlchemy's recommended pattern for thread-safe concurrent access.

**Why separate GraphClient per thread:** MS token provider may refresh tokens. Using one shared client would require synchronization; separate clients per thread are simpler and each thread maintains its own token state.

### Sources never write back; destination is always Graph

The `google_workspace` source uses `.readonly` OAuth scopes (gmail/drive/contacts/calendar); the `imap` source connects read-only; the `microsoft365` source only reads from the source tenant. Connectors never mutate the source — all writes go to the Microsoft Graph destination (Outlook, OneDrive, SharePoint). Treat this as a safety invariant when adding source methods.

### Job Execution Model

**Signature:** `fn(ctx: JobContext) → None`

`JobContext` (`context.py`) carries `user` (a `UserMapping`, `source_id`→`dest_id`), `source` (the `BaseSource` connector for this thread), `dest_gc` (destination GraphClient, or `None` in whatif), `mode`, and `config`. Jobs are source-agnostic: they iterate `ctx.source.iter_*` items and write via `microsoft/*` with `ctx.dest_gc`.

**First line of every job:** `ctx.require_capability("<workload>")` — raises if the configured source doesn't support it.

**Mode behavior:**
- `"full"`: Process all items. Connectors capture a sync cursor during iteration; the job persists it via `ctx.source.get_last_cursor(key)` afterward.
- `"delta"`: Read the stored cursor, pass it as `since` to the connector, process only changed items.
- `"whatif"`: Inventory only. Jobs branch to `_whatif_<workload>`, iterate `ctx.source.inventory_*`, and write rows to `_pkg._current_manifest` (a `ManifestWriter`). No Graph calls, no `ItemMap`/`SyncCursor` writes. `ctx.dest_gc` is `None`.

Example (contacts_job.py):
```python
ctx.require_capability("contacts")
since = get_cursor(s, user.source_id, "contacts") if ctx.mode == "delta" else None
for contact in ctx.source.iter_contacts(user, since):
    dest_id = create_contact(gc, ms_user_id, folder_id, contact.graph_body)
    upsert_item(s, user.source_id, "contacts", contact.source_id, dest_id=dest_id, status="done")
if (cursor := ctx.source.get_last_cursor("contacts")):
    save_cursor(s, user.source_id, "contacts", cursor)
```

### Error Handling & Retries

**Graph API retries:** GraphClient wraps all requests with tenacity retry (up to 7 attempts, exponential backoff 2–60s). Retryable: 429 (throttled), 500–504 (server errors), timeouts, network errors.

**Migration job errors:** If a single item fails (e.g., create_contact() throws), the job catches it, logs, and upsets ItemMap with status="failed". The job continues to the next item. Workload-level errors bubble up and mark JobRun.status="failed"; the orchestrator logs but does not re-run.

**Idempotency recovery:** If a job crashes mid-run, re-running the same command resumes from the first non-done item (via is_done() check). Completed items will be skipped.

### Config Propagation to Jobs

Per-job config arrives via `ctx.config` (and `ctx.source`/`ctx.dest_gc`) — see Job Execution Model. The package-level `_pkg._current_config` / `_pkg._current_manifest` globals remain: `_current_config` is set by the Orchestrator in `__init__()`, and whatif jobs read `_current_manifest` (set by the `whatif` CLI command) to record planned items.

### SharePoint migrations (tenant-level, not per-user)

Two SharePoint flows exist; both bypass the per-user `run_workload` path and instead use a dedicated `Orchestrator` method that builds one source + one destination client and a sentinel `JobContext`. Both upload through the shared `microsoft/files.py` writers with `drive_root=f"drives/{dest_drive_id}"` and namespace state by source id (`shared_drive:<id>` / `sharepoint_site:<id>`).

- **`migrator shared-drives`** (google_workspace source) — `Orchestrator.run_shared_drives()` impersonates the Workspace `admin_email` to enumerate/download Drive content, and for each `shared_drives` mapping calls `microsoft/sharepoint.py:ensure_site_for_drive()` — provisions a connected M365 group/team site (`POST /groups`, polls `/groups/{id}/sites/root`), resolves its default document library, and records it in `FolderMap` (`workload="sharepoint_site"`) for idempotent reuse.
- **`migrator sharepoint`** (microsoft365 source) — `Orchestrator.run_sharepoint_sites()` migrates SharePoint libraries tenant-to-tenant. `M365Source.resolve_site_drive()` resolves the source site's library drive; the destination is an existing `dest_site` (`resolve_existing_site_drive()`) or an auto-provisioned `target_site_alias` (reusing `ensure_site_for_drive()`).

`M365Source` file reads are **drive-generic**: `_iter_drive(drive_root, …)` walks any drive's delta feed and stamps `SourceFile.drive_root` so `fetch_file()` reads content from the right drive (OneDrive `users/<id>/drive` or SharePoint `drives/<id>`). Both flows require `Group.ReadWrite.All` + `Sites.*` on the destination app (only auto-provisioning needs `Group.ReadWrite.All`).

**Delta support.** Both flows accept `--delta` (`Orchestrator.run_shared_drives(mode=...)` / `run_sharepoint_sites(mode=...)`), mirroring the per-mailbox delta handling in `files_job.run_files`. The two helpers `files_job._delta_cursor()` / `_persist_cursor()` wrap the SyncCursor read/write: a full pass passes `since=None` and persists the cursor the connector captured during iteration; a delta pass reads the stored cursor and passes it as `since` (skipping any drive/site with no seeded cursor). Cursors are namespaced in SyncCursor by the per-flow workload string (`shared_drive:<drive_id>` / `sharepoint_site:<site_id>`) under `ctx.user.source_id` (the impersonation admin / the `__sharepoint__` sentinel).
> - **shared-drives** (Google source): `iter_shared_drive_files(user, drive, since)` captures a per-drive Changes-API page token (`drive.py:get_changes_start_token(drive_id=…)`) on the full pass and uses `iter_drive_changes(…, drive_id=…)` on delta. Connector cursor key == workload string (`shared_drive:<drive_id>`).
> - **sharepoint** (M365 source): `iter_site_files(drive_id, since)` → `_iter_drive()` captures the Graph deltaLink under connector key `sharepoint:<source_drive_id>`; the job re-reads it via that key but persists/loads SyncCursor under `sharepoint_site:<source_site_id>`.

### Workload Structure

Each workload (contacts, calendar, files, mail) is isolated:
- Separate job function (contacts_job.run_contacts, etc.)
- Separate state tables (ItemMap/FolderMap scoped by workload name)
- Separate sync cursors (SyncCursor scoped by workload name)
- Separate concurrency and feature configs (e.g., MailWorkloadConfig.multi_label_policy)

This allows workloads to run independently, be enabled/disabled, and be re-run without affecting others.

### Transform Layer

`src/migrator/transform/` holds the pure (no I/O) Google→Graph data-model conversions. Jobs call these to translate source shapes into Graph request bodies. Keep this logic here rather than inline in jobs — it is unit-testable in isolation and shared across full/delta/whatif modes.

- **labels.py** — `label_to_folder_path()` maps a Gmail label to an Outlook folder path (rewriting `/` separators to `\`). `resolve_label_placement()` is the core mail-foldering logic: it maps system labels to well-known Outlook folders (via `SYSTEM_LABEL_FOLDER`; some resolve to `None` and become flags/categories or are skipped) and applies `MailWorkloadConfig.multi_label_policy` to user labels — `"categories"` files a message in one primary folder and attaches the rest as Outlook categories, `"duplicate"` copies it into every label's folder. Returns `(folder_paths, categories)`.
- **recurrence.py** — `rrule_to_graph_recurrence()` converts an iCal RRULE string + start datetime into a Graph `recurrence` object (pattern + range). Handles BYDAY→daysOfWeek, weekly/monthly/yearly indices, and the weekday derived from the event start.
- **paths.py** — `sanitize_segment()` / `sanitize_path()` strip characters illegal in OneDrive/SharePoint names so Drive folder structures map cleanly.
- **identities.py** — `IdentityMap` resolves Google email addresses to MS UPNs (from `config.users`) when rewriting attendees, sharing, and sender/recipient fields.

### Whatif Manifest Injection

Parallel to `_current_config`, the package holds `_pkg._current_manifest: ManifestWriter | None` (`src/migrator/__init__.py`). The `whatif` CLI command sets it around the run; jobs in `whatif` mode write inventory rows to it instead of calling Graph. It is `None` outside whatif runs.

### Microsoft Auth Modes

`MSTokenProvider` (auth/ms_auth.py) is an MSAL confidential-client provider with a thread lock and serialized disk token cache. It accepts **either** `certificate_path` + `certificate_thumbprint` (preferred) **or** `client_secret`; supplying neither raises at construction. Scope is fixed to `https://graph.microsoft.com/.default` (app-only). Each per-thread GraphClient holds its own provider — see "Threading & Job Dispatch".
