# Mutation and replay hunt: first bounded round

Assessment bead `phaze-d9ifa`, within `phaze-3l0q6`; inspected 2026-10-11 UTC.
**Three candidates survived different-model verification and synthetic observations: two P2
tag-write boundary failures and one P3 undo-selection failure.** No source, tests, CI or
production state was changed. These are remediation proposals, not implementation authorization.
The operator checkpoint `phaze-bexe7` remains pending; no later hunt round has started.

## Provenance and bounded coverage

Hunt label: `bughunt-2026-10-11-phaze-d9ifa`. Production source is immutable commit
`d1835b022f99397ed6be69359c57938a9ceaffd9`; the task began at
`f683964dcb0bdaa3b9ae8d532364f72c471987a4`, with only assessment documentation added since that
source. Python 3.14.7, uv 0.12.1, bh 0.24.2. The [risk inventory](testing-risk-inventory.md)
supplied the mutation slice. Three finder passes each covered the same 18 files, totalling
6,160 physical lines. Supporting callers, installed SAQ/mutagen code and relevant tests were
also read; they do not expand the claimed complete slice.

| Complete slice | Files |
| --- | --- |
| Tasks | `src/phaze/tasks/execution.py`, `execution_filesystem.py`, `tag_write.py`, `recovery_replay.py`, `_saq_reap.py` |
| Services | `src/phaze/services/tag_writer.py`, `tag_write_disk.py`, `execution_preflight.py`, `execution_dispatch.py`, `execution_dispatch_protocol.py`, `review_tagwrite.py`, `enqueue_router.py` |
| Routers | `src/phaze/routers/tags.py`, `agent_tag_writes.py`, `agent_execution.py` |
| Shared task helpers | `src/phaze/tasks/_shared/replay_safety.py`, `deterministic_key.py`, `queue_factory.py` |

Completed lenses: concurrency, data-integrity/persistence, and security/reviewed-source
contracts. The latter is a source-contract correctness review; it found stale reviewed
metadata, not an authentication defect. The finder inherited the session model; all final
verifiers were fresh-context `gpt-6-astra`, a different model, with high confidence.
No candidate was P0, so the three-perspective escalation rule was not triggered.

The task-owned durable artifact directory is `assessment-artifacts/phaze-d9ifa` beneath the
Beadhive worktrees root. It contains `slice.json`, `plan.md`, the three `*-finder.json` outputs,
the three `*-coverage.json` read-extent records, `journal.jsonl`, final `coverage.md`,
`round-1-confirmed.json`, verdicts, scratch reproductions, logs and exact report bodies.
The coverage JSONs preserve finder-time status, including then-pending verification; final
coverage and the append-only journal supersede those status fields. Artifacts are intentionally
outside the tracked source worktree and contain only synthetic reproduction data.

## Results and metrics

| Lens | Candidates | Confirmed | Mechanical duplicates | Judged duplicates | Filed novel intake | Severity |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Concurrency | 1 | 1 | 0 | 0 | 1 | P2 |
| Data integrity | 1 | 1 | 0 | 0 | 1 | P3 |
| Reviewed-source contracts | 1 | 1 | 0 | 0 | 1 | P2 |
| Round 1 total | 3 | 3 | 0 | 0 | 3 | 2 P2, 1 P3 |

Confirm rate: 3/3, **100%**. Novelty rate after full-body dedup and intake filing: 3/3, **100%**.
P0/P1 share: 0/3, **0%**. There is no earlier round in this expedition for a trend comparison.
Actual spend and cost per filed bug are **unavailable** from the harness; elapsed repro times
are not spend estimates. All three now exist as sanctioned untriaged bug intake; none is
approved or dispatched for implementation.

### 1. A cancelled task leaves a live writer after completed Undo — P2

Root: [write_file_tags](../../src/phaze/tasks/tag_write.py) directly awaits
`asyncio.to_thread(_write_verify_and_rehash, Path(file_path), payload.tags)`. Cancelling that
coroutine cannot stop an already-running executor thread. Installed SAQ cancels a timed-out
task and retries after coroutine cancellation; defaults permit a 600-second timeout and four
retries. Durable snapshot acknowledgement fences terminal attempts before offload, and the
queued-row guard fences new dispatches, but neither revokes an already-admitted thread.

Observed sequence on a synthetic MP3: hold attempt A immediately before its real disk write;
cancel its awaiting coroutine; retry the same deserialized payload as B; B reports completed;
the actual Undo route dispatches a reversal and its task reports completed; confirm original
raw tags; release A. A changes tags and digest after Undo. Both audit rows still say completed,
and the stored file digest no longer matches disk. No A result callback survives to repair it.
This establishes incorrect tags and stale recorded identity, without claiming audio damage.

`test_cancellation_repro.py` / `cancellation-repro-rerun.log`: **1 passed, 11 warnings in 6.38 s**,
`REPRO_EXIT=0`; the header names the task's own test database on port 5433, `exclusive`.
Real task, real mutagen file mutation, real snapshot/result callbacks over ASGI, real PostgreSQL
and actual Undo were used. Delivery was the existing fake broker, whose enqueue payload passes
through the production SAQ serializer. An Event barrier and explicit coroutine cancellation
model the delayed operation; a live SAQ timeout, actual mount hang and process kill were not run.

Closest existing regressions: [tag snapshot retry seam](../../tests/integration/test_tag_snapshot_retry_seam.py),
[tag undo loop seam](../../tests/integration/test_tag_undo_loop_seam.py), and
[task tests](../../tests/review/tasks/test_tag_write.py) covering terminal snapshot acknowledgements
and callback retries. These exercise serial retries or admission checks, rather than an old live
mutation resuming after Undo. Proposed acceptance: hold mutation ownership until the disk worker
has terminated, then demonstrate that retry and Undo cannot be overwritten by the old worker.
Shielding the await or increasing the timeout alone does not establish this property.

Fingerprint: `concurrency:src/phaze/tasks/tag_write.py:write_file_tags:uncancelled-mutation-thread`.
Final source verdict: `cancellation-verdict.json`. Sanctioned intake: **`phaze-zjwp4`**
(untriaged bug; priority/labels requested in its body).

### 2. A partially failed save removes the original Undo anchor — P3

Root: [undo selection](../../src/phaze/routers/tags.py) excludes all `FAILED` rows, while
[disk writing](../../src/phaze/services/tag_write_disk.py) returns `FAILED` for any save exception.
Installed mutagen writes ID3v2 before its ID3v1/end-of-file step, so an exception can occur after
tags changed. The task itself documents FAILED as indeterminate and computes the observed digest.
The correct original remains durable in `before_tags`, but normal Undo cannot select that row.

Observed on a synthetic MP3: inject `OSError` only at `ID3.__save_v1`, after the actual ID3v2 save.
The task reports FAILED; raw disk tags changed; the audit row retains the original artist.
Actual Undo returns “No prior tag write” and dispatches nothing. Remove the injection and dispatch
a successful retry; subsequent actual Undo restores the new artist rather than the original.
The original is manually recoverable from the audit row, so this is not permanent data loss.

`test_failed_save_repro.py` and the revision observation below ran serially in one corrected
own-seat process, `isolated-seam-repros.log`: **2 passed, 11 warnings in 1.79 s**,
`REPRO_EXIT=0`, own database on port 5433, `exclusive`. Real save, task, callbacks, PostgreSQL,
serializer and Undo; deterministic tail-error injection and fake broker delivery. No actual
storage failure, audio-integrity check or separate pre-mutation negative-control run was made.

Closest regressions: [router tests](../../tests/review/routers/test_tags.py), specifically
`test_undo_skips_failed_shadow_and_restores_real_write_snapshot`,
`test_undo_with_only_failed_write_reports_nothing_to_undo`, and
`test_undo_picks_original_write_not_a_later_successful_retry`, plus the real undo seam above.
The first two treat FAILED as non-mutating. Proposed acceptance: distinguish failures before
mutation from possibly landed saves, preserving the oldest genuine chain anchor; do not simply
make every FAILED row undoable.

Fingerprint: `data-integrity:src/phaze/routers/tags.py:_get_write_log_to_undo:failed-save-undo-anchor-loss`.
Final source verdict: `failed-save-verdict.json`. Exact filing proposal: `failed-save-report-body.md`.
Sanctioned intake: **`phaze-ct6l1`** (untriaged bug; P3 priority/labels requested in its body).

### 3. Durable enqueue accepts metadata that changed after review validation — P2

Root: [validate_reviewed_source_versions](../../src/phaze/services/tag_comparison.py) checks
selected-source, legacy tracklist and accepted Discogs evidence but omits the reviewed file and
metadata revisions. Initial [token validation](../../src/phaze/routers/tags.py) compares the full
before/after/source payload and rejects stale metadata. Bulk candidates are all validated before
dispatch; the later enqueue boundary uses their copied tags. A metadata update committed in that
gap is accepted even though the declared token contract would now refuse the same decision.

Observed with synthetic applied records and a genuine review token: initial validation succeeds;
commit a corrected title; real enqueue creates a QUEUED audit with the old reviewed title and
dispatches that title; eager fresh initial validation rejects the same token with HTTP 409.
The missing artist supplied by the synthetic filename ensures the original comparison has a
qualifying change. This observes the real validator-to-enqueue seam, without a live worker or
disk overwrite. The full two-candidate bulk callback schedule remains source-supported rather
than runtime-measured. Already-authorized queued work is outside this finding's contract.

`test_revision_repro.py` passed in the combined serial own-seat process above,
`isolated-seam-repros.log`: **2 passed, 11 warnings in 1.79 s** for the two observations together,
`REPRO_EXIT=0`, own database on port 5433, `exclusive`. Real token encoding/validation, database
revision, enqueue and production queue serializer; fake broker delivery, no disk writes.

Closest regressions: [router stale-review test](../../tests/review/routers/test_tags.py)
`test_write_tags_rejects_metadata_changed_after_review` changes metadata before initial POST
validation. [Selected-source consumers](../../tests/integration/test_selected_source_consumers.py)
`test_actual_selected_tag_approval_and_write_boundary_revalidation` guards a source change at
the durable boundary, rather than this missing metadata comparison. Proposed acceptance: lock,
refresh and compare the relevant file/metadata revisions or used facts before audit creation;
retain existing enrichment evidence guards and cover both later bulk candidates and per-row gaps.

Fingerprint: `security:src/phaze/services/tag_writer.py:enqueue_tag_write:stale-reviewed-metadata`.
Final source verdict: `revision-verdict.json`. Exact filing proposal: `revision-report-body.md`.
Sanctioned intake: **`phaze-83205`** (untriaged bug; P2 priority/labels requested in its body).

## Historical dedup and limitations

The accessible historical corpus held **3,860** issues; the open corpus held **76**. No structured
standard hunt fingerprints were found; historical audio-fingerprint mentions are unrelated.
All three confirmed candidates were compared against the full corpus and each other. Nearby
full bodies and closure descriptions were read, not just titles. There were no duplicate-to-
canonical mappings because none matched the current failure mechanism and required repair.

Current `bh report --help` exposes bug intake with title and description, but no dry-run,
custom priority, custom labels or structured fingerprint metadata. The exact three bodies were
persisted and substantively reviewed by the dispatcher; the first intake had already started
before that review request arrived. The other two preserved finder/source-verifier statements
are explicitly labelled as preceding runtime reproduction. All fingerprints and requested
severity/lens/hunt labels are in the bodies; intake triage must apply the requested priorities
and labels rather than mistake the CLI default priority for the verified severity. No raw
tracker mutation, kickoff approval or remediation dispatch was used. Filing logs and durable
intake readbacks are retained outside the source tree; the refreshed open corpus supplies
the manual post-filing near-duplicate check.

| Candidate | Canonical neighbors read | Why it survives |
| --- | --- | --- |
| Cancelled live writer | `phaze-lwqk`, `phaze-9f82r`, `phaze-ysnp`, `phaze-xq8vq`, `phaze-anrw4`, `phaze-yy9bk`, `phaze-u28m` | Historical HTTP cancellation and concurrent dispatch were handled by durable audit/queued-row guards. One current row and one job payload can still have a cancelled running thread; no ambiguous enqueue, missing audit or lost first snapshot is required. |
| Partial save anchor | `phaze-soph`, `phaze-lwqk`, `phaze-yy9bk` | The earlier non-mutating failed shadow must still be excluded. This first FAILED attempt really changed tags and retains the original snapshot; the repair needs mutation evidence rather than only different ordering. |
| Metadata revision | `phaze-8wzi`, `phaze-tzy6s.11`, `phaze-tzy6s.11.2` | Old inline-edit/form persistence and review-token requirements differ from a metadata revision committed after valid initial review but before durable enqueue. |

Errors are counted by role, rather than mistaken for empty results: **0 finder errors**, **3 lead
interruptions**, and **1 errored verifier attempt** followed by a completed final independent
verdict. The earlier lead's third interruption and recovery are retained in the journal. There
was no repeated operational activity or bypass of the interrupted activity.
Scratch harness errors were separate: one initial cancellation import failure, one attempt to
source a recipe transcript containing status text, and one revision negative-control lazy-load
failure. Only scratch import paths, export extraction and eager reload were corrected. The
successful observations are the final logs above; failed attempts remain preserved.

The initial export filter also missed indented recipe exports. Consequently, the first partial-
save run (**1 passed, 11 warnings in 1.48 s**) and the revised metadata run (**1 passed, 10 warnings
in 0.96 s**) used the default test database, with exclusive session locks. Those locks prevented
concurrent use of that database, but the runs violated the per-worktree seat requirement and are
not the accepted isolated evidence. Final header review caught this; all three exact exports
were copied with leading whitespace removed, and both observations were rerun successfully in
the single owned-seat process above. Original logs and this correction are preserved. Initial
intake bodies retain their original observations/timings; this maintained report states the final
accepted isolation evidence rather than silently replacing history.

Callbacks also logged caught scheduling-ledger cleanup errors because the fake broker fixture has
no live `saq_jobs` table. The real callback returned 200 and committed snapshot/status/hash; the
observations do not establish ledger cleanup or real broker processing. Each successful run used
the session advisory lock; no shared-DB bypass, overlapping own-seat pytest or shared harness
shutdown occurred. No suite-wide coverage measurement was rerun for these temporary observations.
The [baseline](coverage-baseline.md) remains the existing source evidence; no new coverage claim
or changed coverage floor follows from three observations.

## Checkpoint recommendation

The round is productive in bounded tag-state correctness, with **three novel bug intake reports** and
no P0/P1. Its error history prevents a clean-round/convergence claim. Execution/replay files
completed the declared passes with no additional candidate; this is not proof those surfaces are
defect-free. Unrun ground includes other source slices, other lenses, analysis/resource paths,
companion/browser/persistence paths, and every later round.

Recommend reviewing this remediation set and, if the operator elects to continue, moving to the
already planned analysis/resource slice rather than repeating these three passes. The explicit
choices at `phaze-bexe7` are continue the planned next round, narrow further tag-state work,
switch to other unrun ground, or stop and wrap up. This report does not resolve that checkpoint.
