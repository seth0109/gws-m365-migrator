# gws-m365-migrator

A Python CLI tool that migrates email, files, contacts, and calendars from **Google Workspace** to **Microsoft 365** for small tenants (<50 users).

Designed for a big-bang weekend cutover with a post-cutover delta sync. The pipeline is **resumable**, **idempotent**, and **re-runnable** — every item records a source→destination mapping in a local SQLite state store, so an interrupted run picks back up exactly where it stopped.

> **Safety invariant:** all Google scopes are `.readonly`. The tool never writes back to Google.

---

## Architecture

```
   Google APIs (read-only)          Microsoft Graph (write)
   Gmail / Drive / People / Cal     Mail / Files / Contacts / Cal
            │                                  ▲
            └──────► Transform layer ──────────┘
                            │
                  SQLite state store
              (id maps · cursors · job log)
```

- **Extract** from Google APIs via service account + domain-wide delegation.
- **Transform** Google data models to Graph equivalents (folder/label structure preserved).
- **Load** into Microsoft Graph via `httpx` with tenacity-backed retries and a three-layer rate limiter (global + per-mailbox).
- **Persist** every item's source-ID → destination-ID mapping plus per-workload sync cursors so the delta pass is cheap.

Each workload (contacts, calendar, files, mail) runs in its own thread pool with isolated state and configurable concurrency.

---

## Prerequisites

- Python 3.11+
- A **Google Workspace service account** with domain-wide delegation enabled and the four read-only scopes authorized (gmail, drive, contacts, calendar)
- A **Microsoft Entra app registration** in the target tenant with application permissions for Mail, Files, Contacts, and Calendars (Graph) — certificate auth preferred over client secrets
- Admin consent granted on both sides

See `project_plan.md` (Phase 0) for the full access-setup checklist.

---

## Setup

```bash
# 1. Clone and enter the project
cd gws-m365-migrator

# 2. Install in editable mode with dev extras
pip install -e ".[dev]"

# 3. Copy and edit the example config
cp config.example.yaml config.yaml
```

Place credentials under `credentials/`:

```
credentials/
├── google-service-account.json   # downloaded from GCP
└── ms-cert.pem                   # uploaded to the Entra app
```

Edit `config.yaml`:

- `google.admin_email` — a Workspace super-admin (used for impersonation)
- `microsoft.tenant_id` / `client_id` — from the Entra app registration
- `microsoft.certificate_thumbprint` — the thumbprint of the cert uploaded to Entra
- `users[]` — one entry per user, mapping `google_email` → `ms_upn`
- `workloads.*.concurrency` — tune per workload (defaults are sensible)
- `rate_limits.*` — adjust if you hit throttling

---

## Usage

All commands take `--config / -c` (default `config.yaml`) and accept `--user / -u <email>` to scope a run to a single user.

### Dry-run inventory (whatif)

```bash
migrator whatif --config config.yaml --output whatif_manifest.csv
```

Enumerates every contact, calendar event, Drive file, and Gmail message that *would* be migrated for each configured user, and writes a single CSV manifest. Does **not** connect to Microsoft Graph — useful for pre-cutover sizing, change-management approval, and verifying user mappings before any destination side is set up. Add `--user alice@yourdomain.com` to scope to one user.

CSV columns: `timestamp, user_email, ms_upn, workload, source_id, source_path, name, size_bytes, mime_type, modified_time, action, notes`.

### Validate access (run this first)

```bash
migrator smoke-test --config config.yaml
```

Reads Gmail labels for the first user in `config.yaml`, then creates and deletes a test folder in their Microsoft mailbox. Confirms both sides are wired up before you touch real data.

### Migrate one workload

```bash
migrator contacts --config config.yaml
migrator calendar --config config.yaml
migrator files    --config config.yaml
migrator mail     --config config.yaml
```

### Migrate everything

```bash
migrator run-all --config config.yaml
```

Runs all *enabled* workloads sequentially: contacts → calendar → files → mail.

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
gws-m365-migrator/
├── pyproject.toml
├── config.example.yaml
├── src/migrator/
│   ├── cli.py              # typer entrypoints
│   ├── config.py           # pydantic config models
│   ├── orchestrator.py     # thread-pool dispatch, config injection
│   ├── ratelimit.py        # token-bucket rate limiters
│   ├── reporting.py        # validation report
│   ├── auth/               # google + ms auth
│   ├── google/             # read-only google connectors
│   ├── microsoft/          # graph client + writers
│   ├── transform/          # data-model mapping
│   ├── state/              # sqlalchemy models + session
│   └── workloads/          # contacts / calendar / files / mail jobs
└── tests/
```

See `CLAUDE.md` for architectural deep-dives (rate limiting, idempotency, threading, job dispatch).
