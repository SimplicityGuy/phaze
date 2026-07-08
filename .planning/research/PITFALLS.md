# Pitfalls Research

**Domain:** Replacing a stored linear status enum (`files.state`, 17-member StrEnum) with a derived per-file, per-stage status function, in a live batch pipeline with a ~200K-file corpus, distributed agents, at-least-once job delivery, and months-long in-flight work.
**Researched:** 2026-07-08
**Confidence:** HIGH for the DB/planner/SAQ findings (empirically reproduced against PG 18.4 + the installed SAQ source); HIGH for codebase claims (read from `main` @ `ce0c6434`); MEDIUM where noted inline.

> **How to read this.** Every pitfall below is specific to *this* change on *this* system. Generic refactoring advice is omitted. Each carries a **warning sign** (something an engineer actually observes), a **prevention** (testable), and a **phase** from the vocabulary below.

## Phase vocabulary

The design's §11 mitigation ("phase the work: derivation layer first, then readers, then writers, then the drop") is the right spine. This document maps every pitfall to one of these:

| Tag | Phase | Content |
|---|---|---|
| **DERIV** | Derivation layer | `stage_status()`, `eligible()`, the predicates, the partial indexes. Additive, no deletions. |
| **MARK** | Failure markers | analyze + metadata failure markers; terminal-ack completeness; `FAILURE_IS_TERMINAL`. |
| **SIDE** | Sidecars | `AWAITING_CLOUD` routing state, dedup marker, `LOCAL_ANALYZING` derivation. |
| **READ** | Readers cutover | pending sets, `get_pipeline_stats`, `reenqueue`, `dedup`, `proposal`, `fingerprint` progress. |
| **WRITE** | Writers retirement | delete the 20 `state=` writes; **replace the 23 `WHERE state == X` CAS guards**. |
| **UI** | Presentation | templates, `/pipeline/stats` poll shape, degraded-state rendering. |
| **MIG** | Migration | `032` additive + shadow-compare gate + `033` destructive; backfill; rollback net. |
| **TEST** | Test-suite rework | the ~50 files that construct `FileRecord(state=...)`; anti-vacuous-pass discipline. |

---

## Critical Pitfalls

### Pitfall 1: `in_flight = saq_jobs ∪ scheduling_ledger` (design D-01) creates a *permanent stall*, the mirror image of the over-enqueue incident

**What goes wrong:**
The design's D-01 recommends `in_flight = saq_jobs(queued|active) ∪ scheduling_ledger`, with precedence `in_flight ≻ done ≻ failed ≻ not_started`. But the scheduling ledger's clear is **best-effort and callback-dependent**, and there is a documented, in-tree case of a ledger row that survives forever with no live job:

> `_backfill_candidates_stmt` (`services/pipeline.py:1287`) docstring: *"A SAQ timeout abandons a long `process_file` job WITHOUT firing `report_analysis_failed` (which clears the row), so the orphaned ledger row persists into `ANALYSIS_FAILED` — exactly the timed-out set this backfill re-drives."*

That entire backfill feature exists *because* orphaned ledger rows are normal. Under D-01 as written, every one of those files becomes `in_flight(analyze) = true` **forever**, therefore `eligible = false` forever, therefore invisible to the trigger, the recovery producer, and the retry button. `in_flight ≻ done` makes it worse: a file whose analysis later *completes* still renders "running" in the DAG until someone manually deletes the ledger row.

The blast pattern is the exact inverse of the 44,500-job over-enqueue: instead of re-running everything, you silently run nothing, and the corpus (which takes **months**) quietly stops advancing. The over-enqueue announced itself in five minutes. This announces itself in three weeks.

**Why it happens:**
Three separate mechanisms make ledger rows durable-but-stale, and all three are *by design*:
1. `apply_deterministic_key` (`tasks/_shared/deterministic_key.py:158-183`) wraps the ledger upsert in a bare `except Exception: logger.warning(...)` — "the ledger is best-effort here."
2. `increment_completed` (the `after_process` clear) is gated on `getattr(job.queue, "ledger_sessionmaker", None)`. **The agent worker is deliberately Postgres-free and has no `ledger_sessionmaker`.** So for the three enrich stages, the *only* clear is the control-side HTTP callback (`put_metadata` / `put_fingerprint` / `put_analysis` / `report_*_failed`). A killed agent worker, an evicted Kueue pod, a network partition, or a SAQ timeout produces zero callbacks and thus zero clears.
3. `clear_ledger_entry` failures are also swallowed ("a clear hiccup leaves the row for the next recovery; never raise").

`recover_orphaned_work` tolerates all of this because it *cross-checks* the ledger against a domain-completed predicate. `stage_status()` as specified in D-01 has no such cross-check.

**How to avoid:**
- **Do not adopt D-01's naked union.** Use:
  ```
  in_flight(f, stage) ⇔ EXISTS saq_jobs WHERE key = fn||':'||f.id AND status NOT IN ('complete','failed','aborted')
                      ∨ ( EXISTS scheduling_ledger WHERE key = fn||':'||f.id
                          AND enqueued_at > now() - <stage_stall_grace> )
  ```
  `scheduling_ledger.enqueued_at` already exists and is `NOT NULL` with a server default. The grace bound is per-stage and should be `timeout × (retries+1) × safety` — for `process_file` that's `7200 × 3 × 1.5 ≈ 9h`; for metadata/fingerprint, minutes.
- **Change the precedence for the *display* consumer**: `done ≻ in_flight ≻ failed ≻ not_started`. A completed analysis must never render as "running" because a bookkeeping row leaked. Keep `in_flight`'s veto only in `eligible()` (where `NOT done ∧ NOT in_flight` makes both terms redundant anyway). The design's stated rationale ("an in-progress retry shows as running, not as its stale prior outcome") is satisfied by `done ≻ in_flight` too, because a retry only exists when `NOT done`.
- **Emit a stalled-ledger metric**: `COUNT(scheduling_ledger) WHERE enqueued_at < now() - grace AND key NOT IN (live saq keys)`. This is the observable for exactly the failure this pitfall describes, and it costs one query.

**Warning signs:**
- The three enrich `done` counters plateau while `busy` pills sit at a constant non-zero number that never moves.
- `SELECT count(*) FROM scheduling_ledger` grows monotonically across weeks.
- A file shows `analyze: running` in the DAG while `analysis.analysis_completed_at IS NOT NULL`.
- The stage's trigger button reports "0 enqueued" against a visibly non-empty backlog.

**Phase to address:** **DERIV** (the predicate + the grace bound), **MARK** (make every terminal path clear the row), plus a **DERIV** regression test that a ledger row older than the grace bound does *not* suppress eligibility.

---

### Pitfall 2: The `not_started` conflation — four distinct causes, one indistinguishable answer

**What goes wrong:**
`not_started ⇔ ¬done ∧ ¬in_flight ∧ ¬failed`. That single answer is produced by four causally different situations:

| Cause | Ledger row? | `saq_jobs` row? | Output row? | Correct action |
|---|---|---|---|---|
| Never scheduled | no | no | no | **enqueue** |
| Scheduled, worker died before start | yes (stale) | no | no | **re-enqueue** (bounded) |
| Crashed mid-run, retries exhausted, no `/failed` callback | yes (stale) | maybe `failed` | no | **do not auto-retry** (analyze) |
| Job succeeded, callback lost (network partition to control plane) | yes (stale) | `complete` | **no** | **re-enqueue** — the work is genuinely lost |

phaze's `scheduling_ledger` disambiguates row 1 from rows 2–4. That is precisely and only what it was built for ("a never-scheduled `DISCOVERED` file has NO ledger row, so the incident sweep CANNOT recur"). **It does not disambiguate rows 2, 3 and 4 from each other** — and row 3 is the one that must never auto-retry, because it is the 4-hour poison file that detonated the queue.

So: **the ledger does not fully close this.** It closes exactly one of the three dangerous cases.

**Why it happens:**
Because `done` is derived from an output row, and an output row is written by a *remote* process over an at-least-once HTTP channel. Absence of the row therefore carries no information about whether the work ran. The enum accidentally encoded "the control plane observed a terminal outcome" as a first-class fact; a derived model throws that away unless you re-add it.

**How to avoid:**
- **Add a per-stage attempt counter, not just a boolean marker.** The failure marker (D-02: `failed_at` + `error_message` on the output table) must be joined by `attempts int NOT NULL DEFAULT 0`, incremented at *terminal ack* (both success and failure paths). Then:
  - `analyze`: `FAILURE_IS_TERMINAL = true` (already load-bearing; keep).
  - `fingerprint`: `FAILURE_IS_TERMINAL = false`, but bound it: `eligible ⇔ … ∧ attempts < MAX_FINGERPRINT_ATTEMPTS`. Today, an unbounded auto-retry stage exists only because the linear enum happened to gate it. Derivation removes the accidental gate; a poison file will re-enqueue on **every trigger click, forever**.
  - `metadata`: same, and note it becomes the *first* metadata failure that is even visible (`report_metadata_failed` persists nothing today — §2.3 of the design, latent bug 4).
- **Make the "callback lost" case (row 4) detectable, not just tolerated.** A `saq_jobs` row in `complete` with no output row and a live ledger row is *the* signature. Expose it as a "completed-but-unrecorded" count; it is a real distributed-systems bug (agent PUT succeeded server-side, response lost, agent retried, HTTP 200 idempotent — or the PUT never landed). The idempotent natural-key upserts phaze already uses make re-enqueue safe here.
- **Fail closed on unknown, not open.** Where the `in_flight` probe cannot be evaluated (see Pitfall 9), `eligible()` must return `false`. Where the `done` probe cannot be evaluated, `eligible()` must return `false`. Both directions of `_safe_count`-style degradation currently return `0`/empty, which means "nothing is running" and "nothing is done" — i.e. **enqueue everything**. That is the 44.5K incident's causal shape, re-created in a new place.

**Warning signs:**
- Recovery/trigger enqueue counts scale with corpus size rather than with backlog size (the incident's fingerprint: "~11,400 never-scheduled `DISCOVERED` files and detonated the queue to ~44,500 jobs").
- A file's analyze stage runs more than twice with no operator action.
- `stage_progress.analyze.done` and `saq_jobs` `complete` counts diverge persistently.

**Phase to address:** **DERIV** (the predicate + fail-closed degradation), **MARK** (attempt counters + terminal-ack completeness on all three enrich stages).

---

### Pitfall 3: The enum is load-bearing as a **concurrency primitive**, not just a status — deleting it deletes 23 CAS guards

**What goes wrong:**
This is the largest gap in the approved design. `FileRecord.state` is used as a compare-and-swap token by callback handlers defending against at-least-once, out-of-order, and cross-agent delivery. Verified sites:

```
routers/agent_push.py:126     UPDATE files SET state=PUSHED         WHERE id=? AND state=PUSHING
routers/agent_push.py:261     UPDATE files SET state=AWAITING_CLOUD WHERE id=? AND state=PUSHING
routers/agent_s3.py:128       UPDATE files SET state=PUSHED         WHERE id=? AND state=PUSHING
routers/agent_metadata.py:89  UPDATE files SET state=METADATA_EXTRACTED WHERE id=? AND state=DISCOVERED
```

Every one of these is a *guard*, not a status write. Phase 73's code review caught CR-01 as a **blocker** precisely because `/mismatch` was missing this guard ("a duplicate/late/unattributed reporter could clobber an already-advanced file"). Phase 69 CR-01 introduced `LOCAL_ANALYZING` *specifically* so a locally-spilled file could not be double-dispatched to a cloud backend. §4.1 latent bug 6 is "`report_upload_failed` has no CAS guard."

Delete the column and every one of those guards evaporates. The design's §7 call-site inventory lists them as "writers of `FileRecord.state`" — which invites deleting them, not replacing them.

**Why it happens:**
Scalar state columns quietly serve three roles: (a) a status people read, (b) a mutual-exclusion invariant the DB enforces, and (c) an optimistic-concurrency token. Derivation replaces (a). Nobody notices (b) and (c) until a duplicate callback lands in production.

**How to avoid:**
- **Produce a written CAS inventory before any writer is touched.** For each of the 23 `FileRecord.state ==` sites, classify as `{read-filter, display, CAS-guard, dead}` and name the replacement:
  - `PUSHING → PUSHED` CAS ⇒ `UPDATE cloud_job SET status='uploaded' WHERE file_id=? AND status='uploading'` (`cloud_job` already has `uq_cloud_job_file_id` and a status CHECK — the CAS token moves, it does not disappear).
  - metadata `DISCOVERED → METADATA_EXTRACTED` CAS ⇒ **not needed**: the guard exists only to stop a *downgrade* of the linear enum. With no enum, `ON CONFLICT (file_id) DO UPDATE` on `metadata` is already idempotent and order-independent. This guard is one of the ones you legitimately delete — but say so explicitly, don't leave it as an accident.
  - `AWAITING_CLOUD` routing decision ⇒ needs a real sidecar row (design D-03) **with its own uniqueness constraint**, because the routing decision *is* the mutual-exclusion invariant ("this file is claimed for cloud, do not analyze locally").
- **Encode mutual exclusion the enum gave you for free as DB CHECK constraints on the new tables.** A scalar cannot be both `ANALYZED` and `ANALYSIS_FAILED`. A row with both `analysis_completed_at IS NOT NULL` and `failed_at IS NOT NULL` is representable and meaningless. Add:
  ```sql
  ALTER TABLE analysis ADD CONSTRAINT ck_analysis_terminal_xor
    CHECK (NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL));
  ```
  Same for `metadata`. This is the single cheapest defence against the whole class of "sets admit states the scalar couldn't."
- **`LOCAL_ANALYZING` is *not* safely derivable from `in_flight(analyze)`** (design D-03's "very likely fully derivable" is optimistic — flag as **MEDIUM confidence, needs a decision, not an assumption**). Its documented job (`models/file.py`) is to remove the file from `get_cloud_staging_candidates` *while the local job is in flight*. If `in_flight` degrades to `false` on a `saq_jobs` read error (it currently does, `get_stage_busy_counts` returns all-zeros on any exception), a locally-analyzing file becomes a cloud staging candidate and is **double-dispatched to a paid backend**. Either give `LocalBackend.dispatch` a real `cloud_job` row (`backend_id='local'`), or make the staging-candidate query's in_flight probe non-degrading (like `Backend.in_flight_count`, which Phase 69 D-05 deliberately made non-degrade-safe for exactly this reason).

**Warning signs:**
- A file's `cloud_job.status` goes backwards.
- Duplicate `/pushed` callbacks produce two `execution_log`/`cloud_job` transitions.
- A file appears simultaneously in the local analyze lane and a cloud lane in the UI.
- `pg_stat_user_tables.n_tup_upd` on the sidecar spikes without corresponding job completions.

**Phase to address:** **WRITE** (the inventory + replacement guards) — and it must be a *named deliverable*, not a side-effect of deleting writers. CHECK constraints land in **MIG** (`032`, additive). The `LOCAL_ANALYZING` decision lands in **SIDE**.

---

### Pitfall 4: "Row exists ⇒ stage done" — and the *second-order* version nobody checks

**What goes wrong:**
The design correctly names the first-order trap: `analysis` gets a coverage-only partial row upserted at analysis **start** (`routers/agent_analysis.py:294`), so `EXISTS analysis` ≠ done; `analysis_completed_at IS NOT NULL` is the real predicate. `get_stage_progress:384` currently counts bare row existence (latent bug 7).

The second-order traps this pattern always brings, and which the design does not enumerate:

1. **`get_stage_progress`'s `proposals` denominator has the same bug.** `convergence_total` (`pipeline.py:339`) is `EXISTS(FileMetadata) AND EXISTS(AnalysisResult)` — bare row existence on `analysis`. So the proposals *denominator* over-counts by exactly the number of in-flight analyses. Fixing only the `analyze.done` numerator leaves a mismatched pair.
2. **`metadata` is about to acquire the same trap.** D-02 recommends putting `failed_at` on the `metadata` table, which means a failure *inserts a metadata row with all payload columns NULL*. `done(metadata)` must instantly tighten to `EXISTS metadata WHERE file_id=? AND failed_at IS NULL` — and every *other* consumer of "has a metadata row" must tighten too. There are several, and they are not in §7's reader list:
   - `get_discovered_files_with_duration` LEFT JOINs `FileMetadata.duration` (`pipeline.py:1103`) — a failure row has `duration IS NULL`, which the duration router treats as "short", which routes a possibly-4-hour file to local analysis. **This is how a metadata failure turns into an analyze timeout.**
   - `_backfill_candidates_stmt` INNER JOINs `FileMetadata` and filters `duration >= threshold` — a failure row silently enters the join and is excluded only by the duration filter.
   - `convergence_total` / `get_proposal_pending_batches` — a failure row would make a file look proposal-ready.
3. **`fingerprint_results` rows are written for failures too** (`status='failed'`), so row existence has *never* meant done there — but `get_fingerprint_progress` (`services/fingerprint.py:288`) counts `state == FINGERPRINTED`, which (see Pitfall 6) is written by nothing except an analysis rollback. Two different wrong answers in the same subsystem.

**Why it happens:**
`EXISTS(child)` is the obvious, cheap, readable predicate. Nobody re-audits it when a table gains a partial-write or failure-write path years later. The write path and the read predicate live in different files.

**How to avoid:**
- **Make every stage's `done` predicate a `*_completed_at IS NOT NULL` timestamp discriminator, uniformly.** phaze already did this once (migration `028`, `analysis.analysis_completed_at`). Do it for the rest:
  - `metadata.extracted_at`
  - `fingerprint_results.succeeded_at` (per-engine)

  This is not cosmetic — it buys three things at once: (a) it makes "row exists ≠ done" structurally unrepresentable at the call site, (b) it kills the status-string-drift class (Pitfall 5), and (c) `IS NOT NULL` is **the only predicate form that a parameterized query can never fail to match against a partial index** (see Pitfall 7, empirically verified below).
- **Add a NOT NULL discriminator + CHECK, don't rely on naming.** `CHECK (extracted_at IS NOT NULL OR failed_at IS NOT NULL OR <in_flight_marker>)` if you want a total classification.
- **Grep-gate it.** A CI check (`just docs-drift`-style, this project already has the pattern) that fails on any `exists(select(AnalysisResult...))` / `exists(select(FileMetadata...))` that does not carry the discriminator predicate. Static AST guards already exist in this repo (`tests/test_task_split.py`, the Phase-30 enqueue-router AST guard) — reuse the machinery.

**Warning signs:**
- `analyze.done` count decreases after a deploy (you fixed the numerator).
- `proposals.total < proposals.done`.
- A file with a NULL `duration` and a `metadata` row appears in the local analyze lane and times out at 4h.

**Phase to address:** **DERIV** (predicates + the AST guard), **MARK** (the `failed_at` semantics), **MIG** (`032` adds the discriminator columns and backfills them from the existing rows: `UPDATE metadata SET extracted_at = created_at` — a `TimestampMixin` gift).

---

### Pitfall 5: Multi-row output tables — `fingerprint_results` aggregation, and the `done ≻ failed` precedence silently deletes a working retry

**What goes wrong:**
`fingerprint_results` is one row per `(file_id, engine)` (`ix_fprint_file_engine`, unique). A file can be `chromaprint=success` **and** `panako=failed` at the same instant. The design's precedence is `in_flight ≻ done ≻ failed`, with `done(fingerprint) = any engine succeeded`.

**Today**, `get_fingerprint_pending_files` (`pipeline.py:1359-1370`) returns `METADATA_EXTRACTED` files **UNION** every file with `FingerprintResult.status == 'failed'` that is not `FINGERPRINTED`. Since `FINGERPRINTED` is written by essentially nothing (Pitfall 6), **a file with a failed engine is always in the pending set**, even if the other engine succeeded. That is the deliberate D-16 "failed engines auto-retry" behavior.

Under `done ≻ failed` with `eligible = ¬done ∧ ¬in_flight`, that file is now `done` and therefore **never eligible**. Panako never retries. The design's own table says `FAILURE_IS_TERMINAL[fingerprint] = false` with rationale *"Failed engines auto-retry — today's deliberate D-16 behavior, preserved."* **It is not preserved.** The precedence rule and the eligibility rule contradict each other, and the contradiction is invisible because both are individually reasonable.

Three more places this class of bug lands:

1. **The engine set is open.** `done = any engine succeeded` means adding a third engine leaves 200K files permanently `done` and never fingerprinted by the new engine. `done = all known engines succeeded` means adding an engine un-dones 200K files and re-enqueues the corpus. Neither is right; **coverage must be per-engine**.
2. **`status` is `String(20)` with no CHECK constraint.** `done` whitelists `('success','completed')`, `failed` whitelists `('failed')`. Any third value — `'skipped'`, `'not_found'`, a typo — is neither `done` nor `failed` nor `in_flight`, so the file is `not_started` and re-enqueues on every trigger, forever. **This project has already shipped this exact bug**: `get_stage_progress` counted `status == "completed"` while every writer persists `"success"`, so `fingerprint.done` was permanently 0 (found in Phase 59 review, fixed PR #189 with `.in_(("success","completed"))`). The fix widened the allowlist. The *class* was never closed.
3. **`tracklists.file_id` is nullable and N-per-file**; `proposals` is N-per-file with ≤1 `pending`; `execution_log` has **no `file_id` at all** and must be joined through `proposals.proposal_id`. Each is an N→1 aggregation with its own precedence question. `execution_log` in particular: a file can have a `completed` execution row for proposal A and a `failed` row for proposal B.

**Why it happens:**
An enum is a scalar; the developer's mental model of "the file's fingerprint status" is a scalar. The table is a set. Collapsing set→scalar requires a precedence rule, and the natural rule (`success wins`) is wrong whenever the set members are *not* redundant attempts at the same thing.

**How to avoid:**
- **Do not aggregate `fingerprint` to a single per-file status for eligibility.** Model it as coverage:
  ```
  required_engines            = {chromaprint, panako}          # config, not inferred from rows
  done_engines(f)             = {e : succeeded_at IS NOT NULL}
  done(f, fingerprint)        ⇔ done_engines(f) ⊇ required_engines
  eligible(f, fingerprint)    ⇔ (required_engines − done_engines(f)) ≠ ∅ ∧ ¬in_flight ∧ attempts < MAX
  ```
  Keep a separate, explicit `partially_done` display state. This preserves D-16 *and* survives adding an engine. The `fingerprint_file` job is already keyed per-file (`fingerprint_file:<file_id>`, one job, both engines) — so the *job* stays per-file, only the *predicate* becomes per-engine. No queue change.
- **Add a CHECK constraint on every status column feeding a derivation** (`fingerprint_results.status`, `cloud_job.status` already has one, `execution_log.status`). Then write a test that asserts `set(CHECK values) == done_values ∪ failed_values ∪ in_flight_values` — i.e. **the classification is total**. This project already does exactly this for stages (`_DOMAIN_COMPLETED_STAGES` XOR live-keys-only, "asserted in a test against `_KEY_BUILDERS` so no stage is silently undefined (T-45-17)"). Apply the same discipline to status values.
- **Prefer complement-of-terminal over allowlist-of-good.** `done` should be `succeeded_at IS NOT NULL`; `failed` should be `failed_at IS NOT NULL`; anything else is `not_started`. There is no third string to typo.

**Warning signs:**
- A `fingerprint.done` or `.failed` counter is exactly `0` forever (the PR #189 signature).
- `SELECT DISTINCT status FROM fingerprint_results` returns a value not in either allowlist.
- Adding an engine causes zero re-fingerprints (or 200K of them).
- A file shows `fingerprint: done` in the DAG and has zero `panako` rows.

**Phase to address:** **DERIV** (per-engine coverage + the totality test), **MIG** (`032` adds the CHECK constraints and `succeeded_at`), **READ** (`get_fingerprint_pending_files` / `get_fingerprint_progress` rewrite).

---

### Pitfall 6: Information the scalar encoded that the set does not — and the two facts that are *actively misleading*

**What goes wrong:**
An enum encodes **order** ("we got at least this far") and **mutual exclusion** ("and nowhere else"). A derived set encodes neither. Some of what is lost is genuinely dead; some is load-bearing; and — the dangerous case — **some enum values assert a fact that is not true**, so derivation *changes behavior* rather than preserving it.

Audited against the tree:

| Enum value | What it actually means today | What derivation gives you | Verdict |
|---|---|---|---|
| `FINGERPRINTED` | **Only writer** is `routers/pipeline.py:935` (`retry_analysis_failed`), rolling a file *out of* `ANALYSIS_FAILED`. **It does not imply a fingerprint succeeded.** | `fingerprint: not_started` → the file is re-fingerprinted | Design §6.1 calls this out. **Correct, intended, must be in the migration docstring.** Note it also silently *changes* `get_fingerprint_pending_files` (which excludes `state != FINGERPRINTED`) and `get_fingerprint_progress` (which counts `state == FINGERPRINTED`) — the latter is a **progress bar that has been reporting the count of analysis-retry rollbacks**, not fingerprints. |
| `EXECUTED` | **Zero writers in `src/`.** The apply path writes `MOVED`/`UNCHANGED`. | n/a | 10 gates in `tag_writer`/`review`/`tags`/`cue`/`tracklists` are **permanently dead**. Deleting them *turns tag writing on for the first time*. That is a behavior change with real filesystem side-effects. **Do not treat as a free cleanup.** |
| `FAILED` | Zero writers, zero readers | n/a | Genuinely dead. |
| `_TERMINAL_FILE_STATES` (`services/proposal.py:39`) | `{APPROVED, REJECTED, EXECUTED, DUPLICATE_RESOLVED}` — **omits `MOVED`/`UNCHANGED`** (latent bug 5) | must become an explicit predicate | The *set* must be reconstructed, not derived from "the file has a proposal." |
| `DUPLICATE_RESOLVED` | 9 exclusion filters in `services/dedup.py` (`state != DUPLICATE_RESOLVED`) | nothing | Needs a real dedup marker (design agrees). Note all 9 are **negative** filters — a missing marker silently *includes* resolved duplicates in every dedup query, which is a visible-but-plausible-looking regression, not a crash. |
| `search_queries.py:88` `state == file_state` | A **user-facing search facet**. | nothing | Not in §7's reader list. The facet must be redefined over derived status or removed. |
| Ordering (`ANALYZED` ⇒ push succeeded) | `_select_done_push_ids` relies on `state IN (PUSHED, ANALYZED, ANALYSIS_FAILED)` because "ANALYZED can only happen after a successful push" | `analysis_completed_at IS NOT NULL` does not imply a push happened (local analysis exists) | The implication must be re-derived from `cloud_job`, not from analyze-done. |

**Why it happens:**
Nobody audits an enum's *writers* before deleting it; they audit its *readers*. A value with zero writers reads as "a state we support" in every reader you inspect.

**How to avoid:**
- **Run a writer census before the reader cutover, and publish it.** Mechanically:
  ```sql
  SELECT state, count(*) FROM files GROUP BY state ORDER BY 2 DESC;   -- on the LIVE corpus
  ```
  Any value with a non-trivial live count and no `src/` writer is a value you are misunderstanding. Any value with zero live rows and live readers is a dead gate. Cross this against `git grep -n "FileState\.<VALUE>"` split into write-sites (`values(state=`, `state=FileState.`) and read-sites.
- **The shadow-compare gate (§6.2) must assert implication in BOTH directions for the values you claim are preserved**, and explicitly enumerate the ones you claim diverge. The design says "assert implication, not equality" — correct — but the divergence list must be a *closed set declared up front* (`FINGERPRINTED` only), so any *other* divergence is a hard failure, not a shrug.
- **Treat `EXECUTED`-gate deletion as its own change with its own tests.** Ten dead gates coming alive at once, in code that writes ID3 tags and CUE files to disk, is not a refactor.

**Warning signs:**
- The live `GROUP BY state` histogram has a value your reader inventory says is impossible.
- After the cutover, `tag_writer` starts doing work.
- Dedup pages show duplicates the operator already resolved.

**Phase to address:** **READ** (writer census as a gate before any reader changes), **MIG** (shadow-compare divergence list), and a **separate** phase or explicit deliverable for the `EXECUTED`-gate revival.

---

### Pitfall 7: Anti-join performance — the partial indexes in the design **will not be used by the queries they were added for**

**What goes wrong:**
Empirically reproduced against the project's own PG 18.4 test container (200K `files`, 60% fingerprinted, 50% with an `analysis` row):

```
-- The design's proposed index:
CREATE INDEX ix_fprint_success ON fingerprint_results(file_id) WHERE status IN ('success','completed');

-- The pending count the /pipeline/stats poll would run:
EXPLAIN SELECT count(*) FROM files f
WHERE NOT EXISTS (SELECT 1 FROM fingerprint_results r
                  WHERE r.file_id=f.id AND r.status IN ('success','completed'));

 Parallel Hash Anti Join
   ->  Parallel Seq Scan on files f
   ->  Parallel Hash  ->  Parallel Seq Scan on fingerprint_results r
```

**The partial index is not used — with literal constants, on correct statistics, at the target corpus size.** It cannot be: the query touches all 200K outer rows, and a hash anti-join over two seq scans is genuinely cheaper than 200K index probes. Measured: **85 ms**. `analysis` pending: **53 ms**. Six such counts ≈ **300–500 ms per poll, every 5 s**.

Two further, sharper findings:

**(a) Parameterized predicates cannot match a partial index — at all.** PostgreSQL docs are explicit: *"Matching takes place at query planning time, not at run time. As a result, parameterized query clauses do not work with a partial index."* Reproduced with the partial index as the only available index:

```
-- literal status, force_generic_plan:
  ->  Index Only Scan using ix_fp2_success on fp2 r
-- parameterized status ($2,$3) -- which is EXACTLY what SQLAlchemy's .in_(("success","completed")) emits:
  ->  Seq Scan on fp2 r
        Filter: ((status = ANY (ARRAY[$2, $3])) AND (file_id = $1))
```
`= ANY(ARRAY['success','completed'])` with **literals** does use the index. The distinction is literal-vs-parameter, not `IN`-vs-`ANY`.

**Why this bites *this* project specifically:** SQLAlchemy's asyncpg dialect *"utilizes `asyncpg.connection.prepare()` for all statements, caching these prepared statement objects... default size of 100"* (SQLAlchemy 2.0 docs). phaze reaches Postgres **through PgBouncer in SESSION mode** (`src/phaze/database.py:28`) — every client connection pins one long-lived upstream server connection, so those named prepared statements persist and accumulate executions. PostgreSQL switches a prepared statement to a generic plan after 5 executions if the generic cost isn't worse. In **CI**, every test opens a fresh connection and executes each statement once → always a custom plan → the parameter is a `Const` at planning time → the partial index matches → the test is fast and green. In **prod**, the same statement is executed thousands of times on a pinned connection. *(The generic-plan flip is cost-gated, so PG usually keeps the cheaper custom plan — I could only force the seq scan with `plan_cache_mode=force_generic_plan`. Severity: **latent footgun, MEDIUM confidence that it fires in practice**; the deterministic finding is that the predicate form decides whether the index is even *eligible*.)*

**(b) Stale statistics on a freshly-created marker table produce a nested-loop anti-join.** Reproduced: a marker/sidecar table created by migration `032` and `ANALYZE`d at 0 rows plans:
```
Nested Loop Anti Join
  ->  Parallel Seq Scan on files f
  ->  Seq Scan on marker m        -- inner seq scan, per outer row
```
Forced nested-loop over the real 200K corpus measured **452 ms** vs **85 ms** for the hash anti-join. PG partially self-corrects (it scales `reltuples` by `relpages` growth, and `ANALYZE` sends a relcache invalidation that flushes cached plans), and autovacuum's analyze threshold for an initially-empty table is ~50 rows, so the window is short. Severity: **moderate, not catastrophic** — but it lands exactly in the minutes after `032` backfills 200K marker rows, on a 5 s poll.

**(c) Observed in the test container:** a parallel hash anti-join **errored** with
```
ERROR: could not resize shared memory segment "/PostgreSQL.4007709500" to 1048576 bytes: No space left on device
```
Docker's default `/dev/shm` is 64 MB. `_safe_count` catches this and returns **0**. See Pitfall 10.

**How to avoid:**
1. **Do not count pending by anti-joining `files`.** Count the *output tables* — which `get_stage_progress` already does and which is an **Index Only Scan** (`count(DISTINCT file_id) FROM fingerprint_results WHERE …` → `Index Only Scan using ix_fprint_success`, 76 ms including the DISTINCT). Then `pending = total − done − in_flight + |done ∩ in_flight|`. **Be careful**: naive `total − done` is wrong the moment a done file is also in-flight (a retry). Either make the sets disjoint by construction or compute the intersection. This is where people ship an off-by-N badge.
2. **Make `done` predicates `IS NOT NULL` timestamp discriminators** (Pitfall 4). `IS NOT NULL` is syntactically un-parameterizable, so the partial index is always eligible. Verified under `force_generic_plan`:
   ```
   PREPARE nn(uuid) AS SELECT EXISTS(SELECT 1 FROM analysis a WHERE a.file_id=$1 AND a.analysis_completed_at IS NOT NULL);
   ->  Index Only Scan using ix_analysis_completed on analysis a
   ```
   This single design choice eliminates (a) entirely.
3. **Where a `status IN (...)` predicate must remain, emit literals**, not binds: `sa.text("status IN ('success','completed')")` or `bindparam(..., literal_execute=True)`. Add a test that the compiled statement contains the literal, not a placeholder.
4. **The *pending set* query (the trigger) is cheap and different from the *pending count* (the poll).** With `LIMIT 500` the planner picks a `Merge Anti Join` over two index-only scans: **7 ms**. So batch the triggers (`LIMIT`) rather than materializing all 200K `FileRecord` ORM objects — which `get_metadata_pending_files` does today (it returns *every* music/video file). Under the new model that becomes a 200K-row ORM materialization on every trigger click.
5. **`ANALYZE` the new tables at the end of migration `032`, after the backfill.** One line. Prevents (b).
6. **`SET max_parallel_workers_per_gather = 0` on the poll path** (or raise `--shm-size`). Prevents (c) and makes poll cost predictable.

**How to catch regressions in CI vs only in prod:**
- CI cannot reproduce this. A 12-row test table plans nothing like a 200K-row table, and a fresh connection never reaches a generic plan.
- **Add a `tests/integration/` "plan guard"**: seed 50K rows (fast with `generate_series`), `ANALYZE`, then assert on `EXPLAIN (FORMAT JSON)` — specifically that no node is `"Node Type": "Seq Scan"` on the output table, and that `ix_analysis_completed` / `ix_fprint_success` appear in the plan for the single-file probe. This project already gates on machine-asserted structural facts (the KDEPLOY-06 verb-floor assertion against the kr8s call graph); the machinery exists.
- **Add a `force_generic_plan` variant of the same test.** That is the *only* way to catch the parameterized-predicate class in CI. It is one `SET` statement.
- **Record the measured `/pipeline/stats` wall time in the phase VERIFICATION doc**, as §11 already requires. Give it a number and a ceiling (suggest: **≤150 ms p95 for the whole poll**, one round trip).

**Warning signs:**
- `pg_stat_statements` shows `mean_exec_time` on the stats query climbing with corpus size.
- `EXPLAIN` in prod (`auto_explain`) shows `Seq Scan` where CI shows `Index Only Scan`.
- The DAG's busy pills lag the actual queue by seconds.
- `could not resize shared memory segment` in Postgres logs, correlated with a stage counter reading 0.

**Phase to address:** **DERIV** (predicate shape + index design + the plan-guard test), **MIG** (`ANALYZE` after backfill), **UI** (one round trip, see Pitfall 10).

---

### Pitfall 8: Live-migration deploy ordering — the two-step gate does **not** protect you unless writers keep dual-writing

**What goes wrong:**
The design's sequence is: `032` additive (+ backfill + indexes) → shadow-compare on the live corpus → `033` destructive (drop `ix_files_state`, drop `files.state`). That is the right *shape* and it is still unsafe as literally specified, for four independent reasons:

1. **`files.state` is `nullable=False` with a Python-side `default=` and NO server default** (`models/file.py:87`). The moment a deploy stops setting `state` in an INSERT path — and `bulk_upsert_files` / `agent_files.py` build explicit `pg_insert` value dicts — every file insert raises `NotNullViolation`. Any intermediate deploy that removes `state` from the writers but not from the schema **breaks ingestion**.

2. **The reverse ordering is worse, and it is the one the two-step invites.** If readers switch to derivation while writers keep writing, you are safe. If writers stop and the column stays, `files.state` **freezes and goes stale**. Now the "safety net" is a lie: rolling back to the previous release resumes reading a stale enum on a corpus that has advanced for hours. Every file mis-gates. The rollback is more destructive than the failure it's rolling back from. **This is the single most important sentence in this document: a two-step migration only protects you if the intermediate state is *dual-written*, not merely *dual-present*.**

3. **Backfilling from a column that in-flight code is still writing.** `032` reads `files.state` to seed the analyze failure marker, the dedup marker, and the cloud sidecar. During the backfill, live callbacks are flipping `DISCOVERED → ANALYSIS_FAILED`, `PUSHING → PUSHED`, etc. A file that becomes `ANALYSIS_FAILED` one second after the backfill's snapshot gets **no failure marker** → reads `not_started` → is eligible → is re-enqueued → a 4-hour re-analysis of a file that has already been proven un-analyzable. That is the 44.5K incident's mechanism at a smaller scale, delivered by the migration itself. The design mitigates only the *cloud-push* mid-flight case ("drain the cloud-push lanes").

4. **`downgrade()` is mandatory and mirrored in this repo** ("Migrations: sync, mirrored `downgrade()`, integration test per migration"). A `033.downgrade()` that recreates `state String(30) NOT NULL DEFAULT 'discovered'` **resets a 200K-file, months-long corpus to `DISCOVERED`**. It will pass the migration integration test (the column comes back!) and destroy the corpus if anyone ever runs it.

**How to avoid:**
- **Sequence, explicitly:**
  1. `032` **additive only**: marker columns (`*_completed_at`, `failed_at`, `error_message`, `attempts`), sidecar rows, CHECK constraints, partial indexes. Then `ALTER TABLE files ALTER COLUMN state SET DEFAULT 'discovered'` (**a server default**, so a later code path that omits it cannot break inserts). Then `ANALYZE` the touched tables. **Do not touch `files.state` data.**
  2. Deploy **readers-on-derivation, writers-still-dual-writing**. `files.state` continues to advance exactly as today. This deploy is fully rollback-safe: both representations are live and correct.
  3. **Shadow-compare** runs against this deploy on the live corpus, for at least one full cycle of every stage (a long analyze is hours; a full corpus sweep is not required, but every *transition* must be observed at least once). Committed as a runnable check, per the design — plus a **counter of divergences per class**, not a boolean.
  4. `033`: in **one transaction**, (a) `CREATE TABLE files_state_archive AS SELECT id, state, now() FROM files;` (b) re-run the delta backfill for anything that changed since `032` — the `DROP COLUMN`'s `ACCESS EXCLUSIVE` lock quiesces writers *for you*, so a backfill in the same transaction as the drop is atomic w.r.t. concurrent writers; (c) `DROP INDEX ix_files_state; ALTER TABLE files DROP COLUMN state;`.
     Set `SET lock_timeout = '3s'` and retry: the `ACCESS EXCLUSIVE` lock queues behind the 5 s poll's `AccessShare` locks *and blocks everything behind it while it waits*. Without `lock_timeout` a single long query converts a catalog-only DDL into a site-wide outage.
  5. Deploy the writer-deleting code **after** `033` (or in the same release, since the ORM will `UndefinedColumn` on every `SELECT files.state` the instant the column is gone — SQLAlchemy selects columns explicitly, so this is a hard 500 on every file query, not a silent NULL).
- **`033.downgrade()` must restore from `files_state_archive`, or raise.** A downgrade that cannot faithfully reconstruct must `raise NotImplementedError("irreversible; restore files.state from files_state_archive")`. The migration integration test must assert **round-trip fidelity on a seeded multi-state corpus**, not merely that the column reappears.
- **Keep `files_state_archive` for at least one milestone.** 200K × ~40 bytes ≈ 8 MB. It is the only rollback net; once the enum is gone it cannot be reconstructed from output tables (see Pitfall 6: `FINGERPRINTED` is unreconstructable *by design*).
- **Quiesce list, explicit, in the release runbook.** Not just "cloud-push lanes":
  | Mid-flight thing | Why it must be quiesced or handled |
  |---|---|
  | `PUSHING` / `cloud_job.status='uploading'` | mid-rsync / mid-S3 multipart; `--profile drain` (the mechanism exists) |
  | `PUSHED` / `SUBMITTED` / `RUNNING` | a Kueue pod or compute agent is running a **4-hour** analysis and will POST a callback to a control plane whose schema changed |
  | `LOCAL_ANALYZING` | in-flight local `process_file` |
  | `AWAITING_CLOUD` | the routing decision that has no sidecar yet |
  | the 5 s poll | pause the UI or accept `lock_timeout` retries |
  | the `*/5` `reconcile_cloud_jobs` + `stage_cloud_window` crons | they *write* state |

  Note that "quiescing a 4-hour analysis" means either waiting up to 4 hours or accepting that a callback will arrive post-migration. The callback path must therefore be **schema-compatible across the drop** — i.e. `put_analysis` must already have stopped writing `state` before `033` runs, which is exactly step 2's dual-write. Dual-write means *keep writing*; the callback must tolerate the column being gone. Resolve this by having step-2 code read `state` never, write `state` always, and guard every `state` write behind a single `_LEGACY_STATE_WRITES_ENABLED` flag flipped off between step 4 and step 5. **Flag-guarded dual-write is the only ordering that is safe in both directions.**

**Warning signs:**
- `NotNullViolation: null value in column "state"` in the scan/ingestion path.
- `UndefinedColumn: column files.state does not exist` after a rollback.
- The shadow-compare's divergence count is non-zero for a class other than `FINGERPRINTED`.
- `033` hangs (lock queue) and every request 504s.

**Phase to address:** **MIG**, and it is the phase most likely to need its own research + a rehearsal against a `pg_dump` restore of the real corpus. Flag it.

---

## Moderate Pitfalls

### Pitfall 9: SAQ's `saq_jobs` — a 2-value allowlist against a 7-value status enum, and the dedup rule nobody read

**What goes wrong:**
Every phaze `saq_jobs` probe whitelists `status IN ('queued','active')` (`_STAGE_BUSY_SQL`, `_LIVE_KEYS_SQL`, `_BACKFILL_SAQ_JOBS_SQL`, `count_inflight_jobs`). SAQ's `Status` enum has **seven** members: `new, queued, active, aborting, aborted, failed, complete` (`saq/job.py:26`).

Read directly from the installed `saq/queue/postgres.py:_enqueue`:
```sql
INSERT INTO saq_jobs (...) VALUES (...)
ON CONFLICT (key) DO UPDATE SET ...
WHERE saq_jobs.status IN ('aborted','complete','failed')
  AND %(scheduled)s > saq_jobs.scheduled
RETURNING 1
```
`_enqueue` returns `None` when no row is returned. So **SAQ's real dedup rule is: a re-enqueue is silently dropped whenever the existing key's row is in `new`, `queued`, `active`, or `aborting`.**

phaze's `in_flight` predicate covers only `queued|active`. A row in `new` or `aborting` therefore reads **not in-flight** → the derived status is `not_started` → the file is eligible → the trigger enqueues it → **SAQ silently returns `None` and does nothing**. The file is eligible forever and never runs. Invisible: no error, no log, the enqueue count just says "skipped."

This is the *same class* as the `'success'` vs `'completed'` bug (PR #189): an **allowlist of known-good values** against an enum you don't own. It has already cost this project one production bug.

Secondary, lower-severity: `"scheduled": job.scheduled or int(now_seconds())`, so two immediate re-enqueues of a terminal key **within the same wall-clock second** hit `now > now` = false and the second is dropped. Irrelevant at human trigger rates; relevant in a tight test loop.

**How to avoid:**
- **Define `in_flight` as the complement of SAQ's own re-enqueue-allowed set**, not as an allowlist:
  ```sql
  status NOT IN ('complete','failed','aborted')
  ```
  This is exactly SAQ's `_enqueue` guard, inverted, so `in_flight ⇔ "a re-enqueue would be a no-op"` — which is the property `eligible()` actually needs. Pin it with a test that reads `saq.job.Status` and asserts the two sets partition it.
- **Pin the SAQ version and add a totality test over `saq.job.Status`** (`assert set(Status) == IN_FLIGHT | TERMINAL`). SAQ is `>=0.26.3` in this project's stack; a minor bump adding a status silently reopens the hole.
- **Keep honoring the existing hard constraints**: `saq_jobs` reads are static SQL, wrapped in `session.begin_nested()`, never referenced from Alembic. All still true.
- **`scheduled = 9999999999` parked jobs read as `queued` and therefore `in_flight`.** This is documented, intentional, and correct — but it means **a paused stage's files are all `in_flight` and all ineligible**. Assert this explicitly in a test, and make sure the UI says "paused," not "running," for them (the derived status cannot distinguish; the pause flag lives in `pipeline_stage_control`).

**Warning signs:**
- Trigger reports "N skipped, 0 enqueued" while the pending count is N.
- `SELECT DISTINCT status FROM saq_jobs` returns a value outside `{queued, active, complete, failed, aborted}`.

**Phase to address:** **DERIV**.

---

### Pitfall 10: The poll — degrade-to-zero is *directionally unsafe* once counts drive enqueues, and 6 anti-joins × 5 s × PgBouncer session mode is the last incident wearing a new hat

**What goes wrong:**

**(a) Degrade-to-zero masks breakage, and now it also causes it.** `_safe_count` returns `0` on any exception; `get_stage_busy_counts` returns all-zeros; `get_live_job_keys` returns the empty set; `get_stage_controls` returns defaults. Today these are all *cosmetic* — they feed cards. After this milestone they feed **eligibility**. And the degradation direction is exactly wrong:

| Probe | Degrades to | Derived meaning | Consequence |
|---|---|---|---|
| `done` count | `0` | "nothing is done" | operator sees a reset pipeline; **and if `pending = total − done`, everything is pending** → enqueue the corpus |
| `in_flight` | `0` / `{}` | "nothing is running" | every in-flight file is eligible → re-enqueue storm (dedup saves you *only* for keys still `queued/active`; see Pitfall 9) |
| `failed` | `0` | "nothing failed" | the poison 4-hour file is eligible again → the 44.5K incident's payload |

I reproduced a real trigger for this: a `Parallel Hash Anti Join` over the 200K corpus in the project's own Docker test Postgres raised `could not resize shared memory segment ... No space left on device` (Docker's default 64 MB `/dev/shm`). `_safe_count` would swallow that and return `0`. **The exact query this milestone introduces can fail, in this project's own container config, in a way that is silently indistinguishable from "the pipeline is empty."**

**(b) The poll's query count.** `pipeline_stats_partial` already issues ~12 sequential round trips (`get_pipeline_stats`, `get_queue_activity`, `_build_dag_context` → `get_stage_progress` (12 `_safe_count`s) + `get_stage_busy_counts` + `get_stage_controls`, `get_straggler_count`, `get_analysis_failed_count`, `get_awaiting_cloud_count`, `get_pushing_count`, `get_pushed_count`, `get_inadmissible_count`, `get_cloud_phase_counts` (×4), …). Replacing one `GROUP BY state` with six anti-joins adds ~300–500 ms of *server* time per poll (measured, warm cache, local SSD, no PgBouncer hop).

**(c) PgBouncer.** phaze reaches Postgres through PgBouncer in **session mode** (`database.py:28`): *"every client connection pins one upstream server connection for its whole lifetime; the shared (phaze,phaze) session pool (cap ~55) deadlocked under normal multi-worker load and /health hung behind the exhausted pool."* The pool cap was raised 55→75 and app-side pools were leaned (PR #221).

Session mode means the number of pinned server connections is driven by **concurrent SQLAlchemy checkouts**, and slow polls increase concurrency: a poll that takes 500 ms instead of 60 ms overlaps with the next tab's poll, so more sessions are checked out simultaneously. Add browser tabs × api workers × a 5 s tick and you walk straight back into `cl_waiting > 0`. Additionally each `Parallel Hash Anti Join` launches **parallel workers**, which are *additional backends* invisible to PgBouncer's accounting but bounded by `max_worker_processes`.

**How to avoid:**
- **Degrade to `None`, render `—`, and set a `degraded: true` flag** on the stats payload; render an amber chip. This project already has the pattern (`localqueue_unreachable` amber alert). **Never let a degraded probe produce a number an operator cannot distinguish from a real one.**
- **`eligible()` must fail closed.** Unknown `in_flight` ⇒ treat as in-flight. Unknown `done` ⇒ treat as done. Unknown `failed` ⇒ treat as failed. A trigger that cannot evaluate its predicates must **enqueue nothing and say so**, not enqueue everything. Precedent exists: `Backend.in_flight_count` was made deliberately non-degrade-safe in Phase 69 D-05 "so the drain never over-dispatches on a transient error." Extend that principle to every enqueue-driving probe.
- **One round trip for the whole poll.** Measured: six separate anti-join counts summed to ~263 ms; the same six folded into one statement with scalar subqueries was **140 ms** and one round trip. Better still, count the *output tables* (index-only scans) instead of anti-joining `files`.
- **Cache the stats payload server-side for ~2–3 s** (a process-local TTL, or an ETag on the HTMX fragment). A 5 s poll from N tabs does not need N executions.
- **`SET LOCAL max_parallel_workers_per_gather = 0; SET LOCAL statement_timeout = '2s';` on the poll transaction.** Bounds both the shm failure and the pool-hold duration.
- **Budget it.** Write the number down: `poll_server_time × concurrent_pollers < pool_size × tick_interval`. At `pool_size=75`, a 5 s tick, and 500 ms polls, you can afford 750 concurrent pollers — comfortable. At 5 s polls (a bad plan, Pitfall 7b) you can afford 75. The margin is the plan, and the plan is not guaranteed.

**(d) One more, specific to session mode + asyncpg:** if anyone "fixes" the pool exhaustion by switching PgBouncer to **transaction mode**, SQLAlchemy's asyncpg dialect breaks — it uses named prepared statements for every statement. The documented fix is `poolclass=NullPool` + `prepared_statement_name_func=lambda: f"__asyncpg_{uuid4()}__"` + PgBouncer `server_reset_query = DISCARD ALL`. Record this so the fix isn't discovered during an incident.

**Warning signs:**
- `SHOW POOLS` → `cl_waiting > 0`, `sv_active == pool_size`.
- `/health` latency tracks `/pipeline/stats` latency.
- A stage counter reads `0` while its output table is visibly non-empty.
- `stage_progress_degraded` / `stage_busy_degraded` warnings in the logs at any nonzero rate.

**Phase to address:** **UI** (poll shape, one round trip, degraded rendering), **DERIV** (fail-closed semantics), **READ** (`get_pipeline_stats` collapse into `get_stage_progress`).

---

### Pitfall 11: The test suite goes vacuous — and this project has already been burned by exactly this

**What goes wrong:**
~50 test files and 201 call sites construct `FileRecord(state=FileState.X)` to set up a scenario. That single kwarg is the *entire* scenario. Replacing it with "insert the right output rows" means a fixture must now write 1–4 rows across `metadata`, `fingerprint_results`, `analysis`, `scheduling_ledger`, and `saq_jobs`.

The failure mode is not a broken test. It is a test that **passes for the wrong reason**:

> A test asserts `file not in get_analyze_pending_files(session)`. It passes. Not because the analyze-failure marker suppressed it — but because the fixture forgot to insert the `files` row's `agent_id`, or wrote `status='succeeded'` instead of `'success'`, or the `metadata` row it inserted has `failed_at` set. The assertion is `assert x not in []`.

This project shipped exactly this pattern and caught it only in code review:

> *"WR-01: the L793 backfill case seeded `with_ledger=False`, filtering the candidate out of the ledger-scoped backfill query so the test passed vacuously even with the gate deleted — fixed to `with_ledger=True` and mutation-verified to genuinely guard L793; `len(rows)==1` no-mutation signal."* (Phase 75)

Negative assertions over multi-table derived predicates are a vacuous-pass factory. Under the current enum, `state=ANALYSIS_FAILED` is one fact and it is either right or a `TypeError`. Under derivation, "not pending" has four independent ways to be true and only one of them is the one under test.

**A second, subtler trap:** if the fixture builder derives its inserts from the same `STAGE_PREDICATES` constant the query builder uses, the test is a **tautology**. It can never detect a wrong predicate. Fixtures must write *facts* (rows with literal column values), never call the derivation layer.

**How to avoid:**
- **A-B pairs with a one-fact difference, mandatory for every negative assertion.** For each "X is not eligible" test, a sibling asserts "X *is* eligible when *only* the distinguishing fact is flipped." Both in the same test function, so they cannot drift.
- **Positive control on the intermediate set.** Before `assert target not in result`, assert `len(result) == N` with a known-eligible decoy present. That is the `len(rows)==1` no-mutation signal Phase 75 landed on, generalized: **a negative assertion must be accompanied by a positive one from the same query.**
- **A committed mutation harness for the derivation module only.** No mutation tool is in `pyproject.toml` today. Adding `mutmut` (or `cosmic-ray`) scoped to `src/phaze/services/stage_status.py` is cheap and bounded — one module, pure functions plus SQL builders. A cheaper, project-idiomatic alternative that has already proven itself here: a **`just mutate-derivation` recipe** that programmatically deletes each guard clause and asserts the corresponding test fails. Phase 75's reviewer did this by hand. Automate it once.
- **A table-driven oracle.** `stage_status()` is a pure function of five booleans (`has_output_row`, `output_completed`, `has_failure_marker`, `has_live_job`, `has_ledger_row`). Enumerate all 32 combinations, hand-write the expected answer once, and assert the *SQL* implementation agrees with the *Python* implementation on real DB rows. This is the only technique that catches "the SQL and the Python drifted."
- **A `make_file_at(stage_states={"metadata": "done", "fingerprint": "failed"})` builder** that writes literal rows. One place to keep in sync. Then a guard test asserts the builder's output actually produces the requested `stage_status` — which is the one place a tautology is *desirable*, because it validates the builder, not the predicate.
- **Per-bucket isolation is a hard constraint here** (`just test-bucket <bucket>`). Derived-status fixtures span `metadata`/`fingerprint`/`analyze` buckets; the shared builder must live in `tests/conftest.py` or `tests/shared/`, and `tests/shared/test_partition_guard.py` must stay green.
- **Delete the enum from tests *last*, not first.** During the dual-write phase, tests can assert *both* representations agree — that is the shadow-compare gate, unit-scale, for free.

**Warning signs:**
- A test's assertion still passes when you `git stash` the production guard it claims to test. (Check this by hand for every guard you're proud of.)
- A negative assertion with no positive assertion in the same test.
- A fixture that imports from `phaze.services.stage_status`.
- Coverage stays at 90% while the derivation module's branch coverage is 60%.

**Phase to address:** **TEST**, running *concurrently with* **DERIV** (the oracle and builder must exist before the first reader is cut over), with the mutation harness as a named deliverable.

---

### Pitfall 12: The UI renders internal state strings, and Jinja hides the breakage

**What goes wrong:**
`templates/pipeline/partials/metadata_workspace.html:43,50` renders `f.state` **as the literal column value** into a "State" column, and gates a color on `f.state in ("metadata_extracted","fingerprinted","analyzed")`. `analyze_workspace.html:81-86` branches on `f.state == 'awaiting_cloud'` / `'analysis_failed'`. `proposals/partials/proposal_row.html:46` branches on `proposal.file.state == "executed"` — a value **nothing writes** (Pitfall 6), so that branch is dead and its `{% else %}` is the only live path.

When `state` is deleted, Jinja's default `Undefined` renders as the **empty string** and compares `False`. So:
- The "State" column silently becomes blank for 200K rows.
- The `awaiting_cloud` / `analysis_failed` badges silently stop rendering.
- `proposal_row`'s dead branch stays dead — no signal.

Nothing 500s. Nothing logs. The UI just quietly stops telling the truth. And because the derived model's whole *point* is that the DAG becomes the operator's window into a months-long process, a silently-blank status column is a serious defect, not a cosmetic one.

**How to avoid:**
- **A presentation-layer `stage_badge(status)` mapping** with an explicit `raise` on an unknown status. Never render a raw derived value.
- **`jinja_env.undefined = StrictUndefined`** for the templates touched by this milestone (or globally, if the blast radius is tolerable). This converts every silent blank into a loud `UndefinedError` at render time, which the existing test suite will catch.
- **A grep/AST guard test**: no template may reference `\.state\b` on a `FileRecord`. The repo already ships a "dead-template entry-literal check" (Phase 66, D-14) — extend it.
- **The derived status is a *set*, and the table has one column.** Decide the display collapse deliberately: `metadata ✓ · fp ⚠ · analyze ⏳` (three chips) is honest; a single word is not. The whole milestone exists because one scalar cannot describe a file.
- **A "paused" file is `in_flight` (Pitfall 9).** The UI must read `pipeline_stage_control.paused` and say "paused," or operators will think the pipeline is running when it is parked at `scheduled=9999999999`.

**Warning signs:**
- A table column is uniformly empty.
- A badge that used to appear never appears.
- `StrictUndefined` errors in the test suite (this is the *good* outcome).

**Phase to address:** **UI**.

---

## Minor Pitfalls

### Pitfall 13: Metadata's pending set changes semantics, and `is_domain_completed` is structurally inert *because* of the old semantics

`get_metadata_pending_files` returns **every** music/video file, forever, relying on deterministic-key dedup. `reenqueue.is_domain_completed`'s metadata branch (`reenqueue.py:266`) is `fid not in done_sets[_METADATA_PENDING]` — which, since the pending set is *everything*, can never be true. It is dead code that looks alive, and `report_metadata_failed` was added (CR-02) specifically to compensate for it. Changing the pending set to `¬done ∧ ¬in_flight` makes that branch live for the first time, simultaneously. **Do not change these in separate phases.** Design D-04 says this; the point here is that the dead-branch-becomes-live transition is the risk, not the pending-set change.

*Prevention:* one phase, one PR, with a test that a metadata-failed file is (a) not in the pending set and (b) `is_domain_completed → True`. **Phase: READ.**

### Pitfall 14: `execution_log` has no `file_id`, and a file can have several proposals

`done(apply)` joins `execution_log → proposals`. A file with an approved-then-failed proposal A and an approved-then-completed proposal B has two rows with different `status`. `get_stage_progress`'s existing `execute_done_stmt` already filters `ExecutionLog.status == COMPLETED` — good. But `_TERMINAL_FILE_STATES` and `store_proposals`' regression guard (latent bug 5, `MOVED`/`UNCHANGED` omitted) need an explicit "this file has been applied" predicate, and "has any execution_log row" is not it.

*Prevention:* define `applied(f) ⇔ EXISTS(execution_log e JOIN proposals p ON e.proposal_id=p.id WHERE p.file_id=f.id AND e.status='completed')`, and make it the single source for all four call sites. **Phase: READ.**

### Pitfall 15: Rescan's `ON CONFLICT DO UPDATE SET state = excluded.state` disappears — verify nothing *depended* on the reset

`ingestion.py:114` / `agent_files.py:132` reset any file to `DISCOVERED` on rescan. The design correctly calls this a bug (progress-wiping). But it is also, today, **the only way to un-stick a file from the enrich deadlock** (§1.1). Operators may have a habit of rescanning to unstick things. After the change, rescan is a no-op for status, which is correct — but the "unstick" affordance must be replaced by an explicit per-file retry, or the operator's mental model breaks.

*Prevention:* ship a per-file / per-stage "reset stage" action alongside. **Phase: UI + MARK** (resetting analyze = clearing the failure marker).

### Pitfall 16: `pg_dump`/restore and `saq_jobs`

The shadow-compare and any rehearsal against a corpus dump must account for `saq_jobs` being **SAQ-owned and not Alembic-managed**. A dump/restore of the app schema without `saq_jobs` makes every `in_flight` probe return `false` on the rehearsal DB — which will make the shadow-compare look cleaner than reality, and will make your rehearsed backfill wrong. State it in the runbook.

*Prevention:* rehearse with `saq_jobs` included, or explicitly declare `in_flight` unevaluable on the rehearsal and skip that class of assertion. **Phase: MIG.**

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|---|---|---|---|
| Keep `_safe_count`'s degrade-to-0 for probes that now drive `eligible()` | Zero code change; poll never 500s | Re-creates the 44.5K over-enqueue mechanism with a new trigger | **Never.** Split "display probe" (degrade to `None`) from "decision probe" (fail closed). |
| `in_flight = saq_jobs ∪ scheduling_ledger` with no staleness bound (design D-01 verbatim) | Closes the crash-window in one line | Permanent silent stall on every timed-out `process_file` (Pitfall 1) | Never as-is; acceptable **with** an `enqueued_at` grace bound. |
| Aggregate `fingerprint_results` to one per-file status | One predicate, matches `get_stage_progress` | Kills the per-engine retry (D-16); breaks on adding an engine | Acceptable **only** for the display collapse, never for `eligible()`. |
| `done = EXISTS(output_row)` for metadata (no discriminator) | No migration column | Reintroduces the `analysis` partial-row bug the day metadata gets a failure marker — which is *this milestone* | Never. |
| Skip the `files_state_archive` snapshot | 8 MB and one line saved | The enum is unreconstructable; a bad `033` is unrecoverable | Never. |
| `status IN (...)` with SQLAlchemy `.in_()` against a partial index | Idiomatic ORM | Index is **ineligible** under a generic plan; invisible in CI (Pitfall 7a) | Acceptable if the index predicate is `IS NOT NULL`-shaped instead. |
| Denormalize a stage-bitmap column now | Fast polls | The exact class of bug being deleted, reborn (dual source of truth, drift) | The design's §5 YAGNI is right. Only with a *measured* slow poll recorded in VERIFICATION. |
| Keep `state` writes "just in case" after `033` | Feels safe | Impossible — the column is gone; the code 500s | n/a — this is why flag-guarded dual-write (Pitfall 8) must precede the drop. |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|---|---|---|
| **SAQ / `saq_jobs`** | `in_flight ⇔ status IN ('queued','active')` — a 2-value allowlist against a 7-value enum SAQ owns | `status NOT IN ('complete','failed','aborted')` — the exact inverse of SAQ's own `ON CONFLICT` re-enqueue guard. Pin with `assert set(saq.job.Status) == IN_FLIGHT \| TERMINAL`. |
| **SAQ / Alembic** | Referencing `saq_jobs` from a migration | Every migration since `020` carries the "must NEVER reference `saq_jobs`" banner. Backfills that need it are **control-side runtime reconciles** (`backfill_ledger_from_saq_jobs` is the precedent). |
| **`scheduling_ledger`** | Treating a ledger row as "a job is running" | It means "a job was scheduled and no terminal ack was observed." Best-effort write, best-effort clear, **no clear at all on the agent worker** (no `ledger_sessionmaker`). Bound it by `enqueued_at`. |
| **`scheduling_ledger`** | Assuming recovery's ledger semantics transfer to `stage_status()` | Recovery cross-checks the ledger against a domain-completed predicate. `stage_status()` must too, or Pitfall 1 fires. |
| **PgBouncer (session mode)** | Adding round trips to a 5 s poll and assuming the pool absorbs it | Slow polls raise *concurrent* checkouts, which is what pins server connections in session mode. One round trip; `statement_timeout`; server-side TTL cache. |
| **PgBouncer (transaction mode)** | Switching modes to fix pool exhaustion | Breaks SQLAlchemy+asyncpg (named prepared statements for every statement). Needs `NullPool` + `prepared_statement_name_func` + `DISCARD ALL`. Document before an incident forces the discovery. |
| **asyncpg + partial indexes** | Assuming an `EXPLAIN` from `psql` reflects prod | asyncpg prepares & caches (100/conn); session-pinned connections reach generic plans; parameterized predicates can never match a partial index predicate. Test with `SET plan_cache_mode = force_generic_plan`. |
| **Cloud-burst agents (rsync / S3 / Kueue pods)** | Migrating while a 4-hour analysis is mid-flight | Its callback will arrive after `033`. The callback path must not reference `state` by then → flag-guarded dual-write, flag off before the drop. Drain `PUSHING`/`uploading` via `--profile drain`. |
| **Kueue pods** | Assuming `--profile drain` covers them | It drains the *push* lanes. An admitted Kueue Job keeps running. `reconcile_cloud_jobs` (`*/5`) writes `state` — quiesce the cron too. |
| **Docker Postgres** | Ignoring `/dev/shm` | A `Parallel Hash Anti Join` at 200K rows raised `could not resize shared memory segment` in this repo's own test container. Reproduced. `max_parallel_workers_per_gather=0` on the poll, or `--shm-size`. |

## Performance Traps

Measured on PostgreSQL 18.4 (the project's `phaze-test-db` container), 200 000 `files`, 120 349 fingerprinted, 100 000 `analysis` rows (70 078 completed), warm cache, local SSD, no PgBouncer hop. **Real prod numbers will be worse.**

| Trap | Symptoms | Prevention | When it breaks |
|---|---|---|---|
| `NOT EXISTS` anti-join over `files` for a **count** | `Parallel Hash Anti Join` + two `Seq Scan`s; **85 ms** (fingerprint), **53 ms** (analysis) each; ×6 = 300–500 ms/poll | Count the **output table** instead (`count(DISTINCT file_id) … WHERE <discriminator>` → `Index Only Scan`). Compute `pending` by set arithmetic, handling `done ∩ in_flight`. | Immediately at 200K, on a 5 s poll |
| Partial index + **parameterized** predicate | Plan shows `Seq Scan … Filter: (status = ANY (ARRAY[$2, $3]))`; identical query with literals shows `Index Only Scan`. Never reproduces in CI. | Shape `done` predicates as `*_completed_at IS NOT NULL` (un-parameterizable). Otherwise emit literals. Test under `plan_cache_mode=force_generic_plan`. | Prod only, under session-pinned connections + prepared-statement reuse |
| Freshly-created marker table, stats say 0 rows | `Nested Loop Anti Join` with an inner `Seq Scan`; **452 ms** vs 85 ms (forced-plan measurement) | `ANALYZE <new tables>` as the last statement of migration `032`, after the backfill | The minutes between `032`'s backfill and autovacuum's first analyze |
| Parallel query in a container | `ERROR: could not resize shared memory segment … No space left on device` → `_safe_count` returns **0** | `SET LOCAL max_parallel_workers_per_gather = 0` on the poll; raise `--shm-size` | Observed at 200K in this repo's own test container |
| `get_metadata_pending_files` returning **all** music/video `FileRecord` ORM objects | Trigger click stalls; RSS spikes on the api worker | Batch with `LIMIT` — the `LIMIT 500` anti-join plans as a `Merge Anti Join` over two index-only scans: **7 ms** | Already broken today; the derived model makes it look intentional |
| Six probes × N browser tabs × api workers, session-mode PgBouncer | `SHOW POOLS` `cl_waiting > 0`; `/health` hangs | One round-trip poll (140 ms measured for 6 folded counts vs 263 ms separate) + a 2–3 s server-side TTL cache | Reproduced in prod once already (the PgBouncer incident) |

## Security Mistakes

Small surface — this is an internal refactor — but three real ones.

| Mistake | Risk | Prevention |
|---|---|---|
| Building `saq_jobs.key` predicates by string-interpolating a stage/function name | SQL injection into a table SAQ owns; the codebase's own T-44-05 / T-42-03 discipline forbids f-string SQL | Bound params + a fixed `STAGE_TO_FUNCTION` map. `_backfill_candidates_stmt` already models this (`"process_file:" + cast(FileRecord.id, String)`). |
| Deriving the failure marker's `error_message` straight from an agent-supplied exception string and rendering it | Stored XSS into the admin UI; `error_message` is `Text`, agent-controlled | Escape at render; the repo has a live precedent — `_diff_row.html` needed `\|tojson` not `\|e` for an Alpine JS context. Cap the length at write time. |
| Losing the AUTH-01 guard when replacing state-based CAS in the agent callbacks | A registered agent could advance/clobber another agent's file | The `/mismatch` 403 reporter-identity gate (Phase 73 D-07) and the "key from the PATH `file_id` ONLY" rule (T-45-05) must be preserved verbatim through the WRITE phase. |

## UX Pitfalls

| Pitfall | User impact | Better approach |
|---|---|---|
| Collapsing three parallel stage statuses into one "State" word | The operator cannot see that a file is metadata-done but analyze-failed — **the exact thing this milestone exists to expose** | Three chips per row: `md ✓ · fp ⚠ · an ⏳`. |
| Rendering a degraded probe's `0` as a real count | Operator believes the pipeline reset; may trigger a full re-run of a months-long corpus | `—` + amber "stats degraded" chip; never a number. |
| A paused stage's files rendering as "running" | Operator waits for progress that will never come | Read `pipeline_stage_control.paused`; render "paused." |
| Removing rescan's implicit unstick with no replacement | Operator's only recovery habit stops working | Ship a per-file / per-stage "reset stage" action (clears the failure marker, deletes the stale ledger row). |
| An unbounded fingerprint auto-retry on a poison file | The stage's counter oscillates forever; the operator can't tell it from progress | `attempts < MAX`, then a visible `failed (5 attempts)` terminal chip. |

## "Looks Done But Isn't" Checklist

- [ ] **Derivation layer:** often missing the **fail-closed** degradation path — verify `eligible()` returns `False`, not `True`, when the `saq_jobs` probe raises.
- [ ] **Derivation layer:** often missing the **`saq_jobs` status totality test** — verify `set(saq.job.Status) == IN_FLIGHT ∪ TERMINAL`.
- [ ] **Failure markers:** often missing the **attempt counter** — verify a fingerprint failure cannot re-enqueue unboundedly.
- [ ] **Failure markers:** often missing the **`report_metadata_failed` write** — verify a terminally-failed metadata extraction is visible in the UI and in `stage_progress`.
- [ ] **Failure markers:** often missing the **mutual-exclusion CHECK** — verify `analysis` cannot have both `analysis_completed_at` and `failed_at` set.
- [ ] **Sidecars:** often missing the **uniqueness constraint** that made the enum a CAS token — verify a duplicate `/pushed` callback cannot double-transition.
- [ ] **Sidecars:** often missing a decision on `LOCAL_ANALYZING` — verify a locally-analyzing file cannot become a cloud staging candidate when the `saq_jobs` probe degrades.
- [ ] **Readers:** often missing the **writer census** — verify every one of the 17 enum values has a documented live-corpus row count and a replacement (or a "dead" verdict).
- [ ] **Readers:** often missing `search_queries.py:88` (the user-facing state facet) and `services/fingerprint.py:288` (a progress bar counting analysis-retry rollbacks).
- [ ] **Writers:** often missing the **23 CAS guards** — verify each is classified `{read-filter, display, CAS-guard, dead}` with a named replacement.
- [ ] **Writers:** often missing that deleting the dead `EXECUTED` gates **turns tag/CUE writing on** — verify with an explicit test, not a diff.
- [ ] **UI:** often missing `StrictUndefined` — verify a deleted attribute raises in tests rather than rendering blank.
- [ ] **Migration:** often missing the **server default** on `files.state` in `032` — verify ingestion still inserts if a code path omits `state`.
- [ ] **Migration:** often missing that a **non-dual-writing intermediate deploy makes rollback destructive** — verify the `_LEGACY_STATE_WRITES_ENABLED` flag exists and defaults on.
- [ ] **Migration:** often missing the **delta backfill inside `033`'s transaction** — verify a file that flips state between `032` and `033` gets its marker.
- [ ] **Migration:** often missing a **faithful `033.downgrade()`** — verify round-trip fidelity on a seeded multi-state corpus, or that it raises.
- [ ] **Migration:** often missing `ANALYZE` after backfill — verify with `EXPLAIN` that no anti-join plans a nested loop.
- [ ] **Migration:** often missing the **`4h analyze` quiesce** — verify a callback landing after `033` does not 500.
- [ ] **Tests:** often missing the **positive control** in a negative assertion — verify by deleting the guard and watching the test fail.
- [ ] **Tests:** often missing a **`force_generic_plan` plan-guard** — the only CI-detectable form of Pitfall 7a.
- [ ] **Tests:** often missing per-bucket isolation — verify `just test-bucket metadata|fingerprint|analyze` each pass **alone**.

## Recovery Strategies

| Pitfall | Recovery cost | Recovery steps |
|---|---|---|
| Permanent stall from stale ledger rows (P1) | **LOW** | `DELETE FROM scheduling_ledger WHERE enqueued_at < now() - interval '12 hours' AND key NOT IN (SELECT key FROM saq_jobs WHERE status NOT IN ('complete','failed','aborted'))`. Then re-trigger. The over-enqueue risk is bounded because `eligible` still excludes `done`. |
| Re-enqueue storm from a degraded probe (P2/P10) | **MEDIUM** | Pause all three stages (`pipeline_stage_control.paused`), purge the parked jobs (`DELETE FROM saq_jobs WHERE status='queued' AND key LIKE '<fn>:%'` — note the Phase-32 doubling lesson: purge, then rebuild from the ledger, never enqueue on top), fix the probe, unpause. |
| Panako never retried (P5) | **LOW** | Once per-engine coverage lands, the affected files become eligible automatically. No data loss. |
| Deleted `EXECUTED` gates started writing tags (P6) | **HIGH** | `tag_write_log` is an audit table and tag writes go to *destination copies*. Reverse from the log. But verify the log covers every write before enabling. |
| `033` ran, corpus mis-derived (P8) | **HIGH → LOW with the net** | Restore `files.state` from `files_state_archive`, redeploy the previous release. **Without the archive: unrecoverable.** This is the entire justification for it. |
| `033` hangs on `ACCESS EXCLUSIVE` (P8) | **LOW** | `lock_timeout` makes it abort instead of queueing. Without it: cancel the blocking backend (`pg_cancel_backend`), then retry with `lock_timeout` set. |
| Vacuous test suite discovered post-merge (P11) | **MEDIUM** | Run the mutation harness over `stage_status.py`; every surviving mutant is an untested guard. Cheap because the module is small and pure. |
| PgBouncer exhaustion recurs (P10) | **MEDIUM** | Already runbooked (PR #221 + homelab cap raise). Add: cut the poll to one round trip; TTL-cache the payload. |

## Pitfall-to-Phase Mapping

| # | Pitfall | Prevention phase | Verification |
|---|---|---|---|
| 1 | Ledger-union permanent stall | **DERIV** + **MARK** | Test: a `scheduling_ledger` row older than the stage grace bound does **not** suppress eligibility, and a `done` file with a stale ledger row renders `done`, not `running`. Metric: stalled-ledger count. |
| 2 | `not_started` conflation / four causes | **DERIV** + **MARK** | Test matrix over `{ledger?, saq_jobs status, output row?}` → expected `{enqueue, re-enqueue, terminal}`. Test: unknown-probe ⇒ `eligible == False`. |
| 3 | Enum as CAS token; 23 guards | **WRITE** (+ CHECKs in **MIG**) | A committed `docs/state-cas-inventory.md` with 23 rows, each `{site, classification, replacement}`. Test: duplicate `/pushed` callback is a no-op. |
| 4 | Row-exists ≠ done (2nd order) | **DERIV** + **MARK** + **MIG** | AST guard: no `exists(select(AnalysisResult/FileMetadata))` without its discriminator. Test: `proposals.total` excludes in-flight analyses. |
| 5 | Multi-row fingerprint aggregation | **DERIV** + **MIG** | Test: `chromaprint=success, panako=failed` ⇒ `eligible(fingerprint) == True` (D-16 preserved). Test: `set(CHECK values) == done ∪ failed`. |
| 6 | Lost / misleading enum information | **READ** (writer census gate) + **MIG** | Live-corpus `GROUP BY state` census committed. Shadow-compare declares `FINGERPRINTED` as the **only** allowed divergence class; any other ⇒ hard fail. |
| 7 | Anti-join cliffs / partial-index mismatch | **DERIV** + **MIG** | `tests/integration/` plan guard at 50K rows, run **twice** (`auto` and `force_generic_plan`), asserting index usage. Measured poll p95 recorded in VERIFICATION, ceiling ≤150 ms. |
| 8 | Live-migration ordering & rollback | **MIG** | `_LEGACY_STATE_WRITES_ENABLED` flag exists. `033` does archive → delta-backfill → drop, in one txn, under `lock_timeout`. `033.downgrade()` round-trip test on a seeded multi-state corpus. Rehearsal against a corpus `pg_dump`. |
| 9 | SAQ status allowlist | **DERIV** | `assert set(saq.job.Status) == IN_FLIGHT \| TERMINAL`. Test: a `new`-status row suppresses eligibility. |
| 10 | Degrade-to-zero + poll cost + PgBouncer | **UI** + **DERIV** | Payload carries `degraded: bool`; template renders `—`. Poll is one statement. `statement_timeout` + `max_parallel_workers_per_gather=0` set. Load test: 20 concurrent pollers, `cl_waiting == 0`. |
| 11 | Vacuous tests | **TEST** (concurrent with **DERIV**) | `just mutate-derivation` committed and green. Every negative assertion has a same-function positive control. 32-case table-driven oracle: SQL == Python. |
| 12 | UI renders raw state | **UI** | `StrictUndefined`. Grep guard: no `.state` in templates. Three-chip per-stage rendering. |
| 13 | Metadata pending-set semantics + dead branch | **READ** | Single PR. Test: metadata-failed file ⇒ not pending **and** `is_domain_completed == True`. |
| 14 | `execution_log` has no `file_id` | **READ** | One `applied(f)` predicate, four call sites, one test. |
| 15 | Rescan's implicit unstick | **UI** + **MARK** | A per-stage "reset" action exists and is documented in the runbook. |
| 16 | `saq_jobs` absent from rehearsal dumps | **MIG** | Runbook states it; the rehearsal script either includes `saq_jobs` or skips `in_flight` assertions explicitly. |

**Ordering implication for the roadmap:** the design's §11 sequence is right, with one correction — **TEST must run concurrently with DERIV, not after**, because the oracle and the fact-writing fixture builder are prerequisites for trusting every subsequent phase. And **WRITE must not be "delete the writers"**; it must be "replace 23 CAS guards, then delete the writers," which makes it the highest-risk phase after MIG. Both **MIG** and **WRITE** should be flagged for phase-level research.

---

## Sources

**Primary — this codebase (`main` @ `ce0c6434`, read directly):** `src/phaze/models/file.py` (the enum, `nullable=False`, no server default), `src/phaze/models/analysis.py` (`analysis_completed_at`, migration 028), `src/phaze/models/fingerprint.py` (unique `(file_id, engine)`, unconstrained `status`), `src/phaze/models/scheduling_ledger.py`, `src/phaze/models/execution.py` (no `file_id`), `src/phaze/services/pipeline.py` (`get_stage_progress`, `get_stage_busy_counts`, `_safe_count`, `_STAGE_BUSY_SQL`, `_LIVE_KEYS_SQL`, `_backfill_candidates_stmt`, `get_metadata_pending_files`, `get_fingerprint_pending_files`, `pipeline_stats_partial`), `src/phaze/tasks/reenqueue.py` (the `ANALYSIS_FAILED` "do NOT re-enqueue" warning, `is_domain_completed`), `src/phaze/tasks/_shared/deterministic_key.py` (`apply_deterministic_key`, `increment_completed` — the `ledger_sessionmaker` gate), `src/phaze/routers/agent_{metadata,fingerprint,analysis,push,s3}.py` (the CAS guards, the no-op failure acks), `src/phaze/database.py:26-42` (the PgBouncer **session-mode** incident comment), `src/phaze/templates/pipeline/partials/{metadata,analyze}_workspace.html`. **Confidence: HIGH.**

**Primary — installed SAQ source:** `.venv/.../saq/queue/postgres.py::_enqueue` (the `ON CONFLICT … WHERE status IN ('aborted','complete','failed') AND scheduled > scheduled` dedup rule), `.venv/.../saq/queue/base.py::enqueue` (never sets `job.scheduled`), `.venv/.../saq/job.py::Status` (7 members). **Confidence: HIGH.**

**Primary — empirical, PostgreSQL 18.4 in `phaze-test-db`:** all plans and timings in Pitfall 7 and the Performance Traps table were produced by seeding 200 000 `files` + representative `fingerprint_results` / `analysis` / `metadata` rows and running `EXPLAIN` / `\timing`. Including the reproduced `could not resize shared memory segment` error. **Confidence: HIGH for the mechanism; MEDIUM for absolute timings** (local SSD, warm cache, no PgBouncer hop — real numbers will be worse). The `force_generic_plan` result is HIGH-confidence for *eligibility*; whether `plan_cache_mode=auto` ever actually selects the seq-scan generic plan in prod is **MEDIUM confidence / speculative** (PG's cost comparison usually prevents it).

**Official documentation:** [PostgreSQL — Partial Indexes](https://www.postgresql.org/docs/current/indexes-partial.html) — *"Matching takes place at query planning time, not at run time. As a result, parameterized query clauses do not work with a partial index."* [SQLAlchemy 2.0 — PostgreSQL dialects (asyncpg)](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html) — *"utilizes `asyncpg.connection.prepare()` for all statements, caching these prepared statement objects... default size of 100"*, plus the `NullPool` + `prepared_statement_name_func` PgBouncer workaround. **Confidence: HIGH.**

**Project incidents cited and generalized:**
- **2026-06-18, ~44 500-job over-enqueue** (`tasks/reenqueue.py` module docstring): recovery derived work from complement-of-done pending sets with no record that a stage was ever *scheduled*. → Generalized in Pitfalls 2 and 10: *a decision probe that degrades to "nothing is running / nothing is done" enqueues the corpus.*
- **PR #189 — `fingerprint.done` always 0**: `get_stage_progress` matched `status == "completed"` while writers persist `"success"`. → Generalized in Pitfalls 5 and 9: *never classify with an allowlist of known-good values against an enum you don't own; classify as the complement of the terminal set, and assert totality.*
- **Phase 75 WR-01 — the vacuous-pass regression test**: a fixture flag (`with_ledger=False`) filtered the candidate out of the query under test, so the test passed with the guard deleted. → Generalized in Pitfall 11: *every negative assertion needs a same-function positive control; multi-table derived predicates multiply the ways to pass for the wrong reason.*
- **PgBouncer session-pool exhaustion** (`database.py:26-42`, PR #221): `/health` hung behind an exhausted 55-slot session pool. → Generalized in Pitfall 10: *in session mode, slower queries raise concurrent checkouts, and concurrent checkouts are what pin server connections.*
- **Phase 73 CR-01 (blocker)** — `/mismatch` wrote `state=AWAITING_CLOUD` without the sibling `PUSHING` CAS guard. → Generalized in Pitfall 3: *the enum is a concurrency primitive; deleting it deletes 23 guards.*
- **Phase 69 D-05** — `Backend.in_flight_count` deliberately **not** degrade-safe "so the drain never over-dispatches on a transient error." → The precedent for Pitfall 10's fail-closed rule.
- **Phase 32 queue-doubling** — purge then rebuild from the ledger; never enqueue on top. → Recovery Strategies.

---
*Pitfalls research for: retiring a linear `FileState` enum in favor of derived per-stage status, against a live 200K-file corpus with distributed agents.*
*Researched: 2026-07-08*
