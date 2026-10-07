# phaze-ib3ub — a review-gated junk-companion deletion flow

- **Bead:** `phaze-ib3ub` (spike, molecule `phaze-gyj6i`), blocks the decision bead `phaze-5qymm`
- **Date:** 2026-10-07 (production read at `2026-10-07 01:52 UTC`)
- **Tree:** branch `wt/bead/issue/phaze-ib3ub`, forked off `main` at `e9c5ed86`
- **Production:** the deployed `ghcr.io/simplicityguy/phaze:2026.10.2` API on `host-prod`. Every
  query ran in the `phaze-api` container under `SET default_transaction_read_only = on` and
  `SET statement_timeout = '60s'`, `SELECT` only; the probe scripts were removed from the container
  and the host afterwards
- **Status:** evidence only. **No product code, test code or build config changed.**

______________________________________________________________________

## Question

The operator wants high-confidence junk companions flagged and listed for approval, with nothing
deleted until they approve it:

> **Operator decision 2026-10-06** (dispatch session, `AskUserQuestion`). Question as put:
> *"\"Automatically mark useless files for deletion\": how should a marked file surface?"* Answer
> as given (selected option label, verbatim): *"Review queue, you approve (Recommended)"*. Durable
> record: the description of epic `phaze-gyj6i`.

This spike answers the third question the epic poses: *how does an operator-approved "delete this
junk companion" fit phaze's existing proposal and execution flow, and what has to exist before the
agent can delete a file?* Specifically:

1. How do rename/move proposals flow today? Can a delete be a new proposal kind on that flow, or
   does it need a flow of its own? Can the agent delete anything from disk today?
2. What happens downstream when a companion's `files` row is deleted, and when a re-scan finds the
   file again? What has to change so an approved delete stays deleted and a rejected one is not
   proposed again?
3. What audit trail is needed (who approved, when, what was removed), and how does it fit
   `execution_log`?
4. How large is the population? This is the blast-radius statement CLAUDE.md rule 4 requires.

Which files count as junk is **not** decided here. That is the content survey's job
(`phaze-lm73u`). Wherever this document needs that number, it says **pending content_survey** and
uses a cheap proxy that is labelled as one.

## Method

- **Code archaeology** on the forked tree: `src/phaze/models/{proposal,execution,file,
  file_companion,dedup_resolution,tag_write_log,orphan_companion_diagnostic}.py`,
  `src/phaze/services/{proposal_queries,execution_dispatch,execution_dispatch_protocol,
  scan_deletion,stage_status,dedup,dedup_review,companion}.py`,
  `src/phaze/services/pipeline/proposals.py`, `src/phaze/tasks/{execution,execution_filesystem,
  scan}.py`, `src/phaze/routers/{proposals,agent_proposals,agent_execution,agent_files}.py`,
  `src/phaze/agent_watcher/observer.py`, `src/phaze/schemas/{agent_tasks,agent_proposals,
  agent_execution}.py` and `docker-compose.agent.yml`.
- **A whole-tree search for every archive-touching delete**, using
  `grep -rnE '\.unlink\(|os\.remove|shutil\.rmtree|os\.rmdir|\.rmdir\(|os\.unlink' src/phaze`. Each
  hit was then read to see what it deletes.
- **Two read-only production probes** in `phaze-api` on `host-prod`. They read companion rows by
  extension, link state, filename frequency, how many directories each SHA-256 appears in, the
  orphan-companion diagnostics, and the row counts of `proposals`, `execution_log`,
  `tag_write_log` and `dedup_resolution`. The queries fetch narrow columns and the aggregation runs
  in Python, so there are no N×M joins. Raw output, which includes archive filenames, stays in the
  per-bead scratch directory and is **not** reproduced here. Filenames below are placeholders
  (`<site-01>.nfo`), and content digests are `<hash-1>`.

## Evidence

### E1. Can the agent delete a file today? No, except as the second half of a move

The whole-tree search finds 22 delete calls under `src/phaze`. Each one falls into one of these
groups:

| Where | What it deletes |
|---|---|
| `tasks/execution_filesystem.py` (9 sites) | the **source of an approved move**, after the destination is claimed (hard-link on the same filesystem, or a verified copy plus a commit marker across filesystems); also its own `.phaze-tmp` copies and `.phaze-committed` markers |
| `tasks/functions.py`, `job_runner.py`, `services/video_audio.py` | analysis scratch: extracted audio and staged temp files |
| `tasks/agent_worker.py`, `tasks/push.py` | the agent's own scratch and secret temp directories |
| `scripts/download_models.py` | partial model downloads |

**No code path anywhere removes an archive file without first writing its replacement.** The move
engine's module docstring says so outright: *"Content identity alone never authorizes deleting a
source: replay also requires proposal-specific corroboration."* Every unlink of an archive file
depends on a destination that was just verified. A pure delete has no destination, so it gets none
of the guarantees that make the move engine safe.

Other features that look destructive do not touch the disk either:

- **Dedup resolution is a database marker only.** `DedupResolution` is a sidecar row ("existence =
  resolved; undo = DELETE the row"), and `services/dedup_review.py` commits plans to the database.
  Nothing is enqueued to an agent. Companions are also **excluded** from dedup altogether
  (`_dedup_population_clause`, which the docstring calls "PROVISIONAL", under operator decision D1
  of 2026-08-24, recorded on bead `phaze-j8hjn`).
- **Scan-batch deletion and watcher-twin retirement** (`delete_scan_cascade`,
  `delete_file_cascade`) delete database rows and never touch files.

**Where a delete task would run:** `docker-compose.agent.yml` mounts the archive read-write only
on `worker-meta` (and the opt-in `worker-drain`), with the comment *"Every meta-lane task that
mutates the archive lives behind this mount: execute_approved_batch (move/rename), write_file_tags
(mutagen save), write_cue_sheet (CUE write)."* A delete task belongs on the meta lane as a fourth
archive mutator. The analyze lane, the io lane and the watcher mount the archive `:ro` and could
not run one.

### E2. How rename/move proposals flow today

1. **Generate.** `services/pipeline/proposals.py::get_proposal_pending_batches` selects files that
   pass `_proposal_pending_clauses()`: not yet proposed, with metadata and analysis done or
   skipped, and no enrich stage in flight. The files are grouped by directory and sent to the LLM
   (`tasks/proposal.py`), which may read **companion text** as context (`fetch_companion_contents`
   over `file_companions`). The results are upserted into `proposals` (`RenameProposal`), with a
   partial unique index allowing one PENDING row per file (`uq_proposals_file_id_pending`).
2. **Review.** `routers/proposals.py` handles approve, reject, undo, edit and bulk, through the
   `APPROVE_REJECT_FROM` / `UNDO_FROM` state machine in `models/proposal.py`. Approve carries an
   optimistic-concurrency token (`expected_updated_at`) and refuses destination collisions
   (`get_review_collision_ids`). The UI is the propose workspace
   (`templates/pipeline/partials/propose_workspace.html`, `_diff_row.html`), and every row renders
   as a filename diff.
3. **Dispatch.** `services/execution_dispatch.py::get_approved_proposals_grouped_by_agent` turns
   every APPROVED row into an `ExecuteBatchProposalItem` (`source_path`, `proposed_path`,
   `proposed_filename`, `sha256_hash`). It groups them per agent, drops revoked agents, and chunks
   them by 500. `execution_dispatch_protocol.py` enqueues the chunks with Redis progress sentinels.
4. **Execute.** On the agent, `tasks/execution.py::execute_approved_batch` runs `_execute_one` per
   item. It POSTs an IN_PROGRESS `ExecutionLog` row with `operation="move"`, then calls the
   filesystem engine's `move()`. On success it PATCHes the proposal to `executed` with
   `file_state="moved"` and the new `current_path`. On failure it PATCHes `failed`.
5. **Done.** In `services/stage_status.py`, `done(PROPOSE)` / `done(REVIEW)` mean that *any*
   proposal row exists for the file, and `done(APPLY)` means a `completed` `execution_log` row
   exists, reached by joining through `proposals`.

**Measured on production: this flow has never run.** `proposals` has **0** rows, and so do
`execution_log`, `tag_write_log` and `dedup_resolution`. Adding a delete kind to this pipeline
would therefore build on code with no production executions at all. Its tests are its only record.

### E3. A new proposal kind on `proposals` — NO-GO

Adding a delete would mean a `kind` discriminator (`rename | delete`) on `RenameProposal`, with
dispatch and execution branching on it. The schema and the readers argue against it:

- **Required columns do not fit.** `proposals.proposed_filename` is `NOT NULL`. So is
  `execution_log.destination_path`. A delete has neither, so it would need a sentinel value, and
  every reader would have to recognise that sentinel.
- **The discriminator fails open, in the destructive direction.** 26 source files under `src/phaze`
  reference `RenameProposal`. Among them: the dispatcher, the collision check, the preflight, the
  stage predicates, the audit-log queries, the record and tags routers, and two templates. Any
  reader that forgets the `kind` filter treats a delete as a rename. In
  `get_approved_proposals_grouped_by_agent` that means a junk-delete item reaches `move()`. The
  safe default would have to be added at 26 sites, and the first one missed is a live defect.
- **The stage semantics would lie.** `done(PROPOSE)` is "a proposal exists", so every junk
  candidate would read as *proposed* and every executed delete as *applied* on the pipeline
  dashboard. The epic asks for the opposite: junk is not the rename pipeline.
- **The review UI is a filename diff.** `_diff_row.html` renders old name → new name with a
  destination-collision guard. A delete row needs different evidence: why the file was judged junk
  (the signature), how many copies exist, its size, and whether it is linked to media.
- **The agent contract is move-shaped.** `ExecuteBatchProposalItem` uses `extra="forbid"`, and its
  fields name a move. `_report_success` reports `file_state="moved"` with a `current_path`.

Phaze already has **two precedents for a separate archive-mutating flow** that reuses the shape of
proposals without sharing their table: tag writes (`TagWriteLog`, `tasks/tag_write.py::
write_file_tags`, `routers/tags.py` review) and CUE writes (`tasks/cue_write.py::
write_cue_sheet`, `WriteCueSheetPayload`). Each has its own table, its own meta-lane task and its
own wire payload, and the rename path never sees them. **A junk delete should follow those
precedents.**

### E4. What happens downstream when a companion row is deleted

There is one shared list of child tables, `services/scan_deletion.py::_file_descendant_steps`, and
both cascades use it. `delete_scan_cascade` applies it to a batch and `delete_file_cascade` to one
file (added by `phaze-oxn2m`). For a companion row, the cascade deletes:

- its `file_companions` links, in **both** directions. Production has 19,983 link rows over 14,438
  distinct linked companions. The media files themselves are untouched.
- its `proposals` **and their `execution_log` rows**. This matters for the audit: **if the delete
  audit hung off `proposals`/`execution_log`, retiring the row would erase the record that the file
  was deleted.**
- `analysis*`, `metadata`, `tag_write_log`, `tracklists` (with their versions, tracks and Discogs
  links), `dedup_resolution` (both foreign keys), `cloud_job`, `cloud_budget`, `stage_skip`, and
  `scheduling_ledger` rows (matched by natural id).

`retention_blockers(..., retiring=True)` is how `phaze-oxn2m` keeps operator-reviewed state from
being deleted on a guess. It refuses on relocation, on any non-PENDING proposal, on a tag write, a
dedup resolution, a tracklist or a companion link. Measured on companions in production:
**0** relocated, **0** proposals, **0** tag writes, **0** dedup resolutions, **0** tracklists. The
only blocker that can fire today is `companion_link`, and for a junk delete that link is exactly
what is being discarded on purpose.

**One schema hazard has bitten this repo before.** A new sidecar with a foreign key to `files.id`
and no `ON DELETE` will block both cascades. That is the `stage_skip` incident written up in the
cascade (*"ForeignKeyViolation -> 500 -> batch permanently undeletable"*). Any new junk-review
table either gets an explicit step in `_file_descendant_steps`, or is deliberately free of foreign
keys. E6 argues for the second.

### E5. Re-scan behaviour: what keeps a decision decided

- **Rows are keyed on `(agent_id, original_path)`.** `routers/agent_files.py::_upsert_rows` runs
  `INSERT … ON CONFLICT (agent_id, original_path) DO UPDATE` and refreshes only
  `sha256_hash`, `file_size`, `batch_id`, `file_type` and `updated_at`. **The `id` is kept.** So
  while the row survives, a decision keyed on `files.id` survives a re-scan, but the row's
  **hash can change underneath it**.
- **A re-scan does not notice absence.** `scan_directory` posts only what it finds. The watcher
  **ignores delete events** (`agent_watcher/observer.py`: *"Delete and \*DirEvent types are
  ignored"*). A file deleted from disk keeps its row forever unless something retires it.
- **A deleted row comes back with a new identity.** If the row is deleted (by a cascade) while the
  file is still on disk, the next scan inserts a **new** `files.id`. Every decision keyed on the old
  id is gone, so a rejected candidate would be **proposed again**.
- **Batch deletion moves with the file.** A re-scan reassigns `batch_id` to the newest batch, so
  deleting an *old* batch leaves a re-scanned file alone. Deleting the batch that *currently* owns
  the file deletes everything keyed to it.
- **Orphan companions are not rows.** In `tasks/scan.py::_split_companions`, a companion with no
  media beside it, and none matching its name in the parent directory, is posted to
  `orphan_companion_diagnostics` as metadata only, with no hash and no `files` row. Production has
  **4,580** distinct orphan paths across 3 batches (`.cue` 110, `.m3u` 1,578, `.nfo` 1,747, `.txt`
  1,145). That includes **290** copies of the same stamp filename as the 3,555 ingested ones below.
  A flow keyed on `files.id` cannot reach them.

So, to keep **an approved delete deleted**: after the agent confirms the unlink, retire the row,
because a stale row would otherwise sit there for a file that no longer exists. Since the watcher
ignores deletes and a scan only posts what it finds, nothing re-creates the row unless the file
**comes back at the same path**, for example when a download client or rsync re-delivers the
release. In that case the file is ingested with a new id. Recognising it requires a record that
outlives the row, keyed on the natural identity `(agent_id, original_path, sha256)`.

To make sure **a rejected delete is not proposed again**, the rejection has to survive three
things: a re-scan (keyed on `id`, which is fine while the row lives), a batch deletion (which
cascades anything keyed to the file), and a hash change. A rejection keyed on the natural identity,
in a table with no foreign keys, survives all three. On a hash change it correctly stops applying:
different bytes are a different judgement.

### E6. Audit trail

`execution_log` cannot carry it as it stands. `proposal_id` is `NOT NULL REFERENCES proposals`,
`destination_path` is `NOT NULL`, and the cascade in E4 deletes the log rows along with the
proposal. That last point rules out reusing it even with a dummy proposal row, because the flow
ends by retiring the file row.

**Who approved:** phaze has no operator identity. The admin UI has no authentication principal and
proposals record no approver. Approval time is not durable either: `UNDO_FROM` allows APPROVED →
PENDING, and only `updated_at` moves. Today "who" can only mean "the single operator, through the
admin UI". Recording a real identity would need an auth layer that does not exist, which is a
question for the decision bead, not something to infer here.

What the audit must record, mirroring `TagWriteLog`'s append-only shape:

| Field | Why |
|---|---|
| `agent_id`, `original_path`, `original_filename`, `file_type` | what was removed, where. It must outlive the `files` row, so these are **copies**, not a foreign key |
| `sha256_hash`, `file_size` at proposal time | what the operator approved. The agent refuses if the bytes on disk differ |
| `file_id` (plain UUID, **no foreign key**) | traceability while the row exists. No FK, so neither cascade is blocked or emptied (the `scheduling_ledger` FK-free precedent) |
| `signature` / `reason` (e.g. which junk rule matched, copy count) | the evidence the operator reviewed |
| `status` pending → approved → rejected / executed / failed, with `proposed_at`, `decided_at`, `executed_at` | a durable decision time, not an `updated_at` that undo rewrites |
| `decided_via` (`ui` / `bulk`) | the honest substitute for "who" until an identity exists |
| `error_message`, `failed_at_step` | the same failure contract as `ExecutionLog` |

That one table is the review queue, the audit trail **and** the tombstone that E5 needs.

### E7. Population — blast radius (CLAUDE.md rule 4)

Measured 2026-10-07 on production. All 40,810 companion rows belong to one fileserver agent:

| Measure | Count |
|---|---|
| `files` rows, all types | 145,057 |
| companion rows: `.cue` / `.m3u` / `.nfo` / `.pls` / `.txt` | 2,261 / 14,703 / 18,389 / 3 / 5,454 = **40,810** |
| … linked to media in `file_companions` | 14,438 |
| … relocated by phaze (`current_path ≠ original_path`) | 0 |
| … carrying a proposal, tag write, dedup resolution or tracklist | 0 / 0 / 0 / 0 |
| orphan-companion diagnostics (not rows; distinct paths) | 4,580 |
| rows named exactly `<site-01>.nfo` (the epic's release-site stamp) | **3,555**, plus 290 orphan diagnostics |
| SHA-256 values by number of distinct directories they appear in: 1 / 2–4 / 5–19 / 20–99 / 100+ | 35,266 / 606 / 12 / 10 / 1 |
| **proxy:** rows whose SHA-256 appears in ≥ 5 directories | **3,911** rows (`.nfo` 3,630, `.txt` 269, `.m3u` 12), 23 hashes, 61 of them linked |
| … in ≥ 20 directories | 3,808 rows (`.nfo` 3,563, `.txt` 245), 11 hashes |
| … in ≥ 100 directories | 3,382 rows, **one** hash (`<hash-1>`, a 2,100-byte `.nfo` named `<site-01>.nfo` in 3,372 of them) |

Of the ten most widespread hashes, five are byte versions of the `<site-01>.nfo` stamp
(2,079–2,198 bytes) and four are versions of a second site's `<site-02>.txt` stamp (64–614 bytes).
The tenth is a 25,848-byte `.txt` that appears in 26 directories under a generic info name, which
shows the proxy is **not** a junk classifier: a long text file repeated across 26 releases may well be a
shared series tracklist. **The real junk count is pending content_survey (`phaze-lm73u`).**

**Blast-radius statement.** *This changes the path for the 40,810 ingested companion rows on
`host-store`'s agent (of which the cheap signature proxy reaches 3,911, and the real junk count is
pending content_survey), plus potentially the 4,580 orphan-companion paths, which are not rows
today. What currently works that this could break: (a) the rename/move review and execution path,
if a delete is folded into `proposals` (26 source files read `RenameProposal`; that flow has never run
on production); (b) scan-batch and watcher-twin deletion, if the new table holds a foreign key to
`files` with no cascade step; (c) LLM proposal context, which reads the 14,438 linked companions
through `file_companions`; (d) the media file beside a deleted companion, if the agent's delete
ever resolves to the wrong path. The test that proves it still works: **none exists**, because no
delete path exists. That is the finding.* The implementation beads must each name theirs (see
Recommendation §4).

## Verdict

**GO for a separate, dedicated junk-review flow. NO-GO for a new proposal kind on `proposals`.**

- Phaze has **no** path today that deletes an archive file on its own. The only archive unlinks are
  move sources, and they are safe only because a verified destination exists first. A delete path
  has to be built, with its own guards.
- Folding delete into `RenameProposal` fails open across 26 reader files, breaks two `NOT NULL`
  columns, misreports stage state, and puts the audit trail in tables the row-retirement cascade
  empties.
- A separate flow follows two existing precedents (tag writes, CUE writes): its own table, review
  page, meta-lane task and wire payload. It touches neither the rename path nor its stage
  semantics.
- The work is **medium-sized, not large**: one table plus migration, one review page, one agent
  task, one report endpoint and a detector. Every piece has a precedent in the tree, and the rw
  meta-lane mount it needs already exists. The detector's rules are the open half, and they belong
  to `phaze-lm73u`/`phaze-5qymm`.

## Recommendation

1. **One FK-free table** (for example `companion_junk_review`) that is the queue, the audit trail
   and the tombstone, with the fields in E6. Add a guard test asserting that `delete_scan_cascade`
   and `delete_file_cascade` both succeed with a row present for the file, and **leave the row in
   place**.
2. **Detector → pending rows only.** The detector skips any file whose natural identity
   `(agent_id, original_path, sha256)` already has a rejected or executed row. It ships behind the
   rules that `phaze-lm73u` measures. The SHA-256-spread proxy is a candidate *signal*, not a
   verdict (E7's 26-directory `.txt`).
3. **A review page modelled on the tags review**, not the propose workspace. It shows each row's
   signature, copy count, size and link state, and supports approve, reject, undo and bulk, with the
   same `expected_updated_at` optimistic token and a state machine that refuses to leave a terminal
   status.
4. **A meta-lane agent task, `delete_companion_files`**, with a wire payload naming
   `(review_id, source_path, sha256, size)`. Before unlinking it requires all of the following:
   containment under the scan roots (`resolve_and_check_containment`); `lstat` shows a regular file
   (not a symlink, not a directory); the extension is in `INGESTIBLE_COMPANION_EXTENSIONS` (so a
   **media file is refused by construction**); and the size and SHA-256 equal the approved values.
   It unlinks, fsyncs the directory, and on replay treats `ENOENT` as already-done only when
   corroborated by job metadata (the `_moved_flag_key` pattern). It reports the outcome, and the
   control side then retires the row with `delete_file_cascade`. Per CLAUDE.md rule 3 these guards
   are tested against a **real filesystem**, not a mock: a media extension, an escaping path, a
   hash mismatch, a symlink and a directory must each be refused.
5. **Questions for the operator (`phaze-5qymm`), not decided here:**
   - **Quarantine or unlink?** A move into a trash directory, purged later, is reversible and could
     reuse the move engine's no-clobber primitives. But the directory would have to be excluded
     from the scan and the watcher, or it gets ingested again. Unlinking is simpler and cannot be
     undone.
   - **Re-appearance.** When a previously deleted `(agent_id, original_path, sha256)` turns up
     again, should it go back into the review queue (the default this spike assumes, since
     "nothing deleted without approval") or be auto-approved? Auto-approval would amend the
     operator decision of 2026-10-06 recorded on epic `phaze-gyj6i`.
   - **Scope of a rejection.** Does rejecting a candidate cover only that file, or every file with
     the same hash, so that rejecting one copy of a stamp rejects all 3,382?
   - **Orphan companions** (4,580 paths, no row, no hash). Leave them out of v1, or have the agent
     hash and report them as candidates?
   - **Who approved.** Is "single operator, via the UI, at `decided_at`" enough, or does the
     operator want an identity layer?

**Lesson, general form (CLAUDE.md rule 5).** *A discriminator column added to a table with many
readers fails open at every reader that predates it.* This is not specific to `proposals`: it
applies to any destructive or semantic variant introduced into a shared table. The table-per-flow
precedent (`TagWriteLog`, CUE writes) is how this repo already avoids it, and this spike's
recommendation follows that precedent. It is recorded here and is not yet written anywhere more
general.
