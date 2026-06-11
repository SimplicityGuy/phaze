---
phase: 33
slug: saq-monitoring-ui-mounted-in-phaze-api
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-11
---

# Phase 33 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) |
| **Config file** | pyproject.toml ([tool.pytest.ini_options]) |
| **Quick run command** | `uv run pytest tests/test_web/test_saq_mount.py tests/test_main_lifespan.py -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60–120 seconds (full suite) |

---

## Sampling Rate

- **After every task commit:** quick run command
- **After every plan wave:** full suite command
- **Before `/gsd:verify-work`:** full suite green + coverage ≥85%
- **Max feedback latency:** ~120 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 33-00-01 | 00 | 0 | harness | — | `FakeQueue.info()` returns a QueueInfo-shaped object usable by saq_web | unit | `uv run pytest tests/_queue_fakes_test.py -k info -q` | ❌ W0 | ⬜ pending |
| 33-01-01 | 01 | 1 | mount helper | — | `build_saq_app(queues)` returns a Starlette app serving `/` (200) over the passed queues | unit | `uv run pytest tests/test_web/test_saq_mount.py -k build -q` | ❌ W0 | ⬜ pending |
| 33-01-02 | 01 | 1 | no second pool | — | `saq_web` is called with the passed Queue instances only; no Queue/pool constructed in the helper | unit | `uv run pytest tests/test_web/test_saq_mount.py -k reuse -q` | ❌ W0 | ⬜ pending |
| 33-01-03 | 01 | 1 | config flag | — | `settings.enable_saq_ui` exists, default True | unit | `uv run pytest tests/test_web/test_saq_mount.py -k flag -q` | ❌ W0 | ⬜ pending |
| 33-02-01 | 02 | 2 | lifespan mount | — | after lifespan startup, GET `/saq/` → 200 and `/health` still → 200 (mount-in-lifespan served) | integration | `uv run pytest tests/test_main_lifespan.py -k saq -q` | ❌ W0 | ⬜ pending |
| 33-02-02 | 02 | 2 | queues assembled | — | controller queue + one per-agent queue (non-revoked) passed to the mount | unit | `uv run pytest tests/test_main_lifespan.py -k queues -q` | ❌ W0 | ⬜ pending |
| 33-02-03 | 02 | 2 | flag off → no mount | — | `enable_saq_ui=False` → no `/saq` route registered; `/health` still 200 | unit | `uv run pytest tests/test_main_lifespan.py -k disabled -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/_queue_fakes.py` — add `info()` to `FakeQueue` returning a minimal `saq` `QueueInfo`-shaped object (name + counts) so `saq_web`/`build_saq_app` can render without real Redis (RESEARCH: `saq_web` calls `q.info()` on each queue).
- [ ] Account for `saq_web`'s module-level globals clobber (`QUEUES.clear()` on each call): tests that call the real `saq_web`/`build_saq_app` more than once must tolerate shared state — prefer building once per test or asserting the registry post-call. Document in the mount helper.

*Existing pytest infrastructure otherwise covers all phase requirements.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `/saq` dashboard renders behind the reverse proxy | observability | Real proxy path-prefix + real Redis with live jobs | After redeploy: open `/saq` via the proxy, confirm the controller + `phaze-agent-nox` queues list with live job counts; confirm assets load under the proxy prefix |

*Unit/integration tests cover the mount, queue assembly, flag, and route isolation; the proxy-prefix asset resolution is inherently a manual check.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (`FakeQueue.info()`)
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
