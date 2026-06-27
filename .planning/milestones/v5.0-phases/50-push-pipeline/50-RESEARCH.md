# Phase 50: Push pipeline - Research

**Researched:** 2026-06-25
**Domain:** Distributed file transfer (rsync-over-SSH-over-Tailscale) + bounded-window SAQ scheduling + integrity verification + scratch lifecycle
**Confidence:** HIGH (codebase wiring verified against current source; rsync/ssh semantics cross-checked against man-page behavior)

## Summary

Phase 50 is ~90% **brownfield wiring** of existing, well-established patterns and ~10% **one genuinely novel capability** (rsync-over-SSH from an asyncio subprocess — no precedent in `src/`). Almost every architectural decision is already locked in `50-CONTEXT.md` (D-01..D-14); this research verifies each decision against the *actual current code* and supplies implementation-ready detail for the discretion areas (rsync flags, attempt-counter storage, eligibility ordering, config names, and the routing-seam reshape).

The two-stage flow (`push_file` on the fileserver queue → `process_file` on the compute queue) maps cleanly onto the existing per-agent SAQ queue model, the `_KEY_BUILDERS` deterministic-dedup chokepoint, the scheduling ledger, and the `_DOMAIN_COMPLETED_STAGES` recovery predicate. The "stay one ahead" controller cron is a near-clone of Phase 49's `release_awaiting_cloud`. The sha256 verify reuses `phaze.services.hashing.compute_sha256` off the event loop via `asyncio.to_thread` (the exact pattern already used at `scan.py:268`). Scratch cleanup is a `finally`-block `unlink` plus a compute-startup full-sweep janitor.

**One important refinement to D-01 surfaced by the code** (see Critical Findings §1): the fileserver agent is *Postgres-free by hard invariant* (`tests/test_task_split.py`) and has **no ORM and no way to resolve the compute agent's queue name or read `FileRecord.sha256_hash`**. Therefore `push_file` should **not** enqueue `process_file` directly from inside the agent process. Instead it should call a thin control-side internal-API callback (mirroring the existing `put_analysis` → "transition state + clear ledger" pattern) that performs the `PUSHING→PUSHED` transition, clears the `push_file` ledger row, and enqueues `process_file` (with `expected_sha256` + `scratch_path`) control-side. This honors D-01's *intent* ("on push success, `process_file` is enqueued on the compute queue") while respecting the agent boundary.

**Primary recommendation:** Implement the **single-entry / hold-then-stage** routing seam (every cloud-routed long file → `AWAITING_CLOUD`; a single staging cron tops up the ≤N window), reuse `compute_sha256` + `asyncio.to_thread` for verify, drive `push_file` via `asyncio.create_subprocess_exec` (never a shell) with default-atomic rsync + `--partial-dir` + `--timeout` + pinned `known_hosts`, and route the push-success follow-on through a control-side callback rather than an agent-side enqueue.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions (D-01..D-14 — verbatim intent)
- **D-01:** Two SAQ stages: new `push_file` on the **file-server** queue (rsyncs to compute scratch) → on success enqueues `process_file` on the **compute** queue (reads the scratch copy). Each stage ledger-tracked + deterministic-keyed.
- **D-02:** "Stay one ahead" driven by a **single controller cron** modeled on `release_awaiting_cloud` (`*/5`, gated on an online compute agent). Counts staged + in-flight, enqueues `push_file` for the next eligible file(s) until the window (≤N) is full. Single driver, recovery-only-compatible, no completion-chaining hooks.
- **D-03:** Window size **N = config knob, default 2** (one analyzing + one staged). Follow `*_threshold`/`*_sec` convention (e.g. `cloud_max_in_flight: int = 2`). The bound **must never be exceeded** (load-bearing — prevents the 144-file backfill from blowing up scratch disk).
- **D-04:** Compute offline mid-window → **hold & resume** (ledger rows stay, file stays in cloud state). **Never fall back to local analysis.** Dead-agent scratch reconciled by the startup janitor (D-14).
- **D-05:** **Static config on the file-server**: `push_ssh_host`, `push_ssh_user`, `push_ssh_key` (`_FILE` secret), `push_scratch_dir`. (Dynamic heartbeat discovery deferred.)
- **D-06:** Push runs **rsync over SSH** via an asyncio subprocess, using `--partial-dir`/temp-name + **atomic rename** so the compute agent never sees a half-written file at the final path. rsync wire checksum + app-level sha256 verify (defense in depth). Resumable. Remaining flags (`--inplace`, `--timeout`, compression) Claude's discretion within atomicity + integrity goals.
- **D-07:** **Pinned `known_hosts` (strict).** Operator-provisioned host key mounted (via `_FILE`-style secret); `StrictHostKeyChecking=yes`. One-time setup → Phase 51 runbook.
- **D-08:** Two new `FileState` members: **`PUSHING`** (rsync in progress) and **`PUSHED`** (on compute scratch, awaiting/within analysis). Code-only StrEnum over `String(30)` → **no migration** (`AWAITING_CLOUD` precedent).
- **D-09:** Two new dashboard count cards — **"Staged (pushing)"** and **"Analyzing (cloud)"** — reusing `_safe_count` + count-card pattern. Click-through deferred.
- **D-10:** `PUSHING`/`PUSHED` wired into the recovery/reenqueue **domain-completed predicate** as **not terminal/done** (still need analysis) → remain eligible for re-drive. `process_file` "done" stays `{ANALYZED, ANALYSIS_FAILED}`.
- **D-11:** **Expected sha256 travels in `ProcessFilePayload`** (control plane has `FileRecord.sha256_hash`). Include `expected_sha256` + scratch path. Compute `process_file` reads the scratch copy (ephemeral) instead of `original_path` — payload carries a flag/scratch path distinguishing compute-scratch read from file-server local-mount read.
- **D-12:** On **sha256 mismatch**: compute agent fails cleanly + deletes the bad scratch file; control plane **re-drives the push** up to a **configurable max attempts (default ~3)**; after the cap → **`ANALYSIS_FAILED`**. Attempt-counter storage is Claude's discretion.
- **D-13:** **Scratch cleanup in `process_file`'s `finally`** (success OR terminal failure).
- **D-14:** **Compute-agent startup janitor** sweeps orphaned scratch files (killed/interrupted worker). Safe because the window is small and any still-needed file is re-pushed on demand.

### Claude's Discretion
- **Routing seam (Phase 49 integration):** funnel all cloud-routed long files through a cloud-pending state + staging cron (single entry, simplest) **vs** fast-path immediate `push_file` when the window has room. **Hard constraint: the ≤N window is never exceeded.**
- Re-push attempt-counter storage location; eligibility ordering (FIFO by discovery / oldest cloud-pending first); exact rsync flags beyond atomicity + integrity; config knob names/defaults (convention match to `cloud_route_threshold_sec`).

### Deferred Ideas (OUT OF SCOPE)
- Dynamic compute-agent target discovery via heartbeat `last_status` (multi/rotating agents) — static config (D-05).
- Cloud-agent compose, Tailscale ACL, least-privilege Postgres role, runbook docs — **Phase 51 (CLOUDDEPLOY-01..04)**.
- Click-through drill-down for the new cloud count cards.
- Cost/throughput-aware routing (CLOUDROUTE-05).
- Round-robin / least-loaded dispatch among multiple compute agents.
- **Object storage / presigned-URL staging** — v5.0 explicitly chose rsync push; the old "upload→object-storage→reconcile" sketch is NOT this architecture.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CLOUDPIPE-01 | Control plane keeps ≤ configured cloud files staged-or-in-flight ("stay one ahead"; default 2), ledger-driven. | §"Stay one ahead" controller cron; `cloud_max_in_flight` knob; window counted from `FileState` IN (PUSHING, PUSHED). |
| CLOUDPIPE-02 | File-server pushes a cloud-routed file to compute scratch over rsync/SSH-over-Tailscale; file-server initiates, compute only receives. | §rsync-over-SSH from asyncio; `push_file` task on fileserver queue; directional invariant. |
| CLOUDPIPE-03 | Compute verifies sha256 after transfer before analyzing; mismatch fails cleanly + triggers re-push. | §sha256 verify off event loop (`compute_sha256` + `asyncio.to_thread`); §mismatch → control re-drive. |
| CLOUDPIPE-04 | Compute deletes scratch copy after analysis (success or terminal failure), bounding disk to in-flight set. | §scratch cleanup `finally` + startup janitor. |
| CLOUDPIPE-05 | Failed/interrupted push/analysis re-driven, no orphaned scratch files, no double-enqueue (idempotent, ledger-tracked). | §deterministic key `push_file:<file_id>`; §recovery classification; §startup janitor. |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.14 exclusively; `uv` only** — every command prefixed `uv run` (e.g. `uv run pytest`, `uv run ruff check .`, `uv run mypy .`). Never bare `pip`/`python`/`pytest`/`mypy`.
- **Ruff line-length 150**; double quotes; type hints on every function; rule sets include `S` (bandit-equivalent — `S603`/`S607` subprocess rules are live), `PTH` (use `pathlib`, not `os.path`), `ASYNC`, `TCH`/`UP`.
- **mypy strict**, excludes `tests/|prototype/|services/`. New `src/phaze/tasks/push.py` and config/schema fields are mypy-strict.
- **85% coverage minimum**, Codecov with service flags. New `push_file` + cron + payload changes need tests.
- **Pre-commit frozen-SHA hooks must pass** (bandit `-x tests -s B608`; bandit will scrutinize `subprocess` — use `create_subprocess_exec` with a list argv and no `shell=True`; a justified `# noqa: S603`/inline bandit nosec may be needed and should carry a comment).
- **PR per phase** on a worktree branch (already on `gsd/phase-50-push-pipeline`); never push to main.
- **Commit frequently**; keep service READMEs + `justfile` + `scripts/update-project.sh` current as new commands/services appear.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Decide when to stage the next cloud file (≤N window) | Control plane (controller cron) | — | Only the control plane has ORM/ledger visibility into the window; agents are stateless executors. |
| Resolve the compute target (queue + agent) | Control plane | — | `select_active_agent(kind="compute")` needs ORM; the fileserver agent is Postgres-free. |
| Physically transfer the file (rsync) | File-server agent (`push_file`) | — | File-server owns the media mount + initiates the push (CLOUDPIPE-02 directional invariant). |
| Build `ProcessFilePayload` with `expected_sha256` | Control plane | — | `FileRecord.sha256_hash` requires ORM (D-11). |
| sha256 verify of the scratch copy | Compute agent (`process_file`) | — | Verify happens where the bytes landed, before analysis (CLOUDPIPE-03). |
| Run essentia analysis | Compute agent (pebble pool) | — | Unchanged from Phase 43/49; analyzer is already path-agnostic. |
| Scratch cleanup (per-job + startup sweep) | Compute agent | — | Scratch is local to the compute agent (CLOUDPIPE-04). |
| State transitions (PUSHING/PUSHED/…) + ledger writes/clears | Control plane | — | `FileRecord.state` + ledger are Postgres; agents report via the internal API. |

## Standard Stack

**No new Python packages.** This phase adds no third-party dependencies. It introduces **two system binaries** at the file-server agent runtime: `rsync` and the OpenSSH client (`ssh`). These are *not* present in the current agent image by default (the agent Dockerfile is python-slim-derived; see memory `project_agent_image_missing_libs`) — **provisioning them in the image is Phase 51 (CLOUDDEPLOY-01)**, but Phase 50's code *assumes their presence* and must degrade with a clear error if absent (see Pitfall 5).

### Reused in-repo modules (the real "stack" for this phase)
| Module / symbol | Purpose | Why reuse |
|-----------------|---------|-----------|
| `phaze.services.hashing.compute_sha256(Path) -> str` | Chunked (64 KB) streaming sha256, sync | Already the codebase standard; called off-loop via `asyncio.to_thread` at `scan.py:268`. Pure stdlib (hashlib + pathlib) → safe to import in the Postgres-free agent `process_file`. |
| `asyncio.create_subprocess_exec` | Spawn rsync without a shell | stdlib; injection-safe (argv list, no `shell=True`). No new dep. |
| `phaze.services.analysis_enqueue.enqueue_process_file` | Build + enqueue `process_file` with deterministic key + policy | Extend to carry `expected_sha256` + `scratch_path` (keyword-only, default None — same pattern as the Phase-44 `fine_cap`/`coarse_cap` additions). |
| `phaze.tasks.release_awaiting_cloud.release_awaiting_cloud` | Cron template | The staging cron is a near-clone (scan a state, gate on compute agent, enqueue, commit). |
| `phaze.tasks._shared.deterministic_key._KEY_BUILDERS` | Dedup chokepoint | Add `"push_file": lambda k: str(k["file_id"])`. |
| `phaze.services.scheduling_ledger.*` | Ledger upsert/clear | Reused unchanged; `routing_for_function` derives `"agent"` for `push_file` automatically once it's in `AGENT_TASKS`. |

**Installation:** none (no `uv add`). System-binary provisioning deferred to Phase 51.

## Package Legitimacy Audit

> No external packages are installed by this phase. The Package Legitimacy Gate is **not applicable** (zero new PyPI/npm/crates dependencies). System binaries `rsync` and `openssh-client` are OS packages provisioned by the Phase 51 image build, not language-registry packages — they carry no slopsquatting surface and are out of scope for the registry audit.

## Critical Findings (verify-against-source results)

### 1. D-01 "push_file enqueues process_file directly" needs a control-side callback (agent is Postgres-free)

**Verified:** `tests/test_task_split.py` hard-enforces that `phaze.tasks.agent_worker` (and everything it imports, including `phaze.tasks.functions`/a new `phaze.tasks.push`) **must not import `phaze.database`, `phaze.models.*`, `phaze.tasks.session`, or `sqlalchemy.ext.asyncio`**. The fileserver agent therefore:
- cannot run `select_active_agent(kind="compute")` (ORM) to learn the compute queue name, and
- cannot read `FileRecord.sha256_hash` (ORM) to build the `expected_sha256` for the follow-on `process_file`.

**Established pattern (verified at `routers/agent_analysis.py:184-196`):** an agent stage does its work, then **PUTs a result to a control-side internal API endpoint** which, *in one transaction*, transitions `FileRecord.state` AND clears the stage's ledger row (`clear_ledger_entry(session, f"process_file:{file_id}")`). The agent never touches Postgres directly.

**Recommendation:** `push_file` (fileserver) rsyncs, then calls a **new thin internal-API callback** (e.g. `PATCH /api/internal/agent/push/{file_id}` body `{"status": "pushed"}`, token-authed like the existing agent endpoints) that control-side:
1. transitions `PUSHING → PUSHED`,
2. clears the `push_file:<file_id>` ledger row,
3. enqueues `process_file` on the compute queue via `enqueue_process_file(...)` **with** `expected_sha256=file.sha256_hash` and `scratch_path=<scratch_dir>/<file_id>.<ext>` (control plane reads `sha256_hash` from ORM here — exactly where D-11 says the value is "pinned at enqueue time").

This honors D-01's intent without breaking the agent boundary. **Flag for the planner:** D-01's literal wording ("[push_file] that, on success, enqueues process_file on the compute agent's queue") reads as an agent-side enqueue. It is *physically impossible* for the fileserver agent to build the complete `process_file` payload (it lacks `sha256_hash`) or resolve the compute queue without ORM. The callback design is the only one consistent with the locked Postgres-free boundary; treat it as the implementation of D-01, not a contradiction. (Alternative considered: pass the compute `agent_id` + a pre-computed `expected_sha256` *into* the `push_file` payload so the agent can enqueue directly. Rejected: the agent-side enqueue still cannot clear the `push_file` ledger row, $\Rightarrow$ you need a control-side callback anyway; doing the enqueue there too is strictly simpler and keeps `sha256_hash` ORM-side.)

### 2. The Phase-49 routing seam directly enqueues `process_file` to compute — Phase 50 must replace BOTH paths

**Verified two sites that currently enqueue `process_file` straight onto the compute queue:**
- `routers/pipeline.py::_route_discovered_by_duration` (lines 306-331): `is_long and compute_agent is not None → cloud_files → _enqueue_analysis_jobs(compute_q, …)`.
- `tasks/release_awaiting_cloud.py::release_awaiting_cloud` (lines 80-89): drains `AWAITING_CLOUD` → `enqueue_process_file(compute_queue, …)` and resets to `DISCOVERED`.

Both **bypass the push step and the ≤N window**. Per the discretion hard-constraint ("a direct-to-compute enqueue that bypasses the push step or the window bound is a bug"), both must be reshaped.

**Recommendation — single-entry / hold-then-stage (strongly preferred over the fast-path):**
- In `_route_discovered_by_duration`, change the long-file branch to **always hold in `AWAITING_CLOUD`** (drop the `compute_agent is not None → cloud_files` direct-enqueue). Every cloud-routed long file becomes cloud-pending in one state.
- **Replace** `release_awaiting_cloud` (or evolve it) into the **staging/top-up cron**: instead of draining *all* held files to `process_file`, it stages **only up to the free window slots** to `push_file`. (Reset-to-DISCOVERED logic from D-03a is dropped; held files transition `AWAITING_CLOUD → PUSHING` when staged.)

Rationale: this is the "single entry, simplest invariant" option. The window is enforced in exactly one place (the cron), so it *cannot* be exceeded — the 144-file backfill, "Run analysis" on a large corpus, double-clicks, and double-ticks all funnel through the same bounded top-up. The fast-path option (enqueue `push_file` immediately when the window has room, in the router) creates two enqueue sites sharing one guard → higher TOCTOU surface for the load-bearing ≤N invariant. The ~5-min first-file latency cost is acceptable for hours-long analysis jobs.

**`AWAITING_CLOUD` semantics shift (note for planner):** in Phase 49 `AWAITING_CLOUD` meant "held only because no compute agent is online." In Phase 50 it becomes "cloud-pending, not yet staged" (held regardless of compute availability — the cron stages it when there's a window slot AND a compute agent). This is a deliberate, consistent broadening; update the dashboard card label/help text accordingly (the existing "Awaiting cloud" card stays, joined by the two new D-09 cards).

### 3. Adding `push_file` trips three totality guards — all must be updated together

`push_file` becomes the 9th keyed task. Verified guard tests that will **fail loudly** until updated (this is good — they force completeness):

| Guard | File | Required change |
|-------|------|-----------------|
| Every routable task keyed-or-exempt | `tests/test_deterministic_key.py:197` (`set(_KEY_BUILDERS) | _UNKEYED_TASKS >= routable`) | Add `"push_file"` to `_KEY_BUILDERS` (NOT `_UNKEYED_TASKS` — we *want* `push_file:<id>` dedup). |
| Counters in sync with keyed universe | `pipeline_counters.PIPELINE_FUNCTIONS` (sync comment at `deterministic_key.py:72`) | Add `"push_file"` to `PIPELINE_FUNCTIONS` tuple. |
| Recovery classification is TOTAL (predicate-covered XOR live-keys-only) over `_KEY_BUILDERS` | `tests/test_tasks/test_recovery.py` (asserts against `_KEY_BUILDERS`) | Classify `push_file` — see §recovery below. |
| Router routability | `enqueue_router.AGENT_TASKS` frozenset | Add `"push_file"` (it's a file-touching agent task → routes to the per-agent fileserver queue). |

## Architecture Patterns

### System Architecture Diagram

```
                        ┌─────────────────────────── CONTROL PLANE (app-server) ───────────────────────────┐
 "Run analysis" /       │                                                                                   │
 Backfill button  ──────┼─► _route_discovered_by_duration                                                   │
                        │      long file ─► state = AWAITING_CLOUD   (HOLD — no direct compute enqueue)      │
                        │                                                                                    │
                        │   CronJob(stage_cloud_window, "*/5")  ── gated on select_active_agent(compute) ──┐ │
                        │      window = COUNT(state IN {PUSHING, PUSHED})                                  │ │
                        │      slots  = cloud_max_in_flight − window                                        │ │
                        │      pick `slots` AWAITING_CLOUD (FIFO by created_at), state→PUSHING,             │ │
                        │      enqueue push_file ──────────────────────────────────────────────┐           │ │
                        │        (before_enqueue hook writes ledger row push_file:<id>)         │           │ │
                        └──────────────────────────────────────────────────────────────────────┼───────────┘ │
                                                                                                │             │
                          per-agent SAQ queue  phaze-agent-<fileserver_id>  ◄──────────────────┘             │
                                                                                                              │
        ┌───────────────── FILE-SERVER AGENT (kind=fileserver) ─────────────────┐                            │
        │  push_file(payload):                                                  │                            │
        │    rsync --partial-dir … -e "ssh -i KEY -o StrictHostKeyChecking=yes  │                            │
        │       -o UserKnownHostsFile=KNOWN_HOSTS"  <original_path>             │                            │
        │       <user>@<host>:<scratch_dir>/<file_id>.<ext>   ───── over Tailscale ─────►  (atomic rename)   │
        │    on rc==0 ► PATCH /api/internal/agent/push/{file_id} {pushed} ──────┼──────────────┐             │
        │    on rc!=0 ► raise (SAQ retry, --partial resumes); terminal ► report │              │             │
        └───────────────────────────────────────────────────────────────────────┘              ▼             │
                                                                              CONTROL: PUSHING→PUSHED,        │
                                                                              clear push_file ledger,         │
                                                                              enqueue process_file on         │
                                                                              compute queue w/ expected_sha256│
                                                                              + scratch_path ────────────┐    │
        ┌───────────────── COMPUTE AGENT (kind=compute, OCI arm64) ─────────────┐                        │    │
        │  startup janitor: sweep entire scratch_dir (off-loop)                 │ ◄──── phaze-agent-<compute_id>
        │  process_file(payload with scratch_path + expected_sha256):           │                             │
        │    read_path = scratch_path or original_path                          │                             │
        │    actual = await asyncio.to_thread(compute_sha256, read_path)        │                             │
        │    if actual != expected_sha256: delete scratch; report push-mismatch ┼──► CONTROL re-drive push    │
        │    else: analyze_file(read_path) in pebble pool                       │      (attempt<max) or        │
        │    finally: if scratch_path: Path(scratch_path).unlink(missing_ok)    │      ANALYSIS_FAILED         │
        │    PUT /api/internal/agent/analysis/{file_id} ───────────────────────┼──► CONTROL: PUSHED→ANALYZED  │
        └───────────────────────────────────────────────────────────────────────┘      clear process_file led│
```

### rsync-over-SSH from asyncio (the novel part — implementation-ready)

**Invocation (no shell — injection-safe).** `create_subprocess_exec` with a list argv; the `-e "ssh …"` is a *single* argv element rsync parses internally (it is NOT a shell):

```python
# Source pattern: stdlib asyncio.subprocess; rsync(1) / ssh(1) man pages.
rsync_args = [
    "rsync",
    "--partial-dir=.rsync-partial",   # resumable; partials kept out of the final-name space
    "--timeout", str(cfg.push_timeout_sec),   # I/O stall timeout -> rsync exit 30
    "-e",
    # ONE argv element. BatchMode=yes: never prompt (fail instead of hanging).
    f"ssh -i {key_path} -o StrictHostKeyChecking=yes "
    f"-o UserKnownHostsFile={known_hosts_path} -o BatchMode=yes "
    f"-o ConnectTimeout={cfg.push_connect_timeout_sec}",
    str(source_path),                                   # FileRecord.original_path on the media mount
    f"{cfg.push_ssh_user}@{cfg.push_ssh_host}:{remote_dest}",  # <scratch_dir>/<file_id>.<ext>
]
proc = await asyncio.create_subprocess_exec(
    *rsync_args,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
try:
    _out, err = await asyncio.wait_for(proc.communicate(), timeout=cfg.push_timeout_sec + 30)
except TimeoutError:
    proc.kill()
    await proc.wait()
    raise
if proc.returncode != 0:
    raise RuntimeError(f"rsync exit {proc.returncode}: {err.decode(errors='replace')[:500]}")
```

**Why these flags (D-06 discretion → concrete recommendation):**

| Flag | Decision | Rationale |
|------|----------|-----------|
| *(default temp-file behavior)* | **Keep — gives atomicity for free** | `[VERIFIED: rsync(1) behavior, cross-checked via RsyncProject discussions]` rsync writes a temp file `.<name>.XXXXXX` **in the destination directory** and `rename()`s it over the final name on completion. Same-dir rename is atomic → the compute agent never sees a half-written file at the final scratch path. **This satisfies D-06 without `--inplace`.** |
| `--inplace` | **DO NOT use** | Defeats the atomic-rename guarantee — writes bytes directly into the destination file, so a reader (or a crash) sees truncated/partial data at the final path. Directly contradicts D-06's "never sees a half-written file." |
| `--partial-dir=.rsync-partial` | **Use** | Resumability (D-06). Keeps the partial in a sibling dotdir, not under the final name, so an interrupted transfer never collides with the atomic final and the janitor can sweep `.rsync-partial` too. (Plain `--partial` also works but leaves partials in the final-name space.) |
| `--timeout=N` | **Use** | I/O stall → exit 30; bounds a hung transfer. Pair with ssh `ConnectTimeout` for connect stalls. |
| `-z` / `--compress` | **OMIT** | Audio (mp3/m4a/ogg/flac/aac/opus) is already compressed; `-z` burns CPU for ~0 gain. |
| `-c` / `--checksum` | **OMIT** | Forces a full read+checksum of both ends before transfer (expensive on multi-GB files). The app-level sha256 verify post-transfer (D-06 defense-in-depth) already covers integrity; rsync's per-block rolling checksum guards the wire during transfer. |
| `-a` / `--archive` | **OMIT** | Preserves perms/owner/times; owner-preserve fails cross-host without root and is irrelevant for an ephemeral scratch copy. Transfer the bytes only. |
| `-o BatchMode=yes` (ssh) | **Use** | Never prompt for a password/passphrase — fail fast instead of hanging a worker slot. |
| `-o StrictHostKeyChecking=yes` + `UserKnownHostsFile` | **Use (D-07)** | Pinned host key. Mount the operator-provisioned `known_hosts` via the `_FILE` secret convention. |

**Scratch filename = `<file_id>.<file_type>`** (or bare `<file_id>`). Use the server-generated UUID, **never the original filename** — this eliminates path-traversal / shell-metacharacter risk in the remote path and makes the cleanup/janitor target deterministically computable from `file_id`. Re-push overwrites cleanly (atomic rename replaces).

**rsync exit codes to handle** `[CITED: rsync(1) man page; samba.org rsync list]`:

| Code | Meaning | Handling |
|------|---------|----------|
| 0 | Success | PATCH "pushed" callback. |
| 23 | Partial transfer due to error (perms, I/O) | Failure → SAQ retry (`--partial` resumes). |
| 24 | Partial transfer due to vanished source files | Source changed/removed mid-transfer; failure → retry (will likely re-detect). |
| 30 | Timeout in data send/receive | Stall → retry. |
| 35 | Timeout waiting for daemon connection | Retry. |
| 12 | Protocol data-stream error | Retry; if persistent, config/version mismatch. |
| 255 | SSH transport failure (auth/host-key/network) | Retry, but persistent 255 = config error (bad key, host-key mismatch, Tailscale down) — surface clearly. |

**Two timeout layers (mirror the `process_file` 6600<7200 pattern):** the outer SAQ job `timeout` on `push_file` must exceed the inner rsync `--timeout`. A multi-GB concert file over Tailscale can take many minutes — recommend `push_file` SAQ `timeout` generous (e.g. 3600 s) with rsync `--timeout` for *stalls* (e.g. 600 s), inner < outer.

### sha256 verify off the event loop (CLOUDPIPE-03)

**Verified existing pattern** — `tasks/scan.py:268`: `sha256_hash = await asyncio.to_thread(compute_sha256, full_path)`. `compute_sha256` (`services/hashing.py`) is a chunked (64 KB) streaming hash, pure stdlib, **safe to import in the Postgres-free `process_file`** (no models/sqlalchemy).

In `process_file`, before analysis, when `payload.scratch_path` and `payload.expected_sha256` are set:
```python
actual = await asyncio.to_thread(compute_sha256, Path(payload.scratch_path))
if actual != payload.expected_sha256:
    # delete the corrupt scratch copy and report a NON-terminal push-integrity failure
    Path(payload.scratch_path).unlink(missing_ok=True)
    await api.<report_push_mismatch>(payload.file_id)   # control re-drives push (D-12)
    return {"file_id": str(payload.file_id), "status": "push_mismatch"}
```
(There is already a precedent for the verify-then-fail shape at `tasks/execution.py:170-174` — sha256 mismatch raises `ValueError`; reuse the comparison idiom.)

### "Stay one ahead" controller cron (CLOUDPIPE-01, the load-bearing ≤N invariant)

Model on `release_awaiting_cloud` (verified template). New `stage_cloud_window(ctx)` registered as `CronJob(stage_cloud_window, "*/5 * * * *")` on the **controller** (alongside the existing crons in `controller.py` `settings["cron_jobs"]`):

```
1. GATE: agent = select_active_agent(session, kind="compute")  # NoActiveAgentError -> clean no-op (hold, D-04)
2. WINDOW: window = COUNT(FileRecord.state IN {PUSHING, PUSHED})   # staged + in-flight
   slots = cloud_max_in_flight - window;  if slots <= 0: return {"staged": 0, ...}
3. SELECT next `slots` files WHERE state == AWAITING_CLOUD ORDER BY created_at ASC LIMIT slots
   (optionally FOR UPDATE SKIP LOCKED for belt-and-suspenders)
4. For each: state = PUSHING; enqueue push_file on the fileserver queue
   (before_enqueue hook writes the push_file:<id> ledger row; deterministic key dedups a double-tick)
5. commit
```

**Eligibility ordering (discretion → recommend FIFO):** `ORDER BY FileRecord.created_at ASC` (oldest cloud-pending first). Deterministic, fair, and matches operator intuition for a backlog drain. (The 144 backfilled failures all enter `AWAITING_CLOUD` together; FIFO drains them in a stable order.)

**TOCTOU / never-exceed-N analysis:**
- **Primary guarantee:** SAQ crons run on a *single controller worker* (one event loop); cron ticks do not overlap, so the count→stage sequence is effectively serialized. The committed `AWAITING_CLOUD → PUSHING` transition means the next tick's `COUNT(PUSHING…)` already includes just-staged files.
- **Backstop 1 (double-tick / double-click same file):** the `push_file:<file_id>` deterministic key collapses a repeat enqueue to a no-op (SAQ incomplete-set dedup), exactly as `process_file:<id>` does today.
- **Backstop 2 (window math):** because the window is counted from `FileState` (committed truth), the worst case of a stale read is *under*-staging (a transient dip below N), never *over*-staging — and the next tick corrects it. To make step 3+4 atomic even under hypothetical concurrent controllers, do the COUNT + SELECT + `state=PUSHING` UPDATE in **one transaction** and (optionally) `SELECT … FOR UPDATE SKIP LOCKED` the candidate rows. Recommend including the single-transaction guard; `FOR UPDATE SKIP LOCKED` is optional hardening given the single-worker reality.

This bounds scratch disk to ≤N files regardless of how many files the operator throws at it (the explicit anti-goal from D-03 / CLOUDPIPE-01).

### Recovery classification of PUSHING/PUSHED (CLOUDPIPE-05, D-10)

`push_file` needs an entry in the recovery model (`tasks/reenqueue.py`). Two sub-questions:

1. **Is `push_file` predicate-covered or live-keys-only?** The fileserver agent **cannot clear its own ledger row** (no `ledger_sessionmaker` on the agent queue — verified: the clear is a no-op agent-side). The clear happens in the control-side "pushed" callback (§1). But a *crash before the callback* leaves the row uncleared with no domain signal unless we add one. Therefore make `push_file` **predicate-covered**: add `"push_file"` to `_DOMAIN_COMPLETED_STAGES` with predicate **"done when `FileRecord.state` ∈ {PUSHED, ANALYZED, ANALYSIS_FAILED}"** (i.e., the file has advanced past pushing). A `push_file` ledger row whose file is still `PUSHING`/`AWAITING_CLOUD`/`DISCOVERED` is genuinely orphaned → re-drive.
2. **Where does a re-driven `push_file` route?** Like the AWAITING_CLOUD held-rows handling already in `recover_orphaned_work` (lines 304-332), a re-driven `push_file` must route to a **fileserver** agent (it reads the media mount). Add a partition mirroring the existing `held_agent_rows` logic, but selecting `kind="fileserver"` for `push_file`. **Important:** recovery re-driving `push_file` for a file already counted in the window does not break ≤N — the deterministic key dedups a still-live push, and a genuinely-lost push that's re-driven was already occupying its PUSHING slot. New work is only ever introduced by the staging cron.

**`process_file` "done" stays `{ANALYZED, ANALYSIS_FAILED}`** (D-10) — unchanged; PUSHED is *not* added to the analyze-done set (a PUSHED file still needs analysis).

### ProcessFilePayload extension + scratch read-path swap (D-11, D-13)

**Current schema (`schemas/agent_tasks.py:28-43`)** — `ProcessFilePayload(extra="forbid")` has `file_id, original_path, file_type, agent_id, models_path, fine_cap?, coarse_cap?`. **Add two optional fields (default None — preserves the local fileserver path and `extra="forbid"`):**
```python
expected_sha256: str | None = None   # control pins from FileRecord.sha256_hash (D-11)
scratch_path: str | None = None      # compute reads this ephemeral copy instead of original_path
```
**No separate boolean "ephemeral" flag needed** — `scratch_path is not None` *is* the compute-read/ephemeral signal (simpler, one source of truth). In `process_file`:
```python
read_path = payload.scratch_path or payload.original_path   # swap; analyzer is path-agnostic (verified analysis.py)
# ... sha256 verify (above) ...
try:
    analysis = await run_in_process_pool(ctx, _load_analyze_file(), read_path, payload.models_path, ...)
    # ... existing PUT result ...
finally:
    if payload.scratch_path:                                # D-13: cleanup on success OR terminal failure
        Path(payload.scratch_path).unlink(missing_ok=True)
```
The `finally` wraps the existing try/except body. The five-field bulk local producer (`_enqueue_analysis_jobs` → `enqueue_process_file`) passes neither new field → short local files behave exactly as today (CLOUDROUTE-03 unchanged-behavior guarantee).

**Extend `enqueue_process_file`** with keyword-only `expected_sha256: str | None = None`, `scratch_path: str | None = None` (mirroring how `fine_cap`/`coarse_cap` were added), threaded into the `ProcessFilePayload(...)`. The "pushed" callback (§1) calls it with both set.

### Scratch cleanup + startup janitor (CLOUDPIPE-04, D-13/D-14)

- **Per-job:** the `finally` `unlink(missing_ok=True)` above. Runs on success, timeout (`TimeoutError`), crash (`ProcessExpired`), generic error, and sha256-mismatch.
- **Startup janitor (compute-only):** in `agent_worker.startup`, gated on `cfg.kind == "compute"`, sweep the *entire* scratch dir off the event loop:
  ```python
  if cfg.kind == "compute":
      await asyncio.to_thread(_sweep_scratch, Path(cfg.cloud_scratch_dir))   # unlink every file + .rsync-partial
  ```
  **Full-sweep is safe and correct (D-14):** at startup no `process_file` job is running yet, so nothing is mid-read; any file still needed is `PUSHING`/`PUSHED` in DB state and will be re-pushed by the staging cron / recovery. No ORM needed (the agent is Postgres-free) — this is precisely why D-14 says "delete all" is safe (the window is small, re-push is on demand). **Precedent:** this is the agent-side analog of the controller's startup reconciliation (`backfill_ledger_from_saq_jobs` + `recover_orphaned_work` in `controller.startup`).
  **Gate carefully:** the fileserver agent runs the *same* `agent_worker` module — it must NOT sweep (it has no scratch dir; `cloud_scratch_dir` is unset/irrelevant for `kind=fileserver`). Gate on `kind == "compute"` AND a configured scratch dir.

### Recommended new files / change surface

```
src/phaze/tasks/push.py                     # NEW: push_file(ctx, **kwargs) + rsync argv builder + exit handling
src/phaze/schemas/agent_tasks.py            # ADD PushFilePayload; ADD expected_sha256/scratch_path to ProcessFilePayload
src/phaze/tasks/functions.py                # process_file: read_path swap, sha256 verify, finally-cleanup
src/phaze/tasks/agent_worker.py             # register push_file in functions[]; compute-only startup janitor
src/phaze/tasks/_shared/deterministic_key.py# _KEY_BUILDERS["push_file"] = lambda k: str(k["file_id"])
src/phaze/services/pipeline_counters.py     # PIPELINE_FUNCTIONS += ("push_file",)
src/phaze/services/enqueue_router.py        # AGENT_TASKS |= {"push_file"}
src/phaze/tasks/reenqueue.py                # _DOMAIN_COMPLETED_STAGES += push_file; fileserver-routed re-drive partition
src/phaze/tasks/release_awaiting_cloud.py   # EVOLVE into / REPLACE with stage_cloud_window (bounded top-up)
src/phaze/tasks/controller.py               # register stage_cloud_window cron (replace release_awaiting_cloud cron)
src/phaze/routers/pipeline.py               # _route_discovered_by_duration: long file -> always AWAITING_CLOUD; two new count cards
src/phaze/routers/agent_analysis.py (or new # control callbacks: PATCH .../push/{file_id} (pushed) + push-mismatch report
  routers/agent_push.py)                     #   -> state transition + ledger clear + enqueue process_file
src/phaze/services/analysis_enqueue.py      # enqueue_process_file: + expected_sha256/scratch_path kwargs
src/phaze/services/pipeline.py              # get_pushing_count / get_pushed_count (_safe_count); window-count helper
src/phaze/models/file.py                    # FileState.PUSHING, FileState.PUSHED (no migration)
src/phaze/config.py                         # ControlSettings: cloud_max_in_flight, push_max_attempts
  #                                           AgentSettings: push_ssh_host/user, push_ssh_key(_FILE),
  #                                           push_known_hosts(_FILE), cloud_scratch_dir, push_timeout_sec
src/phaze/services/agent_client.py          # PhazeAgentClient: report_pushed / report_push_mismatch methods
src/phaze/templates/pipeline/partials/      # two new count-card partials (clone awaiting_cloud_card.html)
```

### Anti-Patterns to Avoid
- **`asyncio.create_subprocess_shell` / `shell=True`** — command injection. Always `create_subprocess_exec` with a list argv. (Bandit `S602`/`S605` will flag the shell variants.)
- **`--inplace`** — defeats atomic delivery (D-06). Never combine with the atomicity requirement.
- **Enqueuing `process_file` from inside the fileserver `push_file`** — agent has no ORM to build `expected_sha256` or resolve the compute queue; use the control-side callback (§1).
- **Counting the window from the ledger or `saq_jobs` instead of `FileState`** — `FileState` (PUSHING/PUSHED) is the committed truth D-08 added precisely so the cron counts directly and never over-stages.
- **Sweeping scratch on the fileserver agent** — gate the janitor on `kind == "compute"`.
- **Putting `original_filename` (untrusted) into the rsync remote path** — use `<file_id>.<ext>`.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Resumable, atomic large-file transfer | A chunked HTTP upload + reassembly + manual rename | `rsync --partial-dir` over SSH | rsync already does atomic same-dir rename, resume, wire checksums, timeouts (D-06). |
| Streaming sha256 | A new hashing loop | `phaze.services.hashing.compute_sha256` via `asyncio.to_thread` | Already the codebase standard (`scan.py:268`); chunked, off-loop, stdlib-only (agent-boundary safe). |
| Deterministic job dedup | A custom "is this already queued?" check | `_KEY_BUILDERS["push_file"]` + SAQ incomplete-set | The whole point of the Phase-32 chokepoint; double-tick/double-click collapse to no-ops for free. |
| Restart/loss recovery for the push stage | A bespoke re-drive loop | `recover_orphaned_work` + `_DOMAIN_COMPLETED_STAGES` classification | Ledger-driven recovery already exists; just classify `push_file`. |
| Ledger write/clear | Manual INSERT/DELETE at the push site | `before_enqueue` hook (write) + control-side callback `clear_ledger_entry` | Mirrors `process_file`; keeps the agent Postgres-free. |
| Host-key trust | `StrictHostKeyChecking=no` / TOFU | Pinned `UserKnownHostsFile` + `StrictHostKeyChecking=yes` (D-07) | TOFU on first connect is a MITM window; pin the key (Phase 51 runbook provisions it). |

**Key insight:** every "hard" part of this phase (idempotency, recovery, dedup, integrity hashing, atomic delivery, off-loop CPU work) already has a blessed primitive in the repo or in rsync. The only truly new code is the ~40-line rsync argv builder + exit-code handling and the bounded-window cron math.

## Common Pitfalls

### Pitfall 1: Exceeding the ≤N window (scratch-disk blowup)
**What goes wrong:** "Run analysis" on the 144-file backfill (or a double-tick) stages many `push_file`s at once → compute scratch fills.
**Why it happens:** counting the window from a stale source, or leaving a direct-to-compute enqueue path (the Phase-49 router branch / `release_awaiting_cloud`) that bypasses the cron.
**How to avoid:** single-entry hold-then-stage (§Critical Finding 2); count from `FileState IN {PUSHING, PUSHED}`; do count+select+`state=PUSHING` in one transaction; rely on the deterministic key as the double-tick backstop.
**Warning signs:** scratch dir grows beyond `cloud_max_in_flight` files; more than N files in PUSHING/PUSHED simultaneously.

### Pitfall 2: Agent-boundary import violation
**What goes wrong:** `push.py` or the `process_file` sha256 path imports something that transitively pulls `sqlalchemy.ext.asyncio` → `tests/test_task_split.py` fails (and the agent could no longer run Postgres-free).
**Why it happens:** reaching for an ORM helper or `scheduling_ledger` from the agent task.
**How to avoid:** agent tasks talk to control only via `PhazeAgentClient` (HTTP). `compute_sha256` is import-safe (stdlib). Run `uv run pytest tests/test_task_split.py` after touching `push.py`/`functions.py`/`agent_worker.py`.

### Pitfall 3: rsync absent / wrong host key at runtime
**What goes wrong:** the agent image lacks `rsync` or `ssh`, or the pinned `known_hosts` doesn't match → exit 255 / FileNotFoundError on every push, files stuck PUSHING.
**Why it happens:** image provisioning is Phase 51; the host key must be operator-provisioned (D-07).
**How to avoid:** detect a missing binary (FileNotFoundError from `create_subprocess_exec`) and surface a clear terminal error rather than retrying forever; document the dependency in Environment Availability. Don't silently fall back to local analysis (CLOUDROUTE-02).
**Warning signs:** persistent exit 255; `rsync: command not found`.

### Pitfall 4: sha256-mismatch loop (no attempt cap)
**What goes wrong:** a persistently corrupt source or transfer path re-pushes forever.
**Why it happens:** re-drive without a max-attempts cap (D-12).
**How to avoid:** cap at `push_max_attempts` (default 3); after the cap → `ANALYSIS_FAILED` + clear ledger + cleanup so it surfaces on the dashboard instead of looping.
**Recommended attempt-counter storage (discretion):** carry `push_attempt: int = 0` **in the `push_file` ledger-row payload (JSONB)** — incremented when control re-drives on mismatch; when `attempt+1 > push_max_attempts`, transition to `ANALYSIS_FAILED` instead of re-enqueuing. Rationale: migration-free (the ledger payload is already JSONB and already replayed by recovery), survives the control-plane round trip, and lives next to the rest of the push job's identity. (Alternatives: a `FileRecord.push_attempts` column — needs an Alembic migration, against the code-only-state-changes grain of this milestone; SAQ `retries` — resets across the control-plane callback and conflates rsync-transport retries with integrity re-pushes. Both less preferred.)

### Pitfall 5: Killed worker skips the `finally` → orphaned scratch
**What goes wrong:** SIGKILL (OOM, container stop) skips the cleanup `finally`.
**Why it happens:** `finally` only runs on graceful exceptions, not SIGKILL.
**How to avoid:** the compute-startup janitor (D-14) full-sweeps the scratch dir on next boot; the file's PUSHING/PUSHED state + recovery re-drive re-push it if still needed. This is the explicit safety net for the hard-kill case.

## Runtime State Inventory

> This is a feature-addition phase, not a rename/refactor. Included because it introduces new persistent runtime state on a remote host.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | New `FileRecord.state` values `PUSHING`/`PUSHED` (code-only StrEnum over `String(30)`, no migration). Attempt counter in `scheduling_ledger.payload` JSONB (recommended). | Code edit only; no data migration. |
| Live service config | rsync/ssh push target (`push_ssh_host/user`, `cloud_scratch_dir`) lives in **fileserver AgentSettings** (env/`_FILE`). The pinned `known_hosts` + SSH identity key are operator-provisioned files mounted into the fileserver agent — **NOT in git** (D-07). Tailscale ACL + the actual provisioning are **Phase 51**. | Phase 50 adds the config *fields*; Phase 51 provisions the values/runbook. |
| OS-registered state | None — no OS scheduler/service registration. The compute agent's **scratch directory** is new on-disk state on the remote OCI box, bounded to ≤N files and swept at startup. | Janitor (D-14) reconciles. |
| Secrets / env vars | New `_FILE` secrets: `push_ssh_key` (SSH identity), `push_known_hosts` (pinned host key). Add to `AgentSettings.SECRET_FILE_FIELDS`; never log (D-13 token-preview discipline). | Code edit (config fields + secret-file resolution); operator supplies values in Phase 51. |
| Build artifacts / installed packages | Fileserver agent image must gain `rsync` + `openssh-client`; compute agent image needs a scratch volume. Both are **Phase 51 (CLOUDDEPLOY-01)**. No stale artifacts from this phase. | Phase 51 image/compose work; Phase 50 code assumes presence + degrades clearly if absent. |

## Code Examples

### Existing off-loop sha256 (the pattern to reuse) — `tasks/scan.py:268`
```python
# Source: src/phaze/tasks/scan.py (verified current)
sha256_hash = await asyncio.to_thread(compute_sha256, full_path)
```

### Existing agent→control callback that clears the ledger (the pattern `push_file` follow-on mirrors) — `routers/agent_analysis.py:189-196`
```python
# Source: src/phaze/routers/agent_analysis.py (verified current)
await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.ANALYZED))
# Phase 45 (L-02): clear the agent-stage scheduling-ledger row in the SAME transaction
await clear_ledger_entry(session, f"process_file:{file_id}")
```

### Existing cron template (the staging cron mirrors this shape) — `tasks/release_awaiting_cloud.py:44-92`
```python
# Source: src/phaze/tasks/release_awaiting_cloud.py (verified current)
async with ctx["async_session"]() as session:
    held = await get_files_by_state(session, FileState.AWAITING_CLOUD)
    if not held:
        return {"released": 0, "skipped": 0}
    try:
        agent = await select_active_agent(session, kind="compute")
    except NoActiveAgentError:
        return {"released": 0, "skipped": 0}        # clean no-op: hold (D-04)
    compute_queue = ctx["task_router"].queue_for(agent.id)
    # ... enqueue, transition, commit once ...
```

## State of the Art

| Old Approach (earlier research memory) | Current Approach (v5.0) | When Changed | Impact |
|----------------------------------------|-------------------------|--------------|--------|
| Upload → object storage → presigned-URL GET → reconcile by file_id | rsync push over SSH-over-Tailscale to a compute scratch dir; NO object storage | v5.0 scoping (2026-06-24) | Simpler, free (OCI A1), no S3/GCS dependency; the push is file-server-initiated (CLOUDPIPE-02). |
| `AWAITING_CLOUD` = "held only when no compute agent online" (Phase 49) | `AWAITING_CLOUD` = "cloud-pending, staged by the bounded cron" (Phase 50) | This phase | The staging cron is the single window driver; the Phase-49 direct-to-compute enqueue paths are removed. |

**Deprecated/outdated by this phase:**
- `release_awaiting_cloud`'s "drain ALL held → process_file directly + reset to DISCOVERED" behavior — replaced by bounded top-up to `push_file`.
- The `is_long and compute_agent → direct process_file` branch in `_route_discovered_by_duration`.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The agent runtime image will have `rsync` + `openssh-client` available (provisioned in Phase 51); Phase 50 code assumes presence. | Standard Stack / Pitfall 3 | If Phase 50 is exercised before Phase 51 lands, every push fails with FileNotFoundError/exit 255. Mitigation: clear terminal error + Environment Availability flag. |
| A2 | rsync's default temp-file-in-dest-dir + atomic rename holds on the compute agent's filesystem (same-dir rename is atomic). | rsync flags | Some exotic FS could break same-dir atomic rename; OCI A1 default ext4/xfs is fine. `[VERIFIED: rsync(1) behavior; ext4/xfs rename() is atomic POSIX]`. |
| A3 | A single controller worker runs the staging cron (no concurrent ticks). | Stay-one-ahead cron | If multiple controller workers ran, two ticks could over-stage; the deterministic key + committed PUSHING transition + single-transaction count/select mitigate, but `FOR UPDATE SKIP LOCKED` would be needed for true multi-worker safety. Current deploy = single controller worker. |
| A4 | Recommended config names (`cloud_max_in_flight`, `push_max_attempts`, `cloud_scratch_dir`, `push_ssh_host/user`, `push_ssh_key`, `push_known_hosts`, `push_timeout_sec`) — these are *recommendations*, not locked (D-05/D-03/D-12 left names to discretion). | Config | Wrong names are cosmetic; Phase 51 (CLOUDDEPLOY-02) formalizes the full knob set + master toggle. Planner/operator confirm. |
| A5 | Attempt counter in the ledger payload JSONB (vs a FileRecord column). | Pitfall 4 | If the team prefers an explicit column, that's a migration; the ledger-payload approach is migration-free and recommended but not locked. |
| A6 | The push-success follow-on goes through a control-side callback rather than an agent-side enqueue (refinement of D-01). | Critical Finding 1 | If a reviewer insists on the literal agent-side enqueue, it's blocked by the Postgres-free boundary (cannot read `sha256_hash` / resolve compute queue). Needs explicit sign-off that the callback IS the implementation of D-01. |

## Open Questions (RESOLVED)

1. **Does the push-mismatch re-drive go via `AWAITING_CLOUD` (re-enter staging, gives up the window slot) or a direct `push_file` re-enqueue (keeps the slot)?**
   - What we know: D-12 says "control plane re-drives the push up to max attempts."
   - What's unclear: whether the re-push reuses the in-flight slot or re-queues through the window.
   - Recommendation: **direct `push_file` re-enqueue keeping the PUSHING slot** (the file already occupies a window slot; sending it back to AWAITING_CLOUD would free the slot and let an unrelated file in, then this one re-competes — churns window accounting). Increment `push_attempt` in the ledger payload; on cap, transition `ANALYSIS_FAILED`.
   - **(RESOLVED):** Direct `push_file` re-enqueue keeping the PUSHING slot — implemented in plan 50-05 Task 2 (mismatch callback increments `push_attempt` in ledger payload; caps → `ANALYSIS_FAILED`).

2. **One callback endpoint or two?** A "pushed" success callback (transition + clear + enqueue `process_file`) and a "push-mismatch" callback (re-drive/cap) could be one `PATCH .../push/{file_id}` with a status body or two endpoints.
   - Recommendation: one endpoint, `{"status": "pushed" | "mismatch"}`, mirroring how `put_analysis` vs `report_analysis_failed` split — actually the existing code uses two endpoints (`put_analysis`, `report_analysis_failed`), so two endpoints is the more consistent choice. Planner's call.
   - **(RESOLVED):** Two endpoints (mirrors the existing `put_analysis` / `report_analysis_failed` split) — implemented in plan 50-05.

3. **Master enable toggle** (CLOUDDEPLOY-04) is Phase 51, but the staging cron will run `*/5` as soon as it's registered. Should Phase 50 gate the cron on a config flag now (no-op when no compute agent online already makes it inert)?
   - Recommendation: the `select_active_agent(kind="compute")` gate already makes the cron a clean no-op with no compute agent, so Phase 50 needs no extra toggle; the explicit master toggle is correctly Phase 51's. No action needed in 50.
   - **(RESOLVED):** No Phase-50 toggle — the `select_active_agent(kind="compute")` gate makes the cron inert with no compute agent; the master toggle stays Phase 51 (CLOUDDEPLOY-04). Correctly absent from the plans.

## Environment Availability

| Dependency | Required By | Available (dev host) | Version | Fallback |
|------------|------------|----------------------|---------|----------|
| `rsync` (system) | `push_file` transport | dev: usually present on macOS; **agent image: NOT yet** | — | None — clear terminal error; provision in Phase 51 image. |
| `openssh-client` (`ssh`) | `push_file` `-e ssh` transport | dev: present on macOS; **agent image: NOT yet** | — | None — same as above. |
| Tailscale path file-server→compute | the actual transfer | Not in Phase 50 scope (Phase 51 ACL) | — | None — Phase 51 provisions. |
| Postgres + Redis (broker/cache) | cron, ledger, queues | ✓ (existing) | 16+/7+ | — |
| essentia (compute analysis) | `process_file` on compute | ✓ on arm64 image (Phase 47) | TF2.20/py3.14 | — |

**Missing dependencies with no fallback:** `rsync` + `ssh` in the **file-server agent image** — Phase 50 code assumes them; image provisioning is Phase 51 (CLOUDDEPLOY-01). Plan must (a) detect absence and fail the push with a clear, non-retrying-forever error, and (b) NOT fall back to local analysis (CLOUDROUTE-02). This is the only true cross-phase coupling.

## Validation Architecture

> `nyquist_validation: true` in `.planning/config.json` — section included.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (+ pytest-cov), per CLAUDE.md |
| Config file | `pyproject.toml` `[tool.pytest…]` / `[tool.coverage…]` |
| Quick run command | `uv run pytest tests/test_task_split.py tests/test_deterministic_key.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CLOUDPIPE-01 | Window never exceeds N; cron tops up to N | unit | `uv run pytest tests/test_tasks/test_stage_cloud_window.py -x` | ❌ Wave 0 |
| CLOUDPIPE-02 | rsync argv built correctly (no shell; pinned known_hosts; atomic flags); exit-code handling | unit | `uv run pytest tests/test_tasks/test_push.py -x` | ❌ Wave 0 |
| CLOUDPIPE-03 | sha256 verify before analyze; mismatch → delete + report (no analyze) | unit | `uv run pytest tests/test_tasks/test_process_file_scratch.py -x` | ❌ Wave 0 |
| CLOUDPIPE-04 | `finally` cleanup on success/timeout/crash/mismatch; compute-only startup janitor sweep | unit | `uv run pytest tests/test_tasks/test_scratch_cleanup.py -x` | ❌ Wave 0 |
| CLOUDPIPE-05 | `push_file:<id>` dedup; recovery re-drives orphaned push to a fileserver; double-tick no-op | unit | `uv run pytest tests/test_deterministic_key.py tests/test_tasks/test_recovery.py -x` | ⚠️ extend existing |
| (boundary) | agent worker stays Postgres-free with `push.py` added | unit (subprocess) | `uv run pytest tests/test_task_split.py -x` | ✅ extend |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_task_split.py tests/test_deterministic_key.py -x` (fast guards that catch boundary/totality breaks immediately).
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (85% gate).
- **Phase gate:** full suite green + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files` before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/test_tasks/test_push.py` — rsync argv builder + exit-code mapping (mock `asyncio.create_subprocess_exec`; assert no `shell`, pinned `StrictHostKeyChecking=yes`, no `--inplace`).
- [ ] `tests/test_tasks/test_stage_cloud_window.py` — window math: N=2, simulate window full → 0 staged; window with 1 free slot → 1 staged; no compute agent → no-op; FIFO ordering.
- [ ] `tests/test_tasks/test_process_file_scratch.py` — `scratch_path` read swap; sha256 match → analyze; mismatch → delete + report + no analyze.
- [ ] `tests/test_tasks/test_scratch_cleanup.py` — `finally` unlink across all exit paths; janitor sweeps + gated on `kind=="compute"`.
- [ ] Extend `tests/test_deterministic_key.py` (`push_file` keyed) + `tests/test_tasks/test_recovery.py` (classification totality + fileserver re-drive routing).
- [ ] Extend `tests/test_task_split.py` to import the new `push.py`/`functions.py` graph.
- Framework install: none (pytest already configured).

## Security Domain

> `security_enforcement` absent in config → treated as enabled. This phase introduces a subprocess + SSH transport + remote writes, so security is genuinely load-bearing.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | SSH public-key auth (`-i` identity key, `_FILE` secret); existing token auth on the internal-API callbacks. |
| V3 Session Management | no | No user sessions in this path. |
| V4 Access Control | yes | File-server initiates, compute only receives (CLOUDPIPE-02 directional invariant); Tailscale ACL scopes the path (Phase 51). |
| V5 Input Validation / Injection | yes | `create_subprocess_exec` with a **list argv, never a shell** — primary command-injection control. Scratch path derived from server UUID `file_id`, not untrusted filename. |
| V6 Cryptography | yes | Pinned host key `StrictHostKeyChecking=yes` + mounted `UserKnownHostsFile` (no TOFU); sha256 integrity verify; SSH key + known_hosts as `_FILE` secrets, never logged. |
| V12 Files & Resources | yes | Bounded scratch (≤N) + cleanup + janitor; no path traversal (UUID filenames); `unlink(missing_ok=True)` confined to `cloud_scratch_dir`. |

### Known Threat Patterns for {rsync/SSH subprocess + remote scratch}
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Command injection via shell metacharacters in a path | Tampering / Elevation | `create_subprocess_exec` list argv, no `shell=True`; UUID-derived remote path. |
| MITM on first SSH connect (TOFU) | Spoofing / Tampering | Pinned `known_hosts` + `StrictHostKeyChecking=yes` (D-07); Tailscale authenticates the network path. |
| Corrupt/partial transfer analyzed as valid | Tampering | app-level sha256 verify before analysis (CLOUDPIPE-03) + rsync atomic rename (no half-files). |
| Secret leakage (SSH key / host key in logs) | Information Disclosure | `_FILE` secrets in `SECRET_FILE_FIELDS`; D-13 token-preview logging discipline; never log the `-i` path contents or full rsync stderr beyond a bounded snippet. |
| Scratch-disk exhaustion (DoS) | Denial of Service | ≤N window (CLOUDPIPE-01) + per-job cleanup + startup janitor (CLOUDPIPE-04). |
| Compute agent reaching back to pull (boundary inversion) | Elevation | Hard directional invariant — compute only receives; no pull path exists. |
| Hung transfer pins a worker slot forever | Denial of Service | rsync `--timeout` + ssh `ConnectTimeout` + outer SAQ job timeout; `BatchMode=yes` prevents auth-prompt hangs. |

## Sources

### Primary (HIGH confidence)
- Current repository source (verified by direct read): `enqueue_router.py`, `agent_task_router.py`, `deterministic_key.py`, `functions.py`, `agent_tasks.py`, `analysis_enqueue.py`, `reenqueue.py`, `agent_worker.py`, `controller.py`, `release_awaiting_cloud.py`, `scheduling_ledger.py` (+ model), `file.py`, `config.py`, `pipeline.py` (router + service), `agent_analysis.py`, `hashing.py`, `execution.py`, `pipeline_counters.py`, and the guard tests `test_task_split.py` / `test_deterministic_key.py` / `test_tasks/test_recovery.py`.
- `50-CONTEXT.md` (D-01..D-14), `49-CONTEXT.md`, `ROADMAP.md` §Phase 50/51, `REQUIREMENTS.md` (CLOUDPIPE/CLOUDDEPLOY/CLOUDROUTE).
- rsync(1) / ssh(1) man-page semantics for exit codes and atomic temp-file rename.

### Secondary (MEDIUM confidence)
- [rsync atomic transfer / temp-file rename behavior (RsyncProject discussions)](https://github.com/RsyncProject/rsync/discussions/651) — default same-dir temp + atomic rename; `--temp-dir` cross-FS loses atomicity.
- [rsync exit code 23 (partial transfer) — samba.org rsync list](https://lists.samba.org/archive/rsync/2023-December/033084.html)
- [rsync timeout exit code 30 (Plesk forum)](https://talk.plesk.com/threads/migration-failed-rsync-error-timeout-in-data-send-receive-code-30-at-io-c-195-sender-3-1-2.352378/)
- [rsync error code 23 explainer (Bobcares)](https://bobcares.com/blog/rsync-error-code-23/)

## Metadata

**Confidence breakdown:**
- Codebase wiring (two-stage flow, ledger, recovery, payload, cron, routing seam): **HIGH** — every claim verified against current source, including the guard tests that enforce completeness.
- rsync/SSH flags + exit codes + atomic-rename: **HIGH** — standard, stable man-page behavior cross-checked against multiple sources.
- D-01 callback refinement + attempt-counter + eligibility ordering recommendations: **MEDIUM** — sound and code-consistent, but discretion areas the planner/operator should lock.
- Cross-phase dependency on Phase 51 image (rsync/ssh binaries, Tailscale, known_hosts): **HIGH** that the dependency exists; **the provisioning itself is out of scope.**

**Research date:** 2026-06-25
**Valid until:** 2026-07-25 (codebase wiring is the dominant content and is stable; re-verify the change-surface line references if Phase 50 planning slips past a major refactor).
