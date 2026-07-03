# Requirements: Phaze — Milestone 2026.7.0 Engineering Improvements

**Defined:** 2026-07-02
**Core Value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata — human-in-the-loop, nothing moves without review.

**Milestone framing:** Cleanup / engineering-debt paydown. **No product-behavior change and no backend behavior change** — every requirement below is CI/build/versioning/test/dead-code infrastructure or presentation-only. The "user" for these requirements is the project maintainer/operator, not the end user.

## Milestone 2026.7.0 Requirements

Requirements for this milestone. Each maps to exactly one roadmap phase (63+).

### CI — Parallel test partition & code-change gating

- [x] **CI-01**: The ~1,750-test pytest suite is partitioned into independently-runnable buckets — one per pipeline workflow-step (discovery, metadata, fingerprint, analyze, identify/tracklist, review/apply, agents/distributed) plus a generic/shared bucket (schema, config, helpers, routing) — each selectable in isolation without running the whole suite.
- [x] **CI-02**: CI fans the buckets out across parallel jobs (job matrix and/or `pytest-xdist`) instead of one serial run, measurably cutting wall-clock CI time.
- [x] **CI-03**: Per-shard `.coverage` files are combined into a single coverage report and one Codecov upload, preserving the enforced coverage gate (no per-shard coverage loss, no double-counting).
- [x] **CI-04**: The full build/test/security CI runs only when code changes; docs-, `.planning/`-, and markdown-only changes skip the heavy jobs while required status checks still report success (skip-with-success, not skip-absent — required checks stay satisfiable on doc-only PRs).

### VER — CalVer release versioning

- [x] **VER-01**: Release versioning uses CalVer `YYYY.MM.REVISION` with no leading-zero month (first release `2026.7.0`), and a REVISION convention that supports multiple same-month patch releases.
- [x] **VER-02**: The release procedure (pyproject `version` + `uv.lock` bump → annotated tag push → GHCR publish) and the README version/badge line reflect the CalVer scheme.
- [x] **VER-03**: Published Docker image tags and any compose/deploy references use the CalVer version.
- [x] **VER-04**: The milestone↔version mapping in ROADMAP.md and MILESTONES.md is updated so milestones read as named and releases as dated, without breaking the historical `vN.M` record.

### COV — Per-module coverage uplift

- [x] **COV-01**: Under-covered source modules are raised to a per-module coverage floor, prioritizing the v7.0-touched and worst offenders (`services/agent_liveness.py`, `routers/shell.py`, `services/pipeline.py`, `routers/tracklists.py`, `routers/pipeline.py`, `main.py`, and the 71–78% tail), with added tests asserting observable behavior — not coverage-padding.
- [x] **COV-02**: The enforced coverage gate is raised above the current 90.38% baseline (exact project and/or per-module target set at plan time) and wired into CI so future coverage regressions fail the build.

### DOCS — Documentation-drift gate

- [ ] **DOCS-01**: A CI gate cross-checks REQUIREMENTS.md traceability against passed phases and fails when the table is stale (a passed phase's requirements left unmarked, or a requirement marked without a passed phase), closing the manual REQUIREMENTS/ROADMAP sync gap called out across the retrospectives.

### CLEAN — Dead-code sweep & /saq re-link

- [ ] **CLEAN-01**: A discreet in-UI link to the still-mounted `/saq` SAQ monitor is restored in the shell (natural home: the Agents/Compute page), reachable without typing the raw URL. Presentation-only.
- [ ] **CLEAN-02**: Vestigial dead code (unused templates, routers, assignments surfaced during the v7.0 cutover) is identified and removed, and the dead-template guard's blind spot for its own unused entry-root literals (per the v7.0 retrospective) is closed.

## Future Requirements

Deferred — tracked but not in this milestone's roadmap.

### Multi-cloud backends

- **MCB-01..**: Pluggable analysis-backend registry (local + 1+ Kueue + 1+ cloud-compute simultaneously, cost-tiered ranks + caps, static/no-provisioning). Design already on `main` (PR #182); promote to its own milestone after this one.

## Out of Scope

Explicitly excluded to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Any product/end-user feature change | This milestone is engineering-debt paydown only |
| Any backend/pipeline behavior change | Coverage & cleanup work must not alter runtime behavior; `/saq` re-link is presentation-only |
| AcoustID / MusicBrainz Track-ID (IDENT-03) | Deferred from v7.0; a feature, not engineering cleanup |
| Rewriting the coverage tooling (pytest-cov → other) | Combine-across-shards on existing pytest-cov is sufficient; no tooling swap |
| Retroactively re-tagging historical `vN.M` releases as CalVer | CalVer applies going forward; the historical record stays intact |
| Full monorepo/service split or build-system change | Out of scope; CI partitioning is job-level, not repo restructuring |

## Traceability

Which phases cover which requirements. Populated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| CI-01 | Phase 63 | Complete |
| CI-02 | Phase 63 | Complete |
| CI-03 | Phase 63 | Complete |
| CI-04 | Phase 63 | Complete |
| VER-01 | Phase 65 | Complete |
| VER-02 | Phase 65 | Complete |
| VER-03 | Phase 65 | Complete |
| VER-04 | Phase 65 | Complete |
| COV-01 | Phase 64 | Complete |
| COV-02 | Phase 64 | Complete |
| DOCS-01 | Phase 66 | Pending |
| CLEAN-01 | Phase 66 | Pending |
| CLEAN-02 | Phase 66 | Pending |

**Coverage:**
- Milestone requirements: 13 total
- Mapped to phases: 13 ✓ (Phase 63: CI-01..04 · Phase 64: COV-01/02 · Phase 65: VER-01..04 · Phase 66: DOCS-01, CLEAN-01/02)
- Unmapped: 0 ✓ (no orphans, no duplicates)

---
*Requirements defined: 2026-07-02*
*Last updated: 2026-07-02 — roadmap created; all 13 requirements mapped to phases 63-66*
