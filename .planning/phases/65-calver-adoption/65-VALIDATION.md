---
phase: 65
slug: calver-adoption
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-02
audited: 2026-07-03
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
| W0 (retarget) | 01 | 0 | VER-02 | — | N/A | unit | `uv run pytest tests/agents/deployment/test_agent_compose.py::test_ci_workflow_triggers_on_version_tags -x` | ✅ retarget | ✅ green |
| W0 (new) | 01 | 0 | VER-04 | — | N/A | structural | `uv run pytest ...::test_milestones_mapping_table_intact -x` | ✅ W0 | ✅ green |
| W0 (new) | 01 | 0 | VER-01 | — | N/A | doc-string | `uv run pytest ...::test_calver_scheme_documented -x` | ✅ W0 | ✅ green |
| CI glob edit | — | 1 | VER-02 | — | N/A | unit | `...::test_ci_workflow_triggers_on_version_tags -x` | ✅ | ✅ green |
| detect-changes proof | — | 1 | VER-02 | — | N/A | unit | `...::test_ci_detect_changes_forces_code_changed_on_tags -x` (no change) | ✅ | ✅ green |
| image-tag invariant | — | 1 | VER-03 | — | N/A | unit | `...::test_docker_publish_workflow_tags_both_latest_and_version -x` (docstring-only) | ✅ | ✅ green |
| pyproject bump | — | 1 | VER-01/02 | — | N/A | source | `grep '^version = "2026.7.0"' pyproject.toml` + `uv lock` sync | ✅ | ✅ green |
| docs + MILESTONES rewrite | — | 1 | VER-01/02/03/04 | — | N/A | doc-review + structural | mapping/doc-string guards above + reviewer judgment on D-12/D-13 prose | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

All 8 rows verified green on 2026-07-03 (`uv run pytest tests/agents/deployment/test_agent_compose.py` → 13/13; the 5 mapped node-ids re-run individually all PASSED). The retargeted glob guard was additionally hardened to exact-shape in code review (commit `d1e3a54`).

---

## Wave 0 Requirements

- [x] Retarget `tests/agents/deployment/test_agent_compose.py::test_ci_workflow_triggers_on_version_tags` (line ~332 assertion + docstring) — the `v*.*.*` → CalVer glob assertion is the critical D-02 proof. Was RED before the `ci.yml` edit (Wave 1), GREEN after (Wave 2); further hardened to exact-shape in review. **(VER-02)**
- [x] Add `test_milestones_mapping_table_intact` — asserts a `Milestone | Version | Date` table exists in `MILESTONES.md` AND the historical `v1.0..v7.0` rows are present verbatim. **(VER-04)**
- [x] Add `test_calver_scheme_documented` — doc-string grep asserting `docs/deployment.md` (and/or `MILESTONES.md`) state the `YYYY.MM.REVISION` scheme, the no-leading-zero-month rule, and the per-month zero-based REVISION convention. **(VER-01)**
- [x] Update the docstring of `test_docker_publish_workflow_tags_both_latest_and_version` to describe the CalVer form (assertion body unchanged — it checks `type=semver`/`type=ref,event=tag`, not a literal `v`). **(VER-03/D-06)**

*Wave 0 makes VER-01/VER-04 automated rather than doc-review-only, and pins the VER-02 critical glob as a real RED→GREEN gate.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Forward-looking-vs-historical string classification (D-12/D-13) | VER-02/VER-03 | "Instructs next release → rewrite; records past event → leave" is reviewer judgment, not machine-checkable per-string | ✅ PERFORMED 2026-07-03 (code review + verifier + secure-phase): repo-wide grep for `PHAZE_IMAGE_TAG=v[0-9]` / `:v<version>` / `v*.*.*` outside `.planning/` returns ZERO live-source hits (all remaining hits are `.planning/` history or the test file's own descriptive comment); no historical/feature-era label was altered. |
| Release procedure prose reflects CalVer | VER-02 | The step-by-step cut lives in external memory `project_release_procedure.md` (not a repo file) — cannot be test-asserted | ✅ PERFORMED 2026-07-03: `docs/deployment.md:352` documents bare CalVer tags, no-leading-zero month, and per-month zero-based REVISION; `:354` states the annotated-tag-push invariant + delete-recreate recipe. Memory `project_release_procedure.md` updated to CalVer (this session). |

---

## Validation Audit 2026-07-03

| Metric | Count |
|--------|-------|
| Requirements audited | 4 (VER-01..04) |
| COVERED (automated, green) | 4 |
| PARTIAL | 0 |
| MISSING | 0 |
| Manual-only (performed + passed) | 2 |
| Gaps found | 0 |
| Resolved | 0 (none needed) |
| Escalated | 0 |

State A audit: the planning-time strategy's Wave-0 guards all shipped and run green (13/13 module; 5 mapped node-ids individually PASSED). No auditor spawn needed — zero gaps. Both manual-only items were performed and passed (see table above). Phase is **Nyquist-compliant**.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (VER-01 doc guard, VER-04 mapping guard, VER-02 retarget)
- [x] No watch-mode flags
- [x] Feedback latency < 10s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** verified 2026-07-03
