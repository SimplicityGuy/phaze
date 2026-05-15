---
phase: 28
plan: 06
subsystem: ui-templates / docs
tags: [wave-3, task-04, banner, alpine-js, doc-sweep, tdd]
dependency_graph:
  requires:
    - "28-01 (tests/test_template_helpers/ package + src/phaze/templates/_partials/ anchor + test_cross_fs_fingerprint_notice.py stub)"
    - "28-04 (duplicates/list.html host page — no shared edit point, but the executor verified the {% block content %} structure matches PATTERNS lines 791-805)"
  provides:
    - "src/phaze/templates/_partials/cross_fs_fingerprint_notice.html (TASK-04 operator-visible disclosure surface)"
    - "duplicates/list.html includes the banner above its <h1> on every page load"
    - "PROJECT.md Constraints paragraph documenting per-agent fingerprint indices + XAGENT-01"
  affects:
    - src/phaze/templates/duplicates/list.html
    - .planning/PROJECT.md
tech_stack:
  added: []
  patterns:
    - "Alpine.js x-data='{ open: true }' + x-show='open' + @click='open = false' for in-memory dismissal (no localStorage)"
    - "HTML-entity icon convention extended from warning &#9888; (collision_block.html) to info &#9432;"
    - "role='status' (informational) chosen over role='alert' (urgent) for a by-design limitation disclosure"
    - "FastAPI Jinja2Templates test harness reused from test_progress_partial.py (Plan 28-04)"
key_files:
  created:
    - src/phaze/templates/_partials/cross_fs_fingerprint_notice.html
  modified:
    - src/phaze/templates/duplicates/list.html
    - .planning/PROJECT.md
    - tests/test_template_helpers/test_cross_fs_fingerprint_notice.py
  deleted:
    - src/phaze/templates/_partials/.gitkeep  # Wave 0 anchor replaced by the real partial
decisions:
  - "[Phase 28-06]: cross_fs_fingerprint_notice.html banner is dismissible per session only (no localStorage); included on duplicates/list.html as the first child of the space-y-6 div above the <h1>"
  - "[Phase 28-06]: Constraints paragraph in PROJECT.md placed AFTER the bulleted constraints list and BEFORE the Key Decisions section -- keeps the existing Key Decisions row 'Per-agent fingerprint DB (v4.0)' intact while adding operator-facing prose at the section's natural narrative seam"
  - "[Phase 28-06]: .gitkeep anchor in src/phaze/templates/_partials/ removed in the same commit as the real partial (deletion is intentional and documented inline in the commit message)"
metrics:
  duration_seconds: 480
  duration_human: "~8 min"
  tasks_completed: 1
  files_changed: 5
  commits: 2
  completed_date: "2026-05-15"
---

# Phase 28 Plan 06: TASK-04 Operator Disclosure (Cross-FS Fingerprint Notice Banner) Summary

Lands the operator-facing portion of TASK-04: a dismissible (per-session) Alpine.js info banner on the Duplicate Resolution page disclosing the v4.0 per-file-server fingerprint-locality limitation, plus an operator-facing Constraints paragraph in `PROJECT.md`. The config-validator portion of TASK-04 landed in Plan 28-01 (audfprint/panako URL allow-list). This plan closes the task and Phase 28's operator-visible work.

## What Was Built

### TDD RED → GREEN sequence

- **RED commit `11a98f5`** (`test(28-06): add failing tests for cross-FS fingerprint notice banner`) — replaced the Wave 0 module-level `pytest.skip` stub with eight real tests against the not-yet-created banner partial: Alpine.js dismissal state, `role="status"` (not `alert`), info glyph (`&#9432;` not `&#9888;`), dismiss button with `aria-label`, no `localStorage` reference (source-file inspection — not rendered output), heading copy, body XAGENT disclosure copy, and the `duplicates/list.html` inclusion contract. All eight failed with `TemplateNotFound`.
- **GREEN commit `ca97e30`** (`feat(28-06): add cross-FS fingerprint notice banner + PROJECT.md constraint`) — created the banner partial, included it in `duplicates/list.html`, added the operator-facing paragraph to `PROJECT.md`, and removed the Wave 0 `.gitkeep` anchor (its purpose — to keep the empty directory in git — is now served by the real partial sibling). All eight tests flipped to PASSED; pre-commit hooks green on all four touched files.

### Banner partial (`src/phaze/templates/_partials/cross_fs_fingerprint_notice.html`)

Key attributes (per UI-SPEC C3 + PATTERNS S7):

| Attribute | Value | Source |
|-----------|-------|--------|
| Alpine state | `x-data="{ open: true }"` | UI-SPEC C3 / PATTERNS lines 348-353 |
| Show binding | `x-show="open"` | UI-SPEC C3 |
| ARIA role | `role="status"` (NOT `role="alert"`) | UI-SPEC C3 / threat-model `T-28-06-A11y` |
| Icon glyph | `&#9432;` (info — NOT `&#9888;` warning) | UI-SPEC C3 / PATTERNS S7 |
| Dismiss handler | `@click="open = false"` | UI-SPEC C3 |
| Dismiss a11y | `aria-label="Dismiss notice"` | UI-SPEC C3 accessibility contract |
| Persistence | NONE — no `localStorage`; reload restores | CONTEXT.md D-14 (explicit) |
| Surface colors | `bg-blue-50 dark:bg-blue-950/30` + `border-blue-200 dark:border-blue-900` | UI-SPEC Color §"Notice (cross-FS fingerprint)" row |

### duplicates/list.html host edit

Inserted `{% include "_partials/cross_fs_fingerprint_notice.html" %}` as the first child of the existing `<div class="space-y-6">` inside `{% block content %}`, immediately before the `<h1 class="...">Duplicate Resolution</h1>` line. Tailwind `space-y-6` automatically supplies the vertical gap below the banner.

### PROJECT.md Constraints paragraph

Appended as a single paragraph in the **Constraints** section (after the existing bulleted list and before the `## Key Decisions` heading). The paragraph reads:

> **Per-agent fingerprint indices (v4.0).** Each file server's `audfprint` and `panako` sidecars index ONLY that file server's local files. Duplicate audio content landing on different file servers will NOT cross-match. Cross-file-server fingerprint matching is XAGENT-01 (deferred to a post-v4.0 milestone). The Duplicate Resolution admin UI surfaces this constraint as an inline, per-session-dismissible banner on every page load so the operator interprets fingerprint-derived results with this scope in mind.

The existing `Per-agent fingerprint DB (v4.0)` row in the Key Decisions table is preserved unmodified — the new paragraph supplements it with operator-facing prose, matching CONTEXT.md D-13's "ADDS an operator-facing paragraph in the Constraints section" instruction.

## 28-V-NN Test ID Status

| Test ID | Status (this plan) | Notes |
|---------|--------------------|-------|
| **28-V-24** (banner partial renders + dismiss attributes + role="status" + no localStorage + inclusion in duplicates/list.html) | **GREEN** | All eight assertions in `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py` pass |

No other 28-V-NN IDs are owned by this plan.

## Deviations from Plan

None — plan executed exactly as written.

The plan's `<files_modified>` listed `.planning/STATE.md` but the orchestrator's spawn message explicitly instructed: "DO NOT edit .planning/STATE.md from inside the worktree." That instruction overrides the plan's file list; the STATE.md accumulation entry is surfaced below under the **Recommended STATE.md entry** heading for the orchestrator to append after wave merge.

## Recommended STATE.md entry

Append the following single bullet to `.planning/STATE.md` §"Accumulated Context → Decisions" (alongside the Phase 28-01/02/03/04/05 bullets the orchestrator is accumulating from sibling SUMMARYs in this wave):

```
- [Phase 28-06]: cross_fs_fingerprint_notice.html banner is dismissible per session only (no localStorage); included on duplicates/list.html as the first child of the space-y-6 div above the <h1>; Constraints paragraph in PROJECT.md documents XAGENT-01 (deferred cross-file-server fingerprint matching)
```

## Auth Gates

None. This plan touched no HTTP endpoints, credentials, or external services.

## Threat Surface Scan

No new threat surface introduced. The plan's `<threat_model>` mitigations are all met:

- **T-28-06-I (Information Disclosure)** — `accept` disposition: the banner intentionally discloses the per-file-server-indexing architecture; that is the design.
- **T-28-06-T (Tampering)** — `mitigate`: no `localStorage` is referenced anywhere in the partial source (`test_banner_has_no_localstorage_reference` enforces this via source-file inspection); reload always restores the banner.
- **T-28-06-V13 (XSS via banner content)** — `mitigate`: banner copy is server-side static; Jinja2Templates autoescape is default-on for `.html` templates (FastAPI convention).
- **T-28-06-A11y (Screen-reader handling)** — `mitigate`: `role="status"` chosen (informational) per `test_banner_has_role_status_not_alert`; dismiss button has `aria-label="Dismiss notice"` per `test_banner_has_dismiss_button_with_aria_label`.

No `## Threat Flags` section needed.

## Known Stubs

None. The banner is fully wired and operator-visible. The "Learn more" anchor uses `href="#"` per UI-SPEC C3 ("v4.0 placeholder; planner SHOULD wire to PROJECT.md anchor if PROJECT.md gets a doc-link target during D-13 work, otherwise leave the anchor pointing at `#` with a `title='See PROJECT.md'` attribute") — the executor kept `href="#"` because `PROJECT.md` has no inline heading anchor target and a fully-rendered docs page is out of scope for v4.0. The `title="See PROJECT.md"` attribute is present so hovering reveals the doc reference.

This is **not** a stub in the data-rendering sense (no empty data flowing to UI); it's a UI-SPEC-sanctioned placeholder anchor with a fallback hover-tooltip.

## Plan Verification

Executed the plan's `<automated>` command:

```bash
uv run pytest tests/test_template_helpers/test_cross_fs_fingerprint_notice.py -x
```

Result: **8 passed, 0 failed, 0 skipped**.

Wider check (no regressions to sibling template-helper tests or to test infrastructure landed in Plan 28-01):

```bash
uv run pytest tests/test_template_helpers/ tests/test_services/test_fingerprint_locality.py tests/test_schemas/ -x
```

Result: **124 passed, 0 failed, 0 skipped**.

Done criteria from `<done>`:

- `28-V-24` (banner partial renders + dismiss attributes) GREEN ✓
- `test -f src/phaze/templates/_partials/cross_fs_fingerprint_notice.html` succeeds ✓
- `grep -c "localStorage" src/phaze/templates/_partials/cross_fs_fingerprint_notice.html` returns 0 ✓
- `grep -c 'role="status"' src/phaze/templates/_partials/cross_fs_fingerprint_notice.html` returns 1 (≥ 1) ✓
- `grep -c "_partials/cross_fs_fingerprint_notice" src/phaze/templates/duplicates/list.html` returns 1 (≥ 1) ✓
- `grep -c "XAGENT-01" PROJECT.md` returns 1 (≥ 1) ✓ — the new Constraints paragraph names XAGENT-01
- `.gitkeep` removed from `src/phaze/templates/_partials/` (the banner replaces it; same commit) ✓
- Pre-commit on touched files green ✓

`grep -c "Phase 28" .planning/STATE.md ≥ 4` is **not** asserted from this worktree per the orchestrator's STATE.md-isolation instruction — the four bullets land in STATE.md when the orchestrator accumulates them after wave merge.

## Post-Merge Smoke Test (manual)

After wave 3 merges to main:

1. `just up` (or equivalent — boot the application server in the project's local dev stack).
2. Open `/duplicates/` in a browser.
3. Confirm the blue `Fingerprint matches are file-server-scoped` banner renders **above** the `Duplicate Resolution` heading.
4. Click the `×` button → banner hides immediately (no page reload).
5. Reload the page → banner re-appears (per-session dismissal contract).
6. Hover the `Learn more` link → tooltip shows `See PROJECT.md`.

## Phase 28 TASK-04 Closure

This plan closes Phase 28 TASK-04 in full:

| Sub-surface | Plan | Status |
|-------------|------|--------|
| Config-side allow-list validator (audfprint_url / panako_url) | 28-01 | Landed 2026-05-15 (commits `3ed23b6` RED, `814085f` GREEN) |
| Operator-facing PROJECT.md Constraints paragraph | 28-06 (this) | Landed 2026-05-15 (`ca97e30`) |
| Operator-visible UI banner on duplicates page | 28-06 (this) | Landed 2026-05-15 (`ca97e30`) |

TASK-04 has no remaining sub-surfaces. The fingerprint-locality limitation is now structurally disclosed at three layers: config validation (rejects forged URLs at boot), public docs (PROJECT.md), and live UI (cannot be permanently silenced).

## TDD Gate Compliance

- RED gate (`test(...)` commit `11a98f5`): added the failing tests + replaced the Wave 0 stub. ✓
- GREEN gate (`feat(...)` commit `ca97e30`): minimal production implementation (banner partial + host include + PROJECT.md paragraph + .gitkeep removal) flips the failing tests to passing. ✓
- REFACTOR gate: not required — the partial is the minimal-surface implementation; no follow-up cleanup needed.

Gate sequence verified in `git log --oneline -3`:

```
ca97e30 feat(28-06): add cross-FS fingerprint notice banner + PROJECT.md constraint
11a98f5 test(28-06): add failing tests for cross-FS fingerprint notice banner
df5b677 docs(phase-28): update tracking after wave 2
```

## Self-Check: PASSED

Verified all paths and commit hashes:

- File checks (FOUND):
  - `src/phaze/templates/_partials/cross_fs_fingerprint_notice.html`
  - `src/phaze/templates/duplicates/list.html` (includes the partial)
  - `.planning/PROJECT.md` (contains `XAGENT-01`)
  - `tests/test_template_helpers/test_cross_fs_fingerprint_notice.py` (8 passing tests)
- Commit checks (FOUND):
  - `11a98f5` (RED) — `test(28-06): add failing tests for cross-FS fingerprint notice banner`
  - `ca97e30` (GREEN) — `feat(28-06): add cross-FS fingerprint notice banner + PROJECT.md constraint`
- `.gitkeep` correctly deleted (`git ls-files src/phaze/templates/_partials/.gitkeep` returns nothing).
