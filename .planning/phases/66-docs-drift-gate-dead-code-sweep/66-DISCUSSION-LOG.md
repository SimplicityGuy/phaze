# Phase 66: Docs-Drift Gate & Dead-Code Sweep - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-03
**Phase:** 66-docs-drift-gate-dead-code-sweep
**Areas discussed:** Drift gate: what it checks, Drift gate: form & where, /saq link visibility, Dead-code sweep scope

---

## Drift gate: what it checks (DOCS-01)

| Option | Description | Selected |
|--------|-------------|----------|
| ROADMAP [x] checkbox | ROADMAP checkbox is sole pass marker | |
| STATE.md / VERIFICATION | Derive pass from VERIFICATION/STATE | |
| Both must agree | ROADMAP [x] AND VERIFICATION artifact together | ✓ |

**User's choice:** Both must agree

| Option | Description | Selected |
|--------|-------------|----------|
| Bidirectional | Fail on passed-but-unmarked AND marked-but-unpassed | ✓ |
| Passed → reqs only | Only passed-but-unmarked | |
| Req → phase only | Only marked-but-unpassed | |

**User's choice:** Bidirectional

| Option | Description | Selected |
|--------|-------------|----------|
| Both, must agree | Per-req checkbox AND table Status must agree + match phase-pass | ✓ |
| Traceability table only | Status column sole truth | |
| Checkbox only | Per-req `[x]` sole truth | |

**User's choice:** Both, must agree (checkbox + table Status must match each other and phase-pass)

| Option | Description | Selected |
|--------|-------------|----------|
| VERIFICATION.md = passed | Phase verified if NN-VERIFICATION.md status=passed | ✓ |
| Any VERIFICATION.md exists | Presence is enough | |
| You decide | Planning picks | |

**User's choice:** VERIFICATION.md = passed

| Option | Description | Selected |
|--------|-------------|----------|
| Active milestone only | Only .planning/REQUIREMENTS.md | |
| Active + archived | Also re-validate archived milestone pairs | ✓ |

**User's choice:** Active + archived

| Option | Description | Selected |
|--------|-------------|----------|
| Unmarked+unpassed = OK | In-flight phase with unmarked reqs passes | ✓ |
| You decide | Planning defines tolerance | |

**User's choice:** Unmarked+unpassed = OK

**Notes:** Flagged for research — archived milestones predate the verifier and their VERIFICATION
artifacts may be absent or in `milestones/vN.M-phases/`; the gate must degrade gracefully (archived
pairs validated for internal consistency, active pairs for full ROADMAP+VERIFICATION agreement).

---

## Drift gate: form & where (DOCS-01)

| Option | Description | Selected |
|--------|-------------|----------|
| pytest guard | Test in shared bucket like test_docs_ia_current.py | ✓ |
| Standalone CI step | scripts/*.py as own job | |
| Both: script + pytest | Logic in script + pytest wrapper | |

**User's choice:** pytest guard

| Option | Description | Selected |
|--------|-------------|----------|
| Runs on doc-only too | Executes even when only .planning/markdown changed | ✓ |
| Only when code runs | Rides inside normal suite; doc-only PRs skip it | |

**User's choice:** Runs on doc-only too

| Option | Description | Selected |
|--------|-------------|----------|
| Precise, actionable | Name the exact offender phase/requirement | ✓ |
| You decide | Planning designs message format | |

**User's choice:** Precise, actionable

**Notes:** Combination (pytest guard + runs-on-doc-only) implies a lightweight always-run CI invocation
of just this guard, or adding REQUIREMENTS/ROADMAP/.planning paths to its trigger — without breaking
CI-04's skip-with-success for the heavy jobs.

---

## /saq link visibility (CLEAN-01)

| Option | Description | Selected |
|--------|-------------|----------|
| Gate on enable_saq_ui | Render only when settings.enable_saq_ui is true | ✓ |
| Always show | Render unconditionally (risks 404) | |

**User's choice:** Gate on enable_saq_ui

| Option | Description | Selected |
|--------|-------------|----------|
| Discreet footer/utility link | Small muted text link on agents.html | ✓ |
| Header/toolbar icon | Icon-button in header | |
| You decide | Planning/UI picks | |

**User's choice:** Discreet footer/utility link

| Option | Description | Selected |
|--------|-------------|----------|
| New tab | target=_blank rel=noopener | ✓ |
| Same tab | Navigate in place | |

**User's choice:** New tab

**Notes:** admin_agents.py doesn't currently pass enable_saq_ui into context — CLEAN-01 must add it
(presentation-only context addition, not a backend behavior change).

---

## Dead-code sweep scope (CLEAN-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Templates + surfaced items | Orphans + audit/retro-named dead items (bounded) | |
| Full repo dead-code hunt | vulture/coverage across all of src/ | ✓ |
| You decide | Research inventories | |

**User's choice:** Full repo dead-code hunt (with the confirmed-dead-only guardrail)

| Option | Description | Selected |
|--------|-------------|----------|
| vulture-assisted + verify | vulture candidates, manually verify before deleting | ✓ |
| Manual inventory only | Hand-audit from findings | |
| You decide | Research picks approach | |

**User's choice:** vulture-assisted + verify

| Option | Description | Selected |
|--------|-------------|----------|
| Assert literals resolve | Guard asserts every entry .html literal resolves on-disk | |
| AST unused-assignment check | Detect the unused assignment via AST/vulture | |
| You decide | Planning chooses mechanism | ✓ |

**User's choice:** You decide (leaning: assert-literals-resolve; required outcome: a dead entry-root
literal must fail the guard rather than mask an orphan)

**Notes:** vulture is not installed / not in pyproject — needs adding as a dev dependency (cooldown-safe).
Plan for a vulture whitelist to avoid false-positives on framework-invoked code (FastAPI handlers,
Pydantic validators, SQLAlchemy hooks). Full-repo hunt is broader than CLEAN-02's literal wording but
is added thoroughness within the same requirement, not new scope.

---

## Claude's Discretion

- **D-14 blind-spot mechanism** — user chose "you decide". Leaning documented in CONTEXT.md
  (assert entry literals resolve on-disk); planning free to choose, provided a dead entry-root literal
  fails the guard rather than masking an orphan.

## Deferred Ideas

- Rewriting coverage/CI tooling or a full monorepo/service split — out of scope per milestone.
- Multi-cloud backends (MCB-01..) — next named milestone, phase 67+.
