# TODO / Backlog

Open work items identified during review, roughly ordered by data-fidelity
impact. None of the **Open** items are wired up yet — treat as the backlog.
Mirrors the "Known Gaps / TODO" section of `CLAUDE.md`.

## Recently completed

- **Calendar attendees/organizer identity remap.** `calendar_job.run_calendar`
  now applies `transform/identities.py:IdentityMap.remap_event()` (config-driven
  `source_id`→`dest_id`) to each event body before `create_event`, rewriting
  attendee + organizer addresses to destination UPNs so migrated events
  reference live mailboxes instead of dead source ones. *(The M365 source still
  doesn't carry `organizer` in `_EVENT_FIELDS` — Graph sets organizer to the
  calendar owner on create — so the organizer remap is defensive for now.)*
- **`validate` report includes namespaced workloads.** `reporting.py` no longer
  hardcodes `("contacts","calendar","files","mail")`; it discovers every
  `(user, workload)` pair from `ItemMap` so `shared_drive:*` / `sharepoint_site:*`
  results appear, unioned with the standard per-user workloads so a configured
  user with zero rows still surfaces.

---

## Open — Correctness / data fidelity

- **Timestamp-based delta for mail/contacts/calendar is lossy.** Only files use
  real Graph delta tokens; mail/contacts/calendar set the cursor to "now" and
  filter on `receivedDateTime`/`lastModifiedDateTime ge since`
  (`connectors/m365.py`). This misses folder moves and read/flag/category
  changes after cutover, and is clock-skew sensitive.
  **Change adds:** proper Graph delta queries (`/messages/delta`,
  `/contacts/delta`, `/events/delta`) so post-cutover delta syncs capture moves,
  flag/read/category changes, and deletions without clock-skew risk.

- **Recurring-event exceptions are lost.** Only the series master + plain
  instances are pulled; modified single occurrences of a recurring series aren't
  reconciled.
  **Change adds:** enumeration and re-application of per-occurrence exceptions so
  edited/cancelled instances of a recurring series survive migration.

- **No contact photos or calendar attachments.** `_CONTACT_FIELDS` omits the
  photo; `create_event` posts only the body, not event attachments.
  **Change adds:** contact photo fetch/upload (`GET/PUT …/photo/$value`) and
  event attachment migration so contacts and events keep their images/files.

## Open — Coverage / functionality

- **No permissions/sharing migration.** `SourceFile` carries no ACL data —
  Drive/SharePoint sharing, link permissions, and ownership are dropped.
  **Change adds:** a permissions model on `SourceFile` plus a destination writer
  that recreates sharing/permissions on migrated files.

- **`validate` report can't detect data loss.** `reporting.py` only counts local
  `ItemMap` statuses — it never reconciles source vs. destination item
  counts/checksums, so silently-skipped or never-enumerated items won't surface.
  **Change adds:** source↔destination reconciliation (counts/checksums) so items
  that were never enumerated or silently skipped are flagged.

- **Thin pre-flight checks.** `smoke-test` probes one pilot user only; there's no
  bulk validation that all `dest_id` mailboxes exist / are licensed /
  provisioned before a `run-all`.
  **Change adds:** a bulk pre-flight that verifies every `dest_id` mailbox
  exists, is licensed, and is provisioned before a full run.

## Open — Reliability / scale

- **Files are fully buffered in memory.** `fetch_file` returns full `bytes` and
  the upload writers take full `content: bytes` — a multi-GB file is loaded
  entirely into RAM on both download and upload.
  **Change adds:** streaming download→upload to cap memory use and lift the
  practical file-size ceiling.

- **Tenant-level SharePoint/Shared-Drive flows are single-threaded.**
  `run_shared_drives` / `run_sharepoint_sites` loop drives and files
  sequentially, unlike the per-user workloads.
  **Change adds:** a `ThreadPoolExecutor` over drives/files to parallelize large
  libraries.

## Open — Testing

- **Job/writer paths lack mocked-Graph coverage.** Tests are mostly pure-logic
  plus mail-writer and the new identity/reporting tests; the per-user job loops
  (contacts/calendar/files/mail) and the Graph writers have no fake-client
  integration tests.
  **Change adds:** a recording/fake `GraphClient` harness and end-to-end coverage
  of the job loops and writers.
