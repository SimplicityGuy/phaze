# Phase 65: CalVer Adoption - Research

**Researched:** 2026-07-02
**Domain:** Release versioning (CalVer), GitHub Actions tag-glob filtering, docker/metadata-action tag strategy, docs/config forward-vs-historical string migration
**Confidence:** HIGH

## Summary

This is a CI/config/docs versioning-scheme migration with effectively zero runtime code. Move release versioning from milestone-aligned `vN.M` to bare calendar-based CalVer `YYYY.MM.REVISION` (first tag `2026.7.0`, no leading-zero month, REVISION = per-month zero-based counter). The technical core is a **single high-consequence edit**: the `ci.yml` `on: push: tags:` glob that gates whether a tag push runs CI/publish at all. Every other change (metadata-action, docs, MILESTONES table) is low-risk because the machinery already handles arbitrary semver-shaped refs.

Two authoritative facts drive the whole plan. **(1)** GitHub Actions filter patterns DO support `+` (one-or-more) and `[0-9]` bracket ranges — the official docs literally give `v[12].[0-9]+.[0-9]+` as a tag-filter example [CITED: github/docs workflow-syntax.md]. So the tightest correct glob is `'[0-9]+.[0-9]+.[0-9]+'` (quoted — starts with `[`). The CONTEXT note that "regex `+` is NOT supported" is **incorrect**; `+` is supported and yields the cleanest pattern. **(2)** `docker/metadata-action` `type=semver` accepts a bare `2026.7.0` tag as valid semver — `{{version}}` → `2026.7.0`, `{{major}}.{{minor}}` → `2026.7`, and `type=ref,event=tag` emits the raw `2026.7.0` [CITED: docker/metadata-action README]. So `docker-publish.yml` needs **no** action-config change — only the git-tag trigger and one guard test change.

**Primary recommendation:** Set `ci.yml` tags filter to `tags: ["[0-9]+.[0-9]+.[0-9]+"]` (CalVer-only, drop `v*.*.*`), leave `docker-publish.yml` metadata-action untouched, retarget the ONE literal-glob guard test (`test_ci_workflow_triggers_on_version_tags`, not the one CONTEXT named), rewrite only forward-looking `v4.0.0`-style pin/rollback examples in docs, and add a Milestone|Version|Date mapping table to `MILESTONES.md` with historical `vN.M` rows verbatim.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** CalVer git tags are **bare, no `v` prefix** — first tag `2026.7.0`.
- **D-02:** `ci.yml` publish trigger `on: push: tags: ["v*.*.*"]` **replaced outright** with a CalVer glob. **CalVer-only** — legacy `v*.*.*` is NOT retained.
- **D-03:** The `detect-changes` tag-force branch (`REF_TYPE == "tag"`) is ref-type-based; must keep working for a CalVer tag — verify (the `on: push: tags` filter is what gates whether the workflow runs at all).
- **D-04:** Preserve the annotated-tag-PUSH-triggers-GHCR-publish invariant + the `git push --delete`/recreate recovery recipe under the new scheme. Tag stays annotated (`git tag -a`).
- **D-05:** A tagged release MUST still publish BOTH a `:latest`-eligible image AND a version-pinnable `:2026.7.0`, plus the `2026.7` month tag from `type=semver,pattern={{major}}.{{minor}}`.
- **D-06:** With the bare tag, `type=ref,event=tag` emits `2026.7.0`. The guard test asserting a `:v<version>` tag **must be retargeted** to CalVer form — do NOT weaken the "both `:latest` and a version-pinnable tag" assertion.
- **D-07:** REVISION = per-month, zero-based counter (Nth release within `YYYY.MM`, starts at `0`; resets each calendar month). `2026.7.0` → `2026.7.1` → next month `2026.8.0`.
- **D-08:** Milestone name decoupled from version number; multiple milestones/patches in the same month just increment REVISION.
- **D-09:** Going forward milestones read as **named**, releases as **dated** CalVer. Add/maintain a `MILESTONES.md` mapping table: **Milestone | Version | Date**.
- **D-10:** Historical rows keep their `vN.M` verbatim — no retro-rename/re-version.
- **D-11:** Current milestone (literally "2026.7.0 Engineering Improvements"): keep referencing **by name**, record its release as `2026.7.0`. No project-wide rename churn.
- **D-12:** Rewrite **only forward-looking** procedure/example text (the "how to release" steps and `PHAZE_IMAGE_TAG=v4.0.0`-style pinning/rollback examples get a CalVer example).
- **D-13:** Leave ALL historical record verbatim. **Rule:** instructs the *next* release → update; records a *past* event → leave.

### Claude's Discretion
- The exact CI trigger glob (must match `2026.7.0`, reject branch/noise refs) — pin it. D-02 fixes policy (CalVer-only), not the regex.
- Precise wording/columns of the `MILESTONES.md` mapping table (D-09 fixes intent).
- Whether `pyproject.toml` `version` is bumped to `2026.7.0` **within** this phase vs at milestone-release time — sequence against the release procedure (this phase *adopts* the scheme; the actual annotated tag is a milestone-completion step).

### Deferred Ideas (OUT OF SCOPE)
- None deferred. Retroactive re-tagging of historical `vN.M` releases as CalVer is **hard out of scope** (not a deferral). No product/backend/UI behavior change. README badges stay one-line; do not re-add removed badges.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| VER-01 | CalVer `YYYY.MM.REVISION`, no leading-zero month (first `2026.7.0`), REVISION convention supporting same-month patches | REVISION semantics (D-07/D-08) documented as prose in `docs/deployment.md` + `MILESTONES.md` note; calver.org confirms `YYYY.MM.MICRO` is standard (§CalVer Reference). Verifiable by doc-string check. |
| VER-02 | Release procedure (pyproject `version` + `uv.lock` bump → annotated tag push → GHCR publish) + README version/badge line reflect CalVer | `ci.yml` glob edit + retargeted guard test (§Guard Tests); README has NO version badge today (§Edit Surface, Open Q1). Release-procedure prose lives in `docs/deployment.md` + memory (§Release Procedure). |
| VER-03 | Published Docker image tags + compose/deploy references use CalVer | metadata-action confirmed to emit `2026.7.0` / `2026.7` unchanged (§metadata-action). Compose files use `${PHAZE_IMAGE_TAG}` indirection — comments only, no `image:` literal change (§Edit Surface). |
| VER-04 | Milestone↔version mapping in ROADMAP.md + MILESTONES.md reads named milestones / dated releases, historical `vN.M` intact | New Milestone\|Version\|Date table seeded with v1.0..v7.0 verbatim + `2026.7.0` row (§Mapping Table). ROADMAP phase-table already uses `2026.7.0` as the milestone label for phases 63-66. |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Tag-push → run pipeline gate | CI (`ci.yml` `on: push: tags`) | — | The single glob decides whether a tag ref runs any workflow. Highest consequence. |
| Full-pipeline forcing on tag ref | CI (`detect-changes` job) | — | Ref-type-based (`REF_TYPE == "tag"`), glob-agnostic — no change needed (verified). |
| Image tag minting | CI (`docker-publish.yml` metadata-action) | — | `type=semver`/`type=ref` already parse `2026.7.0`; no config change. |
| Version source of truth | Build (`pyproject.toml` `version` + `uv.lock`) | — | Moves together via `uv lock`; never hand-edit `uv.lock`. |
| Release procedure prose | Docs (`docs/deployment.md`) + external memory | — | Forward-looking pin/rollback examples rewritten; step-by-step cut lives in memory (not a repo file). |
| Milestone↔version mapping | Docs (`MILESTONES.md`, `ROADMAP.md`) | — | Additive mapping table; historical detail sections untouched. |

## Standard Stack

No packages installed or changed. This phase touches YAML workflows, TOML, Markdown, and one Python guard-test file. Existing tooling only: GitHub Actions, `docker/metadata-action@v6.1.0` (already pinned by SHA `80c7e94`), `uv` (for the version bump), `pytest`/`yaml.safe_load` (guard tests).

## Package Legitimacy Audit

Not applicable — this phase installs **no external packages**. All changes are to existing config/docs/test files. No registry interaction, no dependency additions.

## Runtime State Inventory

> Included because this is a versioning-scheme migration (string-replacement flavored). The relevant "runtime state" is the published-image registry and the tag-trigger behavior, not stored data.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — no DB/datastore encodes the release-version scheme. Verified: version lives only in `pyproject.toml`/`uv.lock` and git tags. | None |
| Live service config (GHCR) | Historical `v1.0`..`v7.0` image tags remain published in GHCR. They are **NOT** retagged (out of scope). `:latest` continues to advance; new releases publish `2026.7.0`. | None — historical tags stay pullable verbatim; documented as intentional. |
| OS-registered state | None. | None |
| Secrets/env vars | `PHAZE_IMAGE_TAG` is the pin var (unchanged name); its *example values* in docs/compose-comments change (`v4.0.0` → `2026.7.0`). | Doc/comment example edits only (D-12). |
| Build artifacts / tag-trigger consequence | After the glob swap, a future push of a **legacy `vN.M.P` tag would no longer trigger publish**. | Accepted per D-02 (re-tagging historical releases is out of scope, so that publish path may lawfully stop firing). |

**The canonical question — after every file is updated, what still carries the old scheme?** Only (a) the immutable historical GHCR image tags and git tags (intentionally verbatim, D-10) and (b) the external memory `project_release_procedure.md` (not a repo file — see §Release Procedure for the surfaced memory-update note).

## Architecture Patterns

### The CI Tag-Gate Flow (highest-consequence edit)

```
git tag -a 2026.7.0 && git push origin 2026.7.0     [annotated tag push, D-04]
        │
        ▼
ci.yml  on: push: tags: ["[0-9]+.[0-9]+.[0-9]+"]     ← THE GATE (D-02). Wrong glob = silent no-publish.
        │  (tag ref matches → workflow runs)
        ▼
detect-changes job: REF_TYPE == "tag" → code-changed=true   [glob-agnostic, D-03, NO CHANGE]
        │
        ▼
quality → test → security → docker → aggregate-results (all green)
        │
        ▼
docker-publish.yml → docker/metadata-action                 [NO CHANGE — parses 2026.7.0 as semver]
        │  type=raw latest (default branch) ┐
        │  type=semver {{version}}  → 2026.7.0
        │  type=semver {{major}}.{{minor}} → 2026.7
        │  type=ref,event=tag        → 2026.7.0
        ▼
GHCR: ghcr.io/simplicityguy/phaze:{latest, 2026.7.0, 2026.7}   [publish invariant preserved, D-05/D-06]
```

### Pattern 1: The CalVer trigger glob
**What:** Bare 3-segment digit glob under `on: push: tags:`.
**Recommendation (pin this):**
```yaml
# Source: GitHub docs filter-pattern cheat sheet — official example `v[12].[0-9]+.[0-9]+`
on:
  push:
    branches: ["main"]
    tags: ["[0-9]+.[0-9]+.[0-9]+"]   # CalVer-only. Quoted because the pattern starts with `[`.
```
- `[0-9]+` = one-or-more digits (both `[]` ranges and `+` are supported by GHA filter patterns [CITED: github/docs]).
- `.` is a **literal** dot in GHA filter globs (no regex meaning).
- Full-ref match: `2026.7.0` ✓, `2026.12.10` ✓, `2026.7.1` ✓ | rejects `main` (no dots), `v2026.7.0` (`v` ∉ `[0-9]`), `2026.7` (only 2 segments), `2026.7.0-rc1` (trailing `-rc1` unmatched).
- **Must be quoted** in YAML — a bare `[0-9]...` starts a YAML flow sequence [CITED: github/docs: "If you start a pattern with `*`, `[`, or `!`, you must enclose the pattern in quotes"].

**Alternative (also valid, looser):** `'[0-9]*.[0-9]*.[0-9]*'` — CONTEXT's example. `*` matches zero-or-more of ANY char (not just digits), so it would also accept malformed refs like `2a.3.4b`. The `+` form is tighter and docs-backed; **recommend `+`**.

### Anti-Patterns to Avoid
- **Retaining `v*.*.*` alongside the CalVer glob.** D-02 is explicit: CalVer-only. Keeping both would leave a dead publish path and contradict the locked decision.
- **Editing `docker-publish.yml` metadata-action tag lines.** Unnecessary and risky — `type=semver` already handles `2026.7.0`. Touching it invites regressions in the `-arm64` / `/job` sibling jobs that share the identical tag block.
- **Hand-editing `uv.lock`.** Bump `version` in `pyproject.toml`, then `uv lock` to sync the `phaze` entry (MEMORY: pyproject ↔ uv.lock move together).
- **Rewriting historical strings.** "v4.0 shipped…", `## v7.0 UI Redesign` headers, `since vX` comments — all stay verbatim (D-13).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| CalVer scheme design | A bespoke version format | calver.org `YYYY.MM.MICRO` family | Standard, documented; nothing about `2026.7.0` (no-leading-zero month) is nonstandard. |
| Semver→image-tag derivation | Custom tag-string logic in the workflow | Existing `docker/metadata-action` `type=semver` | Already parses `2026.7.0`; emits `2026.7.0` + `2026.7`. |
| Tag-ref matching in a test | A Python re-implementation of GHA glob semantics (`+`, `[]` are NOT Python `fnmatch`) | Assert the literal pattern **string** is present in `ci.yml` | GHA globs ≠ `fnmatch`; simulating them in-test is fragile. Structural string assertion is what the existing guard test already does. |

**Key insight:** The entire phase leverages existing, already-correct machinery. The only genuine engineering decision is the exact glob string — everything else is text edits + one test retarget.

## Guard Tests — EXACT change surface

All in `tests/agents/deployment/test_agent_compose.py` (CONTEXT referenced the path `tests/test_deployment/...` — the actual path is `tests/agents/deployment/test_agent_compose.py`).

| Test | Line(s) | Verdict | Action |
|------|---------|---------|--------|
| **`test_ci_workflow_triggers_on_version_tags`** | **332** | **MUST CHANGE (critical)** | Line 332 literally asserts `any("v*.*.*" in str(t) for t in tags)`. **This is the real D-02 guard test** (CONTEXT named the wrong one). Retarget to assert the chosen CalVer glob present, e.g. `any("[0-9]+.[0-9]+.[0-9]+" in str(t) for t in tags)`, AND add `assert not any("v*.*.*" in str(t) for t in tags)` (CalVer-only, D-02). Update docstring (lines 316-333: "3-part semver tag", "PHAZE_IMAGE_TAG=vX.Y.Z") to CalVer wording. **Keep** the branch-preservation assertion (lines 336-339) unchanged. |
| `test_docker_publish_workflow_tags_both_latest_and_version` | 173-206 | **DOCSTRING ONLY** (assertion already CalVer-safe) | The functional assertion (lines 196-197) checks `type=semver` / `type=ref,event=tag` presence — it **never tests a literal `v`**, so it PASSES unchanged under CalVer. Per D-06 "retarget, don't weaken": update the docstring (lines 174-185 say `:v<version>`) to `:<version>` (bare CalVer). Optionally strengthen to assert `pattern={{version}}` present (already true). No weakening. |
| `test_ci_detect_changes_forces_code_changed_on_tags` | 342-369 | **NO CHANGE (verifies D-03)** | Asserts `REF_TYPE`/`refs/tags` handling + `code-changed=true` — glob-agnostic. Confirms the tag-force branch keeps firing for a CalVer tag. |
| `test_docker_publish_arm64_job_tags_latest_and_version` | 234-289 | NO CHANGE | Checks `flavor: suffix=-arm64` + semver/ref patterns; no literal `v`. |
| `test_ci_detect_changes_survives_force_push` | 372-395 | NO CHANGE | Unrelated (force-push diff fallback). |
| `test_cleanup_package_list_matches_published_images` | 398-428 | NO CHANGE | Package-set symmetry; version-agnostic. |

**New test opportunity (Nyquist, recommended):** a small positive-assertion test in the same file that the `ci.yml` tags glob equals the exact CalVer pattern and that no `v*.*.*` remains — effectively the retargeted `test_ci_workflow_triggers_on_version_tags`, made explicit. Optionally a `MILESTONES.md` structure guard (see §Validation Architecture).

## Full Edit Surface — file → lines → change/leave

Classified per D-12 (forward-looking → rewrite) / D-13 (historical → leave).

### CI / build (functional)
| File | Line(s) | Current | Change / Leave |
|------|---------|---------|----------------|
| `.github/workflows/ci.yml` | 13 | `tags: ["v*.*.*"]` | **CHANGE** → `tags: ["[0-9]+.[0-9]+.[0-9]+"]` (D-02). Also update the trailing comment on lines 6-11 that says "v\*.\*.\* tags". |
| `.github/workflows/ci.yml` | 50-59 | `REF_TYPE == "tag"` force branch | **LEAVE** (D-03, verified glob-agnostic). |
| `.github/workflows/docker-publish.yml` | 108-115 (and sibling jobs 250-257, 357-363, 587-608) | `type=semver`/`type=ref,event=tag` | **LEAVE** — parses `2026.7.0` unchanged (D-05/D-06). |
| `pyproject.toml` | 7 | `version = "7.0.0"` | **CHANGE → `2026.7.0`** — but SEQUENCE per Claude's Discretion: bump-in-phase vs at-release. `uv lock` after. |
| `tests/agents/deployment/test_agent_compose.py` | 332 (+ docstrings) | see §Guard Tests | **CHANGE** (test 1 assertion + docstrings; test 2 docstring). |

### Docs (forward-looking examples → rewrite `v4.0.0`/`v5.0.0` → `2026.7.0`)
| File | Line(s) | Content | Change / Leave |
|------|---------|---------|----------------|
| `docs/deployment.md` | 262 | `PHAZE_IMAGE_TAG=v4.0.0` (or `latest`) | **CHANGE** → `2026.7.0`. |
| `docs/deployment.md` | 348 | "produce both `:latest` and `:v<version>`" | **CHANGE** → `:<version>` (bare CalVer). |
| `docs/deployment.md` | 349-351 | "Release tags MUST be 3-part semver (`vX.Y.Z`, e.g. `v4.0.0`) — triggers on `v*.*.*`… A 2-part tag (`v4.0`) will not match" | **CHANGE** → bare 3-part CalVer `YYYY.MM.REVISION` (e.g. `2026.7.0`), matched by the CalVer glob; **keep the 3-part rationale** (still required so `{{version}}`/`{{major}}.{{minor}}` resolve). |
| `docs/deployment.md` | 370 | pin "(`v4.0.0`)" | **CHANGE** → `2026.7.0`. |
| `docs/deployment.md` | 398-409 | rollback tag-swap `PHAZE_IMAGE_TAG=v4.0.0` + "tags both `:latest` and `:v<version>`" | **CHANGE** example → `2026.7.0` / `:<version>`; mechanism unchanged. |
| `docs/deployment.md` | 411-415 | "`git checkout v4.0.0`" rollback example | **CHANGE** illustrative example → `2026.7.0` (note: rolling back to a *pre-CalVer* release still uses the old `v4.0.0` tag — the example is illustrative, mechanism unchanged). |
| `docs/deployment.md` | 475-482 | first-time-setup pin `v4.0.0` + "MUST be 3-part `vX.Y.Z`… only publishes on push of `v*.*.*`" | **CHANGE** → CalVer. |
| `docs/deployment.md` | 505 | checklist "(`v4.0.0`)" | **CHANGE** → `2026.7.0`. |
| `docs/deployment.md` | 2, 4 | "Phaze v4.0 Deployment Guide" / "v4.0 (Distributed Agents)" | **LEAVE (flag as discretionary)** — names the architecture era (feature-generation label), not a release instruction (D-13). Changing risks scope creep. |
| `docs/configuration.md` | 291, 350 | `PHAZE_IMAGE_TAG` example "(e.g., `v4.0.0`)" | **CHANGE** → `2026.7.0`. |
| `docs/arm64-agent-image.md` | 128-129 | `just image-build-arm64 v5.0.0` → `:v5.0.0-arm64` | **CHANGE (low priority)** illustrative build example → `2026.7.0` / `2026.7.0-arm64`; suffix mechanism unchanged. |
| `docs/arm64-agent-image.md` | 4-5, 191-194 | "foundation of the v5.0 Cloud Burst path"; "`<version>-arm64` on a release tag" | **LEAVE** — feature-era label (D-13) + generic `<version>` (no literal). |
| `docs/cloud-burst.md` | 215, 250 | `phaze:v5.0.0-arm64` illustration; `PHAZE_IMAGE_TAG=v5.0.0` pin example | **CHANGE (low priority)** forward-looking examples → `2026.7.0`; this doc is v5.0-feature-specific, so scope-check with planner. |

### Compose (indirection — comments only)
| File | Line(s) | Change / Leave |
|------|---------|----------------|
| `docker-compose.agent.yml` | 27 (comment `PHAZE_IMAGE_TAG=v4.0.0`) | **CHANGE comment** → `2026.7.0`. `image:` lines 33/45/57/68 use `${PHAZE_IMAGE_TAG:-latest}` → **LEAVE** (verified: no literal version in any `image:` line). |
| `docker-compose.cloud-agent.yml` | 35 (comment `PHAZE_IMAGE_TAG=v5.0.0`) | **CHANGE comment** → `2026.7.0`. `image:` line 45 uses `${PHAZE_IMAGE_TAG:-latest}-arm64` → **LEAVE**. |

**Guard-test impact of compose:** `test_all_agent_services_pull_from_ghcr` asserts the `PHAZE_IMAGE_TAG` **token** is present, not a literal version — unaffected by comment edits.

### README (VER-02, see Open Q1)
| File | Line | Content | Change / Leave |
|------|------|---------|----------------|
| `README.md` | 12 | Badge row: CI, codecov, `License: MIT`, `Python 3.14+` | **NO version/release badge exists.** VER-02's "README version/badge line" clause has no version string to edit here. See Open Q1 — planner decides whether to add a CalVer reference or treat VER-02's README clause as satisfied by the deployment.md procedure. Keep badges one-line (MEMORY). |
| `README.md` | 39, 138, 149 | "v7.0 DAG-Centric Shell", "v7.0 console", "v7.0 cutover" | **LEAVE** — feature-generation labels describing the current UI era (D-13). |

## Milestone↔Version Mapping (VER-04)

### MILESTONES.md — add a mapping table (additive; detail sections stay verbatim)
Current structure: chronological `## vN.M Name (Shipped: date)` detail sections. Add a Milestone|Version|Date table near the top (before the first detail section). Seed historical rows verbatim (D-10); the current milestone referenced by name (D-11):

```markdown
## Milestone ↔ Version Mapping

| Milestone | Version | Date |
|-----------|---------|------|
| MVP | v1.0 | 2026-03-30 |
| Metadata Enrichment & Tracklist Integration | v2.0 | 2026-04-02 |
| Cross-Service Intelligence & File Enrichment | v3.0 | 2026-04-04 |
| Distributed Agents | v4.0 | 2026-05-17 |
| Cloud Burst Analysis | v5.0 | 2026-06-26 |
| Kubernetes Burst Analysis | v6.0 | 2026-06-29 |
| UI Redesign — DAG-Centric Hybrid Console | v7.0 | 2026-07-02 |
| Engineering Improvements | 2026.7.0 | _(release date TBD)_ |
```
- Version column keeps `vN.M` verbatim for history (D-10); the new row uses bare CalVer `2026.7.0` (D-01) with milestone named "Engineering Improvements" (D-11 — the compound "2026.7.0 Engineering Improvements" name stays as-is elsewhere; no rename churn).
- The existing `## vN.M …` detail sections are **left verbatim** (D-13). Going forward, new milestone detail sections should read name-first with the CalVer date (e.g. `## Engineering Improvements (2026.7.0 — Shipped: YYYY-MM-DD)`).

### ROADMAP.md — already largely conformant
- Phase-status table (line ~205): rows 63-66 already carry `2026.7.0` in the "Milestone" column, and the milestone header (line 18) already reads "2026.7.0 Engineering Improvements". Historical phase rows keep their `vN.M` milestone labels verbatim (D-10).
- Minimal VER-04 action in ROADMAP: none strictly required beyond ensuring no forward-looking text implies a *new* `vN.M`; the concrete VER-04 artifact is the `MILESTONES.md` table. Confirm the ROADMAP "Adopt CalVer" backlog note (line ~964) reads as scheduled/done, not aspirational.

## Release Procedure (D-04 invariant + memory note)

- **Repo-authoritative procedure prose is limited** to the tag-strategy / pinning / rollback text in `docs/deployment.md` (§Edit Surface). Verified: `docs/deployment.md` does **not** contain the step-by-step "cut a release" flow (no `annotated`/`git push --delete`/`uv lock`/`release/` strings present).
- **The full release procedure lives in the external memory `project_release_procedure.md`** (`~/.claude/.../memory/`) — NOT a repo file. It documents: annotated-tag-only releases (no GH Releases), two-place bump (`pyproject` + `uv.lock` via `uv lock`), the `release/vX.Y.Z` PR flow, tag-triggered publish, the `git push --delete origin <tag>` + recreate GOTCHA, and the 3-part-segment requirement.
- **Plan cannot edit memory.** The plan should **surface a memory-update note** (for the human/orchestrator) to update `project_release_procedure.md` to: bare CalVer tags (no `v`), the new glob, first tag `2026.7.0`, the per-month zero-based REVISION convention, and the `release/2026.7.0` branch name. The delete-recreate recipe and annotated-tag invariant carry over verbatim (D-04).

## Common Pitfalls

### Pitfall 1: Wrong or unquoted CI glob → silent no-publish
**What goes wrong:** A tag push that doesn't match `on: push: tags` runs **no workflow at all** — no error, no build, no image. This is the v4.0.2/v7.0 GOTCHA class.
**Why:** The `tags` filter is the sole gate for tag refs; `detect-changes` only runs *after* the workflow triggers.
**How to avoid:** Use `'[0-9]+.[0-9]+.[0-9]+'` (quoted — starts with `[`). The retargeted `test_ci_workflow_triggers_on_version_tags` catches a dropped/wrong pattern in CI **before** the release.
**Warning signs:** Pushing the tag produces zero Actions runs on the tag ref.

### Pitfall 2: Assuming the CONTEXT-named guard test is the critical one
**What goes wrong:** CONTEXT points at `test_docker_publish_workflow_tags_both_latest_and_version` for the glob change, but that test never asserts a literal `v` — it passes unchanged. The **real** literal-`v*.*.*` assertion is in `test_ci_workflow_triggers_on_version_tags` (line 332). Missing it leaves a red test after the glob swap.
**How to avoid:** Retarget line 332 (§Guard Tests). Both tests are in the same file.

### Pitfall 3: `.` treated as regex in the glob
**What goes wrong:** Treating GHA filter globs as regex. `.` is a **literal** dot; `*`/`+`/`[]`/`?` are the only specials. A pattern like `[0-9]+\.[0-9]+` (escaped dot) would try to match a literal backslash.
**How to avoid:** Use a plain literal dot: `[0-9]+.[0-9]+.[0-9]+`.

### Pitfall 4: pyproject/uv.lock drift
**What goes wrong:** Bumping `pyproject.toml` `version` without re-locking leaves `uv.lock`'s `phaze` entry stale; hand-editing `uv.lock` corrupts hashes.
**How to avoid:** Edit `pyproject.toml`, then `uv lock` (MEMORY).

## Code Examples

### CI trigger glob (verified)
```yaml
# Source: GitHub docs — Workflow syntax, "Filter pattern cheat sheet"
# Official example given: `v[12].[0-9]+.[0-9]+`  (bracket ranges + `+` one-or-more)
on:
  push:
    branches: ["main"]
    tags: ["[0-9]+.[0-9]+.[0-9]+"]
```

### docker/metadata-action — NO CHANGE (already CalVer-correct)
```yaml
# Source: docker/metadata-action README — type=semver accepts a bare `1.2.3`-style tag.
# For tag `2026.7.0`:
tags: |
  type=raw,value=latest,enable={{is_default_branch}}
  type=semver,pattern={{version}}          # → 2026.7.0
  type=semver,pattern={{major}}.{{minor}}  # → 2026.7
  type=ref,event=tag                       # → 2026.7.0  (raw tag, no `v` to strip)
```

### Retargeted guard assertion (illustrative)
```python
# tests/agents/deployment/test_agent_compose.py — test_ci_workflow_triggers_on_version_tags
CALVER_GLOB = "[0-9]+.[0-9]+.[0-9]+"
assert isinstance(tags, list) and any(CALVER_GLOB in str(t) for t in tags), (
    f'ci.yml must trigger on the bare CalVer glob {CALVER_GLOB!r}; got tags={tags!r}'
)
assert not any("v*.*.*" in str(t) for t in tags), "legacy v*.*.* glob must be dropped (D-02: CalVer-only)"
```

## CalVer Reference

- calver.org defines the `YYYY.MM.MICRO` family; `2026.7.0` (four-digit year, non-zero-padded month, micro/revision) is a standard, recognized CalVer form [CITED: calver.org]. Nothing about the chosen scheme is nonstandard. The no-leading-zero month (`7`, not `07`) is an explicitly allowed variant.
- REVISION-as-per-month-zero-based (D-07) is a project convention layered on top of the standard MICRO segment — document it in prose (calver.org leaves MICRO semantics to the project).

## Validation Architecture

> `workflow.nyquist_validation` is `true` in `.planning/config.json` — this section is required.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (+ `pyyaml` `yaml.safe_load` for structural workflow parsing) |
| Config file | `pyproject.toml` (`[tool.pytest...]`); tests run via `uv run pytest` |
| Quick run command | `uv run pytest tests/agents/deployment/test_agent_compose.py -x` |
| Full suite command | `uv run pytest` (or bucketed: `just test-bucket agents`) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| VER-01 | CalVer scheme + REVISION convention documented | doc-review + structural | doc-string grep for `YYYY.MM.REVISION` / `2026.7.0` in `docs/deployment.md` + `MILESTONES.md` | ⚠️ partial — add a doc-string guard (Wave 0) |
| VER-02 | CI triggers on CalVer glob; legacy `v*.*.*` dropped; procedure prose reflects CalVer | unit (retargeted) | `uv run pytest tests/agents/deployment/test_agent_compose.py::test_ci_workflow_triggers_on_version_tags -x` | ✅ (retarget existing) |
| VER-02 | detect-changes still forces full pipeline on a tag ref | unit | `...::test_ci_detect_changes_forces_code_changed_on_tags -x` | ✅ (no change — proves D-03) |
| VER-03 | Image tags: both `:latest` + version-pinnable via metadata-action | unit | `...::test_docker_publish_workflow_tags_both_latest_and_version -x` | ✅ (docstring-only) |
| VER-04 | Mapping table exists; historical `vN.M` rows intact | structural (new) | new `test_milestones_mapping_table_intact` asserting a Milestone\|Version\|Date table with `v1.0..v7.0` rows present | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/agents/deployment/test_agent_compose.py -x`
- **Per wave merge:** `uv run pytest` (or `just test-bucket agents` + affected buckets)
- **Phase gate:** Full suite green + `pre-commit run --all-files` (frozen-SHA hooks, actionlint validates the edited `ci.yml`) before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] Retarget `tests/agents/deployment/test_agent_compose.py::test_ci_workflow_triggers_on_version_tags` (line 332 + docstring) — covers VER-02 (the critical D-02 glob).
- [ ] Update docstring of `test_docker_publish_workflow_tags_both_latest_and_version` — covers VER-03/D-06.
- [ ] (Recommended) Add `test_milestones_mapping_table_intact` — covers VER-04 (table present + historical rows verbatim).
- [ ] (Recommended) Add a doc-string guard asserting `docs/deployment.md`/`MILESTONES.md` state the CalVer scheme + REVISION convention — covers VER-01 (else VER-01 is doc-review-only).
- **Non-automatable (doc-review criteria):** the *prose quality* of the rewritten forward-looking examples and the "instructs-next-release vs records-past-event" D-12/D-13 classification per string — reviewer-judged, not test-asserted.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `docs/deployment.md` line numbers (~262/348-351/370/398-415/475-505) are current | Edit Surface | LOW — line drift only; grep-anchored strings (`PHAZE_IMAGE_TAG=v4.0.0`, `v*.*.*`) are the real targets. |
| A2 | No stored data / DB encodes the release-version scheme | Runtime State Inventory | LOW — verified version lives only in pyproject/uv.lock/git tags. |
| A3 | The external memory `project_release_procedure.md` is the sole home of the step-by-step release cut | Release Procedure | LOW — verified `docs/deployment.md` lacks the annotated-tag/delete-recreate prose. |

*(All glob-syntax and metadata-action claims are CITED, not assumed.)*

## Open Questions (RESOLVED)

1. **README VER-02 clause — there is no version badge.** `README.md` line 12 has CI/codecov/license/`Python 3.14+` badges; none encode the release version. VER-02 says "the README version/badge line reflect the CalVer scheme."
   - What we know: no release-version string exists in README to edit; line 39's "v7.0" is a feature-era label (leave, D-13).
   - Recommendation: Planner chooses one of — (a) treat VER-02's README clause as satisfied because there is no stale version claim to fix (the release-procedure prose in `docs/deployment.md` carries CalVer), or (b) add a one-line CalVer release reference/badge (keeping the badge row one-line per MEMORY). Lean (a) to avoid re-adding a removed badge; surface for the discuss/plan step.
   - **RESOLVED — option (a): no README version-badge edit.** VER-02 is satisfied by the `docs/deployment.md` CalVer procedure prose; do NOT add or re-add a removed badge (per `readme-badge-style` memory). Implemented in Plan 02 Task 3 (README left byte-unchanged; decision recorded in SUMMARY).

2. **pyproject bump timing (Claude's Discretion, D).** Bump `version = "2026.7.0"` **within** this phase or at milestone-release time? Recommendation: bump within the phase so the version source-of-truth is consistent with the adopted scheme and the guard tests exercise the final state; the actual annotated tag remains a milestone-completion step (the tag, not the file, triggers publish).
   - **RESOLVED — bump within this phase.** Set `version = "2026.7.0"` in pyproject and `uv lock` now, so the guard tests exercise the final state; the annotated tag remains a milestone-completion step (the tag, not the file, triggers publish). Implemented in Plan 02 Task 1.

3. **cloud-burst.md / arm64-agent-image.md example edits — in scope?** These carry forward-looking `v5.0.0` examples but are v5.0-feature-specific docs. Recommendation: rewrite the pin/build **examples** to `2026.7.0` (D-12) but leave the "v5.0 Cloud Burst" feature-era labels (D-13). Low priority; scope-check with planner to avoid churn.
   - **RESOLVED — in scope.** Rewrite the forward-looking `vN.M.P` pin/build examples in both docs to `2026.7.0` (`2026.7.0-arm64` where the suffix appears), and leave the "v5.0 Cloud Burst" feature-era labels verbatim (D-13). Both files are in Plan 02 Task 3 `files_modified`.

## Environment Availability

> Minimal — no new external dependencies. All tools already present and used across phases 63-64.

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| git (annotated tags) | Release procedure (D-04) | ✓ | system | — |
| uv | `uv lock` after version bump | ✓ | project constraint | — |
| GitHub Actions | CI glob / publish | ✓ (hosted) | — | — |
| docker/metadata-action | Image tag minting | ✓ (SHA-pinned `80c7e94`) | v6.1.0 | — |

**No missing dependencies.**

## Sources

### Primary (HIGH confidence)
- github/docs — Workflow syntax, "Filter pattern cheat sheet" (`workflow-syntax.md`): `+` = "Matches one or more of the preceding character"; `[]` bracket ranges; official tag example `v[12].[0-9]+.[0-9]+`; YAML quoting rule for patterns starting with `*`/`[`/`!`. https://github.com/github/docs/blob/main/content/actions/reference/workflows-and-actions/workflow-syntax.md
- docker/metadata-action README — `type=semver` accepts bare `1.2.3`-style tags → `{{version}}`/`{{major}}.{{minor}}`; `type=ref,event=tag` emits raw tag. https://github.com/docker/metadata-action
- Repo files read directly: `.github/workflows/ci.yml`, `.github/workflows/docker-publish.yml`, `tests/agents/deployment/test_agent_compose.py`, `pyproject.toml`, `README.md`, `docs/deployment.md`, `docs/configuration.md`, `docs/arm64-agent-image.md`, `docs/cloud-burst.md`, `docker-compose*.yml`, `.planning/{MILESTONES,REQUIREMENTS,ROADMAP}.md`, `.planning/config.json`.

### Secondary (MEDIUM confidence)
- GitHub community discussion #26714 + github/docs issue #18969 — corroborate `[]` range + `+` semantics and the YAML-quoting requirement. https://github.com/orgs/community/discussions/26714

### Tertiary (LOW confidence)
- calver.org — `YYYY.MM.MICRO` family reference (cited for scheme legitimacy; not tool-verified this session). https://calver.org/

## Metadata

**Confidence breakdown:**
- CI glob syntax: HIGH — official docs give the exact `[0-9]+` tag example + YAML-quoting rule.
- metadata-action semver on CalVer ref: HIGH — README confirms bare-tag parsing + raw-ref emission.
- Guard-test change surface: HIGH — read the test file directly; identified the mis-attribution (line 332 is the real critical test).
- Edit surface (docs): MEDIUM-HIGH — grep-anchored to literal strings; line numbers approximate (A1).
- Mapping-table shape / README VER-02: MEDIUM — shape is prescriptive; README has no version badge (Open Q1 is a genuine discretion point).

**Research date:** 2026-07-02
**Valid until:** 2026-08-01 (stable; GHA filter syntax + metadata-action v6 are settled).
