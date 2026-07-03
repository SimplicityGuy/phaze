---
phase: 65
slug: calver-adoption
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-02
---

# Phase 65 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (+ `yaml.safe_load` for structural workflow parsing) |
| **Config file** | `pyproject.toml` (`[tool.pytest...]`) — run via `uv run pytest` |
| **Quick run command** | `uv run pytest tests/agents/deployment/test_agent_compose.py -x` |
| **Full suite command** | `uv run pytest` (or bucketed: `just test-bucket agents`) |
| **Estimated runtime** | ~5 seconds (quick) / full suite per bucket |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/agents/deployment/test_agent_compose.py -x`
- **After every plan wave:** Run `uv run pytest` (or `just test-bucket agents` + affected buckets)
- **Before `/gsd:verify-work`:** Full suite green + `pre-commit run --all-files` (frozen-SHA hooks; actionlint validates the edited `ci.yml`)
- **Max feedback latency:** ~5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| W0 (retarget) | 01 | 0 | VER-02 | — | N/A | unit | `uv run pytest tests/agents/deployment/test_agent_compose.py::test_ci_workflow_triggers_on_version_tags -x` | ✅ retarget | ⬜ pending |
| W0 (new) | 01 | 0 | VER-04 | — | N/A | structural | `uv run pytest ...::test_milestones_mapping_table_intact -x` | ❌ W0 | ⬜ pending |
| W0 (new) | 01 | 0 | VER-01 | — | N/A | doc-string | `uv run pytest ...::test_calver_scheme_documented -x` | ❌ W0 | ⬜ pending |
| CI glob edit | — | 1 | VER-02 | — | N/A | unit | `...::test_ci_workflow_triggers_on_version_tags -x` | ✅ | ⬜ pending |
| detect-changes proof | — | 1 | VER-02 | — | N/A | unit | `...::test_ci_detect_changes_forces_code_changed_on_tags -x` (no change) | ✅ | ⬜ pending |
| image-tag invariant | — | 1 | VER-03 | — | N/A | unit | `...::test_docker_publish_workflow_tags_both_latest_and_version -x` (docstring-only) | ✅ | ⬜ pending |
| pyproject bump | — | 1 | VER-01/02 | — | N/A | source | `grep '^version = "2026.7.0"' pyproject.toml` + `uv lock` sync | ✅ | ⬜ pending |
| docs + MILESTONES rewrite | — | 1 | VER-01/02/03/04 | — | N/A | doc-review + structural | mapping/doc-string guards above + reviewer judgment on D-12/D-13 prose | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] Retarget `tests/agents/deployment/test_agent_compose.py::test_ci_workflow_triggers_on_version_tags` (line ~332 assertion + docstring) — the `v*.*.*` → CalVer glob assertion is the critical D-02 proof. Must RED before the `ci.yml` edit, GREEN after. **(VER-02)**
- [ ] Add `test_milestones_mapping_table_intact` (same test module or a new `tests/agents/deployment/` / docs test) — asserts a `Milestone | Version | Date` table exists in `MILESTONES.md` AND the historical `v1.0..v7.0` rows are present verbatim. **(VER-04)**
- [ ] Add `test_calver_scheme_documented` — doc-string grep asserting `docs/deployment.md` (and/or `MILESTONES.md`) state the `YYYY.MM.REVISION` scheme, the no-leading-zero-month rule, and the per-month zero-based REVISION convention. **(VER-01)**
- [ ] Update the docstring of `test_docker_publish_workflow_tags_both_latest_and_version` to describe the CalVer form (assertion body unchanged — it checks `type=semver`/`type=ref,event=tag`, not a literal `v`). **(VER-03/D-06)**

*Wave 0 makes VER-01/VER-04 automated rather than doc-review-only, and pins the VER-02 critical glob as a real RED→GREEN gate.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Forward-looking-vs-historical string classification (D-12/D-13) | VER-02/VER-03 | "Instructs next release → rewrite; records past event → leave" is reviewer judgment, not machine-checkable per-string | Review each edited doc hunk: every rewritten string is a next-release instruction/example; no historical/feature-era label (`v4.0 shipped`, `since vX`, `v5.0 Cloud Burst`) was altered |
| Release procedure prose reflects CalVer | VER-02 | The step-by-step cut lives in external memory `project_release_procedure.md` (not a repo file) — cannot be test-asserted | Confirm `docs/deployment.md` tag-strategy/pinning prose reads CalVer; surface a memory-update note for `project-release-procedure` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (VER-01 doc guard, VER-04 mapping guard, VER-02 retarget)
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
