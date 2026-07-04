---
phase: 70-multi-kueue-n-clusters
plan: 02
subsystem: s3-staging
tags: [s3, staging, bucket, kueue, multi-cluster, presign, reconcile, backends, mkue-02]

# Dependency graph
requires:
  - phase: 70-multi-kueue-n-clusters
    plan: 01
    provides: "cloud_job.staging_bucket column (migration 030) + pure pick_bucket(file_id, bucket_ids) selector"
  - phase: 67-backend-registry
    provides: "BucketConfig / KueueBackend discriminated-union submodels in config_backends.py"
provides:
  - "bucket-parameterized s3_staging: every public verb takes bucket: BucketConfig; _staging_config + active_bucket retired (MKUE-02)"
  - "resolve_bucket_config(cfg, bucket_id): pure ORM-free inverse of pick_bucket, resolving a recorded staging_bucket id to its BucketConfig"
  - "KueueBackend.dispatch stamps staging_bucket = pick_bucket(file.id, config.buckets) + backend_id in the caller's txn (D-06/D-01)"
  - "every presign-GET / inline-delete / complete / abort / reconcile-delete READS the recorded staging_bucket (never re-derives; Pitfall 4)"
affects: [70-03, 70-04, 70-05, multi-kueue, s3-staging, reconcile, backends, agent-callbacks]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Lift-a-global-to-a-parameter refactor: a module-global config read (active_bucket) becomes an explicit per-call bucket: BucketConfig, resolved authoritatively by the caller"
    - "Authoritative-record-then-read: dispatch records the D-06 pick on cloud_job.staging_bucket; presign/cleanup resolve that recorded id via resolve_bucket_config, never re-deriving via pick_bucket (config-drift-safe)"
    - "None-bucket skip: a NULL staging_bucket (compute / unstaged row) short-circuits every S3 op with zero client builds, mirroring the all-local guard"

key-files:
  created: []
  modified:
    - src/phaze/services/s3_staging.py
    - src/phaze/services/cloud_staging.py
    - src/phaze/services/backends.py
    - src/phaze/routers/agent_s3.py
    - src/phaze/routers/agent_files.py
    - src/phaze/routers/agent_analysis.py
    - src/phaze/tasks/reconcile_cloud_jobs.py
    - src/phaze/config.py
    - tests/analyze/services/test_s3_staging.py
    - tests/analyze/services/test_cloud_staging.py
    - tests/analyze/services/test_backends.py
    - tests/agents/routers/test_agent_s3.py
    - tests/agents/routers/test_agent_presign_download.py
    - tests/agents/routers/test_agent_analysis_inline_delete.py
    - tests/analyze/tasks/test_reconcile_cloud_jobs.py
    - tests/analyze/core/test_staging_cron.py
    - tests/analyze/core/test_dispatch_snapshot.py
    - tests/shared/config/test_bucket_registry.py

key-decisions:
  - "resolve_bucket_config landed as a single public helper in s3_staging.py (pure, ORM-free — reads only cfg.buckets) rather than three duplicate module-level _resolve_bucket_config copies; every caller (cloud_staging, backends, routers, reconcile) imports the one seam"
  - "presign_download and report_uploaded 409 when an UPLOADED cloud_job has no resolvable staging_bucket — a corrupt state returns not-ready rather than a dead S3 URL or a None-bucket crash (Rule 2 correctness guard)"
  - "reconcile at-cap KEEPS the current commit-then-delete ordering (only made bucket-aware); the clean-before-flip reorder is explicitly deferred to Plan 05 per the plan note"
  - "The signature change + all callers landed as ONE atomic source commit — the global `uv run mypy .` pre-commit hook rejects any intermediate state and --no-verify is forbidden by CLAUDE.md, so per-task source splitting is infeasible for a mutual-arity refactor"

patterns-established:
  - "s3_staging is the single home of both pick_bucket (stage-time selector) and resolve_bucket_config (read-time resolver); the module stays ORM-free and the router/caller passes cfg down"

requirements-completed: [MKUE-01, MKUE-02]

# Metrics
duration: 55min
completed: 2026-07-04
---

# Phase 70 Plan 02: Deterministic Per-File Bucket Staging Summary

**Lift the module-global `active_bucket` read out of `s3_staging` onto an explicit per-call `bucket: BucketConfig`; make `KueueBackend.dispatch` pick the bucket via `pick_bucket` and RECORD it on `cloud_job.staging_bucket`; make every presign/delete/complete/abort/reconcile call site READ that recorded bucket (never re-derive) — landing atomically with all callers so the suite stays green.**

## Performance

- **Duration:** ~55 min
- **Started:** 2026-07-04T18:30:00Z
- **Completed:** 2026-07-04T19:24:00Z
- **Tasks:** 3 (delivered as 2 commits — see Deviations)
- **Files modified:** 18 (8 source, 10 test)

## Accomplishments

- **Task 1 — parameterize `s3_staging` (MKUE-02):** deleted `_staging_config()`; added a `bucket: BucketConfig` parameter to all 7 public verbs (`create_multipart_upload`, `presign_upload_parts`, `complete_multipart_upload`, `abort_multipart_upload`, `presign_get`, `delete_staged_object`, `ensure_bucket_lifecycle_ttl`), reading the kept-global tuning knobs (TTLs, part-size) locally from `get_settings()`. Added the pure, ORM-free `resolve_bucket_config(cfg, bucket_id)` — the authoritative inverse of `pick_bucket` (resolves a recorded id to its `BucketConfig`, `None` for a NULL/absent id). Module stays ORM-free.
- **Task 2 — record `staging_bucket` at dispatch (D-06/D-01):** threaded `bucket` through `cloud_staging._stage_file_to_s3`/`stage_file_to_s3` and stamped `staging_bucket = bucket.id` on both the upsert `.values(...)` and the `on_conflict_do_update(set_={...})`; `redrive_upload` resolves the recorded bucket (with a re-pick-over-the-backend's-set fallback) and threads it into abort + re-stage. `KueueBackend.dispatch` computes `pick_bucket(file.id, self.config.buckets)`, resolves the `BucketConfig`, stages into it, and records `backend_id` + `staging_bucket` in the same uncommitted session. `agent_s3.report_uploaded` completes the multipart against the recorded bucket.
- **Task 3 — read-recorded-bucket at presign/delete; retire `active_bucket` (Pitfall 4):** `agent_files.presign_download`, `agent_analysis._delete_staged_object_if_cloud`, `agent_s3.report_upload_failed` at-cap cleanup, and `reconcile_cloud_jobs._handle_no_callback_terminal` at-cap delete all read `cloud_job.staging_bucket`, resolve its `BucketConfig`, and pass it to the parameterized verb. A `None` bucket (compute / unstaged row) skips the S3 op cleanly (mirrors the all-local guard); an UPLOADED row with an unresolvable bucket returns 409 rather than a dead URL. `config.active_bucket` retired. No call site re-derives via `pick_bucket`.

## Task Commits

1. **feat(70-02): source refactor (Tasks 1–3)** — `1ff4b56`
2. **test(70-02): per-file bucket coverage (Tasks 1–3)** — `7fcb3ce`

## Files Modified

**Source:**
- `src/phaze/services/s3_staging.py` — 7 bucket-parameterized verbs; `_staging_config` deleted; new `resolve_bucket_config`; ORM-free preserved.
- `src/phaze/services/cloud_staging.py` — `bucket` threaded through the staging core + wrapper; upsert stamps `staging_bucket`; `_redrive_bucket` resolver + bucket-aware `redrive_upload`.
- `src/phaze/services/backends.py` — `KueueBackend.dispatch` picks + resolves the D-06 bucket and records `backend_id` + `staging_bucket`; imports `s3_staging`.
- `src/phaze/routers/agent_s3.py` — `report_uploaded` completes against the recorded bucket (409 on unresolvable); at-cap abort+delete act on the recorded bucket (None-skip).
- `src/phaze/routers/agent_files.py` — `presign_download` selects `staging_bucket`, resolves it, presigns against it (409 on unresolvable).
- `src/phaze/routers/agent_analysis.py` — inline-delete reads `staging_bucket`; None-bucket row skips S3 (mirrors all-local guard).
- `src/phaze/tasks/reconcile_cloud_jobs.py` — at-cap delete resolves + acts on the recorded bucket (commit-then-delete ordering preserved; reorder deferred to Plan 05).
- `src/phaze/config.py` — `active_bucket` property retired.

**Tests:** `test_s3_staging.py`, `test_cloud_staging.py`, `test_backends.py`, `test_agent_s3.py`, `test_agent_presign_download.py`, `test_agent_analysis_inline_delete.py`, `test_reconcile_cloud_jobs.py`, `test_staging_cron.py`, `test_dispatch_snapshot.py`, `test_bucket_registry.py` — updated to pass/seed/resolve the per-file bucket; added 2-bucket "the CALLED bucket is the one acted on" cases, dispatch record assertions, None-bucket skip, and unresolvable-bucket 409 coverage.

## Deviations from Plan

### Rule 3 — auto-fixed blocking issues

**1. [Rule 3 - Blocking] Test files outside the plan's `files_modified` list broke on the signature change**
- **Found during:** Tasks 1 & 3.
- **Issue:** `tests/analyze/core/test_staging_cron.py` and `tests/analyze/core/test_dispatch_snapshot.py` call `_stage_file_to_s3`/`stage_file_to_s3` and drive `KueueBackend.dispatch` through the drain; `tests/shared/config/test_bucket_registry.py` referenced the retired `active_bucket`. None were in the plan's file list but all broke.
- **Fix:** threaded a `bucket` param into the direct staging-core calls; gave the cron/snapshot `_StubCfg` a `buckets` registry + a kueue backend `buckets` binding and pinned `phaze.services.backends.get_settings` so `dispatch` resolves the picked bucket; replaced the obsolete `active_bucket`-raises test with a "multi-bucket kueue now resolves" test.
- **Files modified:** `tests/analyze/core/test_staging_cron.py`, `tests/analyze/core/test_dispatch_snapshot.py`, `tests/shared/config/test_bucket_registry.py`.
- **Commit:** `7fcb3ce`.

**2. [Rule 3 - Blocking] `test_agent_files.py` in the plan is actually `test_agent_presign_download.py`**
- **Issue:** the plan listed `tests/agents/routers/test_agent_files.py`; the presign-download route tests live in `tests/agents/routers/test_agent_presign_download.py` (the `tests/discovery/routers/test_agent_files.py` file covers only the upsert route). Updated the correct file.

### Rule 2 — auto-added correctness guards

**3. [Rule 2 - Correctness] 409 on an UPLOADED row with an unresolvable `staging_bucket`**
- **Issue:** `presign_download` / `report_uploaded` must return a URL / complete the multipart, so the None-skip used for delete/abort would strand or crash them.
- **Fix:** both resolve the recorded bucket and return `409` when it does not resolve (corrupt state — never a dead S3 URL or a `None`-bucket crash). Added `test_presign_download_unresolvable_bucket_returns_409`.

### Structural

**4. Single `resolve_bucket_config` seam instead of three `_resolve_bucket_config` copies**
- The plan described a module-level `_resolve_bucket_config` added to `reconcile_cloud_jobs` (and inline dicts elsewhere). Consolidated into one public, pure, ORM-free `s3_staging.resolve_bucket_config(cfg, bucket_id)` imported by every caller — DRY, and it keeps the single S3/bucket home authoritative. All must-haves and acceptance criteria are satisfied (no re-derive; `s3_staging` ORM-free; `active_bucket`/`_staging_config` gone).

**5. Tasks committed as 2 atomic commits (source, tests) rather than 3 per-task commits**
- The `s3_staging` signature change + all-callers update is a mutually-atomic mypy unit. The global `uv run mypy .` pre-commit hook rejects any intermediate committed snapshot, and CLAUDE.md forbids `--no-verify`, so per-task source splitting is impossible. Source landed in one `feat` commit and the per-file-bucket test coverage in one `test` commit; both passed the full hook suite.

## Authentication Gates

None.

## Known Stubs

None — every call site is fully wired to the recorded `staging_bucket`; no placeholder/mock data left in the runtime path.

## Threat Flags

None — no new network endpoints, auth paths, or trust-boundary schema beyond the plan's `<threat_model>`. All five register mitigations are honored: objects stay private behind short-TTL control-side presigns (T-70-02); callers read the recorded bucket and never re-derive (T-70-02-02); the per-bucket http(s) SSRF validator is reused unchanged (T-70-02-03); creds stay `SecretStr` and `s3_staging` logs no `BucketConfig` (T-70-02-04); `s3_staging` stays ORM-free, verified by the purity check (T-70-02-05).

## Verification

- `uv run pytest tests/analyze/services tests/agents/routers tests/analyze/tasks/test_reconcile_cloud_jobs.py tests/shared/config -q` → **357 passed** (against the ephemeral test DB on 5433).
- `uv run pytest tests/analyze -q` → **429 passed** (confirms the drain/dispatch snapshot + staging cron suites are green).
- Purity: `grep -L "import phaze.models" src/phaze/services/s3_staging.py` matches → **no ORM import**.
- `uv run ruff check .` → All checks passed; `uv run ruff format --check` clean; `uv run mypy .` → no issues in 192 source files.
- Both task commits passed the full pre-commit hook suite (ruff, ruff-format, bandit, mypy) with no `--no-verify`.

## Acceptance Criteria (per task)

- **Task 1:** `grep -v '^#' s3_staging.py | grep -c "_staging_config"` == 0 ✓; 7 verbs carry `bucket: BucketConfig` ✓; no `import phaze.models` ✓; `test_s3_staging.py` green ✓.
- **Task 2:** `KueueBackend.dispatch` contains `pick_bucket` + `staging_bucket` ✓; `_stage_file_to_s3` signature contains `bucket` and the upsert sets `staging_bucket` ✓; `test_backends.py` + `test_cloud_staging.py` green ✓.
- **Task 3:** `grep -v '^#' config.py | grep -c "def active_bucket"` == 0 ✓; `presign_download` passes a bucket resolved from `staging_bucket` ✓; no router calls `pick_bucket` for presign/delete ✓; the five named router/task test suites green ✓.

## Next Phase Readiness

- **Plan 03** can consume `KubeConfig.context` and thread the per-backend `KubeConfig` into `kube_staging` (retiring `active_kube`); `active_bucket` is already gone, so only `active_kube` remains transitional.
- **Plan 05** owns the reconcile clean-before-flip reorder (delete the old object UNDER the still-held advisory lock, before the commit that makes the file a drain candidate) — this plan left the ordering as commit-then-delete but made the delete bucket-aware, so the reorder is a localized change.

## Self-Check: PASSED

`70-02-SUMMARY.md` exists on disk; both task commits (`1ff4b56` feat, `7fcb3ce` test) are present in git history.
