---
phase: 65-calver-adoption
plan: 02
subsystem: ci-versioning
tags: [calver, ci, docker, ghcr, versioning, release]

# Dependency graph
requires:
  - phase: 65-01
    provides: "3 RED CalVer guard tests (ci-glob / MILESTONES mapping / scheme-documented) + de-v'ed docker-publish docstrings"
provides:
  - "ci.yml on.push.tags is the CalVer-only glob [0-9]+.[0-9]+.[0-9]+ (legacy v*.*.* dropped)"
  - "pyproject version 2026.7.0 + uv.lock synced (source of truth on CalVer)"
  - ".planning/MILESTONES.md Milestone|Version|Date mapping table (v1.0..v7.0 verbatim + 2026.7.0) + CalVer convention prose"
  - "forward-looking CalVer examples across docs/ + compose comments; publish invariant (D-04) documented"
affects: [release-procedure, deployment, future-milestones]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "CalVer YYYY.MM.REVISION: bare tag (no v), no-leading-zero month, per-month zero-based REVISION"
    - "single CalVer glob literal [0-9]+.[0-9]+.[0-9]+ shared byte-identical between ci.yml and its guard test"

key-files:
  created:
    - ".planning/phases/65-calver-adoption/65-02-SUMMARY.md"
  modified:
    - ".github/workflows/ci.yml"
    - "pyproject.toml"
    - "uv.lock"
    - ".planning/MILESTONES.md"
    - "docs/deployment.md"
    - "docs/configuration.md"
    - "docs/arm64-agent-image.md"
    - "docs/cloud-burst.md"
    - "docker-compose.agent.yml"
    - "docker-compose.cloud-agent.yml"

key-decisions:
  - "VER-02 README: NO edit — there is no version/release badge, so the README clause is satisfied by deployment.md CalVer prose (no badge added/re-added)."
  - "Bump-in-phase: pyproject went to 2026.7.0 now so the guard tests exercise the final state; the actual annotated tag remains a milestone-completion step."
  - "docker-publish.yml left UNCHANGED (D-05/D-06): its type=semver metadata-action already parses 2026.7.0 -> {{version}}=2026.7.0 / {{major}}.{{minor}}=2026.7."

patterns-established:
  - "D-12/D-13 rule: forward-looking version examples rewritten to CalVer; historical/feature-era vN.M labels left verbatim."

requirements-completed: [VER-01, VER-02, VER-03, VER-04]

# Metrics
duration: ~15min
completed: 2026-07-03
---

# Phase 65 Plan 02: CalVer Adoption (GREEN) Summary

**Turned the three Plan-01 RED gates GREEN: swapped ci.yml's tag glob to the CalVer-only `[0-9]+.[0-9]+.[0-9]+`, bumped pyproject to `2026.7.0` (uv.lock synced), added the MILESTONES milestone-version mapping table, and rewrote every forward-looking CalVer example across docs + compose while leaving all historical record and the docker-publish machinery untouched.**

## Performance

- **Duration:** ~15 min
- **Tasks:** 3
- **Files modified:** 10 (+1 SUMMARY created)

## Accomplishments

- **ci.yml tag glob** flipped from `v*.*.*` to the bare CalVer `[0-9]+.[0-9]+.[0-9]+` (quoted), comment de-`v`-ed; branch CI + `detect-changes` tag-ref force branch left intact (D-02/D-03). This is the highest-consequence edit — a wrong glob is a silent no-publish (the v4.0.2/v7.0 GOTCHA class), now caught by the Plan-01 guard + actionlint.
- **Version source of truth** bumped `7.0.0` → `2026.7.0` (bare CalVer, D-01) and `uv lock` re-synced the `phaze` entry in `uv.lock` (never hand-edited).
- **MILESTONES.md mapping table** added near the top: `| Milestone | Version | Date |` with `v1.0`..`v7.0` verbatim rows + the new `Engineering Improvements | 2026.7.0` row, plus convention prose (names decoupled from numbers; `YYYY.MM.REVISION`; no-leading-zero month; per-month zero-based REVISION). Every `## vN.M` detail section left verbatim (D-13).
- **Forward-looking CalVer docs**: `docs/deployment.md` release/tag-strategy section now documents the CalVer scheme + the D-04 annotated-tag-PUSH-triggers-publish invariant and the `git push --delete origin <tag>` + recreate recipe; pin/rollback examples across deployment/configuration/arm64/cloud-burst docs read bare CalVer; compose comments de-`v`-ed to `:<version>` / `:<version>-arm64`.

## Task Commits

1. **Task 1: Swap ci.yml glob + bump pyproject + uv lock** - `e6dbfb5` (feat)
2. **Task 2: Add MILESTONES mapping table + confirm ROADMAP conformance** - `0f1b17e` (docs)
3. **Task 3: Rewrite forward-looking CalVer doc/compose examples + README/compose decisions** - `6dee358` (docs)

## Files Created/Modified

- `.github/workflows/ci.yml` - `on.push.tags` → CalVer-only glob; comment de-`v`-ed
- `pyproject.toml` - `version = "2026.7.0"`
- `uv.lock` - phaze entry synced via `uv lock`
- `.planning/MILESTONES.md` - milestone↔version mapping table + CalVer convention prose
- `docs/deployment.md` - CalVer pin/rollback examples, `:<version>` phrasing, CalVer scheme + D-04 publish-invariant prose
- `docs/configuration.md` - `PHAZE_IMAGE_TAG` example → `2026.7.0`
- `docs/arm64-agent-image.md` - build example → `2026.7.0` / `2026.7.0-arm64`
- `docs/cloud-burst.md` - release/pin examples → `2026.7.0` / `2026.7.0-arm64`
- `docker-compose.agent.yml` - comments → `2026.7.0` / `:<version>` (image indirection untouched)
- `docker-compose.cloud-agent.yml` - comments → `2026.7.0` / `:<version>-arm64` (image indirection untouched)

## Decisions Made

1. **VER-02 README — no badge (RESEARCH Open-Q1).** README.md was intentionally left UNCHANGED: there is no version/release badge to update, so the VER-02 README clause is satisfied by the `docs/deployment.md` CalVer procedure prose. No badge was added or re-added (one-line badge rule). `git diff --stat README.md` confirms zero changes.
2. **docker-publish.yml UNCHANGED (D-05/D-06).** Verified left untouched — its `docker/metadata-action` `type=semver` already parses `2026.7.0` → `{{version}}=2026.7.0` and `{{major}}.{{minor}}=2026.7`, and `type=ref,event=tag` → `2026.7.0`. No workflow edit was needed to adopt CalVer, and both docker-publish guard tests stayed green.
3. **Bump-in-phase (RESEARCH Open-Q2).** pyproject moved to `2026.7.0` now so the guard tests exercise the final state; the actual annotated release tag remains a milestone-completion step.

## ⚠️ Memory-Update Note (D-04 — action required, cannot be done from the repo)

The external memory `project_release_procedure.md` MUST be updated to reflect CalVer:
- **Tags are bare CalVer** (no `v` prefix) — first tag `2026.7.0`.
- **CI tag glob** is `[0-9]+.[0-9]+.[0-9]+` (was `v*.*.*`).
- **REVISION** is a per-month zero-based counter (Nth release within `YYYY.MM`, resets each month) — supports same-month patch releases (`2026.7.0` → `2026.7.1`).
- **Release branch name** becomes `release/2026.7.0`.
- The **annotated-tag + delete-recreate GOTCHA carries over verbatim**: publish fires on the *push* of an annotated tag (`git tag -a` then `git push origin <tag>`); a bad push is recovered via `git push --delete origin <tag>` + re-tag + re-push.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- Initial Edit calls targeted the shared-checkout paths; re-issued against the worktree copies (`.claude/worktrees/agent-.../`). No content impact.
- `grep -c '${PHAZE_IMAGE_TAG:-latest}'` returned 0 due to shell/regex `{}` interpretation; re-verified with `grep -cF` = 4 (indirection intact). Not a code issue.

## Verification

- **All 4 Plan-01 CalVer guards GREEN** + full module: `uv run pytest tests/agents/deployment/test_agent_compose.py -q` → **13 passed**.
- Acceptance greps all pass: ci.yml has no `v*.*.*` (glob + comment), contains the exact `"[0-9]+.[0-9]+.[0-9]+"` literal; pyproject `2026.7.0`; uv.lock synced; MILESTONES table ≥9 `|` lines with header + v1.0..v7.0 + 2026.7.0 + `YYYY.MM.REVISION` prose; no forward-looking `PHAZE_IMAGE_TAG=v[0-9]` remains; deployment.md `:v<version>`/`v*.*.*` counts = 0; compose comments de-`v`-ed; image indirection unchanged; README unchanged.
- `pre-commit run` (actionlint, yamllint, EOF/whitespace, etc.) clean on every edited file.
- ROADMAP.md verified conformant (already references `2026.7.0` as scheduled; no new `vN.M`) — left unchanged.

## Next Phase Readiness

- CalVer is live across every forward-looking surface; the publish invariant is preserved. The remaining action is the external memory update noted above and the eventual annotated `2026.7.0` tag push at milestone completion.

## Self-Check: PASSED

- All modified/created files present on disk (ci.yml, pyproject.toml, uv.lock, MILESTONES.md, SUMMARY.md, both compose files).
- Commits verified in git log: `e6dbfb5` (Task 1), `0f1b17e` (Task 2), `6dee358` (Task 3), `20a9757` (SUMMARY).
- Working tree clean.

---
*Phase: 65-calver-adoption*
*Completed: 2026-07-03*
