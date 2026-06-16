# m365-migrator

A Python CLI tool that migrates email, files, contacts, and calendars into **Microsoft 365** from multiple sources, for small tenants (<50 users):

- **Google Workspace** → M365 (Gmail, Drive, Contacts, Calendar; plus Shared Drives → SharePoint)
- **IMAP / generic SMTP mail servers** → M365 (mail)
- **Microsoft 365 → Microsoft 365** tenant-to-tenant (mail, OneDrive, SharePoint sites, contacts, calendar)

Designed for a big-bang weekend cutover with a post-cutover delta sync. The pipeline is **resumable**, **idempotent**, and **re-runnable** — every item records a source→destination mapping in a local SQLite state store, so an interrupted run picks back up exactly where it stopped.

> **Safety invariant:** sources are read-only. The tool never writes back to the source (Google scopes are `.readonly`, IMAP is read-only, the M365 source tenant is only read).

---

## Architecture

```
   Source connector (read-only)          Microsoft Graph (write)
   Google / IMAP / M365-tenant   ──►      Mail / Files / Contacts / Cal
            │                                       ▲
            └────────► Transform / normalize ───────┘
                            │
                  SQLite state store
              (id maps · cursors · job log)
```

- **Source connectors** (`src/migrator/connectors/`) abstract the source behind per-workload methods that emit destination-ready items (raw MIME for mail, Graph-shaped bodies for contacts/events). Pick one via the `source.type` config: `google_workspace`, `imap`, or `microsoft365`.
- **Destination** is always Microsoft Graph (`httpx` + tenacity retries + a three-layer rate limiter). Personal files go to OneDrive; Google Shared Drives auto-provision SharePoint sites.
- **Persist** every item's source-ID → destination-ID mapping plus per-workload sync cursors so the delta pass is cheap.

Each workload (contacts, calendar, files, mail) runs in its own thread pool with isolated state and configurable concurrency. Commands skip workloads the configured source doesn't support (e.g. an IMAP source only does `mail`).

---

## Prerequisites

- Python 3.11+
- A **destination Microsoft Entra app registration** with Graph application permissions for Mail, Files, Contacts, and Calendars — certificate auth preferred over client secrets. For `shared-drives` (SharePoint auto-provisioning) also grant `Group.ReadWrite.All` + `Sites.ReadWrite.All`.
- Source-specific access, depending on `source.type`:
  - **google_workspace** — a service account with domain-wide delegation and the four `.readonly` scopes (gmail, drive, contacts, calendar)
  - **imap** — per-user IMAP credentials (username + password/app-password)
  - **microsoft365** — a Graph app registration in the **source** tenant with read permissions for the workloads being migrated
- Admin consent granted on every side in use.

---

## Setup

```bash
# 1. Clone and enter the project
cd m365-migrator

# 2. Install in editable mode with dev extras
pip install -e ".[dev]"

# 3. Copy and edit the example config
cp config.example.yaml config.yaml
```

Place credentials under `credentials/` (e.g. `google-service-account.json`, `ms-cert.pem`).

Edit `config.yaml` (see `config.example.yaml` for all three source blocks):

- `source:` — pick **one** `type` (`google_workspace` | `imap` | `microsoft365`) and fill its fields
- `destination:` — `tenant_id` / `client_id` / cert thumbprint (or `client_secret`) from the destination Entra app
- `users[]` — one entry per user, mapping `source_id` → `dest_id`. For an `imap` source also set `imap_user` + `imap_password_env` (the env var holding the password)
- `shared_drives[]` — (google_workspace only) Shared Drives to move to SharePoint, each with a `target_site_alias`
- `workloads.*.concurrency` — tune per workload (defaults are sensible)
- `rate_limits.*` — adjust if you hit throttling

---

## Usage

All commands take `--config / -c` (default `config.yaml`) and accept `--user / -u <source_id>` to scope a run to a single user. Per-workload commands and `run-all`/`delta`/`whatif` automatically skip workloads the configured source can't do (an `imap` source supports only `mail`).

### Dry-run inventory (whatif)

```bash
migrator whatif --config config.yaml --output whatif_manifest.csv
```

Enumerates every item that *would* be migrated for each configured user (across the supported workloads) and writes a single CSV manifest. Does **not** connect to the destination — useful for pre-cutover sizing, change-management approval, and verifying user mappings. Add `--user alice@yourdomain.com` to scope to one user.

CSV columns: `timestamp, source_user, dest_user, workload, source_id, source_path, name, size_bytes, mime_type, modified_time, action, notes`.

### Validate access (run this first)

```bash
migrator smoke-test --config config.yaml
```

Probes the configured source (Gmail labels / IMAP login / source-tenant Graph read) for the first user, then creates and deletes a test folder in their destination mailbox. Confirms both sides are wired up before you touch real data.

### Migrate one workload

```bash
migrator contacts --config config.yaml
migrator calendar --config config.yaml
migrator files    --config config.yaml   # personal files → OneDrive
migrator mail     --config config.yaml
```

### Shared Drives → SharePoint (google_workspace source)

```bash
migrator shared-drives --config config.yaml
```

For each entry in `shared_drives[]`, auto-provisions a connected SharePoint site (idempotent — reused on re-runs) and copies the Drive's contents into its document library.

### SharePoint site → site (microsoft365 source)

```bash
migrator sharepoint --config config.yaml
```

For each entry in `sharepoint_sites[]`, copies a source-tenant SharePoint document library into the destination tenant — either an existing `dest_site` or an auto-provisioned `target_site_alias`.

### Migrate everything

```bash
migrator run-all --config config.yaml
```

Runs all *enabled and source-supported* workloads sequentially: contacts → calendar → files → mail.

### Delta sync (post-cutover)

```bash
migrator delta --config config.yaml
```

Uses the sync cursors stored during the initial pass to pull only changes since the cutover.

### Validation report

```bash
migrator validate --config config.yaml --output report.html
```

Generates an HTML report comparing source vs destination counts per user/workload.

### Help

```bash
migrator --help
migrator <command> --help
```

---

## Resumability

If a run is interrupted (crash, throttling, network blip), simply re-run the same command. Every item is gated by an `is_done()` check against the `ItemMap` table, so completed items are skipped and the job resumes at the first pending item.

State lives in the SQLite file named by `state_db` (default `migration_state.db`). **Do not delete it between runs** — it holds id mappings, folder maps, sync cursors, and the job audit log.

---

## Development

```bash
pytest                # run all tests
ruff check src/       # lint (E, F, I, UP rules)
ruff check --fix src/ # auto-fix
mypy src/             # strict type checking
```

Line length is 100. Source root is `src/`.

---

## Project layout

```
m365-migrator/
├── pyproject.toml
├── config.example.yaml
├── src/migrator/
│   ├── cli.py              # typer entrypoints
│   ├── config.py           # pydantic config models (source/destination)
│   ├── context.py          # JobContext (user + source + dest passed to jobs)
│   ├── orchestrator.py     # thread-pool dispatch, source/dest client wiring
│   ├── ratelimit.py        # token-bucket rate limiters
│   ├── reporting.py        # validation report
│   ├── connectors/         # source connectors: base, google, imap, m365, factory
│   ├── auth/               # google + ms auth (MSTokenProvider used for both tenants)
│   ├── google/             # read-only google API wrappers
│   ├── microsoft/          # graph client + destination writers + sharepoint provisioning
│   ├── transform/          # data-model mapping
│   ├── state/              # sqlalchemy models + session
│   └── workloads/          # contacts / calendar / files / mail jobs (source-agnostic)
└── tests/
```

See `CLAUDE.md` for architectural deep-dives (rate limiting, idempotency, threading, job dispatch).
