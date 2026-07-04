---
phase: 67
slug: backend-registry-config-model
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-03
validated: 2026-07-04
---

# Phase 67 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/shared/config/ -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60 seconds (config-focused subset is a few seconds) |

---

## Sampling Rate

- **After every task commit:** Run the task's `<automated>` command (config subset is a few seconds)
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green; coverage ≥ 85%
- **Max feedback latency:** ~60 seconds

---

## Per-Task Verification Map

*Populated from the 6 PLAN.md `<automated>` verify commands. Each REG-0X observable behavior maps to a
pytest seam (tmp_path TOML fixtures + the conftest `backends_toml_env` fixture that writes a temp
`backends.toml` and points `PHAZE_BACKENDS_CONFIG_FILE` at it).*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-T1 | 67-01 | 1 | REG-01, REG-02 | T-67-01-04 | per-variant id-tagged fail-fast; bounded rank/cap reject out-of-range | unit | `uv run pytest tests/shared/config/test_backend_registry.py -x` | ✅ | ✅ green |
| 01-T2 | 67-01 | 1 | REG-05, REG-02 | T-67-01-01, T-67-01-02 | per-bucket `endpoint_url` http(s)+netloc SSRF guard; creds typed `SecretStr` | unit | `uv run pytest tests/shared/config/test_backend_registry.py -x` | ✅ | ✅ green |
| 01-T3 | 67-01 | 1 | REG-03 | T-67-01-03 | inline `*_file` eager read, fail-fast on unreadable, strip/verbatim per D-06 | unit | `uv run pytest tests/shared/config/test_backend_secret_files.py -x` | ✅ | ✅ green |
| 02-T1 | 67-02 | 2 | REG-01, REG-04 | T-67-02-04 | single TOML source (no env override); absent-file → implicit-local | unit | `uv run pytest tests/shared/config/test_bucket_registry.py -x` | ✅ | ✅ green |
| 02-T2 | 67-02 | 2 | REG-05 | T-67-02-01, T-67-02-02 | empty resolved registry → fail-fast; scope cardinality (cluster-specific ≤1) | unit | `uv run pytest tests/shared/config/test_bucket_registry.py -x` | ✅ | ✅ green |
| 02-T3 | 67-02 | 2 | REG-04 | T-67-02-03 | `log_effective_registry()` id/kind/rank/cap projection only, no secret material | unit | `uv run pytest tests/shared/config/test_bucket_registry.py -x` | ✅ | ✅ green |
| 03-T1 | 67-03 | 3 | REG-04 | T-67-03-02 | staging-cron `{"staged":0,"skipped":0}` no-op early-returns preserved | unit | `uv run pytest tests/analyze/core/test_staging_cron.py -x` | ✅ | ✅ green |
| 03-T2 | 67-03 | 3 | REG-04 | T-67-03-01 | reads-only rewire (`cloud_enabled`/accessors); no dispatch import; `cloud_lane_kind` context key | integration | `uv run pytest tests/shared/routers/test_pipeline.py tests/shared/core/test_routing_seam.py -x` | ✅ | ✅ green |
| 03-T3 | 67-03 | 3 | REG-04 | T-67-03-03 | Analyze partials render off `cloud_lane_kind`, no template 500 | unit | `uv run pytest tests/shared/core/test_enrich_analyze_workspaces.py -x` | ✅ | ✅ green |
| 04-T1 | 67-04 | 3 | REG-04 | T-67-04-02, T-67-04-03 | `active_bucket` creds stay `SecretStr`; scope-bound bucket; TTL knobs stay global | integration | `uv run pytest tests/analyze/services/test_s3_staging.py tests/analyze/services/test_cloud_staging.py -x` | ✅ | ✅ green |
| 04-T2 | 67-04 | 3 | REG-04 | T-67-04-01, T-67-04-04 | `active_kube` reads only; fail-fast (not silent-pick) on >1 non-local backend | integration | `uv run pytest tests/analyze/services/test_kube_staging.py -x` | ✅ | ✅ green |
| 05-T1 | 67-05 | 3 | REG-04 | T-67-05-03 | agent callbacks read transitional accessors only; no Backend protocol | integration | `uv run pytest tests/agents/routers/test_agent_s3.py tests/agents/routers/test_agent_push.py -x` | ✅ | ✅ green |
| 05-T2 | 67-05 | 3 | REG-04 | T-67-05-01, T-67-05-02 | LocalQueue probe keeps boot-safety try/except; secret-free registry startup log | integration | `uv run pytest tests/shared/tasks/test_controller_startup_localqueue.py -x` | ✅ | ✅ green |
| 06-T1 | 67-06 | 4 | REG-04 | T-67-06-02, T-67-06-03 | D-15 global knobs + control-plane secrets kept; no dangling flat-field read (`mypy .`) | unit | `uv run pytest tests/shared/config/ -x --ignore=tests/shared/config/test_cloud_target.py --ignore=tests/shared/config/test_kube_settings.py --ignore=tests/shared/config/test_s3_settings.py` | ✅ | ✅ green |
| 06-T2 | 67-06 | 4 | REG-04 | T-67-06-01 | role-split no-dead-token gate (removed `PHAZE_CLOUD_TARGET`/`cloud_burst` absent) | unit | `uv run pytest tests/shared/core/test_config_role_split.py -x` | ✅ | ✅ green |
| 06-T3 | 67-06 | 4 | REG-04 | T-67-06-01 | `.env.example` breaking-removal callout; no reintroduced legacy tokens | unit | `uv run pytest tests/shared/core/test_config_role_split.py -x` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*File Exists: ❌ W0 = new test module created in Wave 0; ✅ = existing test rewritten/extended in place.*

---

## Wave 0 Requirements

- [x] New registry test module `tests/shared/config/test_backend_registry.py` — REG-01/02/05 (parse, per-variant id-tagged fail-fast, bucket parse + endpoint SSRF)
- [x] New `tests/shared/config/test_backend_secret_files.py` — REG-03 (inline `*_file` strip/verbatim/fail-fast)
- [x] New `tests/shared/config/test_bucket_registry.py` — REG-04/05 (implicit-local, present-empty fail-fast, cardinality, missing-ref/empty-set, log-no-secrets)
- [x] `backends_toml_env` fixture in `tests/conftest.py` (writes tmp `backends.toml` + sets `PHAZE_BACKENDS_CONFIG_FILE`, clears `get_settings` cache) — shared by all Wave 3 consumers
- [x] Rewrite/delete existing `cloud_target` / flat-field config tests removed by REG-04 (RESEARCH Wave 0 note)

*Existing pytest infrastructure covers the framework; only new test modules + fixtures are needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live all-local deploy boots unchanged with zero config edits | REG-04 | Requires the homelab deploy environment | Deploy image with no `backends.toml`; confirm app boots, logs implicit-local registry, pipeline runs all-local |

*All other phase behaviors have automated (pytest) verification.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-03

---

## Validation Audit 2026-07-04

Post-execution State-A audit: every mapped test file was confirmed present and re-run green against a
fresh ephemeral Postgres/Redis (5433/6380), per-file to avoid the documented colima DB-isolation flake.
All runs are current — i.e. AFTER the code-review fixes (CR-01 boot-safety, WR-01 `scratch_dir`, WR-02
`.env` pointer) landed. REG-01..05 all have passing automated coverage; no MISSING or PARTIAL gaps, so
the gsd-nyquist-auditor was not spawned.

| Metric | Count |
|--------|-------|
| Requirements (REG-01..05) | 5 |
| Mapped tasks | 15 |
| COVERED (green) | 15 |
| PARTIAL / MISSING | 0 |
| Gaps resolved | 0 (none found) |
| Manual-only (deployment-gated) | 1 (live all-local zero-config boot) |

Per-file results: `test_backend_registry` 18 · `test_backend_secret_files` 8 · `test_bucket_registry` 14 ·
`test_staging_cron` 17 · `test_pipeline`+`test_routing_seam` 97 · `test_enrich_analyze_workspaces` 10 ·
`test_s3_staging` 13 · `test_cloud_staging` 5 · `test_kube_staging` 26 · `test_agent_s3` 12 ·
`test_agent_push` 8 · `test_controller_startup_localqueue` 9 · `tests/shared/config/` 75 ·
`test_config_role_split` 16 — all passed.

**Verdict: NYQUIST-COMPLIANT.** The single manual-only item (live all-local zero-config deploy boot)
remains deployment-gated on the homelab rollout — automated tests exercise the implicit-local registry
synthesis, but the live-boot-with-no-`backends.toml` observation needs the real deploy environment.
