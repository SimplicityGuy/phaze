# Pitfalls Research

**Domain:** Generalizing phaze's single-target (`cloud_target` local/a1/k8s) cloud-burst dispatch into a simultaneous, tiered, multi-backend scheduler (registry + `Backend` protocol + rank/cap drain + multi-Kueue)
**Researched:** 2026-07-03
**Confidence:** HIGH (grounded in the actual source: `tasks/release_awaiting_cloud.py`, `tasks/reconcile_cloud_jobs.py`, `services/cloud_staging.py`, `routers/agent_push.py`, `models/cloud_job.py`, `config.py`, `services/pipeline.py`; not generic distributed-systems advice)

---

## The One Fact That Drives Every Pitfall Below

Today's two cloud targets **account for in-flight work through two completely different substrates**, and the design (§4.4) proposes to unify them:

| | **a1 / compute** | **k8s / Kueue** |
|---|---|---|
| In-flight truth today | `COUNT(FileState IN {PUSHING, PUSHED})` — global, FileState-derived (`get_cloud_window_count`, pipeline.py:1243) | same FileState window today; design wants `COUNT(cloud_job WHERE status IN {SUBMITTED,RUNNING})` |
| Sidecar row | **none** — no `cloud_job` row is written for a compute push | `cloud_job` row upserted in `_stage_file_to_s3` (unique FK on `file_id`) |
| Recovery seed | **`scheduling_ledger` row** + a `process_file` enqueue on the compute queue (`agent_push.py`) → recoverable by `recover_orphaned_work` | **NO `process_file` ledger seed** (the DIST invariant) → recovered ONLY by `reconcile_cloud_jobs` |
| Availability gate | GATE 1: compute agent must heartbeat (`select_active_agent(kind="compute")`) | GATE 1 **skipped** (Landmine L2) — ephemeral pods, no persistent agent |

The whole milestone is "make `in_flight_count()` / `dispatch()` / `reconcile()` uniform across N backends." **Every critical pitfall below is a way that unification silently double-books, under-books, double-dispatches, or strands a file** because these two substrates were fused carelessly.

---

## Critical Pitfalls

### Pitfall 1: Double-counting compute in-flight — FileState window AND a new `cloud_job` row

**What goes wrong:**
Design §4.4 says "generalize the `cloud_job` registry to also record compute-agent pushes." If `ComputeAgentBackend.in_flight_count()` reads `cloud_job` rows while the existing FileState `{PUSHING, PUSHED}` window (`get_cloud_window_count`) is *also* still consulted anywhere, a single pushed file counts as 2 against a cap of (default) 1–2. The backend looks full at half load; long files pile up in `AWAITING_CLOUD` and (correctly per rank 99) spill onto slow local — the exact outcome the tiering was built to avoid. The inverse under-count is worse: if you drop the FileState window and the `cloud_job` row for a compute push is written a moment *after* the `PUSHING` flip commits, a concurrent drain tick reads `in_flight_count() = 0` and dispatches a second file, overshooting the cap onto a single compute agent's scratch dir (the `T-50-scratch-dos` class Phase 50's advisory lock was built to prevent).

**Why it happens:**
The refactor treats "add `backend_id` to `cloud_job`" as additive, but for compute the `cloud_job` row is a *brand-new* artifact — today compute pushes write only `scheduling_ledger` + FileState. Two sources of in-flight truth now exist for the same file and nobody deletes the old one.

**How to avoid:**
Pick **ONE** authoritative in-flight substrate for ALL backends and delete the other from the count path. The `cloud_job` registry (now `backend_id`-scoped) is the right choice — it's the only thing that can be per-backend. `in_flight_count(backend)` = `COUNT(cloud_job WHERE backend_id = :id AND status IN {in-flight states})`, computed inside the drain's advisory-locked transaction. The `cloud_job` row MUST be written **in the same transaction and before/with** the `FileState → PUSHING` flip (never after a separate commit), so the count can never lag the state. Add a characterization test asserting `sum(in_flight_count(b) for b in backends) == COUNT(FileState in-flight)` — a divergence is a double/under-count bug.

**Warning signs:**
Cap-of-2 backend that dispatches 3–4 concurrent pushes; or a healthy fast backend that stops accepting work at half its cap while files leak to local. A `cloud_job` count and a `FileState` count that disagree in a diagnostic query.

**Phase to address:** Phase 2 (protocol refactor — the moment `cloud_job` gains `backend_id` and `in_flight_count` is defined); regression-verified in Phase 3 (tiered scheduler).

---

### Pitfall 2: The drain↔reconcile race on a per-backend cap (no shared lock)

**What goes wrong:**
`stage_cloud_window` (the drain) takes `pg_advisory_xact_lock(5_000_504)` — but `reconcile_cloud_jobs` runs on its own `*/5` cron and takes **no lock** (confirmed: the only `pg_advisory` call in the codebase is in `release_awaiting_cloud.py:135`). Today that's safe because the in-flight window is FileState-derived and reconcile terminalizes via the same FileState, so MVCC self-corrects next tick. Once `in_flight_count` becomes `cloud_job.status`-derived and **per-backend**, reconcile mutating `cloud_job.status` (SUBMITTED→RUNNING→SUCCEEDED/FAILED) concurrently with the drain reading per-backend counts opens a window where the drain sees a stale in-flight number and over-dispatches to a backend reconcile just freed *and re-filled*. Worse: both crons can now try to write the **same file's** `FileState` — the drain flipping `AWAITING_CLOUD → PUSHING` on a spillover candidate while reconcile flips that same file `→ ANALYSIS_FAILED` at cap.

**Why it happens:**
The single advisory lock was designed for "overlapping *staging* ticks," not for "staging vs reconcile." With one global window and one dispatch path the two crons never contended on the count. Per-backend counting makes the count reconcile writes to load-bearing.

**How to avoid:**
Make reconcile and the drain **mutually exclusive** by having `reconcile_cloud_jobs` acquire the *same* `pg_advisory_xact_lock` key before it mutates `cloud_job.status` / `FileRecord.state` (or a documented lock-ordering for finer granularity). At minimum, the drain must compute `in_flight_count` and claim candidates in ONE transaction under the lock (it already does), and reconcile's terminal FileState writes must use `FOR UPDATE`/`SKIP LOCKED` against the same rows the drain claims. Add a concurrency test running a drain tick and a reconcile tick against overlapping rows: assert no file ends in two backends and no cap is exceeded.

**Warning signs:**
Intermittent cap overshoot only under load (when reconcile has terminal rows to process); a file observed briefly in both a `cloud_job` in-flight state and `ANALYSIS_FAILED`; "row reconcile failed; continuing" warnings coinciding with drain ticks.

**Phase to address:** Phase 3 (tiered scheduler owns cap semantics); the lock change lands with the scheduler, flagged in Phase 2 when reconcile becomes `backend_id`-aware.

---

### Pitfall 3: Two recovery mechanisms re-driving the same compute file (over-enqueue incident class)

**What goes wrong:**
This is the 44.5k-job over-enqueue incident's ecosystem. Compute (a1) files today are **in the `scheduling_ledger`** (via `agent_push.py`'s `process_file` enqueue) so `recover_orphaned_work` can replay them. k8s files are deliberately **not** ledger-seeded so recover never re-enqueues them onto an agent queue (the DIST invariant). If unification gives compute backends a `cloud_job` row that `reconcile_cloud_jobs` now also re-drives, a stalled compute push can be re-dispatched by **both** `recover_orphaned_work` (ledger replay) **and** the generalized reconcile (cloud_job re-drive) — double dispatch, doubled scratch-dir occupancy, doubled cap consumption. Symmetrically, if you *remove* the compute ledger seed to avoid this but the file's `cloud_job` reconcile path doesn't cover the rsync-push failure modes (sha256 mismatch, agent restart mid-push), a stuck compute file becomes unrecoverable by *either* mechanism.

**Why it happens:**
"Make recovery uniform across backends" (§4.5) collides with the fact that compute and k8s have *opposite* recovery contracts today. The invariant "NO process_file ledger seed for k8s" is easy to preserve; the trap is accidentally giving compute a *second* recovery path instead of *one* uniform one.

**How to avoid:**
Decide, per backend kind, on **exactly one** recovery owner and assert it. Cleanest: `cloud_job` (backend_id-scoped) becomes the single in-flight registry and `reconcile()` is the single recovery driver for **all** backends; compute's `reconcile()` body wraps the existing `/pushed` + callback path but the row is reconciled through `cloud_job`, and the `scheduling_ledger` seed for cloud-routed files is dropped (or explicitly excluded from `recover_orphaned_work` by backend). Keep the AST guard that asserts recover_orphaned_work cannot re-enqueue a cloud-routed file, and extend it to compute-backed files. Preserve the milestone invariant verbatim: no `process_file` ledger seed for any file a backend owns.

**Warning signs:**
A compute file analyzed twice (two `put_analysis` calls for one `file_id` — the idempotent writer masks it but logs show two dispatches); scratch-dir occupancy exceeding cap; `recover_orphaned_work` tally counting cloud-routed files.

**Phase to address:** Phase 2 (defines the uniform `reconcile()` seam + `cloud_job` ownership); guard extended in Phase 3.

---

### Pitfall 4: Dispatch-partial limbo — file `PUSHING` with no reconcilable registry row

**What goes wrong:**
`stage_cloud_window` deliberately flips `FileState → PUSHING` **before** the enqueue outcome is known (the dedup-owns-the-file idiom, line 176). For k8s, `_stage_file_to_s3` upserts the `cloud_job` row *and* enqueues in the same locked transaction, so a crash rolls back both. But in a unified `dispatch()` seam it is easy to write a body where the `PUSHING` flip commits but the `cloud_job` row (or the enqueue) is lost — e.g., an exception between the FileState mutation and the registry upsert, or a backend `dispatch()` that flips state in the scheduler but writes its registry row in the backend body across a commit boundary. Result: a file sits in `PUSHING`/in-flight, **consuming a cap slot**, but `reconcile_cloud_jobs` iterates `cloud_job` rows and never sees it → stuck forever, silently shrinking that backend's effective capacity every time it happens.

**Why it happens:**
The refactor moves the state flip (scheduler) and the registry write (backend body) into different objects; the "one transaction" guarantee Phase 53/54 carefully built is easy to break across the new abstraction boundary.

**How to avoid:**
Make `dispatch(file)` responsible for **both** the `FileState` flip and the `cloud_job` (backend_id) upsert in **one** transaction/session passed in by the scheduler — never let the scheduler flip state and the backend write the row on separate commits. Add an invariant test/health query: **no file in an in-flight FileState without a matching non-terminal `cloud_job` row for the same `file_id`** (and vice versa). Add a reconcile sweep that detects an in-flight FileState with no live registry row and returns it to `AWAITING_CLOUD`.

**Warning signs:**
A backend whose effective throughput silently degrades over days; files in `PUSHING`/`PUSHED` older than any plausible analysis wall-clock; `in_flight_count` pinned near cap with no corresponding running analysis.

**Phase to address:** Phase 2 (the `dispatch()` transaction contract); the orphan-detection sweep in Phase 3.

---

### Pitfall 5: Backend thrash — a file bouncing across flapping backends, with `attempts` mis-scoped

**What goes wrong:**
§4.5: a failed/offline backend returns the file to `AWAITING_CLOUD` and "the next drain tick re-dispatches it to the next eligible backend." With several backends intermittently offline, a file can bounce A→B→A→B every tick, never completing, generating churn (S3 uploads, Job submits, rsync starts) with no progress. Compounding it: `cloud_job` has a **unique FK on `file_id`** (one row per file) and an `attempts` counter that today means "kube submit attempts for THIS file." If spillover re-uses that single row across backends, `attempts` accumulates *across* backends — a file that fails once on each of 3 backends hits `cloud_submit_max_attempts=3` and is marked `ANALYSIS_FAILED` despite each backend having tried it exactly once. If instead you reset `attempts` on backend switch, a flapping pair gives infinite retries (unbounded thrash).

**Why it happens:**
The one-row-per-file `cloud_job` model was built for a single target; spillover introduces a per-(file,backend) notion of "attempts" and "which backend last had it" that the schema doesn't express.

**How to avoid:**
Separate the two counters: keep a **global** per-file dispatch budget (bounds total thrash → `ANALYSIS_FAILED` after N *total* dispatches across all backends) AND a **per-backend** attempt/cooldown so a backend that just failed a file is skipped for that file for a short window (attempt-affinity backoff), preventing A↔B ping-pong. Record `backend_id` + a `last_dispatched_at` on the `cloud_job` row; make the scheduler's "next eligible backend" exclude the backend that failed this file within the cooldown. Bound total re-dispatches explicitly. Add a thrash test: two backends flapping offline/online each tick, assert the file makes bounded attempts and lands `ANALYSIS_FAILED` (or completes), never infinite-loops.

**Warning signs:**
The same `file_id` appearing in dispatch logs across multiple backends within minutes; S3/Job/rsync submit rate far exceeding completion rate; `attempts` climbing on files no single backend actually ran to completion.

**Phase to address:** Phase 3 (tiered scheduler — spillover, re-dispatch, attempt-budget split all live here). Schema (`backend_id`, `last_dispatched_at`) prepared in Phase 2.

---

### Pitfall 6: The `cloud_target` → `backends` shim silently produces an empty or wrong registry

**What goes wrong:**
`cloud_target=local` today means **cloud OFF** (the drain's first line is `if cfg.cloud_target == "local": return no-op`). The back-compat shim (§4.1) must translate the three legacy values into a `backends:` list. Two silent failures: (a) the shim maps `local` → empty `backends` list → every long file wedges in `AWAITING_CLOUD` **forever** with no error and no dispatch (looks like a hung pipeline, not a config error); (b) an operator who sets the new `backends:` **and** leaves a stale `PHAZE_CLOUD_TARGET=a1` env gets ambiguous precedence — the shim silently wins or loses, producing a registry that doesn't match intent. Because the drain simply no-ops when nothing is eligible, **all of these fail as silence**, exactly like the Phase 30 misrouting incident (jobs enqueued to a consumer-less queue — no error, just nothing happens).

**Why it happens:**
`local` is overloaded (it means both "a backend" and "cloud disabled"), and a shim that spans an env-var rename is precisely where "empty is a valid Python list" hides a misconfiguration.

**How to avoid:**
The shim must be **explicit and total**: `local` → a single-entry registry `[local rank=99]` **plus** a distinct "cloud disabled" flag (don't conflate empty-list with off). Emit a startup **log line** stating the effective resolved registry ("backends resolved from legacy cloud_target=a1: [a1-compute rank=10 cap=2]"). Treat "`backends` set AND `cloud_target` set to a non-default" as a **fail-fast** conflict at startup, not a silent precedence pick. A registry that resolves to zero *available* backends while long files exist should raise a dashboard alert (reuse the v6.0 LocalQueue-probe Redis-flag alert pattern), never a silent hold.

**Warning signs:**
Long files accumulating in `AWAITING_CLOUD` with a "staged: 0, skipped: 0" drain tally every 5 min and no error; operator swears cloud is configured but nothing dispatches; the resolved-registry log line missing or empty.

**Phase to address:** Phase 1 (registry & config model owns the shim + resolved-registry logging + conflict fail-fast).

---

### Pitfall 7: Per-entry validator gaps let a misconfigured backend appear "eligible"

**What goes wrong:**
Today three **model-level** validators fail-fast at startup: `_enforce_compute_scratch_dir_when_a1`, `_enforce_s3_config_when_k8s`, `_enforce_kube_config_when_k8s` (config.py:636–681). They exist precisely because a k8s target with no `kube_api_url`, or an a1 target with no `compute_scratch_dir`, otherwise **fails only at dispatch runtime** — the a1 case silently builds a `"None/<file_id>.<ext>"` path and lands every file in `ANALYSIS_FAILED` after `push_max_attempts` (the comment at config.py:643 documents this exact trap). Generalizing to N entries, these must become **per-entry** validators. The gap: an entry appears in the registry, passes a shallow "is it in the list" check, is enumerated as eligible by the scheduler, `is_available()` returns True (agent heartbeats / probe passes), then `dispatch()` fails at runtime because *that entry's* kube/S3/scratch config is missing — reintroducing the exact silent-`ANALYSIS_FAILED` class the three validators were built to kill, now multiplied by N.

**Why it happens:**
The three validators were written against flat singleton fields (`self.s3_bucket`, `self.kube_api_url`). A per-entry registry needs the same checks *inside* each entry, and it's easy to validate the list's shape without validating each entry's kind-specific required fields.

**How to avoid:**
Port all three validators to **per-entry, kind-dispatched** validation that runs at settings-construction time: `kind=kueue` entry ⇒ its `kube_api_url`/`namespace`/`localqueue`/image required; `kind=compute` entry ⇒ its `agent_ref` + scratch config required; `kind=local` ⇒ nothing. Fail-fast at startup with the entry `id` in the message. Do **not** collapse them into one `kind != local` gate (the config.py:623 comment warns this silently changes a1's fail-fast semantics). Keep the milestone's "each entry fails fast" as an explicit acceptance test with a deliberately-broken entry per kind.

**Warning signs:**
Files landing `ANALYSIS_FAILED` shortly after routing to one specific backend while others work; a backend that shows "available" in the UI but never completes a file; runtime `KeyError`/`None`-path errors in a backend body a startup validator should have caught.

**Phase to address:** Phase 1 (registry validators). Verified again in Phase 4 for the multi-Kueue per-cluster fields.

---

### Pitfall 8: One Kueue cluster's probe/dispatch failure poisons the whole tick

**What goes wrong:**
Multi-Kueue means the drain enumerates N `KueueBackend`s and calls `is_available()` (LocalQueue probe) then `dispatch()` (kube POST) per cluster. If cluster C's kube API is unreachable and its `is_available()` **raises** (kr8s connection error) instead of returning False, an unguarded enumeration aborts the *entire* drain tick — starving the healthy clusters and local. Same for a `dispatch()` that raises: `reconcile_cloud_jobs` already has a per-row try/except guard (line 315), but the **drain** (`stage_cloud_window`) has no per-backend guard today because it only ever touched one target. A single flaky cluster then blocks all dispatch.

**Why it happens:**
The single-target drain never needed to isolate one backend's failure from the tick. N backends make "one target down" a routine steady state, not an exception.

**How to avoid:**
Wrap **every** per-backend `is_available()` and `dispatch()` call in the drain in its own try/except (mirror reconcile's per-row guard): a raising/unavailable backend is skipped for this tick, logged, and the tick proceeds to the next backend. `is_available()` should be defined to **return bool, never raise** (catch the kube/probe error inside and return False). Add a test: N backends where one raises on `is_available` and one raises on `dispatch`, assert the others still receive work and no exception escapes the tick.

**Warning signs:**
All backends stop dispatching whenever one cluster goes down; drain tick logging an exception and a `staged: 0` even though healthy backends have capacity and files are waiting.

**Phase to address:** Phase 4 (multi-Kueue), with the per-backend guard pattern established in Phase 3's scheduler loop.

---

### Pitfall 9: Shared S3 bucket + deterministic keys → cross-cluster collision on spillover

**What goes wrong:**
All Kueue clusters share ONE S3 bucket (§3.7) and staging keys + Job names are **`file_id`-scoped and deterministic** (`phaze-analyze-<file_id>`, file_id-scoped S3 key). Within a single cluster, the unique `cloud_job` FK guarantees one file → one object. But **spillover across clusters** re-uses the same `file_id`-scoped key: file fails on cluster A (rank 10) → re-dispatched to cluster B (rank 20). Two hazards: (a) cluster A's no-callback-terminal cleanup deletes the **shared** S3 object (`s3_staging.delete_staged_object(file_id)`, reconcile line 170) while cluster B's pod still needs it → cluster B's just-in-time GET fails, file fails spuriously; (b) if A and B overlap (a reconcile lag leaves A's Job alive when B is dispatched), both pods analyze the same object and both POST results (the idempotent writer dedups the *result* but wastes a full analysis + confuses per-backend accounting). The confirm-gone race guard (`_job_gone`, reconcile line 95) is **per-cluster** (`kube_staging.get_job` targets one kube API) and cannot see cluster A's Job from cluster B's context.

**Why it happens:**
The deterministic-name idempotency that makes single-cluster re-drive safe assumes ONE kube API and ONE owner of the object. Sharing the bucket across clusters breaks the "one owner" assumption for the object lifecycle.

**How to avoid:**
Scope the S3 cleanup decision to "**is this file still owned by the backend that staged this object?**" — delete the staged object only on a *genuinely terminal* outcome (at-cap `ANALYSIS_FAILED` or success-callback), **never** on a spillover/re-drive that hands the file to another backend (the reconcile already preserves the object on the under-cap re-drive path — extend that reasoning to cross-backend spillover). Ensure a file is only ever in **one** cluster at a time (the unique `cloud_job` FK + the mutual-exclusion lock from Pitfall 2 enforce this; verify the spillover path clears the old cluster's Job before the new dispatch). Keep the S3 key `file_id`-scoped (safe with one owner) but make the **owner** explicit via `cloud_job.backend_id`.

**Warning signs:**
Cluster B pods failing on S3 GET (404) right after a cluster A cleanup; two Jobs named `phaze-analyze-<same file_id>` alive in two clusters; duplicate `put_analysis` callbacks for one file_id from different cluster identities.

**Phase to address:** Phase 4 (multi-Kueue owns shared-bucket ownership semantics); spillover-cleanup rule co-designed with Phase 3.

---

### Pitfall 10: The "behavior-preserving" refactor silently changes the GATE asymmetry (Landmine L2)

**What goes wrong:**
The current `if/elif` in `stage_cloud_window` encodes a **subtle, load-bearing asymmetry**: a1 requires a live compute agent (GATE 1), but k8s **skips GATE 1** (lines 137–147) — because k8s uses ephemeral pods with no persistent agent, and the code comment explicitly warns "else every k8s file would wedge in AWAITING_CLOUD forever (Landmine L2)." When Phases 1–2 collapse this into `Backend.is_available()`, a naïve implementation that makes all backends check "is an agent online" reintroduces Landmine L2 for every Kueue backend — files silently wedge. Equally, GATE 2 (fileserver agent must be online — it owns the media mount and runs both rsync push AND the S3 upload) applies to **both** a1 and k8s and must be preserved for both. A "behavior-preserving" refactor that gets either gate wrong changes dispatch behavior invisibly, violating the milestone's whole de-risking premise (phases 1–2 change nothing).

**Why it happens:**
The asymmetry lives in a branch comment, not in a type. Refactoring to a uniform protocol tempts you to make availability uniform too, erasing the intentional per-kind difference.

**How to avoid:**
Encode the gates as **explicit per-kind `is_available()` bodies with the asymmetry documented**: `LocalBackend.is_available` → always True; `ComputeAgentBackend.is_available` → its referenced compute agent heartbeats (GATE 1); `KueueBackend.is_available` → LocalQueue probe passes, **no compute-agent dependency** (GATE 1 skipped). The fileserver GATE 2 (media mount / uploader) is a **scheduler-level** precondition applied to any non-local dispatch, not folded into a backend's `is_available`. Write a **characterization test** capturing today's dispatch decisions (which files dispatch where, under which agent-online combinations) and assert the refactored path produces byte-identical decisions for the single-backend configs before flipping on multiplicity.

**Warning signs:**
After the Phase 2 merge, k8s files that dispatched fine in v6.0 now sit in `AWAITING_CLOUD`; or a1 files dispatch with no compute agent online (GATE 1 lost). Any dispatch-decision diff between v6.0 and the refactored single-backend path.

**Phase to address:** Phase 2 (protocol refactor) — the characterization test is the phase's acceptance gate.

---

### Pitfall 11: Test coverage gap — the old `if/elif` was only *implicitly* tested

**What goes wrong:**
The `cloud_target` switch is exercised today through integration tests that set `cloud_target=a1` or `=k8s` and assert end-to-end behavior. There is no per-branch unit test of "given target X, produce dispatch Y" because the branch was trivial. After the protocol refactor, each `Backend` implementation has four real methods (`is_available`/`in_flight_count`/`dispatch`/`reconcile`) with per-kind logic — but if the suite still only covers them via the two legacy end-to-end paths, the **new** surface (per-backend counting, spillover, the availability asymmetry, the transaction contract) is untested, and the 85% coverage gate can be *satisfied by the old integration tests* while the multiplicity branches (N>1 backends, cross-backend spillover, per-entry validators) have zero real assertions. This is how JOB-ENV-CONTRACT and the v6.0 double-enqueue defects slipped past — behavior that "looked covered" but the specific new seam wasn't asserted.

**Why it happens:**
Coverage percentage measures lines hit, not decisions asserted. A refactor that preserves line coverage via legacy tests hides that the *multiplicity* logic — the entire point of the milestone — is unexercised.

**How to avoid:**
Add **explicit unit tests per `Backend` method per kind** (12 cells minimum), plus scheduler tests that specifically require **N≥2 backends**: rank ordering, cap-full spill to next rank, spill to local only when all higher ranks full/offline, spillover on mid-flight failure, thrash bounding. Add the characterization test (Pitfall 10) and the accounting-consistency test (Pitfall 1). Treat "single-backend integration test passes" as necessary-but-not-sufficient. Extend the existing AST guards (no-default-queue routing, no-cloud-ledger-recover) to the new dispatch seam.

**Warning signs:**
Coverage green but no test file names a second backend; PR diff adds `Backend` implementations with tests that only ever instantiate one; spillover/rank/cap paths with no direct assertion.

**Phase to address:** Every phase adds its own tests, but Phase 2 (protocol) and Phase 3 (scheduler) are where the multiplicity test surface must be built explicitly, not inherited.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Keep counting in-flight from FileState `{PUSHING,PUSHED}` globally and just add `backend_id` for display | No accounting rewrite; Phase 2 stays tiny | Per-backend caps are wrong the moment two backends run (a file on A counts against B); silent over/under-dispatch | **Never** — this is the milestone's core correctness property |
| One shared advisory-lock key for drain AND reconcile | Trivial mutual exclusion | Reconcile now blocks on drain and vice-versa; a long reconcile tick stalls dispatch | Acceptable at this scale (single user, `*/5` crons, tiny row counts) — simplicity beats granular locking |
| Reuse the single `cloud_job` row per file across backend spillover (mutate `backend_id` in place) | No schema change beyond one column | `attempts` semantics blur across backends (Pitfall 5); history of which backend had it is lost | Only if a global dispatch-budget + per-backend cooldown is added alongside |
| Ship the `cloud_target` shim as permanent (no deprecation) | Zero-friction upgrade for existing deploy | Two config surfaces forever; the overloaded `local`=off ambiguity persists | Acceptable for one milestone with a logged deprecation path; schedule shim removal |
| Skip the staleness guard on local (design §4.3 default) | Less code | A momentary higher-rank backlog dumps long files onto slow local, defeating tiering | Acceptable per design default (rank 99 + cap 1) **if** cap 1 is genuinely small; revisit if local thrash observed |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Kueue (kr8s) multi-cluster | One kr8s client / one kubeconfig assumed; `is_available` raises on unreachable cluster and aborts the tick | Per-cluster kr8s client from that entry's `_FILE` kubeconfig; `is_available` catches and returns False; per-backend try/except in the drain |
| Shared S3 bucket (aioboto3) | Delete the `file_id`-scoped object on any terminal, including cross-cluster spillover | Delete only on genuinely-terminal (at-cap fail / success callback); preserve on re-drive AND cross-backend spillover; owner tracked via `cloud_job.backend_id` |
| Compute agent (SAQ per-agent queue) | New `cloud_job` row for compute PLUS the existing `scheduling_ledger` seed → two recovery paths | One registry (`cloud_job`) + one recovery driver (`reconcile`); drop/exclude the cloud-routed ledger seed; keep the no-default-queue routing (Phase 30 invariant) |
| `_FILE` secrets, N per-cluster | Reuse the flat singleton `kube_*`/SA-token fields for all clusters | Per-entry secret resolution; each entry resolves its own `_FILE`-mounted kubeconfig/token; per-entry validator asserts presence |
| `put_analysis` result writer | Assume the dropped kube watch or a reconcile miss loses the result | Unchanged — `put_analysis` by `file_id` stays the sole out-of-band writer; reconcile never writes a result (preserve KSUBMIT-03) |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Per-tick `is_available()` doing a live probe per backend per `*/5` tick | Drain tick latency grows with N backends; kube API rate noise | Cache/parallelize probes; keep probes cheap; the startup-probe→Redis-flag pattern (v6.0) can front the live probe | N clusters × frequent ticks |
| Spillover thrash generating S3/Job/rsync churn | Submit/upload rate ≫ completion rate | Attempt-affinity cooldown + global dispatch budget (Pitfall 5) | Any time ≥2 backends flap |
| Enumerating `in_flight_count` as N separate COUNT queries per tick | Drain tick does N round-trips | Single grouped `COUNT(*) ... GROUP BY backend_id` under the lock | Small N — low risk at this scale, trivial to get right |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| A cluster/provider config leaking media-plane access (breaking DIST-01) | An agent/pod gets S3 importer credentials; media boundary violated | Preserve DIST-01: control plane stays sole S3 importer/presigner; per-entry config never grants a pod bucket creds — only presigned URLs |
| Per-cluster `_FILE` secrets logged in the resolved-registry log line (Pitfall 6) | kubeconfig / SA-token in logs | Log entry `id`/`kind`/`rank`/`cap` only — never resolved secret material (`SecretStr` repr discipline) |
| A misconfigured backend's raw kube/S3 error text surfaced to the admin UI | Credential/endpoint disclosure | Sanitize backend error surfacing; alert with the entry `id`, not the raw exception |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| N per-backend lanes surfaced as N undifferentiated cards | Operator can't tell free-fast from paid-last, or which is offline vs full | Generalize v7.0 Phase 58's local/A1/k8s lane cards to N lanes labeled by `id` + rank + cap + live in-flight/available state (coordinate with the UI phase; §7 flags this) |
| A silent hold (empty registry / all-unavailable) shown as "idle" | Operator thinks the pipeline is done, not misconfigured | Distinguish "no work" from "work waiting, no available backend" with a dashboard alert (reuse the v6.0 LocalQueue amber-alert Redis-flag pattern) |
| Inadmissible (per-cluster) not attributed to a cluster | Operator can't tell which cluster is misconfigured | Attribute the Inadmissible alert to the specific backend `id` |

## "Looks Done But Isn't" Checklist

- [ ] **Per-backend cap:** Often missing the single-substrate guarantee — verify `sum(in_flight_count(b)) == COUNT(in-flight FileState)` and that no path counts both FileState AND `cloud_job` for the same file.
- [ ] **`dispatch()` atomicity:** Often missing the one-transaction contract — verify FileState flip + `cloud_job` upsert commit together; kill the process between them in a test, assert no limbo row.
- [ ] **Spillover:** Often missing thrash bounding — verify a file across flapping backends lands terminal in bounded attempts, and `attempts` isn't mis-scoped across backends.
- [ ] **Shim:** Often missing the empty-vs-off distinction — verify `cloud_target=local` and `=a1` both resolve to a logged, correct registry, and a dual-config conflict fails fast.
- [ ] **Per-entry validators:** Often missing per-kind required-field checks — verify a broken entry of each kind fails at **startup**, not at dispatch.
- [ ] **GATE asymmetry:** Often missing — verify Kueue `is_available` does NOT depend on a compute agent (Landmine L2) and a1 still requires one.
- [ ] **Recovery uniqueness:** Often missing — verify `recover_orphaned_work` cannot re-enqueue any backend-owned file (extend the AST guard to compute-backed files).
- [ ] **Shared-bucket cleanup:** Often missing — verify the S3 object survives cross-backend spillover and is deleted only on genuine terminal.
- [ ] **Drain resilience:** Often missing — verify one backend raising in `is_available`/`dispatch` doesn't abort the whole tick.
- [ ] **Multiplicity tests:** Often missing — verify at least one test instantiates N≥2 backends and asserts rank/cap/spill; coverage-green ≠ multiplicity-tested.

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Double/under-count cap (P1) | MEDIUM | Pick one substrate, migrate `in_flight_count` to `cloud_job` per-backend, add the consistency assertion; drain self-heals next tick once the count is right |
| Drain↔reconcile race (P2) | MEDIUM | Add the shared advisory lock to reconcile; reprocess any file stuck in a split state via the orphan sweep |
| Double recovery / over-enqueue (P3) | HIGH | Purge duplicate in-flight (the v4.0.6/Phase-32 purge+cron-rebuild playbook); enforce single recovery owner; extend AST guard |
| Dispatch limbo (P4) | LOW | Orphan sweep returns in-flight-without-registry-row files to `AWAITING_CLOUD` |
| Thrash (P5) | LOW | Add cooldown + global budget; files self-resolve to terminal once bounded |
| Empty/wrong shim (P6) | LOW | Fix config; the resolved-registry log line + startup conflict fail-fast prevent recurrence |
| Cross-cluster S3 collision (P9) | MEDIUM | Scope cleanup to owner; re-stage the deleted object for the spillover target; enforce one-cluster-at-a-time via the unique FK + lock |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| P1 Double/under-count cap | Phase 2 (protocol) → Phase 3 | `sum(in_flight_count) == FileState in-flight count` assertion; cap-of-2 never dispatches 3 |
| P2 Drain↔reconcile race | Phase 3 (scheduler) | Concurrency test: overlapping drain+reconcile ticks, no cap overshoot, no split state |
| P3 Double recovery / over-enqueue | Phase 2 → Phase 3 | AST guard: `recover_orphaned_work` excludes all backend-owned files; single reconcile owner |
| P4 Dispatch limbo | Phase 2 (dispatch txn) → Phase 3 (orphan sweep) | Crash-between-writes test; invariant query in-flight-state ⇔ live registry row |
| P5 Backend thrash | Phase 3 (spillover) | Two-flapping-backend test: bounded attempts, correct terminal, no infinite loop |
| P6 Shim empty/wrong | Phase 1 (config model) | `local`/`a1` resolve to logged correct registry; dual-config conflict fails fast |
| P7 Per-entry validator gap | Phase 1 (validators), re-checked Phase 4 | Broken entry per kind fails at startup with entry id in message |
| P8 One cluster poisons tick | Phase 4 (multi-Kueue), pattern in Phase 3 | N-backend test with one raising; others still dispatch, no escape |
| P9 Shared-bucket collision | Phase 4 (multi-Kueue) | Spillover preserves object; object deleted only on genuine terminal; one cluster per file |
| P10 GATE asymmetry lost | Phase 2 (protocol) | Characterization test: dispatch decisions byte-identical for single-backend configs |
| P11 Test coverage gap | Phase 2 + Phase 3 | ≥12 per-method-per-kind unit tests + N≥2 scheduler tests exist and assert |

## Sources

- Codebase (HIGH confidence — read directly): `src/phaze/tasks/release_awaiting_cloud.py` (advisory-locked drain, GATE 1/2 asymmetry, PUSHING-before-enqueue idiom, k8s S3 no-commit branch), `src/phaze/tasks/reconcile_cloud_jobs.py` (no advisory lock, delete-after-record ordering, bounded re-drive, `_job_gone` per-cluster race guard, Inadmissible-vs-Pending), `src/phaze/services/cloud_staging.py` (`_stage_file_to_s3` cloud_job upsert, one-commit-after-loop), `src/phaze/routers/agent_push.py` (compute push → `scheduling_ledger` + `process_file` enqueue), `src/phaze/models/cloud_job.py` (unique FK on file_id, `attempts`, `inadmissible`, `cloud_phase`), `src/phaze/config.py` (three per-target fail-fast validators; `cloud_target`/`cloud_max_in_flight`/`cloud_submit_max_attempts`), `src/phaze/services/pipeline.py` (`get_cloud_window_count` FileState-derived), `src/phaze/services/enqueue_router.py` (`select_active_agent(kind=...)`).
- Design spec (HIGH): `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` §4.1–4.5, §5–7.
- Milestone context + `.planning/PROJECT.md`: DIST-01, `put_analysis` sole-writer, no-k8s-ledger-seed invariant, and the documented incident history (Phase 30 default-queue misrouting; v4.0.6/v4.0.8 stranded-jobs/dead-letter; recover over-enqueue 44.5k; JOB-ENV-CONTRACT) that anchors the accounting/over-enqueue/dead-letter pitfall classes.

---
*Pitfalls research for: multi-cloud tiered backend scheduler over an existing single-target dispatch system*
*Researched: 2026-07-03*
