# Phase 67 — Deferred Items

Out-of-scope discoveries logged during execution (not fixed in the plan that found them).

## D-67-06-01 — RESOLVED (orchestrator post-merge fix, 2026-07-03) — `test_agent_presign_download.py` still uses the removed flat `PHAZE_S3_*` env vars (pre-existing wave-4 gap)

- **Resolution:** The `s3_env` fixture was migrated to the shared `backends_toml_env` conftest
  fixture (one-kueue-backend + one shared `[[buckets]]` block pointed at the moto server), exactly
  as Plan 04 did for `test_s3_staging`. All 7 tests in the file pass; ruff clean; `tests/` is
  excluded from the project mypy gate. Fixed by the phase orchestrator during the post-merge
  integration gate rather than deferred to a follow-up phase.


- **Found during:** Plan 67-06 (removal wave) overall verification.
- **File:** `tests/agents/routers/test_agent_presign_download.py` (the `s3_env` fixture, lines ~50-61).
- **Symptom:** 3 tests fail (`test_presign_download_returns_url_and_server_sourced_sha256`,
  `test_presign_download_body_validates_against_response_schema`,
  `test_presign_download_mints_per_call_with_server_sourced_hash`) — the presign-download route
  goes through `services/s3_staging._staging_config()`, which resolves the bucket via the
  transitional `active_bucket` accessor. `active_bucket` derives ONLY from a `backends.toml`
  `[[buckets]]` registry, so the fixture's flat `PHAZE_S3_ENDPOINT_URL` / `PHAZE_S3_BUCKET` /
  `PHAZE_S3_REGION` / creds env vars never populate it → `active_bucket is None` → fail-loud.
- **Root cause = Wave 4 (Plan 67-04), NOT this plan.** Plan 04 rewired `s3_staging._staging_config`
  to read `active_bucket` and migrated its own three seam tests (`test_s3_staging`,
  `test_kube_staging`, `test_cloud_staging`) to the shared `backends_toml_env` fixture, but did
  NOT migrate this route-level test, which also exercises the rewired `s3_staging` internals.
  Plan 04's SUMMARY assumed "staging-adjacent tests ... never touch the rewired internals" — this
  one does (via the real route).
- **Proof it predates Plan 67-06:** with `src/phaze/config.py` reverted to its pre-Plan-06 state
  (flat `s3_*` fields still present), the same 3 tests fail identically — the flat fields were
  never read by the route after Wave 4, so removing them changes nothing. `uv run mypy .` is clean
  (the source-side reader is correctly rewired; only the test fixture is stale), so the
  runtime-breakage threat T-67-06-03 is satisfied.
- **Fix (deferred):** migrate the `s3_env` fixture to write a one-kueue-backend + one-bucket
  `backends.toml` via the shared `backends_toml_env` conftest fixture (endpoint = the moto server
  URL), exactly as Plan 04 did for `test_s3_staging` / `test_cloud_staging`. Small, mechanical, and
  fully in the spirit of the removal wave; left out of Plan 06 because it is outside this plan's
  named file set and the failure is a pre-existing Wave-4 defect.

## D-67-CR-01b — remaining unguarded transitional-accessor reads degrade to page/cron/route errors under a premature multi-cluster registry (Phase 68/69 hardening)

- **Found during:** Phase 67 code-review gate (67-REVIEW.md CR-01).
- **Context:** A registry with >1 non-local backend is SCHEMA-VALID (multi-cluster is the milestone
  goal; D-09 exists precisely to police shared-vs-cluster-specific bucket sharing across multiple
  kueue backends), so `ControlSettings()` constructs and `cloud_enabled` is `True`. The ≤1-non-local
  transitional accessors (`active_cloud_kind`/`active_cap`/`active_bucket`/…) then raise on read
  because single-selection dispatch lands in Phase 69 (SCHED). The reviewer's proposed
  construction-time rejection was NOT applied — it would break the valid multi-cluster schema and make
  the D-09 validation dead code.
- **Resolved here (the severe case):** `controller.startup` read `cfg.active_cloud_kind` OUTSIDE its
  try/except, so a valid multi-cluster registry aborted the whole control-worker boot — a direct
  violation of the D-05 "control plane boots regardless" invariant the same function documents. Fixed
  by wrapping the read so it degrades to "skip the Kueue probe" + clears the stale flag; locked by
  `test_multi_backend_registry_does_not_abort_boot`.
- **Deferred (lower-severity graceful degradation):** the same premature-multi-cluster config still
  makes these unguarded reads raise a page/cron/route error rather than degrading cleanly —
  `routers/pipeline.py` (`build_dashboard_context` "never 500s" + the second read ~810),
  `routers/agent_s3.py` (~113, a partial-state 500 after the multipart already completed), and the
  `release_awaiting_cloud.py` staging cron (~131/145/180, documented "NEVER raise"). These are NOT
  boot-fatal and only trigger when an operator configures multi-cluster before Phase 69 supports it.
  Fold the per-site graceful handling into Phase 69 (SCHED), where multi-backend dispatch replaces the
  transitional accessors entirely and the "pick one" reduction goes away.
