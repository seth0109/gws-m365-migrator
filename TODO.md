# TODO / Backlog

Open work items identified during review, roughly ordered by data-fidelity
impact. None of the **Open** items are wired up yet — treat as the backlog.
This file is the single source of truth for the backlog (`CLAUDE.md` points here).

## Recently completed (2026-09-17 mechanism review)

- **`--user <unmatched>` no longer runs every user.** The CLI exits 1 when the
  filter matches nothing; `Orchestrator.run_workload` treats an explicit list
  (even empty) as-is and only `None` as "all users".
- **Files land in the right folder regardless of listing order.**
  `files_job` resolves parents through a FolderMap-backed `_FolderIndex` and
  defers children whose folder has not been seen yet (Drive `files.list` and
  Graph delta feeds do not order parents first; a delta pass re-emits only the
  changed child). Orphans with a parent outside the corpus go to the root with
  a warning; children of a folder that failed to create fail with a clear
  reason instead of falling through to the root.
- **CLI exit code sees re-run failures.** `upsert_item`/`save_cursor` stamp
  `updated_at` in their ON CONFLICT set (SQLAlchemy does not apply
  `onupdate` there) and `count_failed_items_since` compares in the stored
  string format (same-second failures were sorting *before* the run start).
- **IMAP delta no longer drops mail after expunges** — the search is
  `UID SEARCH UID <uidnext>:*`; a bare set is *sequence numbers* (RFC 3501
  §6.4.8). FLAGS are parsed from every FETCH response part (request-order
  servers put them after the literal), and a per-message FETCH failure becomes
  a `fetch_error` item rather than aborting the folder.
- **A single unreadable message no longer aborts the mailbox run.** Every mail
  connector yields a `SourceMessage.fetch_error` stub for a failed raw fetch
  (Gmail/M365 skip a 404 = deleted mid-run); `mail_job` fails just that item
  and holds the cursor. Google `execute()` calls carry `num_retries`
  (`google.NUM_RETRIES`) for 5xx/429/socket errors.
- **Attached emails survive the JSON import.** `message/rfc822` parts are
  carried as `.eml` file attachments instead of having their body spliced into
  the outer message; the large-MIME pruner extracts them whole too.
- **Delta passes apply modifications.** `SourceContact`/`SourceEvent` carry a
  `source_hash` (People/Calendar etag, Graph changeKey); a delta pass PATCHes
  an already-migrated item whose hash changed and skips unchanged ones. A
  recorded `dest_id` means "exists at the destination" even after a failed
  update, so retries PATCH rather than duplicate. People tombstones
  (`metadata.deleted`) delete the migrated contact instead of creating a blank
  one; cancelled events are deleted in any mode.
- **M365 source reads contact sub-folders** (`contactFolders` walked
  recursively, flattened to the folder display name); `/users/{id}/contacts`
  alone is only the default folder.
- **SharePoint provisioning is resumable** — the group id is recorded
  (`FolderMap` workload `sharepoint_group`) before polling, so a timeout
  re-run resumes the poll instead of POSTing a duplicate `mailNickname`.
- **429 handling** sleeps Retry-After exactly once via tenacity's wait (no extra
  exponential backoff) and no longer tears down the connection pool.
- **Job-loop harnesses** now cover files, contacts and calendar
  (`test_files_job.py`, `test_contacts_job.py`, `test_calendar_job.py`) plus
  `test_imap.py`, `test_gmail.py`, `test_m365_source.py`, `test_state.py`,
  `test_cli.py`.

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

- **Verify on the pilot: creating events with attendees may send invitations.**
  Graph sends meeting requests when an event is created with attendees (the
  mailbox owner becomes organizer) and cancellations on DELETE of a meeting;
  `_map_event` and the M365 `_EVENT_FIELDS` both carry attendees, and the
  identity remap makes those addresses live. Events where the user was only an
  attendee are recreated with the user as organizer. Not verifiable without a
  tenant — migrate one meeting into a pilot mailbox and check attendee inboxes
  before any calendar run. **Change adds (if confirmed):** a
  `workloads.calendar` option to strip attendees into the body text.

- **`multi_label_policy: categories` still duplicates.** `resolve_label_placement`
  returns the system folders *plus* the primary user-label folder, so an Inbox
  message with one user label gets two copies under the policy documented as
  "one primary folder". Decide whether Inbox-with-category or label-folder-only
  is the intended single placement. Whatif notes ("folders: Inbox+1 more") make
  the current behaviour visible.

- **Mail delta adds new messages only.** Read/flag/label changes to an
  already-migrated message are never applied (Gmail history is enumerated for
  `messageAdded`; M365 filters on `receivedDateTime`). Contacts/calendar/files
  now update in place; mail does not.

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

- **M365 contact sub-folder hierarchy is flattened.** Sub-folder contacts now
  migrate, but into a top-level destination folder named after the source
  folder; nested `contactFolders` are not recreated as nested.
  **Change adds:** nested `childFolders` creation in `ensure_contact_folder`.

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
- **Baseline lint/type debt:** ruff 10 (8 E501 + 2 UP028), mypy 20 (untyped Google
  client returns, `Returning Any` in thin writers) — pre-existing, concentrated in
  `google/*` and the thin Graph writers. Verified 2026-09-17.

## Open — Testing

- **Shared-drive / SharePoint flows lack a placement test.** `_migrate` is
  covered through `run_files`; the two tenant-level flows only have cursor
  tests. **Change adds:** a `run_shared_drives` / `run_sharepoint_sites` pass
  through the `test_files_job.py` fake drive client.
