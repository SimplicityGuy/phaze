---
phase: 50
slug: push-pipeline
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-25
---

# Phase 50 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_push_pipeline.py -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60-90 seconds |

---

## Sampling Rate

- **After every task commit:** Run the quick run command for the touched module
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green at ≥85% coverage
- **Max feedback latency:** 90 seconds

---

## Per-Task Verification Map

> Detailed per-task mapping is filled by the planner; the table below records the
> requirement → test-type contract the plans must satisfy (from RESEARCH.md
> Validation Architecture).

| Area | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command |
|------|-------------|------------|-----------------|-----------|-------------------|
| `push_file` rsync argv build | CLOUDPIPE-02 | T-50-injection | argv list (no shell); pinned known_hosts + StrictHostKeyChecking=yes | unit | `uv run pytest tests/test_push_pipeline.py -q -k argv` |
| `push_file` exit-code handling | CLOUDPIPE-02, -05 | — | non-zero/partial exit → job fails, no callback, re-drivable | unit | `uv run pytest tests/test_push_pipeline.py -q -k exit_code` |
| `push_file:<id>` deterministic key | CLOUDPIPE-05 | — | double-tick collapses to no-op | unit | `uv run pytest tests/test_deterministic_key.py -q -k push` |
| sha256 verify (off event loop) | CLOUDPIPE-03 | T-50-corrupt | mismatch → clean fail + scratch delete + re-push | unit | `uv run pytest tests/test_push_pipeline.py -q -k sha256` |
| ProcessFilePayload scratch fields | CLOUDPIPE-03 | — | scratch_path set → ephemeral read; None → local-path read | unit | `uv run pytest tests/test_payload.py -q -k scratch` |
| scratch cleanup `finally` | CLOUDPIPE-04 | T-50-scratch-dos | scratch deleted on success AND terminal failure | unit | `uv run pytest tests/test_push_pipeline.py -q -k cleanup` |
| startup janitor sweep | CLOUDPIPE-04 | T-50-scratch-dos | orphaned scratch swept on compute worker start, in-flight skipped | unit | `uv run pytest tests/test_push_pipeline.py -q -k janitor` |
| staging cron ≤N window | CLOUDPIPE-01, -05 | — | window never exceeds N; 144-file backlog → ≤N enqueued | unit | `uv run pytest tests/test_staging_cron.py -q` |
| PUSHING/PUSHED classified pending | CLOUDPIPE-01, -05 | — | recovery re-drives in-flight states (not done) | unit | `uv run pytest tests/test_reenqueue.py -q -k pushing` |
| routing seam → bounded window | CLOUDPIPE-01 | — | no direct-to-compute enqueue bypasses window | unit | `uv run pytest tests/test_routing_seam.py -q` |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_push_pipeline.py` — stubs for CLOUDPIPE-02/-03/-04 (rsync argv, exit codes, sha256, cleanup, janitor)
- [ ] `tests/test_staging_cron.py` — stub for the ≤N bounded-window controller (CLOUDPIPE-01/-05)
- [ ] `tests/test_routing_seam.py` — stub for the Phase 49 routing-seam reshape (CLOUDPIPE-01)
- [ ] Reuse existing `tests/conftest.py` async fixtures + in-memory/fake SAQ + DB session fixtures (no new framework)

*Existing pytest infrastructure covers the framework; new test files are stubs only.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real rsync-over-SSH-over-Tailscale transfer to a live compute agent | CLOUDPIPE-02 | Needs the Phase 51 agent image (`rsync`/`openssh-client`) + a live Tailscale-joined compute box; not available in CI | Deferred to Phase 51 deploy runbook — Phase 50 unit tests mock the subprocess; end-to-end transfer verified during CLOUDDEPLOY rollout |
| Dashboard count cards render ("Staged"/"Analyzing (cloud)") | CLOUDPIPE-01 | Visual reuse of Phase 49 count-card pattern; rendering verified by eye | Load pipeline dashboard, confirm two new cards show live counts from PUSHING/PUSHED states |

*Subprocess boundary is mocked in unit tests; the live transfer is a Phase 51 concern.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
