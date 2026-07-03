# Phase 65: CalVer Adoption - Pattern Map

**Mapped:** 2026-07-02
**Files analyzed:** 9 (2 new tests + 7 edited files)
**Analogs found:** 6 / 9 (the 3 misses are pure-prose doc edits — no meaningful analog, by design)

> This is a CI/docs/versioning phase — almost entirely EDITS to existing files plus TWO new structural pytest tests added to an existing module. The load-bearing value of this map is analog #1 (the workflow-structural test pattern) — the new tests MUST match it exactly. Everything else is edit-anchor identification.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `tests/agents/deployment/test_agent_compose.py` — NEW `test_milestones_mapping_table_intact` (VER-04) | test (structural) | file-I/O (parse Markdown) | sibling `test_cleanup_package_list_matches_published_images` (set-equality structural guard) + `_load_ci_workflow_triggers` pattern | exact (same file) |
| `tests/agents/deployment/test_agent_compose.py` — NEW `test_calver_scheme_documented` (VER-01) | test (structural) | file-I/O (grep doc strings) | sibling `test_ci_workflow_triggers_on_version_tags` (path-const + `.read_text()` + `any(... in ...)` assertion) | exact (same file) |
| `tests/agents/deployment/test_agent_compose.py` — RETARGET `test_ci_workflow_triggers_on_version_tags` (VER-02) | test (structural) | ITSELF (line 332 in-place edit) | exact (in-place) |
| `tests/agents/deployment/test_agent_compose.py` — docstring-only `test_docker_publish_workflow_tags_both_latest_and_version` (VER-03) | test (structural) | ITSELF (docstring lines 174-185) | exact (in-place) |
| `.github/workflows/ci.yml` — `on: push: tags:` glob (VER-02) | config (CI) | request-response (trigger gate) | line 13 in-place | exact (in-place) |
| `MILESTONES.md` — add Milestone\|Version\|Date table (VER-04) | docs | transform (mapping table) | `.planning/RETROSPECTIVE.md` lines 231-236 (Milestone\|... Markdown table) | role-match |
| `pyproject.toml` — `version` bump (VER-02) | config | — | line 7 in-place | exact (in-place) |
| `docs/deployment.md` — forward-looking pin/rollback examples (VER-01/02/03) | docs (prose) | — | none (see No Analog) | none |
| `README.md`, `docs/configuration.md`, `docs/arm64-agent-image.md`, `docs/cloud-burst.md` — example edits | docs (prose) | — | none (see No Analog) | none |

---

## Pattern Assignments

### `tests/agents/deployment/test_agent_compose.py` — NEW + RETARGETED tests (test, structural)

**Analog:** the sibling tests in the SAME file. This is the single most important section — the two new tests MUST be byte-for-byte consistent with the established structural-guard idiom. Do NOT invent a new style.

**Module-level path constants + parse idiom (lines 32-46) — reuse verbatim, DO NOT re-derive:**
```python
from pathlib import Path
import re
from typing import Any

import yaml

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.agent.yml"
PUBLISH_WORKFLOW_PATH = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "docker-publish.yml"
CI_WORKFLOW_PATH = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"
CLEANUP_WORKFLOW_PATH = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "cleanup-images.yml"
```
- Path constant idiom is `Path(__file__).resolve().parents[3] / "<repo-relative>"`. `parents[3]` = repo root from `tests/agents/deployment/`. The NEW `test_milestones_mapping_table_intact` and `test_calver_scheme_documented` must add their own module-level `MILESTONES_PATH` / `DEPLOYMENT_DOC_PATH` constants in the same style (e.g. `Path(__file__).resolve().parents[3] / ".planning" / "MILESTONES.md"` — note `MILESTONES.md` lives at repo-root `.planning/`, NOT the phase dir; and `docs/deployment.md`).
- Parsing rule: **structural workflow/YAML → `yaml.safe_load(PATH.read_text())`; Markdown/doc-string → plain `PATH.read_text()` + `in`/`any(...)` substring assertions.** The research §Don't-Hand-Roll is explicit: do NOT re-implement GHA glob/`fnmatch` semantics — assert the literal string is present.

**RETARGET `test_ci_workflow_triggers_on_version_tags` (VER-02, D-02) — the critical edit at line 332.** Current body (lines 325-339):
```python
    assert CI_WORKFLOW_PATH.exists(), f"ci.yml missing at {CI_WORKFLOW_PATH}"
    data = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    triggers = _load_ci_workflow_triggers(data)   # <-- reuse existing helper (handles `on:`→bool True)
    push = triggers.get("push")
    assert isinstance(push, dict), f"ci.yml `on.push` must be a mapping; got {push!r}"

    tags = push.get("tags")
    assert isinstance(tags, list) and any("v*.*.*" in str(t) for t in tags), (   # <-- LINE 332: change glob
        f'ci.yml must trigger on 3-part semver tags. ... got tags={tags!r}'
    )

    branches = push.get("branches")   # <-- lines 336-339: KEEP UNCHANGED (branch-CI preservation)
    assert isinstance(branches, list) and branches, (...)
```
Retarget to the CalVer glob AND add the CalVer-only negative assertion (research §Code Examples, illustrative):
```python
    CALVER_GLOB = "[0-9]+.[0-9]+.[0-9]+"
    assert isinstance(tags, list) and any(CALVER_GLOB in str(t) for t in tags), (
        f'ci.yml must trigger on the bare CalVer glob {CALVER_GLOB!r}; got tags={tags!r}'
    )
    assert not any("v*.*.*" in str(t) for t in tags), "legacy v*.*.* glob must be dropped (D-02: CalVer-only)"
```
- **Reuse `_load_ci_workflow_triggers` (lines 292-301) verbatim** — it already handles PyYAML parsing bare `on:` as boolean `True` (`data.get("on", data.get(True))`). Do not re-solve that.
- **Keep the branch-preservation block (lines 336-339) unchanged** — the glob edit must not silently drop branch CI.
- Update the docstring (lines 316-333) from "3-part semver tag" / `PHAZE_IMAGE_TAG=vX.Y.Z` wording to bare CalVer wording.

**NEW `test_milestones_mapping_table_intact` (VER-04) — model on `test_cleanup_package_list_matches_published_images` (lines 398-428)** for the structural-set idiom, but read Markdown instead of YAML:
```python
# Analog structural-guard shape (set-equality / membership over a parsed source):
def test_cleanup_package_list_matches_published_images() -> None:
    assert PUBLISH_WORKFLOW_PATH.exists(), ...
    publish = yaml.safe_load(PUBLISH_WORKFLOW_PATH.read_text())
    ...
    published_packages = {("phaze" + entry["image_suffix"]).rstrip("/") for entry in matrix_include}
    ...
    only_published = published_packages - cleanup_packages   # <-- symmetric-difference diagnostic in the msg
    assert published_packages == cleanup_packages, ("...divergence report...")
```
For the new test: `text = MILESTONES_PATH.read_text()`, then assert (a) a header row with `| Milestone | Version | Date |` (or the equivalent columns the planner chooses) is present, and (b) each historical row string `v1.0` … `v7.0` appears verbatim (D-10), and (c) the `2026.7.0` row is present (D-01/D-11). Mirror the analog's habit of a **rich failure message naming exactly what diverged** (the set-difference style). Assert substring membership, not exact table formatting (be robust to whitespace, like the `.strip()` normalizers elsewhere in the file).

**NEW `test_calver_scheme_documented` (VER-01) — model on the path-const + `.read_text()` + `any(...)` idiom used by `test_ci_workflow_triggers_on_version_tags`:**
```python
# Idiom to copy: exists-guard, read, substring assert with a fix-instruction message.
    assert DOC_PATH.exists(), f"... missing at {DOC_PATH}"
    text = DOC_PATH.read_text()
    assert "YYYY.MM.REVISION" in text and "2026.7.0" in text, (
        "docs/deployment.md (or MILESTONES.md) must document the CalVer scheme + first tag; ..."
    )
```
Assert the CalVer scheme string (`YYYY.MM.REVISION`), the first tag (`2026.7.0`), and the per-month zero-based REVISION convention prose are present in `docs/deployment.md` and/or `MILESTONES.md` (VER-01 is otherwise doc-review-only).

**`test_docker_publish_workflow_tags_both_latest_and_version` (VER-03, D-06) — DOCSTRING ONLY.** The functional assertion (lines 196-197) checks `type=semver` / `type=ref,event=tag` presence and **never tests a literal `v`** — it passes unchanged under CalVer. Update only the docstring (lines 174-185: `:v<version>` → `:<version>`). Do NOT weaken the assertion (D-06: "retarget, don't weaken").

**Helper reuse map (do not duplicate):** `_load_ci_workflow_triggers` (292), `_ci_detect_changes_filter_step` (304), `_extract_api_metadata_action_step` (140), `_metadata_action_tag_lines` (167), `_env_to_strs` (49). NO-CHANGE tests that already prove locked decisions: `test_ci_detect_changes_forces_code_changed_on_tags` (342, proves D-03), `test_docker_publish_arm64_job_tags_latest_and_version` (234), `test_ci_detect_changes_survives_force_push` (372).

---

### `.github/workflows/ci.yml` — tag-glob edit (config, CI trigger gate)

**Analog:** in-place. This is the single highest-consequence edit (wrong/unquoted glob = silent no-publish, the v4.0.2/v7.0 GOTCHA class).

**Current anchor (lines 4-15):**
```yaml
on:
  push:
    # Only main + release tags. ...  publishing now only
    # happens on main and v*.*.* tags.          # <-- lines 6-11 comment references "v*.*.*" — update wording
    branches: ["main"]
    tags: ["v*.*.*"]                            # <-- LINE 13: the edit
  pull_request:
    branches: ["**"]
```
**Change (research-pinned, D-02):** `tags: ["v*.*.*"]` → `tags: ["[0-9]+.[0-9]+.[0-9]+"]` (quoted — a YAML value starting with `[` must be quoted or it parses as a flow sequence). Also update the lines 6-11 comment that says "v\*.\*.\* tags". **LEAVE** the `detect-changes` `REF_TYPE == "tag"` force branch (lines 55-61) — verified glob-agnostic (D-03).

---

### `MILESTONES.md` — add Milestone|Version|Date mapping table (docs, transform)

**Analog:** `.planning/RETROSPECTIVE.md` lines 231-236 (the closest existing `| Milestone | … |` Markdown table in the repo). MILESTONES.md itself currently has ZERO Markdown tables (verified `grep -c "^|"` = 0) — it is chronological `## vN.M Name (Shipped: date)` detail sections only.

**RETROSPECTIVE.md table idiom to mirror (header + separator + `vN.M`-keyed rows):**
```markdown
| Milestone | Phases | Plans | Key Change |
|-----------|--------|-------|------------|
| v1.0 | 11 | 24 | Established GSD workflow, ... |
| v2.0 | 6 | 16 | Research phases before planning, ... |
```

**Existing MILESTONES.md section headers (source-of-truth dates for the table rows):**
```
## v7.0 UI Redesign — DAG-Centric Hybrid Console (Shipped: 2026-07-02)
## v6.0 Kubernetes Burst Analysis (Shipped: 2026-06-29)
## v5.0 Cloud Burst Analysis (Shipped: 2026-06-26)
## v4.0 Distributed Agents (Shipped: 2026-05-17)
## v3.0 Cross-Service Intelligence & File Enrichment (Shipped: 2026-04-04)
## v2.0 Metadata Enrichment & Tracklist Integration (Shipped: 2026-04-02)
## v1.0 MVP (Shipped: 2026-03-30)
```
**Add** (research §Mapping Table) a `## Milestone ↔ Version Mapping` table near the top, BEFORE the first detail section. Historical rows keep `vN.M` verbatim (D-10); add the `Engineering Improvements | 2026.7.0 | _(release date TBD)_` row (D-01/D-11). Leave all existing `## vN.M …` detail sections verbatim (D-13). Note: RESEARCH lists the v4.0 date as `2026-05-17`; the file header says `2026-05-17` — cross-check the RESEARCH seed table (it says `2026-05-17`… actually shows `2026-05-17`) against these headers when seeding.

---

### `pyproject.toml` — version bump (config)

**Analog:** in-place, line 7: `version = "7.0.0"` → `2026.7.0`. **After editing, run `uv lock`** to sync the `phaze` entry in `uv.lock` — NEVER hand-edit `uv.lock` (research Pitfall 4). SEQUENCING is Claude's Discretion (bump-in-phase vs at-release); RESEARCH Open-Q2 recommends bump-in-phase so guard tests exercise the final state.

---

### `docs/deployment.md` — forward-looking pin/rollback examples (docs, prose)

**No code analog** — pure prose edits classified per D-12 (forward → rewrite) / D-13 (historical → leave). Grep-anchored edit targets (line numbers approximate per RESEARCH A1 — anchor on the STRINGS):
```
262: - `PHAZE_IMAGE_TAG=v4.0.0` (or `latest` for first-time setup)      → 2026.7.0
348: ... Tagged releases therefore produce **both** `:latest` and `:v<version>`.  → :<version>
349: Release tags MUST be 3-part semver (`vX.Y.Z`, e.g. `v4.0.0`) — ... on `push` of a `v*.*.*` tag ...  → bare 3-part CalVer YYYY.MM.REVISION, matched by CalVer glob; KEEP the 3-part rationale
403: #    PHAZE_IMAGE_TAG=v4.0.0                                          → 2026.7.0
409: Because `docker-publish.yml` tags both `:latest` and `:v<version>` ...  → :<version>
415: git checkout v4.0.0   # the last-known-good release tag              → illustrative → 2026.7.0 (mechanism unchanged; rolling back to a pre-CalVer release legitimately still uses v4.0.0)
479: PHAZE_IMAGE_TAG=v4.0.0                                              → 2026.7.0
482: ... tags both `:latest` and `:v<version>` ... MUST be a 3-part `vX.Y.Z` ... only publishes on `push` of a `v*.*.*` tag.  → CalVer
```
Rule (D-13): if the string instructs the *next* release → rewrite; if it records a *past* event → leave.

---

## Shared Patterns

### Structural-guard test idiom (cross-cutting — applies to ALL test edits in this phase)
**Source:** `tests/agents/deployment/test_agent_compose.py` (whole file)
**Apply to:** both new tests + both retargeted/docstring tests
- Module-level `*_PATH = Path(__file__).resolve().parents[3] / ...` constants.
- Per test: `assert PATH.exists()` guard first, then parse (`yaml.safe_load` for YAML, `.read_text()` for Markdown/prose), then assert with a **fix-instruction failure message** (every assert in the file ends with a message telling the maintainer exactly what to restore and where).
- Assert LITERAL strings / substring membership; never re-implement GHA glob or `fnmatch` semantics in Python (research §Don't-Hand-Roll).
- Reuse existing helpers; do not duplicate parse logic.

### CalVer glob string (cross-cutting constant)
**Source:** RESEARCH §Code Examples (docs-cited GHA filter syntax)
**Apply to:** `ci.yml` line 13 AND the retargeted `test_ci_workflow_triggers_on_version_tags`
- Canonical value: `[0-9]+.[0-9]+.[0-9]+` (quoted in YAML). `.` is a literal dot in GHA globs (not regex). The same literal string is what the guard test asserts is present — keep them identical.

---

## No Analog Found

Pure-prose forward-looking doc/comment edits — the planner should apply D-12/D-13 classification directly, NOT fabricate a code analog:

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `docs/deployment.md` (pin/rollback/tag-strategy prose) | docs | — | Prose rewrite; edit-anchors listed above are the spec, no pattern to copy |
| `README.md` line 12 badge row | docs | — | NO version badge exists (RESEARCH Open-Q1) — VER-02 README clause likely satisfied by deployment.md prose; keep badges one-line; do NOT re-add a removed badge |
| `docs/configuration.md` (291/350), `docs/arm64-agent-image.md` (128-129), `docs/cloud-burst.md` (215/250) | docs | — | Forward-looking `v4.0.0`/`v5.0.0` example strings → `2026.7.0`; low-priority, scope-check with planner (D-12) |
| `docker-compose.agent.yml` (comment 27), `docker-compose.cloud-agent.yml` (comment 35) | config | — | Comment-only example edits; `image:` lines use `${PHAZE_IMAGE_TAG:-latest}` indirection → LEAVE (guard `test_all_agent_services_pull_from_ghcr` asserts the TOKEN, unaffected) |

**Memory-update note (not a repo file — surface to orchestrator, plan cannot edit it):** `project_release_procedure.md` needs updating to bare CalVer tags, the new glob, first tag `2026.7.0`, per-month zero-based REVISION, and `release/2026.7.0` branch name. The annotated-tag + `git push --delete`/recreate GOTCHA carry over verbatim (D-04).

## Metadata

**Analog search scope:** `tests/agents/deployment/`, `.github/workflows/`, `.planning/` (MILESTONES.md, ROADMAP.md, RETROSPECTIVE.md), `docs/`, `pyproject.toml`, `README.md`, compose files
**Files scanned:** ~14 (test file read in full; ci.yml head; MILESTONES.md head + headers; RETROSPECTIVE table; grep-anchored doc/pyproject/README)
**Pattern extraction date:** 2026-07-02
</content>
</invoke>
