---
phase: 27
slug: watcher-service-user-initiated-scan
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-05-13
revised: 2026-05-13
---

# Phase 27 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Source of truth: `27-RESEARCH.md` §"Validation Architecture" (23 named pytest cases).

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x + pytest-asyncio + respx (existing — no Wave 0 install) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_agent_watcher tests/test_routers/test_agent_scan_batches.py tests/test_routers/test_agent_files_batch_id.py tests/test_routers/test_pipeline_scans.py tests/test_tasks/test_scan_directory.py tests/test_tasks/test_shared_agent_bootstrap.py tests/test_task_split.py tests/test_schemas/ -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | quick ~30s · full ~3min |

---

## Sampling Rate

- **After every task commit:** Run quick command for the touched module's tests
- **After every plan wave:** Run quick command across all Phase 27 modules
- **Before `/gsd-verify-work`:** Full suite green, ≥85% coverage on new modules
- **Max feedback latency:** 30 seconds (quick command)

---

## Per-Task Verification Map

> Each row maps a plan-task to its automated verify command and the requirement it proves. Drawn from RESEARCH.md §"Validation Architecture" plus the additional cases added by the planner during decomposition.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 27-01-T1 | 01 | 0 | DIST-02, SCAN-04 | — | watchdog>=4.0 installed; AgentSettings exposes 4 new knobs with correct defaults | unit | `uv run pytest tests/test_config.py -x -q` (or wherever AgentSettings tests live) + `uv run python -c "from phaze.config import AgentSettings; s=AgentSettings(...); assert s.watcher_settle_seconds==10"` | Plan 01 creates | ⬜ pending |
| 27-01-T2 | 01 | 0 | DIST-02 | T-27-04 | Shared bootstrap module exists, Postgres-free, Pitfall 7 short-circuit on AgentApiAuthError | unit | `uv run pytest tests/test_tasks/test_shared_agent_bootstrap.py::test_whoami_with_retry_short_circuits_on_auth_error -x` | Plan 01 creates | ⬜ pending |
| 27-01-T3 | 01 | 0 | DIST-02, SCAN-03 | — | Test package scaffolding; both new boundary subprocess tests added (one skipped pending Plan 05) | unit (subprocess) | `uv run pytest tests/test_task_split.py::test_shared_bootstrap_stays_postgres_free -x` + `uv run pytest tests/test_agent_watcher/ --collect-only` | Plan 01 creates | ⬜ pending |
| 27-02-T1 | 02 | 1 | SCAN-02, SCAN-03 | — | FileUpsertChunk accepts optional batch_id; preserves extra="forbid" | unit | `uv run pytest tests/test_schemas/test_agent_files.py -x -q` | Plan 02 creates | ⬜ pending |
| 27-02-T2 | 02 | 1 | SCAN-03 | (schema layer) | ScanBatchPatch rejects status="live" at 422 (Literal); ScanBatchPatchResponse echoes full row | unit | `uv run pytest tests/test_schemas/test_agent_scan_batches.py -x -q` | Plan 02 creates | ⬜ pending |
| 27-02-T3 | 02 | 1 | SCAN-01, SCAN-02 | — | ScanDirectoryPayload + TriggerScanForm with extra="forbid" | unit | `uv run pytest tests/test_schemas/test_agent_tasks.py tests/test_schemas/test_pipeline_scans.py -x -q` | Plan 02 creates | ⬜ pending |
| 27-03-T1 | 03 | 2 | DIST-02, SCAN-03 | T-27-01 | PATCH /scan-batches returns 403 BEFORE state-machine; idempotent same-state; LIVE rejected; PhazeAgentClient.patch_scan_batch | contract + respx | `uv run pytest tests/test_routers/test_agent_scan_batches.py -x -q` (11 cases) | Plan 03 creates | ⬜ pending |
| 27-03-T2 | 03 | 2 | SCAN-02, SCAN-03 | T-27-02 | POST /files with batch_id: present → cross-tenant guard 403; absent → LIVE sentinel resolution; auto-enqueue still fires | contract | `uv run pytest tests/test_routers/test_agent_files_batch_id.py tests/test_routers/test_agent_files.py -x -q` (5 new + regression) | Plan 03 creates | ⬜ pending |
| 27-03-T3 | 03 | 2 | DIST-02 | — | agent_scan_batches.router registered in main.py; PATCH endpoint reachable on full app | smoke | `uv run python -c "from phaze.main import create_app; app=create_app(); paths=[r.path for r in app.routes]; assert any('/api/internal/agent/scan-batches' in p for p in paths)"` | Plan 03 modifies main.py | ⬜ pending |
| 27-04-T1 | 04 | 3 | SCAN-01, SCAN-02 | T-27-04, Pitfall 3, 4 | scan_directory: chunking at 500, per-chunk PATCH, terminal PATCH, OSError skip, NFC normalize, followlinks=False, no Postgres imports | unit | `uv run pytest tests/test_tasks/test_scan_directory.py -x -q` (8 cases) | Plan 04 creates | ⬜ pending |
| 27-04-T2 | 04 | 3 | SCAN-01 | (boundary) | scan_directory registered in agent_worker.settings.functions; Postgres-free import graph preserved | unit + subprocess | `uv run pytest tests/test_task_split.py::test_agent_worker_does_not_import_phaze_database -x` + `uv run python -c "from phaze.tasks.agent_worker import settings; assert any(f.__name__=='scan_directory' for f in settings['functions'])"` | Plan 04 modifies | ⬜ pending |
| 27-05-T1 | 05 | 3 | SCAN-03, SCAN-04 | T-27-05, Pitfall 2, 3 | Debouncer state machine; WatcherEventHandler via call_soon_threadsafe; Poster chunk-of-1 with batch_id omitted | unit | `uv run pytest tests/test_agent_watcher/test_debouncer.py tests/test_agent_watcher/test_observer.py -x -q` (10 cases) | Plan 05 creates | ⬜ pending |
| 27-05-T2 | 05 | 3 | DIST-02, SCAN-03 | T-27-04, Pitfall 1, 5, 6, 7 | __main__ boots Observer + sweep; SIGTERM graceful shutdown; respx-mocked event-to-POST E2E; OSError-on-vanished-path | integration | `uv run pytest tests/test_agent_watcher/test_main.py tests/test_task_split.py::test_agent_watcher_does_not_import_phaze_database -x -q` (6 cases) | Plan 05 creates | ⬜ pending |
| 27-06-T1 | 06 | 3 | SCAN-01 | T-27-03 | POST /pipeline/scans validates + enqueues; subpath traversal rejected; cross-root rejection; GET progress halts on terminal | contract | `uv run pytest tests/test_routers/test_pipeline_scans.py -x -q` (10 cases) | Plan 06 creates | ⬜ pending |
| 27-06-T2 | 06 | 3 | SCAN-01 | (UI) | 6 partials rendered byte-for-byte from UI-SPEC; dashboard.html includes both new partials; Pitfall 6 verified at template level | template+contract | `uv run pytest tests/test_routers/test_pipeline_scans.py tests/test_routers/test_pipeline.py -x -q` | Plan 06 creates | ⬜ pending |
| 27-07-T1 | 07 | 5 | DIST-02, SCAN-03 | T-27-04 | docker-compose watcher service block; .env.example documents 4 new env vars; volume :ro; no redis/postgres in depends_on; no MODELS_PATH/OUTPUT_PATH | config | `uv run python -c "import yaml; data=yaml.safe_load(open('docker-compose.yml').read()); w=data['services']['watcher']; assert 'uv run python -m phaze.agent_watcher' in w['command']; assert 'redis' not in w.get('depends_on', {}); assert 'postgres' not in w.get('depends_on', {}); assert all(':ro' in v for v in w['volumes'])"` + `grep -q PHAZE_WATCHER_SETTLE_SECONDS .env.example` + `grep -q PHAZE_WATCHER_MAX_PENDING_SECONDS .env.example` + `grep -q PHAZE_WATCHER_SWEEP_INTERVAL_SECONDS .env.example` + `grep -q PHAZE_SCAN_CHUNK_SIZE .env.example` | Plan 07 modifies | ⬜ pending |
| 27-07-T2 | 07 | 5 | DIST-02 | — | Per-service README ≥30 lines documenting purpose, entry point, env vars, import-boundary, Phase 29 migration | docs | `test $(wc -l < src/phaze/agent_watcher/README.md) -ge 30 && grep -q "phaze.database" src/phaze/agent_watcher/README.md` | Plan 07 creates | ⬜ pending |
| 27-07-T3 | 07 | 5 | — | — | STATE.md accumulates ≥5 Phase 27 decision entries; progress.completed_phases incremented | docs | `grep -c "\[Phase 27" .planning/STATE.md` | Plan 07 modifies | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_agent_watcher/__init__.py` — package marker for new test module (Plan 01 T3)
- [ ] `tests/test_agent_watcher/conftest.py` — shared fixtures `tmp_watcher_root`, `fake_clock`, `mock_api_client` (Plan 01 T3)
- [ ] `pyproject.toml` — `watchdog>=4.0` added to `[project].dependencies` (Plan 01 T1; D-23)
- [ ] `tests/test_task_split.py` — extended with `test_agent_watcher_does_not_import_phaze_database` and `test_shared_bootstrap_stays_postgres_free` parallel cases (Plan 01 T3; D-22 / D-25 parity)
- [ ] `src/phaze/tasks/_shared/agent_bootstrap.py` — shared startup helpers extracted from `agent_worker.py` (Plan 01 T2; D-17)
- [ ] `src/phaze/config.py` — four new `AgentSettings` fields with AliasChoices (Plan 01 T1; D-03, D-11)

*Existing infrastructure (pytest-asyncio, respx, the test-router pattern, the `test_task_split.py` harness) covers the rest — no framework install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Browser-rendered "Trigger Scan" card layout, focus ring, color contrast, reduced-motion respect | UI-SPEC.md §Accessibility, §Responsive | Visual contract; automated DOM tests can't cover rendered pixels | Manual: pull up `/pipeline/`, tab through form, check focus rings, run macOS "Reduce Motion" toggle, verify scan_progress_card polling halts on completion |
| End-to-end on real Docker compose with `rsync --inplace` writing a 200MB file into `/data/music` | SCAN-04 settle behavior under real I/O | Synthetic mtime-stability tests cover the state machine; this proves the wall-clock behavior under a real writer pattern | Manual: `docker compose up watcher`, `rsync --inplace` a large file from a host shell, observe no early POST in agent logs, verify single FileRecord appears after settle period |
| Operator dropdown UX with 10+ registered agents | UI-SPEC.md §Trigger Scan card | Layout/wrapping behavior at scale isn't covered by Playwright-less tests | Manual: register 10 dummy agents in dev DB, view `/pipeline/`, verify dropdown remains usable |

*All other phase behaviors have automated verification.*

---

## Validation Sign-Off

- [x] All planned tasks have `<automated>` verify command OR are in Wave 0
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (watchdog dep, test module skeleton, import-boundary test extension, shared bootstrap module, AgentSettings fields)
- [x] No watch-mode flags (`--watch`, `--reuse-db`, `-x` in quick command is acceptable per pytest convention; quick command uses `-q` not `-x` to surface multiple failures)
- [x] Feedback latency < 30s for quick command
- [x] `nyquist_compliant: true` set in frontmatter — planner has filled the per-task map; checker run pending

**Approval:** pending checker review
