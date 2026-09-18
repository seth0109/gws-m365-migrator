# gws-m365-migrator

A Python CLI (`migrator`) that migrates mail, files, contacts, and calendars into **Microsoft 365** from one of three sources. Built for small tenants (under ~50 users) doing a big-bang cutover followed by a delta sync.

| Source (`source.type`) | Workloads |
|---|---|
| `google_workspace` | Gmail, Drive → OneDrive, Contacts, Calendar, Shared Drives → SharePoint |
| `imap` | Mail only, from any IMAP server |
| `microsoft365` | Tenant-to-tenant: mail, OneDrive, contacts, calendar, SharePoint sites |

Every run is **resumable and idempotent**: each migrated item is recorded in a local SQLite state file, so re-running the same command skips finished items and retries failed ones without creating duplicates.

> **Sources are read-only.** Google is accessed with `.readonly` scopes only, IMAP folders are opened read-only, and the source M365 tenant is only ever read.

---

## Quick start

```bash
git clone <repo-url> && cd gws-m365-migrator
python -m venv .venv && source .venv/bin/activate      # PowerShell: .venv\Scripts\Activate.ps1
pip install -e .

mkdir credentials                                      # drop the destination app's key PEM here
migrator init-config -t <google_workspace|imap|microsoft365> ... -m users.csv   # see Configure
migrator smoke-test                                    # both sides reachable?
migrator whatif                                        # inventory CSV, no writes
migrator run-all                                       # the migration
#   ... cutover ...
migrator delta                                         # changes since the run
migrator validate                                      # HTML status report
```

Every command reads `config.yaml` from the current directory (`--config/-c` to override) and writes its state files there too.

---

## Install

Requires **Python 3.11+**.

```bash
pip install -e .          # runtime only
pip install -e ".[dev]"   # + pytest, ruff, mypy for development
```

The package is built with `hatchling`; `pip install build && python -m build` produces a wheel in `dist/` if you need to ship it.

---

## Prerequisites

### Destination: a Microsoft Entra app registration

Create an app registration in the **destination** tenant, upload a certificate, and grant these **application** permissions with admin consent. Only grant what your source path needs:

| Permission | Needed for |
|---|---|
| `User.Read.All` | Every path (resolves each `dest_id` to a mailbox) |
| `Mail.ReadWrite` | `mail` |
| `Contacts.ReadWrite` | `contacts` |
| `Calendars.ReadWrite` | `calendar` |
| `Files.ReadWrite.All` | `files` (OneDrive) |
| `Sites.ReadWrite.All` + `Group.ReadWrite.All` | `shared-drives` / `sharepoint` (site provisioning) |

**Certificate.** The tool authenticates with a PEM **private key** plus the certificate's SHA-1 thumbprint. One way to make the pair:

```bash
openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
  -subj "/CN=gws-m365-migrator" -keyout credentials/ms-key.pem -out ms-cert.crt
openssl x509 -in ms-cert.crt -noout -fingerprint -sha1     # thumbprint, drop the colons
```

Upload `ms-cert.crt` to the app registration (Certificates & secrets) and keep `credentials/ms-key.pem` local; `config.yaml` points `certificate_path` at the key file. A `client_secret` is accepted instead, but only by hand-editing the config.

### Source access

- **imap** — the server hostname/port and a username + password (or app password) for each mailbox. Nothing to register.
- **google_workspace** — a service account with domain-wide delegation granted the four `.readonly` scopes (`gmail`, `drive`, `contacts`, `calendar`), its JSON key in `credentials/`, and a Workspace admin address to impersonate.
- **microsoft365** — a second app registration in the **source** tenant with the read counterparts of the permissions above (`Mail.Read`, `Contacts.Read`, `Calendars.Read`, `Files.Read.All`, `Sites.Read.All`, `User.Read.All`) and its own key PEM.

---

## Configure

`migrator init-config` writes a validated `config.yaml`. It reads the user mapping from a CSV, finds the key PEM (and, for Google, the service-account JSON) in `--credentials-dir` (default `credentials/`), and fills in sensible defaults. It refuses to overwrite an existing file unless you pass `--force`.

**users.csv** — one row per mailbox (start from `users.example.csv`). `source_id` is the identity at the source, `dest_id` the destination UPN. The two IMAP columns are only read for an `imap` source, and a blank `imap_user` means "same as `source_id`":

```csv
source_id,dest_id,imap_user,imap_password_env
alice@old.example,alice@contoso.onmicrosoft.com,alice@old.example,ALICE_IMAP_PW
bob@old.example,bob@contoso.onmicrosoft.com,bob,BOB_IMAP_PW
```

```bash
# IMAP
migrator init-config -t imap --imap-host imap.old.example \
  --tenant-id <dest-tenant-id> --client-id <dest-app-id> --thumbprint <sha1> -m users.csv

# Google Workspace
migrator init-config -t google_workspace --admin-email admin@old.example \
  --tenant-id <dest-tenant-id> --client-id <dest-app-id> --thumbprint <sha1> -m users.csv

# Microsoft 365 → Microsoft 365 (two PEMs in credentials/, named source*.pem and dest*.pem)
migrator init-config -t microsoft365 \
  --source-tenant-id <src-tenant-id> --source-client-id <src-app-id> --source-thumbprint <sha1> \
  --tenant-id <dest-tenant-id> --client-id <dest-app-id> --thumbprint <sha1> -m users.csv
```

If discovery is ambiguous, point at files explicitly with `--cert`, `--source-cert`, or `--service-account-key`. Prefer hand-editing? `cp config.example.yaml config.yaml`; it documents every field and all three source blocks.

Settings worth a look after scaffolding:

- `workloads.<name>.enabled` / `concurrency` — turn workloads off or tune parallelism (default 2–4 per workload).
- `workloads.mail.import_mode` — `json` (default) lands mail as normal, non-draft messages with the original dates; `mime` is byte-perfect but Graph imports it as drafts dated today.
- `rate_limits.*` — lower these if the destination throttles you.
- `destination.sharepoint_site_owner` — set it before `shared-drives` or `sharepoint`; app-only groups without an owner may never get a site.

---

## IMAP → Microsoft 365 walkthrough

The IMAP path migrates **mail only**. Folder structure, read state, and follow-up flags come across; contacts and calendars do not exist on an IMAP server.

**1. Collect what you need.** The IMAP host and port (993/SSL is the default), a login and password per mailbox, and the destination app (`User.Read.All` + `Mail.ReadWrite`) with its key PEM in `credentials/`.

**2. Write `users.csv`** as shown above. `imap_user` is the IMAP login; leave it blank if it equals `source_id`. `imap_password_env` names an **environment variable** that holds the password, so no secret is written to disk. (An inline `imap_password:` field exists in the YAML for throwaway test accounts only.)

**3. Scaffold and review the config.**

```bash
migrator init-config -t imap --imap-host imap.old.example \
  --tenant-id <dest-tenant-id> --client-id <dest-app-id> --thumbprint <sha1> -m users.csv
```

Add `--imap-port 143 --no-imap-ssl` for a plain-text server. To skip server-side virtual folders, add `exclude_folders: ["Public Folders"]` under `source:` (exact folder names as the server lists them).

**4. Export the passwords** in the shell that will run the migration:

```bash
export ALICE_IMAP_PW='...' BOB_IMAP_PW='...'            # bash
$env:ALICE_IMAP_PW = '...'; $env:BOB_IMAP_PW = '...'    # PowerShell
```

**5. Prove both ends work.** Logs into the first user's IMAP account and lists its folders, then creates and deletes a test folder in that user's destination mailbox:

```bash
migrator smoke-test
```

**6. Inventory.** Connects to IMAP only and writes one CSV row per message (folder, subject, size, date, sender):

```bash
migrator whatif --output whatif_manifest.csv
migrator whatif -u alice@old.example        # one mailbox
```

**7. Migrate.** `run-all` and `mail` are equivalent for an IMAP source; every other workload is skipped as unsupported.

```bash
migrator mail                                # all users
migrator mail -u alice@old.example           # one user (an unknown id exits 1, it never widens to everyone)
```

Where mail lands: folders flagged `\Sent`, `\Drafts`, `\Trash`, `\Junk` (or named like them: `Sent`, `Sent Items`, `Spam`, `Deleted Items`, …) map to the real Outlook Sent Items, Drafts, Deleted Items, and Junk Email. `INBOX` maps to Inbox. Every other folder is recreated with its hierarchy. Unread and flagged messages stay unread and flagged.

**8. After cutover, sync new mail.** The first run stored each folder's `UIDVALIDITY`/`UIDNEXT`; `delta` fetches only messages that arrived since, per folder. It refuses to run for a user without a stored cursor, so always do the full pass first.

```bash
migrator delta
```

**9. Check the outcome.** Every migration command exits **1** if any item or user failed; the details are in `migrator.log` and the report:

```bash
migrator validate --output migration_report.html
```

Re-run the same command to retry failures. Finished messages are never re-imported.

**Known IMAP limits** (see `TODO.md`): folder names are decoded as UTF-8 rather than IMAP modified UTF-7, so non-ASCII folder names may be mangled, and `delta` picks up new messages only; later flag changes or moves are not applied.

---

## Commands

| Command | What it does |
|---|---|
| `init-config` | Scaffold `config.yaml` from CLI flags, `credentials/`, and a mapping CSV |
| `smoke-test` | Probe the source for the first user; create + delete a test folder in their destination mailbox |
| `whatif [-o file.csv]` | Inventory everything that would migrate; source reads only, no destination calls |
| `contacts` / `calendar` / `files` / `mail` | Migrate one workload |
| `run-all` | All enabled, source-supported workloads in order: contacts → calendar → files → mail |
| `shared-drives [--delta]` | Google Shared Drives → auto-provisioned SharePoint sites (`google_workspace` only) |
| `sharepoint [--delta]` | SharePoint document libraries tenant-to-tenant (`microsoft365` only) |
| `delta` | Post-cutover sync using the cursors stored by the full pass |
| `validate [--output file.html]` | HTML report of done / failed / skipped counts per user and workload, from local state |

Per-user commands take `--user/-u <source_id>`; `--user` that matches no configured user is an error. `migrator <command> --help` lists every option.

### Behaviour to know

- **Exit codes.** Migration commands exit 1 when the run left anything behind: a failed user run or any item that ended `failed`. Safe for scripted cutovers.
- **Retries.** Re-run the same command. Done items are skipped, failed items retried, and a workload's sync cursor is only advanced by a run with zero failures, so nothing falls behind the delta.
- **Delta semantics.** Files, contacts, and calendar events changed at the source are updated in place; deleted contacts and cancelled events are removed. Mail delta adds new messages only.
- **State files** are written to the working directory and are gitignored: `migration_state.db` (id maps, folder maps, cursors, job log; **never delete it between runs**), `migrator.log`, the token caches, the whatif CSV, and the HTML report. The CSV and report contain real subjects, file names, and addresses.

---

## Google Workspace and Microsoft 365 notes

- **Google Shared Drives.** List each drive in `shared_drives[]` with a `target_site_alias`; `migrator shared-drives` provisions a private team site per drive (reused on re-runs) and copies the library. Native Docs/Sheets/Slides are exported to Office formats; Forms and Apps Script are skipped and listed in the whatif CSV.
- **Gmail labels.** A message's first user label becomes its folder and the rest become Outlook categories (`multi_label_policy: categories`), or `duplicate` copies it into every label's folder. Starred → follow-up flag; Important → an "Important" category. Spam/Trash are migrated to Junk Email / Deleted Items unless `include_spam_trash: false`.
- **Tenant-to-tenant SharePoint.** Each `sharepoint_sites[]` entry names a `source_site` and either an existing `dest_site` or a `target_site_alias` to provision.
- **Calendar caution.** Verify on one pilot mailbox whether creating events with attendees sends invitations in your tenant before running `calendar` at scale (tracked in `TODO.md`).

---

## Development

```bash
pip install -e ".[dev]"
pytest                # 200+ tests, no network
ruff check src/       # lint (E, F, I, UP); line length 100
mypy src/             # strict
```

`ruff` and `mypy` carry a small documented baseline of pre-existing findings in `google/*` and the thin Graph writers; see `CLAUDE.md` for the counts and the architecture deep-dive (connectors, rate limiting, idempotency, threading). `TODO.md` is the backlog.

```
src/migrator/
├── cli.py            # typer commands
├── configgen.py      # init-config scaffolding
├── config.py         # pydantic config models
├── orchestrator.py   # per-user thread pools, client wiring
├── connectors/       # base items + google / imap / m365 sources
├── google/           # read-only Google API wrappers
├── microsoft/        # Graph client, destination writers, SharePoint provisioning
├── transform/        # label/folder, recurrence, path, identity mapping
├── state/            # SQLAlchemy models + session helpers
├── workloads/        # contacts / calendar / files / mail jobs
├── whatif.py         # manifest writer
└── reporting.py      # validation report
```
