---
phase: 50
slug: push-pipeline
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-25
validated: 2026-06-26
---

# Phase 50 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio, `asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`, `testpaths = ["tests"]`) |
| **Quick run command** | `uv run pytest tests/test_push_pipeline.py -q` (pure-unit files need no DB) |
| **DB-backed run** | Export `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` / `PHAZE_REDIS_URL` at the ephemeral ports (5433/6380), or just run `just integration-test` |
| **Full suite command** | `just integration-test` (spins up ephemeral Postgres 18 + Redis 7, runs `tests/`, auto-teardown) |
| **Estimated runtime** | ~4 min full suite; <15s for the phase-50 file subset |

> Note: most phase-50 behavioral tests (`test_staging_cron`, `test_routing_seam`,
> `test_routers/test_agent_push`, `test_reenqueue`, `test_process_file_scratch`) require a
> live test Postgres via the conftest session fixtures. A bare `uv run pytest` with no
> `TEST_DATABASE_URL` errors at collection (connection refused) — use `just integration-test`.

---

## Sampling Rate

- **After every task commit:** Run the quick run command for the touched module
- **After every plan wave:** Run `just integration-test`
- **Before `/gsd:verify-work`:** Full suite must be green at ≥85% coverage
- **Max feedback latency:** 90 seconds (subset); ~4 min for the full integration suite

---

## Per-Task Verification Map

> Audited 2026-06-26 against the merged implementation. Every requirement maps to a real,
> green automated test. The 2026-06-25 draft referenced four selectors that never shipped
> (`test_payload.py`, `test_deterministic_key.py -k push`, and sha256/cleanup on the wrong
> file) — corrected below.

| Area | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | Status |
|------|-------------|------------|-----------------|-----------|-------------------|--------|
| `push_file` rsync argv build | CLOUDPIPE-02 | T-50-injection | argv list (no shell); pinned known_hosts + StrictHostKeyChecking=yes | unit | `uv run pytest tests/test_push_pipeline.py -k argv` | ✅ 3 |
| `push_file` exit-code handling | CLOUDPIPE-02, -05 | T-50-no-fallback, T-50-secret-leak | non-zero/partial → job fails, no callback, re-drivable; missing binary → terminal, no local fallback; stderr truncated, no key leak | unit | `uv run pytest tests/test_push_pipeline.py -k exit_code` | ✅ 4 |
| `push_file:<id>` deterministic key | CLOUDPIPE-05 | T-50-double-enqueue | double-tick collapses to a SAQ no-op | integration | `uv run pytest tests/test_staging_cron.py -k double_tick` | ✅ 1 |
| sha256 verify (off event loop) | CLOUDPIPE-03 | T-50-corrupt | mismatch → clean fail + scratch delete + report, no analysis | integration | `uv run pytest tests/test_process_file_scratch.py -k sha256` | ✅ 4 |
| ProcessFilePayload scratch fields | CLOUDPIPE-03 | T-50-payload-inject | scratch_path set → ephemeral read; None → local-path read; `extra="forbid"` | unit | `uv run pytest tests/test_schemas/test_agent_tasks.py -k scratch` | ✅ 2 |
| scratch cleanup `finally` | CLOUDPIPE-04 | T-50-scratch-dos | scratch deleted on success AND every terminal failure path | integration | `uv run pytest tests/test_process_file_scratch.py -k cleanup` | ✅ 8 |
| startup janitor sweep | CLOUDPIPE-04 | T-50-scratch-dos | orphaned scratch swept on compute worker start; compute-only gating | integration | `uv run pytest tests/test_push_pipeline.py -k janitor` | ✅ 2 |
| staging cron ≤N window | CLOUDPIPE-01, -05 | T-50-scratch-dos | window never exceeds N; 144-file backlog → ≤N staged; overlapping ticks never exceed window | integration | `uv run pytest tests/test_staging_cron.py` | ✅ 12 |
| PUSHING/PUSHED classified pending | CLOUDPIPE-01, -05 | T-50-orphan-leak, T-50-misroute | recovery re-drives in-flight states to a fileserver-kind agent (never compute) | integration | `uv run pytest tests/test_reenqueue.py -k pushing` | ✅ 5 |
| routing seam → bounded window | CLOUDPIPE-01 | T-50-bypass | no direct-to-compute enqueue bypasses the window | integration | `uv run pytest tests/test_routing_seam.py` | ✅ 4 |
| push callbacks (pushed / mismatch / auth) | CLOUDPIPE-03, -05 | T-50-spoofed-callback, T-50-loop, T-50-integrity-pin | token-authed; idempotent duplicate; mismatch over `push_max_attempts` → ANALYSIS_FAILED | integration | `uv run pytest tests/test_routers/test_agent_push.py` | ✅ 8 |

*Status: ⬜ pending · ✅ green (count) · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_push_pipeline.py` — argv / exit-code / janitor real assertions (CLOUDPIPE-02/-04); stubs converted to live tests in 50-03
- [x] `tests/test_process_file_scratch.py` — sha256 + cleanup real assertions (CLOUDPIPE-03/-04); converted in 50-04
- [x] `tests/test_staging_cron.py` — ≤N bounded-window + deterministic-key dedup (CLOUDPIPE-01/-05); converted in 50-06
- [x] `tests/test_routing_seam.py` — Phase 49 routing-seam reshape → AWAITING_CLOUD (CLOUDPIPE-01); converted in 50-06
- [x] Reused existing `tests/conftest.py` async fixtures + DB session fixtures (no new framework)

*All Wave 0 stub selectors were converted to real assertions by their owning plans (verified: 0 `pytest.skip`/`xfail` remain in the four files; security audit confirmed 33/34/37/22 asserts).*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real rsync-over-SSH-over-Tailscale transfer to a live compute agent | CLOUDPIPE-02 | Subprocess is mocked in unit tests; the real transfer needs the Phase 51 agent image (`rsync`/`openssh-client`) + a live Tailscale-joined compute box — not available in CI | Deferred to the Phase 51 / v5.0 deploy runbook. UAT tests 4–7 (50-UAT.md) are blocked on this same dependency; their logic is green here. |
| Dashboard count cards render + live OOB poll | CLOUDPIPE-01 | Visual reuse of the Phase 49 count-card pattern; rendering verified by eye / live HTTP | Driven live 2026-06-26 (50-UAT.md test 2 + 50-HUMAN-UAT.md): `GET /pipeline/` renders both cards; `GET /pipeline/stats` re-emits them with `hx-swap-oob="true"`; degrade-to-0 confirmed |

*Subprocess boundary is mocked in unit tests; the live transfer is a Phase 51 deploy concern.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 90s (subset)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-06-26 (audit found 0 coverage gaps; 4 stale command references corrected)

---

## Validation Audit 2026-06-26

| Metric | Count |
|--------|-------|
| Requirements audited (CLOUDPIPE-01..05) | 5 |
| Per-task rows | 11 |
| COVERED (green automated test) | 11 |
| PARTIAL | 0 |
| MISSING | 0 |
| Stale command references corrected | 4 |
| Tests generated by auditor | 0 (none needed) |

**Corrections applied:**
- `push_file:<id>` deterministic key: `test_deterministic_key.py -k push` (no such tests) → `test_staging_cron.py -k double_tick`
- sha256 verify: `test_push_pipeline.py -k sha256` (wrong file) → `test_process_file_scratch.py -k sha256`
- ProcessFilePayload scratch fields: `test_payload.py -k scratch` (file does not exist) → `test_schemas/test_agent_tasks.py -k scratch`
- scratch cleanup: `test_push_pipeline.py -k cleanup` (wrong file) → `test_process_file_scratch.py -k cleanup`
- Added the push-callbacks row (`test_routers/test_agent_push.py`, 8 green) — the original table omitted the report_pushed/report_push_mismatch auth + attempt-cap coverage.

All 11 corrected commands were executed against a fresh ephemeral Postgres+Redis and pass green.
