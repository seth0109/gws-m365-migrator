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
pytest configured in pyproject.toml with testpaths=["tests"]. All tests are pure / no-network — Graph interactions are exercised through hand-rolled fake clients (`_FakeGC` / `_RecordingGC` patterns) and `httpx.MockTransport`, not live calls. Coverage: `test_multi_source.py` (config discrimination, source factory dispatch, capability gating, IMAP folder mapping, manifest columns, SharePoint flows), `test_configgen.py` (init-config scaffolding), `test_identities.py` / `test_reporting.py` (identity remap, report workload discovery), `test_mail.py` (CRLF normalization, MIME recovery ladder, JSON import + MAPI extended properties), `test_files.py` (OneDrive provisioning wait, folder ensure/409, PUT upload), `test_graph_client.py` (retry classification, transport reset, auth-header routing, pagination, per-attempt rate limiting), `test_recurrence.py` (RRULE → Graph pattern mapping), `test_sharepoint.py` (group provisioning body, nickname sanitization, poll error handling), `test_paths.py` (name sanitization), `test_contacts.py` (photo selection/upload, folder ensure), `test_calendar_exceptions.py` (occurrence matching + `_apply_exception` against a seeded state DB), `test_mail_job.py` (end-to-end run_mail: idempotent reruns, duplicate prevention, cursor gating, delta refusal, `fetch_error` items), `test_files_job.py` / `test_contacts_job.py` / `test_calendar_job.py` (the other job loops: out-of-order folder placement, delta PATCH-in-place, tombstones, failed-update dest_id retention), `test_imap.py` (UID search wire args, FLAGS order, fetch failures against a fake connection), `test_gmail.py` / `test_m365_source.py` (per-message fetch-error stubs, contact sub-folders), `test_state.py` (`updated_at` on upsert, failure counting), `test_cli.py` (`--user` targeting).

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

### Baseline state of the checks
`pytest` is green (201 tests, ~5–8 s, no network). `ruff check src/` and `mypy src/` are **not** clean: there is a pre-existing baseline of 10 ruff findings (8 E501 + 2 UP028) and 20 mypy errors (untyped `googleapiclient` returns in `google/*`, `Returning Any` in thin Graph writers), tracked in `TODO.md`. Treat those counts as the floor — fix what you touch, don't add new ones, and don't read the existing output as breakage you caused. Neither tool is configured over `tests/` (`src = ["src"]`, `mypy src/`).

### Local artifacts (never commit)
The CLI writes into the current working directory: `migration_state.db` (+ WAL sidecars), `migrator.log`, `.ms_token_cache.json`, `whatif_manifest.csv`, `migration_report.html`. All of these are gitignored (credentials, `*.db`, `*.log`, token caches, `*_manifest.csv`, `*_report.html`). The manifest and report contain real mailbox subjects, file names, and user addresses, so if you add a new output path keep it under one of those patterns. `cli._setup_logging` pins the file handler to `encoding="utf-8"` because this is developed/run on Windows, where the default cp1252 encoder crashes on the `→` in log messages.

### Run the CLI Tool
```bash
migrator --help                              # Show all commands
migrator init-config --type <src> --tenant-id .. --client-id .. --thumbprint .. -m users.csv  # Scaffold config.yaml from credentials/ + mapping CSV
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
Use `--user <source_id>` to limit any *per-user* run to one user; the two tenant-level SharePoint commands take `--delta` instead. Per-workload commands and `run-all`/`delta`/`whatif` skip (or error on) workloads the configured source does not support — see Source Connectors. Typical cutover sequence: `init-config` → `smoke-test` → `whatif` → `run-all` → *(cutover)* → `delta` → `validate`. Config is YAML with a `source:` block (type `google_workspace` | `imap` | `microsoft365`), a `destination:` Microsoft 365 block, user mappings (`source_id` → `dest_id`), optional `shared_drives`, workload settings, and rate limits. See `config.example.yaml`.

## Architecture

### Source/Destination model (the core abstraction)

The tool migrates from a pluggable **source** to a Microsoft 365 **destination**. The source is generalized behind connector classes (`src/migrator/connectors/`); the destination is always Microsoft Graph. This is what lets the same workload jobs serve Google Workspace, generic IMAP, and tenant-to-tenant M365 migrations.

- **Config**: `load_config(path)` validates a Pydantic v2 `Config` with a discriminated `source` union (`GoogleWorkspaceSourceConfig` | `ImapSourceConfig` | `Microsoft365SourceConfig`, discriminated on `type`) and a `Microsoft365DestinationConfig`. `UserMapping` is identity-neutral: `source_id` → `dest_id` (plus optional `imap_user`/`imap_password_env`). `GoogleConfig` is kept as a backwards-compat alias of `GoogleWorkspaceSourceConfig` so the untouched `google/*` connectors still type-check.
- **Source connectors** (`connectors/base.py`): each concrete source subclasses `BaseSource`, advertises a `capabilities` set, and emits **destination-ready** normalized items (`SourceMessage` carries raw RFC822 MIME + folder placement + flags; `SourceContact`/`SourceEvent` carry ready-to-POST Graph bodies; `SourceFile` carries metadata + a lazy `fetch_file`). `connectors/factory.py:build_source()` dispatches on `source.type`; `source_capabilities()` answers gating questions without constructing network clients.
- **JobContext** (`context.py`): replaces the old global-config injection for per-job data. The Orchestrator builds `JobContext(user, source, dest_gc, mode, config)` per user and calls `fn(ctx)`. `ctx.require_capability(workload)` raises if the source can't do that workload. (`_current_config`/`_current_manifest` globals remain only for whatif manifest access.)

**Key leverage:** every mail source emits raw MIME and `microsoft/files.py` takes a `drive_root` prefix — so the destination writers (`microsoft/{mail,files,contacts,calendar}.py`) are shared unchanged across all source types and across OneDrive vs SharePoint.

**Adding a source type** — the seams, in order: a `*SourceConfig` model with a `Literal["…"] type` added to the `SourceConfig` union in `config.py` → a `BaseSource` subclass declaring `capabilities` and implementing only the `iter_*` / `inventory_*` / `fetch_*` methods for those workloads → registration in `connectors/factory.py`, both in `build_source()` **and** in the `_CAPABILITIES` table (the CLI gates on that table so it never has to build a network client) → optional `configgen.py` support for `init-config`. Nothing under `microsoft/` or `workloads/` should need to change; if it does, extend the item dataclasses in `connectors/base.py` instead of branching per source inside a job.

### Google source layer (`google/*` + `auth/google_auth.py`)

Thin per-API wrappers under `google/` that `connectors/google.py` composes; they take a `GoogleConfig` + the user's email and return raw API dicts.

- **Impersonation is per call.** `build_service()` mints fresh domain-wide-delegation credentials (`.with_subject(user_email)`) and a new discovery-cached-off client for *every* wrapper call — there is no long-lived service object to thread around.
- **The read-only invariant is enforced in code**, not just by config: `impersonated_credentials()` raises `ValueError` on any scope that doesn't end in `.readonly`. That guard is the enforcement point for "sources never write back" — don't route around it.
- **Retries are the library's.** There is no tenacity wrapper; every `execute()` passes `num_retries=NUM_RETRIES` (`google/__init__.py`), which is googleapiclient's own exponential backoff on 5xx/429/socket errors. Keep it on new calls. Note that a connector *generator* raising kills enumeration for that user (a generator is dead after it raises), so per-item fetch errors must not escape it: `gmail._get_raw` skips a 404 (deleted between list and get — Gmail documents this for history records) and turns any other `HttpError` into a `{"id", "fetch_error"}` stub that the connector emits as a `SourceMessage.fetch_error`; `mail_job` fails just that item and holds the cursor. The M365 and IMAP connectors do the same.
- **Expired-cursor fallbacks** live here: Gmail `historyId` 404 → warn + full sync (`gmail.iter_messages`), People `syncToken` 410 → recursive re-baseline (`people.iter_contacts`, which also must send `requestSyncToken=True` or the API never returns a token at all). Google Calendar's syncToken 410 still raises — see `TODO.md`.
- **Native Google Docs** are exported, not downloaded: `drive.EXPORT_MIME_MAP` maps Docs/Sheets/Slides/Drawings to Office/SVG formats plus the extension the connector appends; a `None` entry (Forms, Apps Script) becomes `action="skip"` with a note, and any other `application/vnd.google-apps.*` type is skipped the same way.
- **Calendars are pulled with `singleEvents=False`** (series stay intact) and `showDeleted=True`, which is what makes the recurrence-exception reconciliation below possible. `people.iter_contacts` / `calendar.iter_events` / `drive.iter_drive_changes` buffer a whole result set in memory and return `(items, new_cursor)` rather than streaming.

### IMAP source (`connectors/imap.py`)

Mail-only (`capabilities = {"mail"}`), raw `imaplib`, one connection per connector instance (i.e. per thread), logging in as `imap_user or source_id` with `UserMapping.resolve_imap_password()` (env var preferred over inline password). **Folder placement** — a `\Sent`/`\Drafts`/`\Trash`/`\Junk` special-use attribute wins, then a leaf-name heuristic (`sent`, `spam`, `deleted items`, …), otherwise the hierarchy is kept with the server delimiter rewritten to `\`. The tokens it emits (`SentItems`, `JunkEmail`, …) are the same ones the Gmail path produces, so `mail_job` routes both through `resolve_folder_segment()` identically. `\Noselect` folders and `source.exclude_folders` are skipped. **Read-only** is enforced by `select(..., readonly=True)` on every folder, so FETCH never sets `\Seen`. **Cursor** is a JSON map `folder → {uidvalidity, uidnext}`: a delta pass searches `UID SEARCH UID <prev uidnext>:*` per folder (the `UID` criterion is load-bearing — a bare set is *message sequence numbers* per RFC 3501 §6.4.8 and silently skips mail that arrived after expunges), and a changed UIDVALIDITY re-scans that folder from UID 1 — ItemMap keys are `folder:uidvalidity:uid`, so that re-scan does not dedupe against the earlier import. `_fetch_message` parses FLAGS from every FETCH response part — request-order servers (Dovecot) answer `RFC822` first, leaving FLAGS in a trailing bytes element — and turns a per-message FETCH failure into a `fetch_error` item (an `IMAP4.abort` still propagates: the connection is dead). Known gaps (`TODO.md`): folder names are decoded as UTF-8 rather than RFC 3501 modified UTF-7, and literal-form LIST responses are dropped.

### Microsoft 365 source (`connectors/m365.py`)

Reads the *source* tenant through a second per-thread `GraphClient` (see Threading) and never calls `post`/`patch`/`delete` on it. Mail folders are mapped by walking `mailFolders` → `childFolders` and translating top-level `wellKnownName`s to the canonical tokens the other sources emit (`_WELLKNOWN_TO_TOKEN`); messages are fetched as raw MIME via `/messages/{id}/$value`, so the destination writer path is identical to Gmail/IMAP (a 404 mid-run is skipped; any other fetch error becomes a `fetch_error` item). Contacts are read from the default folder **and** every `contactFolders` sub-folder (walked recursively, flattened to the folder display name) — `/users/{id}/contacts` alone is only the default folder; `photo_ref` is the folder-qualified contact path. **Delta is asymmetric:** files (OneDrive and SharePoint, via `_iter_drive`) use real Graph delta links, but mail/contacts/calendar stamp the cursor to "now" and `$filter` on `receivedDateTime`/`lastModifiedDateTime ge since` — lossy for folder moves and flag changes, and it never enumerates recurrence exceptions or hidden folders (all backlog items).

### Mail import (`microsoft/mail.py`)

Graph's MIME create is documented as "Create a **draft**": every MIME import lands with `isDraft: true` and `receivedDateTime` stamped at import time, and neither is correctable after creation. `workloads.mail.import_mode` selects the strategy; `import_message()` dispatches.

- **`json` (default)** — `import_json_message()` parses the MIME and creates the message via the JSON API with the MAPI extended properties that are only writable at create time: `PidTagMessageFlags` (0x0E07 — a clear unsent bit is what makes it a non-draft; also encodes read state), `PidTagClientSubmitTime`/`PidTagMessageDeliveryTime` (0x0039/0x0E06 — original sent/received dates in UTC; received parsed from the topmost `Received` header, falling back to `Date`), and `PidTagTransportMessageHeaders` (0x007D — original header block, capped at 32 KB). `isRead` and categories ride on the create — no follow-up PATCH. Attachments go inline while the total stays under ~2 MB (Graph caps requests at 4 MB); larger sets are added post-create via the attachment APIs (single POST ≤3 MB, chunked upload session above — documented to work on existing messages). If the tenant 400s the fidelity extras (from/sender/replyTo/headers), one retry drops them but always keeps flags + dates. `message/rfc822` parts (forwarded-as-attachment emails) are carried as `.eml` file attachments: to the stdlib they are "multipart", so walking into them would splice the inner body into ours — `_graph_body_and_attachments` recurses with its own visitor and `_split_large_attachments` extracts them whole.
- **`mime`** — byte-perfect content via the original recovery ladder: normalize CRLF (`_normalize_crlf` — Graph rejects bare-LF with an opaque 400 `UnableToDeserializePostBody`) → size routing (≤3 MB single POST; larger strips `Content-Disposition: attachment` parts and re-adds them, inline/cid parts stay) → deserialize-400 diagnostics (header line-length stats; `MIGRATOR_DUMP_REJECTED_MIME=<dir>` dumps the first 5 rejects as `.eml`) + retry with `_TRACE_HEADERS` stripped → `_import_via_json` last resort (same JSON machinery as json mode). Accepts the draft/import-date limitation; flags are PATCHed after import, best-effort.

**Job ordering invariant (`mail_job`):** the item is marked `done` immediately after the primary-folder import succeeds; extra folder copies (`multi_label_policy: duplicate`) and mime-mode flag patches are best-effort afterwards (warn, never fail the item). Flipping the status back after a successful import would re-import the message as a duplicate on the next run — don't. A message with `fetch_error` set is failed before any import attempt (with the source error as `last_error`) and counts toward the cursor gate.

**Folder placement:** `mail_job` routes *top-level* system folders through `resolve_folder_segment()` to Graph **well-known folder ids** (`inbox`, `sentitems`, …) so mail lands in the real Inbox instead of a duplicate custom folder with the same display name. Everything else goes through `ensure_mail_folder()`, which paginates the folder listing with `$top=100` (Graph returns only 10 folders per page by default — a naive single GET misses folders and then 409s on create) and resolves a 409 `ErrorFolderExists` by re-lookup. `ensure_contact_folder` and the drive `ensure_folder` follow the same paginate + 409-recover pattern; `microsoft/calendar.py:ensure_calendar` is the one outlier (single un-paginated GET, no 409 recovery — backlog item).

**Message state:** Gmail STARRED becomes the Outlook follow-up flag (`SourceMessage.is_flagged` → `flag: {flagStatus: flagged}` on the json create, PATCHed in mime mode) and IMPORTANT becomes an `"Important"` category — set by the connector, not the label transform. Spam/Trash are enumerated when `workloads.mail.include_spam_trash` is true (default) and route to JunkEmail/DeletedItems.

### Calendar recurrence exceptions (`calendar_job._apply_exception`)

With `singleEvents=False`, Google returns modified/cancelled single occurrences as separate events carrying `recurringEventId` + `originalStartTime`. The connector stamps these on `SourceEvent` (`master_source_id` / `original_start`); `calendar_job` defers them to the end of the pass (so masters exist), looks up the master's `dest_id` in ItemMap, locates the destination occurrence via `microsoft/calendar.py:find_instance()` (`/events/{master}/instances` windowed around the original start, matched on `originalStart`/`start` normalized to UTC), then PATCHes it (modified) or DELETEs it (cancelled). A cancelled occurrence that's already absent counts as done. Failures hold back the calendar's cursor like any other item. The M365 source doesn't enumerate exceptions at all yet — see TODO.md.

**Contact photos** ride the same lazy pattern as file content: connectors stamp `SourceContact.photo_ref` (Google: People photo URL, skipping default avatars; M365: contact id → `photo/$value`, 404 quieted) and `contacts_job` uploads via `set_contact_photo()` after create, best-effort — photo failures never fail the contact.

### OneDrive/SharePoint files writer (`microsoft/files.py`)

- **Lazy provisioning:** a destination user's OneDrive is provisioned lazily — the first `GET /users/{id}/drive` 404s ("mysite not found") but *queues* provisioning. `files_job` calls `ensure_onedrive()` before any writes; it polls ~2.5 min and raises an actionable error (pre-provision via `Request-SPOPersonalSite` or a user sign-in). Failure skips just that user.
- **Simple upload is PUT-only** (`upload_small_file` → `put_raw`); POST to `:/content` is a 405 per the docs.
- **`ensure_folder`** paginates `/children` and matches client-side, case-insensitively — Graph's `/children` endpoint does not support `$filter` at all — and resolves a 409 `nameAlreadyExists` by re-listing (same pattern as `ensure_mail_folder`).
- **Placement is order-independent** (`files_job._migrate`): parents are resolved through a `_FolderIndex` — an in-memory map backed by `FolderMap`, so a folder migrated by an earlier run resolves in a delta pass — and an item whose parent is not known yet is *deferred* and retried after the pass (neither Drive `files.list` nor Graph delta orders parents before children). When a round makes no progress, items whose parent is not in the batch are orphans relative to the corpus (shared-with-me) and go to the drive root with one warning per parent; children of a folder whose create failed are failed with "parent folder … failed", never rooted. `_process_file` returns `_OK`/`_FAILED`/`_DEFERRED`, not a bool.

### Config Scaffolding (`init-config`)

`src/migrator/configgen.py` holds the pure, network-free logic behind the `init-config` CLI command: `discover_credentials()` resolves the service-account JSON / certificate PEM(s) in a `credentials/` folder (a `microsoft365` source needs two PEMs, disambiguated by `source*`/`dest*` filename keyword); `parse_mapping_csv()` reads `source_id,dest_id[,imap_user,imap_password_env]` rows (header aliases accepted); `build_config_dict()` assembles a `Config`-shaped dict, **validates it via `Config.model_validate`**, and `dump_config_yaml()` serializes it. All four are unit-tested in `tests/test_configgen.py` without typer or any network client; `cli.py:init_config` is a thin wrapper that catches `ConfigGenError` and writes the file (guarded by `--force`).

### Rate Limiting (Three-Layer System)

1. **Global registry** — singleton `migrator.ratelimit.registry`. `Orchestrator._setup_rate_limiters` registers two thread-safe `TokenBucket`s, `"google_global"` and `"graph_global"`, from `config.rate_limits`. Jobs never touch it directly.
2. **Per-user limiter** — one `PerUserRateLimiter` (a dict of buckets keyed by mailbox/drive id, rate `graph_requests_per_mailbox_per_minute`) handed to every GraphClient the Orchestrator builds.
3. **GraphClient** — every `get/get_bytes/post/patch/delete/put_raw/paginate` goes through `_request`, which calls `_apply_rate_limits(user_key)` first: acquire from the global bucket (silently skipped if unregistered — which is how the unit tests run), then from the per-user bucket when a `user_key` is passed. Sleeps when a bucket is empty.

**Key invariant:** Rate limiters are checked before every attempt — `_apply_rate_limits` runs *inside* the tenacity-retried closure, so retries re-acquire tokens rather than bypassing the limiter.

**The Google side is not symmetric.** There is no Google counterpart to `GraphClient`: each function in `google/*.py` calls `registry.acquire("google_global")` inline before its request, wrapped in `except KeyError: pass` so unit tests work without a registered bucket. Keep that inline acquire when adding a `google/*` call — it is the only thing throttling the Google API; the only retry underneath it is googleapiclient's own `num_retries` backoff (see the Google source layer above).

### Idempotency & State Store

**Invariant:** before processing any item (contact, event, email, file) the job checks `is_done(session, user.source_id, workload, item_source_id)` against `ItemMap`, and after a successful write calls `upsert_item(..., dest_id=..., status="done")`. `ItemMap` is unique on `(user_email, workload, source_id)` and carries `status` (pending/done/failed/skipped), `dest_id`, `source_hash`, `attempts`, `last_error`. The `user_email` column in every state table holds the mapping's **`source_id`** (the name predates the multi-source refactor). An interrupted run resumes at the first non-done item with no duplicate writes — see the contacts_job example under Job Execution Model. **A non-null `dest_id` means the item exists at the destination regardless of `status`**: a failed *update* keeps `dest_id` (and the hash the destination copy still reflects), so the retry PATCHes instead of creating a duplicate — read it via `get_item_state()`. `upsert_item` overwrites every column on conflict (pass `dest_id`/`source_hash` explicitly when they must survive) and stamps `updated_at` itself, because ON CONFLICT DO UPDATE bypasses `Column.onupdate`; `count_failed_items_since` (the CLI exit code) compares against that stamp in the stored string format.

**SQLite under threads:** `init_db` sets `check_same_thread=False`, a 30 s busy timeout, and WAL journal mode — the per-workload thread pools commit concurrently and would otherwise hit cross-thread errors and "database is locked".

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

Safety invariant: connectors only read. The enforcement point per source — `google_workspace`: `impersonated_credentials()` rejects any non-`.readonly` scope; `imap`: every folder is opened `readonly=True`; `microsoft365`: `M365Source` only ever calls `get`/`get_bytes`/`paginate` on the source client. All writes go to the destination Graph tenant (Outlook, OneDrive, SharePoint). Keep it that way when adding source methods.

### Job Execution Model

**Signature:** `fn(ctx: JobContext) → None` (`JobContext` is described under the Source/Destination model). Jobs are source-agnostic: they iterate `ctx.source.iter_*` items and write via `microsoft/*` with `ctx.dest_gc`. **First line of every job:** `ctx.require_capability("<workload>")`.

**Mode behavior:**
- `"full"`: Process all items. Connectors capture a sync cursor during iteration; the job persists it via `ctx.source.get_last_cursor(key)` afterward — **only when the run had zero item failures**. Advancing the cursor past a failed item would drop it from every future delta, so a run with failures keeps the old cursor and the next run retries them (idempotency makes the re-scan safe).
- `"delta"`: Read the stored cursor, pass it as `since` to the connector, process only changed items. **A missing cursor refuses the run** (warn + return) instead of silently re-scanning the whole source; the failure-gating above applies here too. In delta, an item the source re-emits that is already `done` is *updated in place* — files compare `content_hash`, contacts/calendar compare `source_hash` (People/Calendar etag, Graph changeKey) and PATCH the recorded `dest_id` when it differs (unchanged → no Graph call). People tombstones (`SourceContact.is_deleted`) delete the migrated contact; cancelled events are deleted in any mode. Mail delta only adds new messages (see `TODO.md`).
- `"whatif"`: Inventory only. Jobs branch to `_whatif_<workload>`, iterate `ctx.source.inventory_*`, and write rows to `_pkg._current_manifest` (a `ManifestWriter`). No Graph calls, no `ItemMap`/`SyncCursor` writes, and `ctx.dest_gc` is `None` — but the orchestrator still records a `JobRun` row per user, so whatif is not *quite* read-only on state (see `TODO.md`).

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

**Graph API retries:** GraphClient wraps all requests with tenacity retry (up to 7 attempts, exponential backoff 2–60s). Retryable: 429 (throttled), 500–504 (server errors), timeouts, network errors, and `httpx.RemoteProtocolError`. Between retries a `before_sleep` hook calls `_reset_transport()` — dropping the pooled connections and redialing fresh — because a half-closed keep-alive socket is the cause of the "works for a while, then every request fails" cascade (surfacing as `RemoteProtocolError` or bogus deserialize-400s). A content-based 400 `UnableToDeserializePostBody` is deliberately **not** retryable at the client layer; the mail layer owns that recovery (see the mail import recovery ladder). A 429 is retried too, but through tenacity's `wait` (`_wait` returns Retry-After seconds, exponential backoff otherwise) so the throttle is slept exactly once, and `_redial` leaves the pool alone for it — the socket is healthy, the server just said no.

**Error visibility:** on any 4xx/5xx, GraphClient logs the response body (Graph's `error.code`/`message` — `raise_for_status()` alone would drop it) plus a truncated echo of the outgoing JSON request body; raw `content=` payloads (MIME, file bytes) are reported by size only. Preserve this when touching `_request` — it's the primary tool for diagnosing body-shape rejections.

**Upload sessions:** requests to non-Graph hosts (the pre-authenticated `uploadUrl`s returned by createUploadSession) are sent **without** the Authorization header — the OneDrive docs warn that including it can 401. Only Graph-host requests get the bearer token + JSON default content type.

**CLI exit codes:** every migration command exits 1 when the run left anything behind — a failed user-level run or any ItemMap row that flipped to `failed` during the run (`cli._exit_if_failures`) — so scripted cutovers can't mistake a bad run for success. `--user` that matches no configured `source_id` also exits 1: `_filter_users` never returns an empty list, and `run_workload` treats only `None` as "all users" (an explicit `[]` runs nobody), so a typo can never widen into a whole-tenant run.

**Migration job errors:** If a single item fails (e.g., create_contact() throws), the job catches it, logs, and upserts ItemMap with status="failed". The job continues to the next item. Workload-level errors bubble up and mark JobRun.status="failed"; the orchestrator logs but does not re-run.

### Package-level globals (`src/migrator/__init__.py`)

Two survive the JobContext refactor: `_current_config` (set by `Orchestrator.__init__`) and `_current_manifest: ManifestWriter | None` (set by the `whatif` CLI command around the run, `None` otherwise). Whatif-mode jobs write inventory rows to the manifest instead of calling Graph. Nothing else should read these — per-job data comes from `ctx`.

### SharePoint migrations (tenant-level, not per-user)

Two SharePoint flows exist; both bypass the per-user `run_workload` path and instead use a dedicated `Orchestrator` method that builds one source + one destination client and a sentinel `JobContext`. Both upload through the shared `microsoft/files.py` writers with `drive_root=f"drives/{dest_drive_id}"` and namespace state by source id (`shared_drive:<id>` / `sharepoint_site:<id>`).

- **`migrator shared-drives`** (google_workspace source) — `Orchestrator.run_shared_drives()` impersonates the Workspace `admin_email` to enumerate/download Drive content, and for each `shared_drives` mapping calls `microsoft/sharepoint.py:ensure_site_for_drive()` — provisions a connected M365 group/team site (`POST /groups`, polls `/groups/{id}/sites/root`), resolves its default document library, and records it in `FolderMap` (`workload="sharepoint_site"`) for idempotent reuse. Groups are created **Private** with a charset-sanitized `mailNickname`, and bound to `destination.sharepoint_site_owner` when set — Graph documents that app-only groups created *without* an owner may never get their site provisioned, so set it. The group id is recorded in `FolderMap` (workload `sharepoint_group`) *before* the site poll, so a provisioning timeout is resumable: the re-run polls the existing group instead of POSTing a second one with the same `mailNickname` (which Graph rejects).
- **`migrator sharepoint`** (microsoft365 source) — `Orchestrator.run_sharepoint_sites()` migrates SharePoint libraries tenant-to-tenant. `M365Source.resolve_site_drive()` resolves the source site's library drive; the destination is an existing `dest_site` (`resolve_existing_site_drive()`) or an auto-provisioned `target_site_alias` (reusing `ensure_site_for_drive()`).

`M365Source` file reads are **drive-generic**: `_iter_drive(drive_root, …)` walks any drive's delta feed and stamps `SourceFile.drive_root` so `fetch_file()` reads content from the right drive (OneDrive `users/<id>/drive` or SharePoint `drives/<id>`). Both flows require `Group.ReadWrite.All` + `Sites.*` on the destination app (only auto-provisioning needs `Group.ReadWrite.All`).

**Delta support.** Both flows accept `--delta` (`Orchestrator.run_shared_drives(mode=...)` / `run_sharepoint_sites(mode=...)`), mirroring the per-mailbox delta handling in `files_job.run_files`. The two helpers `files_job._delta_cursor()` / `_persist_cursor()` wrap the SyncCursor read/write: a full pass passes `since=None` and persists the cursor the connector captured during iteration; a delta pass reads the stored cursor and passes it as `since` (skipping any drive/site with no seeded cursor). Cursors are namespaced in SyncCursor by the per-flow workload string (`shared_drive:<drive_id>` / `sharepoint_site:<site_id>`) under `ctx.user.source_id` (the impersonation admin / the `__sharepoint__` sentinel).
> - **shared-drives** (Google source): `iter_shared_drive_files(user, drive, since)` captures a per-drive Changes-API page token (`drive.py:get_changes_start_token(drive_id=…)`) on the full pass and uses `iter_drive_changes(…, drive_id=…)` on delta. Connector cursor key == workload string (`shared_drive:<drive_id>`).
> - **sharepoint** (M365 source): `iter_site_files(drive_id, since)` → `_iter_drive()` captures the Graph deltaLink under connector key `sharepoint:<source_drive_id>`; the job re-reads it via that key but persists/loads SyncCursor under `sharepoint_site:<source_site_id>`.

### Workload Structure

Each workload (contacts, calendar, files, mail) has its own job function, its own `ItemMap`/`FolderMap`/`SyncCursor` namespace (the `workload` column), and its own `enabled`/`concurrency` config, so any one can be disabled or re-run without touching the others. `run-all`/`delta`/`whatif` iterate `cli._WORKLOAD_ORDER` (contacts → calendar → files → mail).

### Transform Layer

`src/migrator/transform/` holds the pure (no I/O) Google→Graph data-model conversions. Jobs call these to translate source shapes into Graph request bodies. Keep this logic here rather than inline in jobs — it is unit-testable in isolation and shared across full/delta/whatif modes.

- **labels.py** — `label_to_folder_path()` maps a Gmail label to an Outlook folder path (rewriting `/` separators to `\`). `resolve_label_placement()` is the core mail-foldering logic: it maps system labels to well-known Outlook folders (via `SYSTEM_LABEL_FOLDER`; some resolve to `None` and become flags/categories or are skipped) and applies `MailWorkloadConfig.multi_label_policy` to user labels — `"categories"` files a message in one primary folder and attaches the rest as Outlook categories, `"duplicate"` copies it into every label's folder. Returns `(folder_paths, categories)`.
- **recurrence.py** — `rrule_to_graph_recurrence()` converts an iCal RRULE string + start datetime into a Graph `recurrence` object (pattern + range). Handles BYDAY→daysOfWeek, weekly/monthly/yearly indices, and the weekday derived from the event start.
- **paths.py** — `sanitize_segment()` / `sanitize_path()` strip characters illegal in OneDrive/SharePoint names so Drive folder structures map cleanly.
- **identities.py** — `IdentityMap` rewrites source identities to destination M365 UPNs using `config.users` (`source_id` → `dest_id`) as the source of truth — pure/deterministic, no Graph calls. `map_address()` returns the mapped address (or the original when unmapped, so external attendees pass through); `remap_event()` rewrites attendee + organizer addresses on a Graph event body in place. Applied by `calendar_job.run_calendar` before `create_event`, so migrated events reference live destination mailboxes rather than dead source ones.

### Microsoft Auth Modes

`MSTokenProvider` (auth/ms_auth.py) is an MSAL confidential-client provider with a thread lock and serialized disk token cache. It accepts **either** `certificate_path` + `certificate_thumbprint` (preferred) **or** `client_secret`; supplying neither raises at construction. Scope is fixed to `https://graph.microsoft.com/.default` (app-only). Each per-thread GraphClient holds its own provider — see "Threading & Job Dispatch".

## Testing Patterns

There is no `conftest.py` and no shared fixtures package — every test file builds what it needs from these idioms. Match them when adding coverage:

- **Config:** `Config.model_validate({...})` from an inline dict (see `tests/test_mail_job.py:_config`), never a YAML file on disk.
- **Fake destination client:** a plain class exposing just the methods under exercise (`get`/`post`/`patch`/`paginate`) that records calls and returns canned `{"id": ...}` dicts, passed where a `GraphClient` is expected with `# type: ignore[arg-type]`. Failure injection is by call ordinal (`fail_posts={2}`) so a test can fail the *second* import and assert the first stayed `done`.
- **Real `GraphClient`, fake transport:** construct with a stub token provider, then swap `gc._client` for `httpx.Client(transport=httpx.MockTransport(handler))` to assert on headers, URLs, and pagination; `GraphClient.__new__(GraphClient)` skips `__init__` when only one method is under test. Monkeypatch `tenacity.nap.time.sleep` to skip retry backoff.
- **State:** `init_db(tmp_path / "state.db")`, then assert through `session_scope()` and the `state/db.py` helpers. `init_db` assigns **module-level** `_engine`/`_SessionFactory`, so it is a process-wide switch: re-calling it repoints every later `session_scope()`, and deliberately *not* calling it again is how a test simulates a re-run against existing state (`fresh_db=False` in `test_mail_job.py`).
- **Job loops:** subclass `BaseSource`, set `capabilities`, implement only the `iter_*` method under test (recording the `since` it was handed), and hand-build a `JobContext`. That is the entire harness for an end-to-end workload run — no orchestrator, no threads.

## Known Gaps / TODO

The backlog lives in `TODO.md` — open items ordered by data-fidelity impact, each stating the change it adds, plus a "Recently completed" log. Treat `TODO.md` as the single source of truth (this section previously duplicated it and the two drifted). When you complete or discover a backlog item, update `TODO.md` in the same change.
