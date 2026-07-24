# TODO / Backlog

Open work items identified during review, roughly ordered by data-fidelity
impact. None of the **Open** items are wired up yet — treat as the backlog.
This file is the single source of truth for the backlog (`CLAUDE.md` points here).

## Recently completed (2026-07-24 backlog pass)

- **Gmail STARRED → Outlook follow-up flag, IMPORTANT → "Important" category**
  (google connector sets them; both import modes and the whatif notes carry
  them). IMAP/M365 sources already mapped their flag equivalents.
- **Gmail Spam/Trash enumerated** (`workloads.mail.include_spam_trash`, default
  true; they route to JunkEmail / DeletedItems).
- **Contact photos migrate** — lazy `photo_ref`/`fetch_contact_photo` on the
  connectors (Google People photo URL, skipping generated avatars; M365
  `photo/$value`), uploaded best-effort after create. `ensure_contact_folder`
  also gained the pagination + 409 recovery the mail/drive writers have.
- **Recurring-event exceptions reconciled (Google source)** — modified /
  cancelled single occurrences are deferred until the series master exists,
  matched via `/events/{master}/instances` around `originalStartTime`, then
  PATCHed or DELETEd. Idempotent via ItemMap; failures hold the cursor.
- **paths.py sanitization fixed** — extensionless reserved names (rpartition
  bug), COM0/LPT0, desktop.ini, `_vti_`.
- **mail_job end-to-end test harness** (fake source + fake Graph client):
  idempotent reruns, done-before-extras duplicate prevention, failure-gated
  cursors, unseeded-delta refusal.

## Recently completed (2026-07-24 review + fix pass)

- **Mail no longer imports as drafts dated "now".** Default
  `workloads.mail.import_mode: json` creates messages via the JSON API with
  create-time MAPI extended properties (`PidTagMessageFlags`,
  `PidTagClientSubmitTime`/`PidTagMessageDeliveryTime`,
  `PidTagTransportMessageHeaders`) — non-draft, original dates, read state and
  categories on create. `mime` mode (byte-perfect, draft-flagged) remains
  available. *Note: messages migrated before this change are drafts and cannot
  be fixed in place — Graph cannot clear the unsent flag post-create; they need
  delete + re-import.*
- **Files writer conforms to Graph docs.** `upload_small_file` uses PUT (POST
  is 405); `ensure_folder` paginates `/children` and matches client-side
  (the endpoint does not support `$filter`) with 409 re-resolution.
- **Upload-session PUTs no longer send Authorization** (docs warn it can 401);
  they also no longer inherit the JSON content-type default.
- **SQLite configured for the threaded pools** (`check_same_thread=False`,
  30 s busy timeout, WAL).
- **Rerun-duplicate hazard in mail_job removed** — items are marked done
  immediately after the primary import; extra folder copies / mime-mode flag
  patches are best-effort and never flip a done item back to failed.
- **Cursors are failure-safe** — no job advances its sync cursor when the run
  had failed items (they'd fall behind the cursor and be lost to every future
  delta), and an unseeded `delta` refuses instead of silently full-scanning.
- **SharePoint provisioning hardened** — groups created Private, with sanitized
  `mailNickname` and `owners@odata.bind` from `destination.sharepoint_site_owner`
  (app-only groups without owners may never provision their site); the site
  poll re-raises permanent errors instead of sleeping through them.
- **Recurrence mapping fixed** — BYMONTHDAY lists/negatives no longer crash the
  calendar run (calendar_job also isolates enumeration failures per calendar);
  bare `FREQ=MONTHLY`/`YEARLY` derive day/month from DTSTART; `BYSETPOS` and
  `WKST`/`firstDayOfWeek` honored; `relativeYearly` emitted.
- **Contacts delta actually works now** — `requestSyncToken=True` is sent (the
  People API never returns `nextSyncToken` without it) and an expired token
  (410) re-baselines with a full sync. Gmail delta falls back to a full sync on
  an expired historyId (404). M365 drive delta skips `deleted`-facet tombstones.
- **CLI exits non-zero on failure** — any failed user-run or item failure during
  the run exits 1.
- **Calendar attendees/organizer identity remap** (`IdentityMap.remap_event`).
- **`validate` report includes namespaced workloads** (`shared_drive:*` /
  `sharepoint_site:*` discovered from `ItemMap`).

---

## Open — Correctness / data fidelity

- **Timestamp-based delta for mail/contacts/calendar (M365 source) is lossy.**
  Only files use real Graph delta tokens; the M365 connector sets the cursor to
  "now" and filters on `receivedDateTime`/`lastModifiedDateTime ge since`,
  missing folder moves and flag changes, and clock-skew sensitive.
  **Change adds:** proper Graph delta queries (`/messages/delta`,
  `/contacts/delta`, `/events/delta`).

- **JSON mail import re-encodes the body.** The default `import_mode: json`
  parses MIME and rebuilds body/attachments — not byte-perfect (S/MIME,
  unusual multipart structures). `mime` mode preserves bytes but creates
  drafts.
  **Change adds:** evaluate Microsoft's mailbox import/export API (beta) as the
  long-term high-fidelity path.

- **IMAP folder names decode wrong.** Names are decoded as UTF-8 instead of
  RFC 3501 modified UTF-7 (non-ASCII folders mangled and won't round-trip into
  SELECT/STATUS), and LIST responses returned as literals (tuples) are skipped
  entirely — those folders are never migrated.
  **Change adds:** modified-UTF-7 decode + literal LIST handling.

- **Recurring-event exceptions (M365 source only now).** The Google source
  reconciles them; the M365 connector's `/calendars/{id}/events` never
  enumerates exceptions at all.
  **Change adds:** enumerate exceptions (e.g. via `/events/{id}/instances` or
  calendarView) and route them through the same `_apply_exception` path.

- **No calendar attachments.** `create_event` posts only the body.
  **Change adds:** event attachment migration via the attachment APIs.

- **`ensure_calendar` neither paginates nor recovers from a duplicate.** Unlike
  `ensure_mail_folder` / `ensure_contact_folder` / drive `ensure_folder`, it does a
  single un-paginated `GET /users/{id}/calendars` and matches on name. A calendar
  past the first page is missed, and since Graph permits duplicate calendar names
  the create silently succeeds — the pass then writes every event into a second
  calendar of the same name (and a later run can add another).
  **Change adds:** the paginate + re-resolve pattern the other folder writers use.

## Open — Coverage / functionality

- **No permissions/sharing migration.** `SourceFile` carries no ACL data.
  **Change adds:** a permissions model on `SourceFile` plus a destination
  writer that recreates sharing.

- **`validate` report can't detect data loss.** Counts local `ItemMap` only.
  **Change adds:** source↔destination reconciliation (counts/checksums).

- **M365 hidden-folder mail lands in Inbox.** `_folder_paths` doesn't pass
  `includeHiddenFolders=true`, so messages in hidden folders fall back to
  Inbox. **Change adds:** include hidden folders in the folder map.

- **Thin pre-flight checks.** `smoke-test` probes one pilot user only.
  **Change adds:** bulk validation that every `dest_id` mailbox exists, is
  licensed, and is provisioned before a `run-all`.

## Open — Reliability / scale

- **Files are fully buffered in memory.** `fetch_file` returns full `bytes`.
  **Change adds:** streaming download→upload.

- **Tenant-level SharePoint/Shared-Drive flows are single-threaded.**
  **Change adds:** a `ThreadPoolExecutor` over drives/files.

- **Token bucket bursts under contention.** `TokenBucket.acquire` clamps
  tokens at 0, so N waiters on an empty bucket sleep the same duration and
  fire simultaneously, exceeding the configured rate.
  **Change adds:** allow token debt so waiters serialize.

- **Whatif is not fully read-only on state.** M365 `inventory_files` stamps the
  live files delta cursor (via `iter_files`), and whatif runs write `JobRun`
  rows. **Change adds:** inventory paths that don't capture cursors; skip
  JobRun in whatif.

- **Google Drive full vs delta corpora mismatch.** Full pass lists
  `corpora="user"`; the Changes delta uses `includeItemsFromAllDrives=True`,
  surfacing shared-drive changes the baseline never enumerated.
  **Change adds:** align the two scopes.

- **Remaining expired-cursor paths.** Google Calendar syncToken 410 and Graph
  drive-delta 410 still raise instead of re-baselining (People + Gmail now
  handled). **Change adds:** the same catch-and-full-resync fallback.

- **MSAL token-cache writes are not process-safe.** Plain `write_text` with no
  file lock; two concurrent `migrator` processes can corrupt the cache.
  **Change adds:** atomic temp-file + rename write.

- **Large-file re-upload can duplicate on crash.** `upload_large_file` uses
  `conflictBehavior: rename`; a crash between upload-complete and the state
  write re-uploads as "name (1)" on rerun. (Kept `rename` deliberately —
  `replace` would silently clobber sanitized-name collisions.)
  **Change adds:** pre-upload existence check by name in the target folder.

## Open — Hygiene

- **Config models silently ignore unknown YAML keys** (pydantic default) — a
  typo like `concurency:` does nothing. **Change adds:** `extra="forbid"`.
- **`imap_password_env` pointing at an unset env var yields `None`** and fails
  later as an opaque IMAP auth error. **Change adds:** fail-fast validation.
- **`reporting.py` interpolates unescaped strings into HTML.**
  **Change adds:** `html.escape` on user/error fields.
- **`JobRun.finished_at` / counts never populated.**
- **Baseline lint/type debt:** ruff 10 (E501s), mypy 21 (untyped Google client
  returns, `Returning Any` in thin writers) — pre-existing, concentrated in
  `google/*` and the thin Graph writers. Verified 2026-07-24.

## Open — Testing

- **Extend the job-loop harness beyond mail.** `test_mail_job.py` covers
  run_mail end-to-end (idempotency, cursor gating, duplicate prevention) and
  `test_calendar_exceptions.py` covers `_apply_exception`; the contacts /
  calendar-main / files job loops still lack equivalent end-to-end runs.
  **Change adds:** the same fake-source + fake-GraphClient pattern for the
  remaining three loops.
