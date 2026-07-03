---
phase: 65-calver-adoption
verified: 2026-07-03T00:00:00Z
status: passed
score: 4/4 must-haves verified
overrides_applied: 0
---

# Phase 65: CalVer Adoption Verification Report

**Phase Goal:** Move release versioning from milestone-aligned `vN.M` to calendar-based `YYYY.MM.REVISION` (no leading-zero month; first tag `2026.7.0`) across the release procedure, version badges, published image tags, and the milestone↔version mapping — without breaking the historical `vN.M` record.
**Verified:** 2026-07-03
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 (VER-01) | Release versioning uses CalVer `YYYY.MM.REVISION`, no leading-zero month, first release `2026.7.0`, REVISION convention supports same-month patches | ✓ VERIFIED | `pyproject.toml:7` → `version = "2026.7.0"`; `uv.lock` phaze entry synced to `2026.7.0` (`uv lock --check` clean, no drift); `.planning/MILESTONES.md:5-13` and `docs/deployment.md:352` both state the scheme in prose: bare tag, no-leading-zero month, per-month zero-based REVISION resetting each calendar month. `test_calver_scheme_documented` GREEN. |
| 2 (VER-02) | Release procedure (pyproject/uv.lock bump → annotated tag push → GHCR publish) and README version/badge line reflect CalVer | ✓ VERIFIED | `docs/deployment.md:352-354` documents the full CalVer release procedure including the annotated-tag-PUSH-triggers-publish invariant and delete/recreate recovery recipe. README.md has no version/release badge (confirmed `git diff 9eab593~1 HEAD -- README.md` is empty — untouched, consistent with the phase's documented "no badge to update" decision). VER-02's README clause is satisfied by the deployment.md CalVer prose per the phase's explicit decision, recorded in 65-02-SUMMARY.md. |
| 3 (VER-03) | Published Docker image tags and compose/deploy references use CalVer version | ✓ VERIFIED | `docker-compose.agent.yml:27-28` and `docker-compose.cloud-agent.yml:35-37` comments read `PHAZE_IMAGE_TAG=2026.7.0` and bare `:<version>` / `:<version>-arm64` phrasing (de-`v`-ed, matches actual `type=semver` output); `image:` indirection lines (`${PHAZE_IMAGE_TAG:-latest}`) untouched. `docs/configuration.md`, `docs/arm64-agent-image.md`, `docs/cloud-burst.md` pin/build examples all read `2026.7.0` / `2026.7.0-arm64`. `docker-publish.yml`'s `type=semver` metadata-action verified functionally unchanged (only a stale comment corrected in the post-review fix commit `d1e3a54`) — it already emits `{{version}}=2026.7.0`/`{{major}}.{{minor}}=2026.7` with no code change needed. Zero forward-looking `PHAZE_IMAGE_TAG=v[0-9]` examples remain anywhere in docs/compose (`grep -rnE` returns nothing). |
| 4 (VER-04) | Milestone↔version mapping in ROADMAP.md/MILESTONES.md reads milestones as named, releases as dated, historical `vN.M` record intact | ✓ VERIFIED | `.planning/MILESTONES.md:15-24` new `| Milestone | Version | Date |` table: all of `v1.0`..`v7.0` present verbatim plus the new `Engineering Improvements | 2026.7.0 | (release date TBD)` row; every existing `## vN.M …` detail section (e.g. line 28 `## v7.0 UI Redesign …`) left byte-verbatim below the table. ROADMAP.md already read `2026.7.0` throughout (rows 203-206, section header, phase details) — confirmed no `vN.M` was introduced by this phase (`git diff` on ROADMAP.md for the phase touches only status/tracking rows). `test_milestones_mapping_table_intact` GREEN. |

**Score:** 4/4 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `.github/workflows/ci.yml` | CalVer-only tag glob, quoted, no legacy `v*.*.*` | ✓ VERIFIED | Line 13: `tags: ["[0-9]+.[0-9]+.[0-9]+"]`. `grep -c 'v\*\.\*\.\*' .github/workflows/ci.yml` → 0. `pre-commit run actionlint` passes on the file. |
| `pyproject.toml` | `version = "2026.7.0"` | ✓ VERIFIED | Line 7 exact match. No stray `7.0.0` references remain anywhere in repo (`.py`/`pyproject.toml`). |
| `uv.lock` | phaze entry synced via `uv lock`, not hand-edited | ✓ VERIFIED | Line 1429: `version = "2026.7.0"`; `uv lock --check` reports clean resolution (no drift). |
| `.planning/MILESTONES.md` | Milestone\|Version\|Date mapping table | ✓ VERIFIED | Table present near top (lines 15-24), 9 `|`-delimited lines, header + 8 rows; historical `vN.M` detail sections (28+) untouched. |
| `docs/deployment.md` | CalVer scheme + publish-invariant prose, forward-looking examples rewritten | ✓ VERIFIED | Lines 262/349/352/354/374/407/419/483/486/509 all read bare `2026.7.0` / `:<version>` phrasing; the "Phaze v4.0 Deployment Guide" title (line 2, 4, 93) is a feature-era label left verbatim per D-13 (correctly not an instruction). |
| `docker-compose.agent.yml` / `docker-compose.cloud-agent.yml` | Comment examples use CalVer, image indirection untouched | ✓ VERIFIED | Comments de-`v`-ed to `2026.7.0`/`:<version>`/`:<version>-arm64`; `image: ${PHAZE_IMAGE_TAG:-latest}` lines unchanged. |
| `.github/workflows/docker-publish.yml` | UNCHANGED functionally; stray comment corrected | ✓ VERIFIED | `git diff` against pre-phase base shows exactly 1 line changed — the comment `:v<version>` → `:<version>` (post-review fix `d1e3a54`); `tags:` metadata-action body untouched. |
| `tests/agents/deployment/test_agent_compose.py` | 4 guard changes: retargeted glob test + 2 new structural guards + de-v'ed docstrings, all GREEN | ✓ VERIFIED | `uv run pytest tests/agents/deployment/test_agent_compose.py -q` → **13 passed**. Post-review hardening (`d1e3a54`) tightened the glob assertion from substring to exact-shape membership (`CALVER_GLOB in tag_entries`) plus a `startswith("v")`/`"*" in t` rejection, closing WR-02 from code review. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `tests/agents/deployment/test_agent_compose.py::test_ci_workflow_triggers_on_version_tags` | `.github/workflows/ci.yml` | Exact CalVer glob literal `[0-9]+.[0-9]+.[0-9]+` as a discrete `on.push.tags` list entry | ✓ WIRED | Test reads `.github/workflows/ci.yml` via `yaml.safe_load`, asserts the literal is present as an exact list entry and no `v`-prefixed/wildcard variant coexists. Live-run confirms PASS. |
| `pyproject.toml` | `uv.lock` | `uv lock` sync | ✓ WIRED | `uv lock --check` reports no drift; both files agree on `2026.7.0`. |
| `tests/agents/deployment/test_agent_compose.py::test_milestones_mapping_table_intact` | `.planning/MILESTONES.md` | `MILESTONES_PATH.read_text()` substring asserts | ✓ WIRED | Test PASSES; table + all 7 historical version strings + `2026.7.0` row confirmed present by direct read. |
| `tests/agents/deployment/test_agent_compose.py::test_calver_scheme_documented` | `docs/deployment.md` + `.planning/MILESTONES.md` | Combined-text membership asserts (`YYYY.MM.REVISION`, `2026.7.0`, no-leading-zero-month rule, per-month zero-based REVISION rule) | ✓ WIRED | Test PASSES; both docs independently contain the full scheme prose. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full guard-test module green | `uv run pytest tests/agents/deployment/test_agent_compose.py -q` | `13 passed, 1 warning in 0.08s` | ✓ PASS |
| Deployment test package green (broader regression check) | `uv run pytest tests/agents/deployment/ -q` | `33 passed, 1 warning in 0.15s` | ✓ PASS |
| ci.yml still valid YAML/Actions syntax after glob swap | `pre-commit run actionlint --files .github/workflows/ci.yml .github/workflows/docker-publish.yml` | `Lint GitHub Actions workflow files.......Passed` | ✓ PASS |
| uv.lock genuinely synced (not hand-edited/drifted) | `uv lock --check` | `Resolved 174 packages in 5ms` (no changes required) | ✓ PASS |
| No forward-looking `v`-prefixed release instructions remain outside `.planning/` | `grep -rn 'v\*\.\*\.\*\|:v<version>' --include='*.yml' --include='*.md' --include='*.py' . \| grep -v '.planning\|.git'` | Only 2 hits, both inside the test file's own docstring/comment describing what the *legacy* pattern being rejected looks like (not an instruction) | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| VER-01 | 65-01, 65-02 | CalVer scheme adopted, documented, first release `2026.7.0` | ✓ SATISFIED | pyproject/uv.lock at `2026.7.0`; scheme prose in MILESTONES.md + deployment.md; `test_calver_scheme_documented` green. |
| VER-02 | 65-02 | Release procedure + README reflect CalVer | ✓ SATISFIED | deployment.md documents full CalVer release procedure incl. publish invariant; README has no badge to update (explicit, documented decision — verified README untouched). |
| VER-03 | 65-02 | Docker image tags / compose references use CalVer | ✓ SATISFIED | Compose comments + all doc pin/build examples read `2026.7.0`; docker-publish.yml `type=semver` verified functionally compatible with no code change; comment corrected in review-fix. |
| VER-04 | 65-02 | Milestone↔version mapping updated, historical record intact | ✓ SATISFIED | MILESTONES.md new mapping table with `v1.0`..`v7.0` verbatim + `2026.7.0` row; `test_milestones_mapping_table_intact` green; ROADMAP.md already conformant, unchanged. |

Note: `.planning/REQUIREMENTS.md` still shows VER-01..04 as `[ ]` unchecked / "Pending" in its traceability table. This is expected pre-verification tracking state — REQUIREMENTS.md/STATE.md/ROADMAP.md updates are owned by the orchestrator post-verification, not by this verifier or the executor. Codebase evidence above independently confirms all four requirements are satisfied; the orchestrator should flip these to Complete after accepting this report.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `.planning/MILESTONES.md` | 24 | `_(release date TBD)_` in the new mapping table's `2026.7.0` row | ℹ️ Info | Not a debt marker — accurately reflects that the milestone has not yet been tag-released (the annotated `2026.7.0` tag push is an explicit, documented milestone-completion step, not phase-65 scope). Not a blocker: this is present-tense factual state, not deferred/incomplete implementation work. |

No `FIXME`/`XXX`/unreferenced `TODO`/placeholder-render/empty-handler patterns found in any of the 11 files modified by this phase. No stub returns, no hardcoded-empty data flowing to user-visible surfaces (all changes are CI config, version strings, and documentation prose — no runtime code paths were touched, consistent with the phase's "no behavior change" framing).

### Human Verification Required

None. This phase's deliverables (CI trigger config, version strings, documentation prose, and structural guard tests) are all fully verifiable via file inspection and automated test execution — no UI, real-time behavior, or external-service integration is in scope.

### Gaps Summary

No gaps. All four ROADMAP success criteria (VER-01..04) are independently verified against the live codebase, not merely claimed in SUMMARY.md. The single highest-consequence artifact — the `ci.yml` tag-trigger glob — was checked at the byte level (`grep -F` exact literal match, zero legacy occurrences) and is protected by a test that a code-review pass (65-REVIEW.md, WR-01/WR-02) further hardened post-execution to close a substring-match gap, with the fix landed and verified GREEN (13/13) before this verification ran. Historical `vN.M` record is confirmed intact everywhere it matters (MILESTONES.md detail sections, deployment.md's feature-era title, ROADMAP.md phase history). The only informational note is REQUIREMENTS.md traceability rows still reading "Pending" — expected orchestrator-owned post-verification bookkeeping, not a phase-goal gap.

---

_Verified: 2026-07-03_
_Verifier: Claude (gsd-verifier)_
