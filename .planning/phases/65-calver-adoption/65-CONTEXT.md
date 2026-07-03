# Phase 65: CalVer Adoption - Context

**Gathered:** 2026-07-02
**Status:** Ready for planning

<domain>
## Phase Boundary

Move release versioning from milestone-aligned `vN.M` to calendar-based CalVer `YYYY.MM.REVISION` (first release `2026.7.0`, no leading-zero month) across: the release procedure (pyproject `version` + `uv.lock` bump → annotated tag push → GHCR publish), the README version/badge line, published Docker image tags + compose/deploy references, and the milestone↔version mapping in ROADMAP.md / MILESTONES.md — **without breaking the historical `vN.M` record**.

**This is the milestone that adopts CalVer**, so its own release is the first CalVer tag (`2026.7.0`).

**Hard out of scope:** retroactively re-tagging historical `vN.M` releases as CalVer. CalVer applies going forward only. No product/backend/UI behavior change. Keep README badges on one line; don't re-add removed badges.

</domain>

<decisions>
## Implementation Decisions

### Tag form & CI publish trigger
- **D-01:** CalVer git tags are **bare, no `v` prefix** — the first tag is `2026.7.0` (matches calver.org convention + the requirement text literally). This is the higher-surface option and deliberately chosen over keeping `v2026.7.0`.
- **D-02:** The `ci.yml` publish trigger `on: push: tags: ["v*.*.*"]` is **replaced outright** with a CalVer-matching glob (e.g. `["[0-9]*.[0-9]*.[0-9]*"]` — planner/researcher to pin the exact pattern that matches `2026.7.0` and rejects noise). **CalVer-only** — the legacy `v*.*.*` glob is NOT retained (re-tagging a historical `vN.M.P` release is out of scope, so that publish path may lawfully stop firing).
- **D-03:** The `detect-changes` job's tag-force branch (`ci.yml`, `[[ "${REF_TYPE}" == "tag" ]]` forces the full pipeline) is ref-type-based, not glob-based — it should keep working for a CalVer tag, but **verify** it during planning (the `on: push: tags` filter is what gates whether the workflow runs at all for a tag ref).
- **D-04:** Preserve the annotated-tag-PUSH-triggers-GHCR-publish invariant (see canonical ref `project-release-procedure` memory). The tag stays **annotated** (`git tag -a`) and is pushed as a fresh create event. The `git push --delete origin <tag>` + recreate GOTCHA still applies to the new scheme.

### Published image tags (publish invariant)
- **D-05:** A tagged release MUST still publish BOTH a `:latest`-eligible image (main-branch push) AND a version-pinnable `:2026.7.0` image, plus the `2026.7` month-rolling tag emitted by `type=semver,pattern={{major}}.{{minor}}`. CalVer `2026.7.0` is valid semver, so `docker/metadata-action`'s `type=semver` patterns (`{{version}}` → `2026.7.0`, `{{major}}.{{minor}}` → `2026.7`) work unchanged.
- **D-06:** With the bare tag, `type=ref,event=tag` now emits image tag `2026.7.0` (previously `v2026.7.0`). The guard test `tests/test_deployment/test_agent_compose.py::test_docker_publish_workflow_tags_both_latest_and_version` (asserts a `:v<version>` tag) **must be updated** to assert the CalVer form. Do NOT weaken the "both `:latest` and a version-pinnable tag" assertion — retarget it.

### REVISION semantics
- **D-07:** REVISION is a **per-month, zero-based counter** = the Nth release within a given `YYYY.MM`, starting at `0`. First July 2026 release = `2026.7.0`; same-month patches / subsequent releases = `2026.7.1`, `2026.7.2`; the next calendar month resets → `2026.8.0`.
- **D-08:** Multiple milestones OR patch releases landing in the same month simply keep incrementing REVISION — the **milestone name is decoupled from the version number**. This is the convention that supports the prior `v4.0.x`-style same-month patch cadence (VER-01).

### Milestone↔version mapping (VER-04)
- **D-09:** Going forward, milestones read as **named** (e.g. "Engineering Improvements") and releases as **dated** CalVer. Add/maintain a mapping table in `MILESTONES.md` with columns **Milestone | Version | Date** (or equivalent).
- **D-10:** Historical rows keep their `vN.M` verbatim — do NOT retro-rename or re-version past milestones. New rows are dated CalVer.
- **D-11:** For the current transitional milestone (literally named "2026.7.0 Engineering Improvements"): keep referencing it **by name**, and record its release as `2026.7.0` in the mapping. Do not do a project-wide rename churn of the compound milestone name — the "named + table" presentation carries VER-04, not a rename.

### Historical-string boundary
- **D-12:** Rewrite **only forward-looking procedure/example text** — the "how to release" steps and `PHAZE_IMAGE_TAG=v4.0.0`-style pinning/rollback examples in `docs/deployment.md` get a CalVer example (e.g. `PHAZE_IMAGE_TAG=2026.7.0`).
- **D-13:** Leave ALL historical record **verbatim**: "v4.0 shipped …" notes, `since vX` code comments, changelog/migration mentions, past-release references. **Rule:** if the string instructs the *next* release, update it; if it records a *past* event, leave it. (Consistent with VER-04 "historical record intact.")

### Claude's Discretion
- The exact CI trigger glob pattern (must match `2026.7.0`, reject branch/noise refs) — planner/researcher pins it; D-02 fixes the policy (CalVer-only), not the regex.
- The precise wording/columns of the MILESTONES.md mapping table (D-09 fixes intent).
- Whether the `pyproject.toml` `version` is bumped to `2026.7.0` within this phase vs at milestone-release time — planner to sequence against the release procedure (this phase *adopts* the scheme; the actual annotated tag is a milestone-completion step).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & roadmap
- `.planning/ROADMAP.md` §"Phase 65: CalVer Adoption" (lines ~929-942) — goal, success criteria, notes (retro-tagging out of scope; badge one-line rule).
- `.planning/REQUIREMENTS.md` — VER-01, VER-02, VER-03, VER-04 (the four locked requirements) + traceability table.
- `.planning/MILESTONES.md` — the milestone↔version mapping surface to update (VER-04).

### Release procedure (the invariant to preserve)
- Memory `project-release-procedure` (`~/.claude/projects/-Users-Robert-Code-public-phaze/memory/project_release_procedure.md`) — annotated-tag-only releases (no GH Releases); two-place version bump (pyproject + `uv.lock` via `uv lock`); `release/vX.Y.Z` PR flow; the tag-triggered publish; the `git push --delete`/recreate GOTCHA; the 3-part-segment requirement.

### CI / publish machinery (files to edit)
- `.github/workflows/ci.yml` — `on: push: tags: ["v*.*.*"]` (line ~13) → CalVer glob (D-02); `detect-changes` tag-force branch (`REF_TYPE == "tag"`, lines ~50-59) (D-03).
- `.github/workflows/docker-publish.yml` — `docker/metadata-action` tag strategy (lines ~104-115: `type=raw latest`, `type=semver {{version}}` / `{{major}}.{{minor}}`, `type=ref,event=tag`) (D-05/D-06).
- `tests/test_deployment/test_agent_compose.py::test_docker_publish_workflow_tags_both_latest_and_version` — guard test asserting `:latest` + `:v<version>`; retarget to CalVer form (D-06).

### Version strings & docs (forward-looking edits only — D-12/D-13)
- `pyproject.toml` line 7 — `version = "7.0.0"` (the going-forward version source).
- `README.md` line 12 — the one-line badge row (CI/codecov/license/python badges); line 39 & others reference "v7.0" as milestone/feature labels (historical — leave per D-13).
- `docs/deployment.md` — release/publish section (~lines 330-415): GHCR publish description, `PHAZE_IMAGE_TAG=v4.0.0` pinning + rollback examples (~262, 370, 402, 415, 479), and the "3-part semver `vX.Y.Z`" note (~349-351). Rewrite forward-looking examples; the 3-part-tag rationale text needs updating for the CalVer glob.
- `docs/arm64-agent-image.md`, `docs/configuration.md` — also surfaced release/tag mentions; scope-check against D-12/D-13.
- Compose files (`docker-compose.agent.yml`, `docker-compose.cloud-agent.yml`) use `${PHAZE_IMAGE_TAG:-latest}` — variable-driven, no literal version; likely no change, verify.

### External
- https://calver.org/ — the CalVer scheme reference (`YYYY.MM.MICRO` family).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `docker/metadata-action` `type=semver` already parses arbitrary semver-shaped refs — CalVer `2026.7.0` flows through `{{version}}`/`{{major}}.{{minor}}` with no new action config; only the git-tag *trigger* and the guard *test* change.
- The release-procedure is already codified in memory + `docs/deployment.md`; this phase edits those two authoritative surfaces rather than inventing a procedure.

### Established Patterns
- `PHAZE_IMAGE_TAG` env var indirection in all agent/cloud compose files means image pinning is version-string-agnostic — deploy refs update by *example text*, not by literal edits to compose (D-12).
- CI already distinguishes "tag ref → full pipeline" via `detect-changes` `REF_TYPE`; the CalVer change is confined to the `on: push: tags:` glob + the metadata guard test.
- A structural guard test already pins the tag strategy — the pattern is "assert the published tag set," so the CalVer migration is a test-retarget, not a test-deletion (D-06).

### Integration Points
- `ci.yml` `on: push: tags:` glob is the single gate deciding whether a tag ref runs CI at all → the highest-consequence edit (a wrong glob = silent no-publish, exactly the v4.0.2/v7.0 GOTCHA class).
- `pyproject.toml` `version` ↔ `uv.lock` `phaze` entry must move together (`uv lock`, never hand-edit uv.lock).

</code_context>

<specifics>
## Specific Ideas

- First CalVer tag is exactly `2026.7.0` (this milestone's release).
- Month segment has **no leading zero** (`2026.7.0`, not `2026.07.0`).
- REVISION starts at `0` and resets per calendar month.
- Preserve the "annotated tag PUSH triggers GHCR publish" invariant and the delete-recreate recovery recipe under the new scheme.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope. (Retroactive re-tagging of historical `vN.M` releases is explicitly out of scope per ROADMAP notes, not a deferral to a future phase.)

</deferred>

---

*Phase: 65-CalVer Adoption*
*Context gathered: 2026-07-02*
