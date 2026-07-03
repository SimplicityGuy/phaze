---
phase: 67
slug: backend-registry-config-model
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-03
---

# Phase 67 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_config.py -q` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~60 seconds (config-focused subset is a few seconds) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_config.py -q` (plus the new registry test module)
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green; coverage ≥ 85%
- **Max feedback latency:** ~60 seconds

---

## Per-Task Verification Map

*Populated during planning / Nyquist validation once PLAN.md tasks exist. Each REG-0X observable behavior from RESEARCH.md `## Validation Architecture` maps to a pytest seam (tmp_path TOML fixtures + monkeypatched `PHAZE_BACKENDS_CONFIG_FILE` pointer).*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| TBD | — | — | REG-01..05 | — | fail-fast at startup, no secrets in logs | unit | `uv run pytest -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] New registry test module (e.g. `tests/test_backends_registry.py`) — stubs for REG-01..05 (discriminated-union fail-fast with entry id, zero-config implicit-local resolution, scope-cardinality rejection, missing-mount-path fail-fast, effective-registry startup log with no secret material)
- [ ] tmp_path TOML fixtures + `PHAZE_BACKENDS_CONFIG_FILE` monkeypatch helper in `tests/conftest.py` (or the module)
- [ ] Rewrite/delete existing `cloud_target` / flat-field config tests removed by REG-04 (RESEARCH Wave 0 note)

*Existing pytest infrastructure covers the framework; only new test modules + fixtures are needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live all-local deploy boots unchanged with zero config edits | REG-04 | Requires the homelab deploy environment | Deploy image with no `backends.toml`; confirm app boots, logs implicit-local registry, pipeline runs all-local |

*All other phase behaviors have automated (pytest) verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 60s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
