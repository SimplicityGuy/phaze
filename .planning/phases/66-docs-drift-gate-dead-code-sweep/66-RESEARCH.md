# Phase 66: Docs-Drift Gate & Dead-Code Sweep - Research

**Researched:** 2026-07-03
**Domain:** CI/test-tooling (pytest structural guards), FastAPI/Jinja presentation, Python dead-code analysis (vulture)
**Confidence:** HIGH (all findings verified against on-disk artifacts and existing code in this repo)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** A phase counts as "passed" only when **both** signals agree: the `- [x]` checkbox on its `**Phase NN...**` line in `ROADMAP.md` **AND** its `NN-VERIFICATION.md` exists with status/verdict = `passed`. ROADMAP `[x]` alone is not sufficient.
- **D-02:** The cross-check is **bidirectional**. Fail if a passed phase has any mapped requirement not marked Complete, **and** fail if a requirement is marked Complete without its mapped phase being passed.
- **D-03:** `REQUIREMENTS.md` encodes status twice — the per-requirement `- [ ]/[x]` checkbox **and** the Traceability-table `Status` column. The gate requires **both to agree** with each other and with phase-pass. A drifted checkbox-vs-table is itself a failure.
- **D-04:** Scope = **active milestone + archived milestones**. ⚠ Research flag — archived milestones predate the gsd-verifier; VERIFICATION artifacts may be absent. The gate must degrade gracefully: archived pairs validated for **internal consistency** (checkbox↔table↔all-marked-Complete), NOT failing on missing legacy VERIFICATION files; active milestone gets full ROADMAP+VERIFICATION agreement.
- **D-05:** In-flight phases are tolerated: a not-yet-passed phase with unmarked requirements must PASS. Only genuine drift fails. (Phase 66 itself is `[ ]` with its reqs `[ ]` → PASS.)
- **D-06:** Form = a **pytest guard** in the shared bucket, following `tests/shared/core/test_docs_ia_current.py` / `test_dead_template_guard.py` (suggested name `test_requirements_traceability.py`). No new standalone-script tooling.
- **D-07:** The gate **must run on doc-only PRs**. Needs a path around CI-04's doc-only skip while preserving skip-with-success for the heavy jobs.
- **D-08:** On drift, emit **precise, actionable** messages naming the exact offender.
- **D-09:** Render the /saq link **only when `settings.enable_saq_ui` is true**. `admin_agents.py` must add `enable_saq_ui` to the `admin/agents.html` context — presentation-only, NOT a backend change.
- **D-10:** Placement = a **discreet footer/utility link** on the Agents/Compute page, low visual weight.
- **D-11:** Opens in a **new tab** — `target="_blank" rel="noopener"`.
- **D-12:** Scope = **full-repo dead-code hunt** across `src/phaze/`. Guardrail: only **confirmed-dead** code removed — verified against dynamic references and a green test suite.
- **D-13:** Method = **vulture-assisted + manual verify**. vulture is not installed — add as a dev dependency (cooldown-safe). Plan for a whitelist/ignore list for framework-invoked code.

### Claude's Discretion
- **D-14 (blind spot mechanism — "you decide"):** Close the dead-template guard's blind spot for its own unused entry-root literals. Leaning: add an assertion that every router-captured `"...html"` entry literal that lives under `templates/` resolves to an on-disk template. Required outcome: a dead entry-root literal must fail the guard rather than mask an orphan.

### Deferred Ideas (OUT OF SCOPE)
- Rewriting coverage/CI tooling or a full monorepo split.
- Multi-cloud backends (MCB-01..) — next named milestone (phase 67+).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| DOCS-01 | CI gate cross-checking REQUIREMENTS.md traceability against passed phases; fails when stale | §Deep-Dive 1 (parser + degradation rule), §Deep-Dive 2 (CI wiring). On-disk layout verified. |
| CLEAN-01 | Restore discreet in-UI `/saq` link on Agents page, presentation-only | §CLEAN-01. Live page + settings attribute + gate mechanism all confirmed in-repo. |
| CLEAN-02 | Full-repo dead-code sweep (vulture) + close dead-template guard blind spot | §Deep-Dive 3. vulture version verified; guard blind-spot fix de-risked (all 67 literals resolve today). |
</phase_requirements>

## Summary

This is a maintainer-facing cleanup phase with a hard **no backend / no product behavior change** constraint. All three deliverables are test/CI/tooling additions or a presentation-only template edit. Nothing touches runtime pipeline behavior.

The three requirements decompose cleanly into three independent workstreams that can be planned as separate waves (or a single wave with three plans), with no ordering dependency between them:

1. **DOCS-01** — a new filesystem pytest guard `tests/shared/core/test_requirements_traceability.py` that parses `.planning/` markdown and asserts the requirements/roadmap/VERIFICATION triad stays in sync. It follows the established repo idiom exactly (`_REPO_ROOT = Path(__file__).resolve().parents[3]`, pure-`read_text` parse-then-assert, precise failure messages, zero DB/app imports → hermetic). The one genuinely open design question — how the "both-must-agree" rule degrades for archived milestones — is answered by the on-disk reality: **v7.0 has zero phase dirs and therefore zero VERIFICATION artifacts, and even v4.0 has spotty VERIFICATION coverage (phases 39–42 have none).** The only defensible rule is: **active milestone = full ROADMAP-[x]+VERIFICATION-passed gating; archived milestones = internal-consistency-only.**

2. **CLEAN-01** — add `enable_saq_ui` to the `admin/agents.html` render context in `routers/admin_agents.py` and a small muted `{% if enable_saq_ui %}`-guarded footer link. The shell's Agents page (rail + header both link `/admin/agents`) IS `admin/agents.html` via the `page` handler — confirmed. `enable_saq_ui` lives on `BaseSettings` so it is available on both role settings.

3. **CLEAN-02** — add `vulture>=2.16` (published 2026-03-25, cooldown-safe) to the dev group, run a one-shot manual-verify sweep over `src/phaze`, and add a new assertion to `test_dead_template_guard.py` that every router `"...html"` literal resolves to an on-disk template. **This last fix is fully de-risked: all 67 current router `.html` literals already resolve, so the assertion goes green today with no allowlist.**

**Primary recommendation:** Implement DOCS-01 as a hermetic filesystem guard in the `shared` bucket, wire it into the always-run `code-quality.yml` job via a `just docs-drift` recipe (this is the clean path around CI-04's skip), apply the active-vs-archived degradation rule below, and ship CLEAN-01/CLEAN-02 as the small presentation + tooling edits they are.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Docs-drift detection (DOCS-01) | CI / Test tooling | — | A pytest structural guard over `.planning/` markdown; no runtime tier involved. |
| Docs-drift "must run on doc-only PR" (D-07) | CI orchestration (`code-quality.yml`) | Test bucket (`shared`) | The always-run quality job is the only job that survives CI-04's doc-only skip. |
| /saq link (CLEAN-01) | Frontend Server (Jinja template + router context) | Config (settings flag) | Presentation-only; the link is a template concern gated by an existing settings flag. |
| Dead-code sweep (CLEAN-02) | Dev tooling (vulture, one-shot) | Source (`src/phaze/`) | Analysis tool + manual deletion; not wired as a blocking gate. |
| Dead-template guard fix (D-14) | CI / Test tooling | — | Extends an existing pytest AST/filesystem guard. |

## Standard Stack

### Core (already present — no new runtime deps)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pytest | >=9.1.1 (installed) | Guard test host | All repo guards are pytest tests. [VERIFIED: pyproject.toml] |
| jinja2 | 3.1.x (installed) | `meta.find_referenced_templates` for the dead-template guard | Already used by `test_dead_template_guard.py`. [VERIFIED: test source] |
| just | 4.x (CI + local) | Command runner; workflows delegate to it | Project convention (CLAUDE.md / MEMORY). [VERIFIED: justfile + workflows] |

### Supporting (one new dev dependency)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| vulture | >=2.16 | Dead-code candidate finder for the CLEAN-02 sweep | One-shot + optional `just vulture` recipe; NOT a blocking gate. [VERIFIED: PyPI] |

**Installation (dev group only):**
```toml
# pyproject.toml [dependency-groups] dev — append after "ruff>=0.15.18" (list is alpha-sorted; vulture sorts last)
"vulture>=2.16",
```
Then `uv lock && uv sync` (via `just install`).

**Version verification:** vulture **2.16** released **2026-03-25** (≈3 months before today's 2026-07-03), so it clears the relative `exclude-newer = "7 days"` cooldown and resolves cleanly. Requires Python >=3.9, supports 3.14. [VERIFIED: PyPI / vulture releases] Floor `>=2.16` is safest (matches the current release); `>=2.14` would also resolve and gives more headroom — either is cooldown-safe.

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| vulture | `ruff` unused-import rules (F401) | ruff already runs but only catches unused imports/locals, not unused module-level functions/classes/attrs. vulture is the right tool for whole-symbol dead code. Keep both. |
| vulture as blocking CI gate | one-shot sweep + optional recipe | Framework false-positives make a blocking gate high-noise; D-12/D-13 explicitly call for manual verify. One-shot is correct. |

## Package Legitimacy Audit

> One new external package: `vulture`. slopcheck was not run in this session (offline sandbox); vulture is a decade-old, widely-used tool with a canonical source repo, verified below via PyPI + GitHub.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| vulture | PyPI | 2.16 released 2026-03-25; project since 2012 | ~millions/mo (established) | github.com/jendrikseipp/vulture | not-run (offline) | Approved — dev-only, cooldown-safe, canonical repo |

**Packages removed due to slopcheck [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** none

*slopcheck was unavailable at research time. Per protocol, the planner should gate the vulture install behind a `checkpoint:human-verify` task OR the installer should confirm `github.com/jendrikseipp/vulture` is the resolved source before `uv sync`. vulture is a long-established, high-reputation package (author is a Python-community maintainer), so the residual risk is low; the cooldown window already excludes any freshly-published typosquat.*

## Deep-Dive 1 — DOCS-01 parser + archived-milestone degradation (D-01..D-05)

### On-disk layout (VERIFIED this session)

**Active milestone (2026.7.0):**
- Requirements: `.planning/REQUIREMENTS.md` — Traceability table + per-req checkboxes.
- Roadmap: `.planning/ROADMAP.md` — phase-pass `- [x] **Phase NN: ...**` lines.
- VERIFICATION: `.planning/phases/{NN-name}/{NN}-VERIFICATION.md` — exists for 63, 64, 65. Phase 66 has none yet (in-flight).

**Archived milestones:**
- Requirements/roadmap: `.planning/milestones/vN.M-REQUIREMENTS.md` / `vN.M-ROADMAP.md` (v1.0–v7.0 all present).
- Phase dirs: `.planning/milestones/vN.M-phases/{NN-name}/` — **but only for v1.0–v6.0. v7.0 has NO `v7.0-phases/` directory at all** (only REQUIREMENTS/ROADMAP/MILESTONE-AUDIT). [VERIFIED: `find`]
- VERIFICATION presence is **spotty even where phase dirs exist:**
  - Inconsistent filenames: most are `{NN}-VERIFICATION.md`, but phase 31 and 46 are bare `VERIFICATION.md`, and phase 48 has BOTH.
  - **Phases 39–42 (v4.0) have NO VERIFICATION file** (shipped via PR, "Executed" status).
  - v7.0 phases (57–62): **zero VERIFICATION files on disk.**

**Conclusion (answers the D-04 research flag):** Applying the D-01 "ROADMAP-[x] AND VERIFICATION-passed" rule to archived milestones is **impossible** — v7.0 would fail every requirement (no VERIFICATION files), and v4.0 phases 39–42 would too. The only sound rule:

> **Active milestone** (`.planning/REQUIREMENTS.md` + `.planning/ROADMAP.md` + `.planning/phases/`): full D-01 gating — phase passed ⟺ ROADMAP `[x]` **AND** `{NN}-VERIFICATION.md` frontmatter `status: passed`.
>
> **Archived milestones** (`.planning/milestones/vN.M-*`): **internal-consistency-only** — for each requirement, its checkbox state, its Traceability `Status` cell, and (for a "done" requirement) the presence of a passed-marker must agree with each other. Do NOT require VERIFICATION files. Since every archived milestone shipped, the effective archived assertion is: *every non-deferred requirement is checkbox-`[x]` AND table-`Complete/Done`, and every deferred requirement is checkbox-`[ ]` AND table-`Deferred`.* Archived ROADMAP `[x]` may optionally be cross-checked but MUST NOT depend on VERIFICATION files.

### Parsing specifics (all formats VERIFIED against on-disk files)

**Requirement checkbox** — consistent across ALL milestones:
```
- [x] **DOCS-01**: A CI gate cross-checks ...
```
Regex: `^- \[([ x])\] \*\*([A-Z]+-\d+)\*\*`  → (checkbox_state, req_id)

**Traceability table row** — pipe table, format varies by milestone in two ways:
```
| DOCS-01 | Phase 66 | Pending |                                   # active
| DIST-01 | Phase 29 — Deployment Hardening & Agents Admin | Complete |  # v4.0 (phase name appended)
| SHELL-01 | Phase 57 — Shell & DAG rail | Done |                  # v7.0 (Status="Done")
| RECORD-05 | Future (deferred — v7.x) | Deferred |                # deferred (no phase number)
```
Regex: `^\|\s*([A-Z]+-\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|` → (req_id, phase_cell, status_cell)
- Extract phase number from `phase_cell` via `Phase (\d+(?:\.\d+)?)`. **No match ⟹ deferred/unmapped** (e.g. "Future (deferred…)") — such rows participate only in the deferred-consistency check, never phase-pass.

**Status vocabulary normalization** (REQUIRED — vocab differs across milestones):
- COMPLETE ← `{"complete", "done"}` (case-insensitive)
- DEFERRED ← `{"deferred"}`
- PENDING/OTHER ← `{"pending", "in progress", ...}`
Active uses Complete/Pending; v7.0 uses Done/Deferred; v1–v6 use Complete. Normalize before comparing.

**ROADMAP phase-pass line** — same shape in active and archived (archived lines live inside `<details>` blocks, which are plain HTML wrappers — the `- [x]` markdown lines inside parse identically):
```
- [x] **Phase 63: Parallel CI & Code-Change Gating** — ... (completed 2026-07-02)
- [ ] **Phase 66: Docs-Drift Gate & Dead-Code Sweep** — ...
- [x] **Phase 57.1: Incremental window persistence ...** — ...   # decimal phase (archived only)
```
Regex: `^- \[([ x])\] \*\*Phase (\d+(?:\.\d+)?)[:\s]` → (checkbox_state, phase_number). The `(?:\.\d+)?` handles the 57.1 decimal insert.

**VERIFICATION status** — active files carry YAML frontmatter AND a body line; prefer the frontmatter:
```
status: passed        # line ~4, YAML frontmatter — PRIMARY signal
**Status:** passed    # body — fallback
```
Read `.planning/phases/{dir starting with NN-}/*VERIFICATION*.md`; match `^status:\s*passed` in frontmatter (fallback `\*\*Status:\*\*\s*passed`). Missing file OR non-`passed` ⟹ phase not passed (active only).

### Drift conditions the gate must fail on (active milestone)

Given the requirement→phase mapping from the Traceability table and phase-pass from ROADMAP+VERIFICATION:

1. **Passed-but-unmarked (checkbox):** phase passed, but a mapped req's checkbox is `[ ]` → `Phase 65 passed but VER-03 checkbox [ ] unmarked`
2. **Passed-but-unmarked (table):** phase passed, but a mapped req's table Status ≠ Complete → `Phase 65 passed but DOCS table Status 'Pending' for VER-03`
3. **Marked-but-unpassed:** a req's checkbox `[x]` (or table Complete) but its mapped phase not passed → `DOCS-01 marked Complete but Phase 66 not passed`
4. **Checkbox↔table drift (D-03):** a req's checkbox state disagrees with its table Status → `table Status 'Complete' ≠ checkbox [ ] for VER-03`
5. **In-flight tolerance (D-05, must PASS):** phase not passed + req checkbox `[ ]` + table Pending → PASS. **This is exactly Phase 66's own state during this work** (Phase 66 `[ ]`; DOCS-01/CLEAN-01/CLEAN-02 all `[ ]` + Pending) — the guard MUST be green while Phase 66 is being built. Add an explicit test for this.

### Suggested guard structure (mirrors existing idiom)
```python
# tests/shared/core/test_requirements_traceability.py
from pathlib import Path
import re

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLANNING = _REPO_ROOT / ".planning"

# one assertion function per drift class, e.g.:
def test_active_passed_phases_have_all_requirements_marked() -> None: ...
def test_active_marked_requirements_have_passed_phases() -> None: ...
def test_active_checkbox_and_table_status_agree() -> None: ...
def test_archived_milestones_internally_consistent() -> None: ...
def test_inflight_phase_with_unmarked_requirements_passes() -> None: ...  # D-05 regression
```
Each failure message names the exact offender (D-08). Keep the parser as small pure helpers (`_parse_requirements`, `_parse_roadmap_phases`, `_verification_passed`) so the assertions read declaratively — same shape as `test_docs_ia_current.py`.

**Hermeticity:** the guard reads only files (no `phaze.*` imports, no DB, no settings) — so it is immune to the `get_settings` lru_cache leak / `saq_jobs` stub poison hermeticity gotcha and passes cleanly in isolation via `just test-bucket shared`. [VERIFIED: pattern matches `test_dead_template_guard.py`, which has the same zero-import property]

## Deep-Dive 2 — DOCS-01 CI wiring around CI-04 (D-06/D-07)

### The CI-04 skip contract (VERIFIED in `.github/workflows/ci.yml`)
- `detect-changes` job runs `just detect-code-changes` (→ `scripts/classify-changed-files.sh`); `.md`/`.planning/`/`docs/`/`LICENSE`/`.txt`-only diffs emit `code-changed=false`.
- `test`, `security`, `docker`, `docker-publish` jobs are **`if: needs.detect-changes.outputs.code-changed == 'true'`** → skipped on doc-only PRs.
- **`quality` job (`code-quality.yml`) has NO `if:` gate → it runs on EVERY PR, including doc-only.** [VERIFIED: ci.yml lines 94–95]
- `aggregate-results` requires `QUALITY_RESULT == success` unconditionally, and accepts `skipped` for the gated jobs on doc-only PRs (skip-with-success). The `aggregate-results` check is the repo's required merge gate (ruleset). [VERIFIED: ci.yml + MEMORY]

### Recommended wiring (the clean path)

**Add the guard invocation to the always-run `code-quality.yml` job**, via a new `just` recipe (workflows delegate to just — project convention):

```make
# justfile [group('test')]
[doc('Run the REQUIREMENTS/ROADMAP docs-drift traceability guard (DOCS-01)')]
docs-drift:
    uv run pytest tests/shared/core/test_requirements_traceability.py -q
```
```yaml
# .github/workflows/code-quality.yml — add ONE step after "Run pre-commit hooks"
      - name: 🧭 Docs-drift traceability gate
        run: just docs-drift
```
`code-quality.yml` already runs `just install` (uv sync), so pytest + the guard are available with no extra setup.

**Why this is correct:**
- The guard now runs on **doc-only PRs** (the exact moment drift is introduced) because `quality` is never skipped.
- It does **NOT** re-enable the heavy `test`/`security`/`docker` jobs on doc-only PRs → CI-04's skip-with-success is preserved untouched.
- Because the guard ALSO lives in the `shared` test bucket (D-06), code PRs exercise it a second time in the `test` job — redundant but harmless, and it means the guard is covered by the normal bucket run for coverage purposes.

**Alternative (not recommended):** adding `.planning/**` to a trigger path to force the heavy jobs to run on doc-only PRs — this would break the CI-04 skip-with-success contract and re-introduce full test/security/docker runs on doc edits. Rejected. The always-run-quality-job path is strictly better.

### Bucket placement (D-06)
Put the test file at `tests/shared/core/test_requirements_traceability.py`. The `shared` bucket is one of the 9 in `tests/buckets.json`. [VERIFIED] Confirm it passes in isolation:
```bash
just test-bucket shared
```
Because the guard imports nothing from `phaze` and touches no DB fixture, `conftest.py` will NOT auto-mark it `integration`, and it cannot be poisoned by the `get_settings` lru_cache / `saq_jobs` stub cross-test leaks (those only affect tests that import app settings/queues). [VERIFIED: conftest auto-mark logic keys off DB_FIXTURES fixturenames + path parts]

### Optional: extend the CI-wiring guard
`tests/shared/test_ci_workflow_wiring.py` already structurally asserts parts of `tests.yml`. Optionally add a small assertion there (or in the new guard) that `code-quality.yml` contains a `just docs-drift` step, so the DOCS-01 wiring itself can't silently drift out. [Discretionary — not required by CONTEXT.]

## Deep-Dive 3 — CLEAN-02 vulture sweep + guard blind-spot fix (D-12/D-13/D-14)

### vulture setup (D-13)
- Add `vulture>=2.16` to `[dependency-groups] dev` (alpha-sorted → after `ruff`). Cooldown-safe (see Standard Stack). [VERIFIED]
- **Whitelist approach — recommended:** generate and commit a `vulture_whitelist.py` at repo root via `uv run vulture src/phaze --make-whitelist > vulture_whitelist.py`, hand-audit it (remove any genuinely-dead entries), then run sweeps as `uv run vulture src/phaze vulture_whitelist.py`. This is vulture's documented mechanism for framework false-positives and is greppable/reviewable in one file (superior to scattering `# noqa`-style comments through source, which pollutes runtime files for a dev-only tool). Supplement with `--ignore-decorators` for the decorator-driven frameworks:
  ```bash
  uv run vulture src/phaze vulture_whitelist.py \
    --min-confidence 80 \
    --ignore-decorators "@router.*,@app.*,@field_validator,@model_validator,@validator,@pytest.fixture"
  ```
- **`--min-confidence`:** vulture's default is 60. Recommend running the **manual sweep at 60** (catch everything, then hand-verify), but if a `just vulture` recipe is added for repeatability, set it to **80** to keep signal high. Do NOT wire vulture as a **blocking** CI gate — framework false-positives make it too noisy, and D-12/D-13 explicitly want manual verification. A non-blocking `just vulture` recipe is fine.

### Known vulture false-positive sources in THIS codebase (verified patterns)
| Pattern | Where | Handle via |
|---------|-------|-----------|
| FastAPI route handlers | every `@router.get/post/...` in `routers/*.py` | `--ignore-decorators "@router.*,@app.*"` |
| Pydantic validators | `config.py`, schema models | `--ignore-decorators "@field_validator,@model_validator,@validator"` |
| pytest fixtures (if src scanned) | n/a (scan `src/phaze` only) | `--ignore-decorators "@pytest.fixture"` |
| Transient ORM attrs | `admin_agents._load_agents` sets `a._status`; Phase 27 `_agent_name`, `_elapsed_seconds` | whitelist entries (`_status`, `_agent_name`, `_elapsed_seconds`) |
| SAQ hooks / lifespan callbacks | `main.py`, task settings | whitelist as needed |
| CLI/entry points | `phaze.entrypoint`, `agents-add` CLI | whitelist / `--ignore-names` |

### Likely real dead-code candidates to scope the sweep (from repo history)
- `src/phaze/utils/` — docstring notes "Future phases may add humanize-style helpers here" → check for unused helpers.
- Post-v7.0-cutover residue: the v7.0 shell superseded legacy pages; Phase 62 deleted 20 templates and drained the guard allowlist, but Python-side helpers that only fed deleted templates may linger (services like `build_dashboard_context`, `get_stage_progress`, `get_queue_activity` are noted as STILL LIVE — do NOT remove; they feed the Analyze workspace + `/pipeline/stats`). Verify each candidate against dynamic refs before deletion (D-12 guardrail).
- The `_STAGE_PLACEHOLDER` symptom itself was already removed (PR #191); only stale references in test docstrings remain — the sweep should confirm no live `_STAGE_PLACEHOLDER` code remains (it doesn't).

**Guardrail (D-12):** delete only after (1) `grep -rn` for dynamic references (string-based dispatch, getattr, template names, SAQ task names) and (2) a green `just test` / `just integration-test`. Nothing may alter runtime behavior.

### D-14 — dead-template guard blind-spot fix (DE-RISKED)

**The blind spot (VERIFIED in `test_dead_template_guard.py`):** `test_no_orphan_templates` builds `all_templates` from on-disk `templates/**/*.html`, builds `reachable` from router literals + their transitive closure, and asserts `all_templates - reachable == ∅`. A router literal that points at a **deleted** template is added to `reachable` but, because it isn't on disk, it contributes nothing to `all_templates` and is never flagged. So a dead entry-root literal (the `_STAGE_PLACEHOLDER` shape) is silently tolerated — the guard only detects orphaned *files*, never orphaned *literals*.

**The fix (CONTEXT leaning — confirmed sound):** add a NEW assertion — every router-captured `"...html"` literal must resolve to an on-disk template:
```python
def test_entry_literals_resolve_to_templates() -> None:
    """Every router "...html" literal points at a real template (no dead entry-root literal)."""
    missing = sorted(lit for lit in _entry_templates() if not (_TEMPLATES / lit).is_file())
    assert not missing, (
        "router source references template literals that don't exist on disk "
        f"(dead entry-root literal — delete the literal or restore the template): {missing}"
    )
```

**Why it's fully de-risked:** I enumerated all router `.html` literals this session — **67 literals, every one resolves to an on-disk template, zero non-resolving.** [VERIFIED: script over `routers/*.py` vs `templates/`] So the new assertion is **green today with no allowlist needed**, and if a `_STAGE_PLACEHOLDER`-shape dead literal is ever reintroduced it fails loudly and precisely. The existing docstring's caveat ("a stray non-template `.html` literal is harmless") is currently vacuous — there are no such stray literals — so the assertion is safe to add as-is. If a legitimate non-template `.html` literal (e.g. an external href) is ever added, introduce a tiny explicit `_NON_TEMPLATE_HTML` frozenset with an inline justification (mirroring the `_ALLOWLIST` idiom), rather than weakening the assertion.

**Rejected alternative (per CONTEXT):** AST/vulture unused-assignment detection — closer to root cause but heavier and overlaps the D-12 sweep. The literal-resolves assertion is lighter, targeted, and lives in the exact guard that had the blind spot.

## CLEAN-01 — /saq re-link (verified wiring)

- **Live page confirmed:** the v7.0 shell surfaces Agents via `/admin/agents` (both `shell/partials/rail.html:225` and `shell/partials/header.html:46` link it). That route is `routers/admin_agents.py::page`, which renders `admin/agents.html` for non-HTMX loads. `execution.py:328` renders a *different* thing — `execution/partials/agents_table.html` via SSE (an inline status partial), NOT the Agents page. **CLEAN-01 targets `admin/agents.html` + `admin_agents.py` — correct.** [VERIFIED: grep]
- **Settings attribute confirmed:** `settings.enable_saq_ui` is defined on `BaseSettings` (`config.py:292`, before `ControlSettings` at 338), default `True`, alias `PHAZE_ENABLE_SAQ_UI`. It gates the `/saq` mount in `main.py:160`. Being on `BaseSettings`, it's available on both Control and Agent settings. [VERIFIED: config.py]
- **Context addition:** `admin_agents.py::page` does NOT currently import settings. Add `from phaze.config import get_settings` and inject `"enable_saq_ui": get_settings().enable_saq_ui` into the `page` handler's context dict (line ~102). Prefer `get_settings()` (respects the test lru_cache-clear fixture) over a module-level import. The `/_table` partial handler does NOT need it — the link lives in the full page shell, not the polled partial.
- **Template edit:** inside `admin/agents.html` `{% block content %}`, after the main `<div class="space-y-6">…</div>` (line ~19), add a discreet muted footer link:
  ```jinja
  {% if enable_saq_ui %}
  <p class="mt-6 text-xs text-gray-400 dark:text-gray-500">
    <a href="/saq" target="_blank" rel="noopener" class="hover:underline">SAQ monitor ↗</a>
  </p>
  {% endif %}
  ```
  Low visual weight (D-10), new tab (D-11), gated on the flag (D-09) so it never renders a dead 404 when `/saq` is unmounted.
- **Test:** add a render test asserting the link appears when `enable_saq_ui=True` and is absent when `False` (mirrors `test_main_lifespan.py` monkeypatch pattern on `enable_saq_ui`). This belongs in the `agents` bucket (or `shared/web`) — it uses a TestClient/DB fixture so it will be integration-marked.

## Architecture Patterns

### Existing guard idiom (follow exactly for DOCS-01)
```python
# Source: tests/shared/core/test_dead_template_guard.py (in-repo, VERIFIED)
_REPO_ROOT = Path(__file__).resolve().parents[3]     # tests/shared/core/X.py -> repo root
# pure read_text parse; one assertion function per behavior; precise failure messages;
# NO phaze.* import -> hermetic, immune to settings/queue cross-test poison.
```

### Anti-Patterns to Avoid
- **Re-enabling heavy CI jobs on doc-only PRs** to run the drift gate — breaks CI-04 skip-with-success. Use the always-run quality job instead.
- **Applying VERIFICATION-file gating to archived milestones** — v7.0/others lack the files; it would fail every archived requirement. Internal-consistency-only for archived.
- **Wiring vulture as a blocking gate** — framework false-positives; D-12/D-13 want manual verify.
- **Weakening the dead-template closure to force green** — the guard docstring already warns against this; add the new assertion, don't relax the old one.
- **Deleting `build_dashboard_context`/`get_stage_progress`/`get_queue_activity`** — vulture may flag view-adjacent helpers, but these still feed the Analyze workspace + `/pipeline/stats`. Verify dynamic refs first.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Whole-symbol dead-code detection | a custom AST walker | vulture | Handles confidence scoring, decorators, whitelists; the D-13 chosen tool. |
| Template reachability | a new orphan-finder | extend existing `test_dead_template_guard.py` | The closure logic already exists; D-14 just adds one assertion. |
| CI doc-only routing | a new detect-changes variant | the already-unconditional `quality` job | It already survives the CI-04 skip. |
| Markdown table parsing | a markdown library dependency | small regexes (as the repo's other guards do) | The tables are simple pipe tables; regex keeps the guard dependency-free and hermetic. |

**Key insight:** every deliverable here extends an existing, proven repo mechanism (guard idiom, always-run quality job, dead-template guard, settings flag) rather than introducing new infrastructure — which is exactly what a "no behavior change" cleanup phase should do.

## Common Pitfalls

### Pitfall 1: Status-vocabulary mismatch across milestones
**What goes wrong:** the gate hard-codes `"Complete"` and reports false drift on v7.0 (which uses `"Done"`).
**How to avoid:** normalize `{complete, done} → COMPLETE`, `{deferred} → DEFERRED` before comparing. **Warning sign:** archived-milestone assertions fail on first run.

### Pitfall 2: Deferred/"Future" rows treated as phase-mapped
**What goes wrong:** rows like `| IDENT-03 | Future (deferred — …) | Deferred |` have no phase number; a naïve `Phase \d+` extraction returns None and the row is mis-scored as "marked but unpassed."
**How to avoid:** rows whose phase cell yields no `Phase N` match are deferred/unmapped — assert only checkbox-`[ ]` + Status-Deferred consistency, never phase-pass.

### Pitfall 3: Phase 66's own in-flight state trips the gate
**What goes wrong:** during this phase, DOCS-01/CLEAN-01/CLEAN-02 are `[ ]` + Pending and Phase 66 is `[ ]`; a gate that only checks "passed⟹marked" is fine, but one that checks "all requirements marked" fails.
**How to avoid:** implement D-05 tolerance explicitly and add `test_inflight_phase_with_unmarked_requirements_passes`. The gate must be green throughout Phase 66's development.

### Pitfall 4: Decimal + `<details>`-wrapped phases missed
**What goes wrong:** regex `Phase \d+` misses `Phase 57.1`; parser skips `<details>`-collapsed archived sections.
**How to avoid:** `Phase (\d+(?:\.\d+)?)`; parse `<details>` bodies as plain markdown (the `- [x]` lines inside are normal markdown, VERIFIED).

### Pitfall 5: VERIFICATION filename inconsistency (archived)
**What goes wrong:** globbing `{NN}-VERIFICATION.md` misses bare `VERIFICATION.md` (phases 31, 46).
**How to avoid:** this only matters if archived VERIFICATION is ever consulted — the recommended rule is NOT to consult archived VERIFICATION at all. For the ACTIVE milestone, glob `*VERIFICATION*.md` within the phase dir to be filename-robust.

## Runtime State Inventory

> This phase is code/test/tooling only — no data migration, no service reconfiguration, no rename. Included for completeness.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — no DB or datastore change. | none |
| Live service config | None — no external service touched. | none |
| OS-registered state | None. | none |
| Secrets/env vars | `PHAZE_ENABLE_SAQ_UI` already exists (Phase 33); CLEAN-01 only READS it, does not add/rename. | none |
| Build artifacts | Adding `vulture` to the dev group changes `uv.lock`; `just install`/`uv sync` regenerates the dev venv. | `uv lock && uv sync` after adding the dep |

**Verified:** no runtime state carries any renamed/removed identifier — CLEAN-02 removes only confirmed-dead code (D-12 guardrail), and `_STAGE_PLACEHOLDER` was already physically removed in PR #191.

## Validation Architecture

> nyquist_validation is not disabled in `.planning/config.json` (no `workflow.nyquist_validation: false`), so this section applies.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest >=9.1.1 (+ pytest-asyncio, pytest-cov, pytest-xdist) [VERIFIED: pyproject] |
| Config file | `pyproject.toml [tool.pytest.ini_options]` |
| Quick run command | `just test-file tests/shared/core/test_requirements_traceability.py` |
| Full suite command | `just integration-test` (ephemeral PG+Redis) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DOCS-01 | drift detected: passed phase w/ unmarked req fails | unit (fs) | `uv run pytest tests/shared/core/test_requirements_traceability.py -x` | ❌ Wave 0 |
| DOCS-01 | checkbox↔table disagreement fails (D-03) | unit (fs) | same file | ❌ Wave 0 |
| DOCS-01 | in-flight phase (Phase 66 state) PASSES (D-05) | unit (fs) | same file | ❌ Wave 0 |
| DOCS-01 | archived milestone internal-consistency (no VER files) | unit (fs) | same file | ❌ Wave 0 |
| DOCS-01 | runs on doc-only PR (wired into quality job) | CI structural | `just docs-drift` locally; assert step present in `code-quality.yml` | ❌ Wave 0 |
| CLEAN-01 | link renders when `enable_saq_ui=True`, absent when False | integration (TestClient) | `uv run pytest tests/agents/…::test_saq_link -x` | ❌ Wave 0 |
| CLEAN-02 | every router `.html` literal resolves to a template (D-14) | unit (fs) | `uv run pytest tests/shared/core/test_dead_template_guard.py -x` | ✅ extend existing |
| CLEAN-02 | full suite green after dead-code deletion | full suite | `just integration-test` | ✅ existing |

### Sampling Rate
- **Per task commit:** `just docs-drift` + the touched guard file.
- **Per wave merge:** `just test-bucket shared` (+ `agents` for CLEAN-01) in isolation.
- **Phase gate:** full suite green before `/gsd:verify-work`; the drift gate must be green while Phase 66 is still `[ ]`.

### Wave 0 Gaps
- [ ] `tests/shared/core/test_requirements_traceability.py` — new DOCS-01 guard (covers the 5 drift classes + in-flight tolerance + archived consistency).
- [ ] `justfile` — `docs-drift` recipe.
- [ ] `.github/workflows/code-quality.yml` — one step invoking `just docs-drift`.
- [ ] `tests/shared/core/test_dead_template_guard.py` — add `test_entry_literals_resolve_to_templates` (D-14).
- [ ] `admin/agents.html` render test for the `/saq` link (both flag states).
- [ ] `pyproject.toml` — `vulture>=2.16` dev dep + `uv.lock` regen.

## Security Domain

> `security_enforcement` is not disabled. This phase is test/CI/presentation-only; the security surface is minimal.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | unchanged |
| V3 Session Management | no | unchanged |
| V4 Access Control | no | `/saq` access control is unchanged (reverse-proxy internal-realm auth, Phase 33 LOCKED); CLEAN-01 only adds a link. |
| V5 Input Validation | minimal | the drift guard reads trusted repo files only; no external input. |
| V6 Cryptography | no | unchanged |

### Known Threat Patterns for {stack}
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Reverse-tabnabbing on the new `/saq` link | Tampering | `rel="noopener"` on the `target="_blank"` anchor (D-11) — already specified. |
| Dead-link 404 when `/saq` unmounted | (UX, not security) | `{% if enable_saq_ui %}` gate (D-09). |
| Supply-chain risk from the new vulture dep | Tampering | dev-only; cooldown window excludes fresh typosquats; canonical source repo verified; planner may add `checkpoint:human-verify`. |

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual REQUIREMENTS/ROADMAP sync (drift found across retrospectives) | Automated pytest drift gate | This phase (DOCS-01) | Closes the manual-sync gap. |
| Dead-template guard flags orphan files only | + flags orphan entry-root literals | This phase (D-14) | The `_STAGE_PLACEHOLDER` class of bug now fails loudly. |

**Deprecated/outdated:** none relevant.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | slopcheck not run (offline); vulture treated as trusted via PyPI+GitHub reputation + cooldown | Package Legitimacy | Low — vulture is a decade-old high-reputation tool; planner can add a verify checkpoint. |
| A2 | The recommended archived degradation rule (internal-consistency-only) matches the operator's intent | Deep-Dive 1 | Medium — CONTEXT D-04 explicitly states this intent ("archived stay internally consistent, not fail on missing VERIFICATION"), so risk is low; confirm at plan/discuss if any doubt. |
| A3 | `--min-confidence 80` for an optional recipe is a sensible default | Deep-Dive 3 | Low — tunable; the manual sweep runs at 60 regardless. |

**Note:** A2 is the only assumption touching a locked decision, and it aligns with the explicit CONTEXT D-04 wording; treat as confirmed unless the planner sees conflicting intent.

## Open Questions

1. **Should archived ROADMAP `[x]` be cross-checked at all, or only REQUIREMENTS internal consistency?**
   - What we know: archived milestones all shipped; ROADMAP archived lines are all `[x]`.
   - What's unclear: whether the gate should bother re-reading archived ROADMAP or just assert REQUIREMENTS checkbox↔table↔Complete.
   - Recommendation: assert REQUIREMENTS internal consistency for archived (checkbox↔table↔Complete/Deferred); optionally cross-check archived ROADMAP `[x]` presence but never gate on VERIFICATION. Keep it simple — the higher-value check is the active milestone.

2. **Does the planner want a non-blocking `just vulture` recipe committed, or a pure one-shot sweep?**
   - Recommendation: commit a `just vulture` recipe (repeatable, documents the flags) but do NOT add it to pre-commit/CI as blocking. The actual deletions are a one-shot manual-verify pass this phase.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| uv | all commands | ✓ | project standard | — |
| pytest | all guards | ✓ | >=9.1.1 (installed) | — |
| jinja2 | dead-template guard | ✓ | 3.1.x (installed) | — |
| just | CI + local recipes | ✓ | 4.x | — |
| vulture | CLEAN-02 sweep | ✗ (to be added) | 2.16 (cooldown-safe) | `ruff` F401 covers unused imports only — not a full substitute |

**Missing dependencies with no fallback:** none blocking (vulture is additive dev tooling).
**Missing dependencies with fallback:** vulture — partial fallback to ruff for imports, but vulture is the D-13 chosen tool and resolves cleanly.

## Sources

### Primary (HIGH confidence — verified in-repo this session)
- `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md` — active traceability + phase-pass formats.
- `.planning/milestones/vN.M-{REQUIREMENTS,ROADMAP}.md` + `find` over `vN.M-phases/` — archived layout, VERIFICATION presence/naming (v7.0 has none; 39–42 have none; 31/46 bare filename).
- `.planning/phases/{63,64,65}-VERIFICATION.md` — `status: passed` frontmatter format.
- `tests/shared/core/test_dead_template_guard.py`, `test_docs_ia_current.py` — guard idiom + the D-14 blind spot.
- `.github/workflows/{ci,code-quality,tests}.yml`, `scripts/classify-changed-files.sh`, `justfile`, `tests/buckets.json`, `tests/conftest.py` — CI-04 skip contract + always-run quality job + bucket/hermeticity facts.
- `src/phaze/routers/admin_agents.py`, `src/phaze/main.py` (saq mount), `src/phaze/config.py` (enable_saq_ui on BaseSettings), `src/phaze/templates/{admin/agents.html,shell/partials/rail.html,header.html}` — CLEAN-01 wiring.
- Script over `routers/*.py` vs `templates/` — **67 router `.html` literals, all resolve** (D-14 de-risk).
- `.planning/RETROSPECTIVE.md §~200-225` — the `_STAGE_PLACEHOLDER` blind-spot narrative.
- `pyproject.toml [tool.uv]` — `exclude-newer = "7 days"` cooldown; `requires-python >=3.14,<3.15`.

### Secondary (MEDIUM confidence — external, verified)
- [vulture · PyPI](https://pypi.org/project/vulture/) — 2.16 released 2026-03-25, Python 3.9–3.14.
- [jendrikseipp/vulture releases + CHANGELOG](https://github.com/jendrikseipp/vulture/releases) — Python 3.14 support, `--make-whitelist`, `--ignore-decorators`.
- [Python 3.14 Readiness](http://pyreadiness.org/3.14/) — vulture 3.14-ready.

## Metadata

**Confidence breakdown:**
- DOCS-01 parser + degradation rule: HIGH — every format and the VERIFICATION-absence reality verified on disk.
- DOCS-01 CI wiring: HIGH — CI-04 skip contract and the un-gated quality job verified in ci.yml.
- CLEAN-01: HIGH — live page, settings attribute, and gate mechanism all confirmed in-repo.
- CLEAN-02 vulture: MEDIUM-HIGH — version/cooldown verified via PyPI; slopcheck not run (offline).
- CLEAN-02 D-14 fix: HIGH — de-risked by enumerating all 67 literals (all resolve today).

**Research date:** 2026-07-03
**Valid until:** ~2026-08-02 (stable domain; only vulture version could move — re-check if the release cadence matters).

## RESEARCH COMPLETE
