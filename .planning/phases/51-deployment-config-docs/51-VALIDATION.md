---
phase: 51
slug: deployment-config-docs
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-06-26
---

# Phase 51 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) via `uv run` |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_deployment/ tests/test_config/ -q` |
| **Full suite command** | `uv run pytest -q` |
| **Estimated runtime** | ~quick <30s · full per existing suite |

---

## Sampling Rate

- **After every task commit:** Run the quick run command (deployment + config tests).
- **After every plan wave:** Run the full suite command.
- **Before `/gsd:verify-work`:** Full suite must be green.
- **Max feedback latency:** ~30 seconds (quick) per task.

---

## Per-Task Verification Map

> Filled per-plan during planning; the rows below are the validation targets the planner MUST cover with automated tests. The two most testable behaviors are the master-toggle routing gate and the compute-compose invariants.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 51-01-* | 01 | 1 | CLOUDDEPLOY-04 | — | `cloud_burst_enabled=False` → long files route LOCAL (no AWAITING_CLOUD, no push); `=True` → long files route cloud | unit | `uv run pytest tests/test_pipeline/ -k cloud_burst -q` | ❌ W0 | ⬜ pending |
| 51-01-* | 01 | 1 | CLOUDDEPLOY-04 | — | staging cron (`stage_cloud_window`) no-ops when toggle OFF | unit | `uv run pytest -k stage_cloud_window_disabled -q` | ❌ W0 | ⬜ pending |
| 51-02-* | 02 | 1 | CLOUDDEPLOY-01 | — | cloud compose: worker-only, no media mount, no DATABASE_URL, named scratch volume, `-arm64` image | unit | `uv run pytest tests/test_deployment/ -k cloud_compose -q` | ❌ W0 | ⬜ pending |
| 51-02-* | 02 | 1 | CLOUDDEPLOY-02 | — | `cloud_burst_enabled` + push `_FILE` secrets round-trip through pydantic-settings | unit | `uv run pytest tests/test_config/ -k cloud_burst -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_pipeline/` (or existing pipeline test module) — `cloud_burst_enabled` routing gate stubs for CLOUDDEPLOY-04.
- [ ] `tests/test_deployment/test_cloud_compose.py` — new compute-compose invariant test, mirroring `tests/test_deployment/test_agent_compose.py`.
- [ ] `tests/test_config/` — `cloud_burst_enabled` field + `_FILE`-secret round-trip stubs for CLOUDDEPLOY-02.

*Framework already present — no install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| OCI A1 brings up the compute agent over Tailscale | CLOUDDEPLOY-01 | Requires real OCI Always-Free A1 + Tailscale tailnet; no CI substrate | Follow `docs/cloud-burst.md` runbook on a provisioned A1; confirm worker registers + drains a cloud `process_file`. |
| Tailscale ACL scopes A1 → `lux:{5432,6379,8000}` + `nox→A1:22` only | CLOUDDEPLOY-03 | Tailnet ACL is applied in the homelab tailnet, not in CI | Apply the spec'd ACL JSON; verify A1 can reach only those ports and `nox` can SSH A1. |
| Least-privilege PG broker role connects + runs SAQ, no app-table access | CLOUDDEPLOY-03 | Live Postgres role grants; verified empirically during research, re-verify post-apply | Run the runbook role SQL on lux Postgres; confirm SAQ `queue.connect()` succeeds and the role cannot SELECT app tables. |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
