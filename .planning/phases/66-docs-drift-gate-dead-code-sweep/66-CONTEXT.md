# Phase 66: Docs-Drift Gate & Dead-Code Sweep - Context

**Gathered:** 2026-07-03
**Status:** Ready for planning

<domain>
## Phase Boundary

The final phase of the **2026.7.0 Engineering Improvements** milestone. Delivers three
maintainer-facing cleanup requirements, with a hard **no backend / no product behavior
change** constraint:

- **DOCS-01** — a CI gate that cross-checks `REQUIREMENTS.md` traceability against passed
  phases and fails loudly when the docs are stale.
- **CLEAN-01** — restore a discreet in-UI link to the still-mounted `/saq` SAQ monitor on the
  Agents/Compute page. Presentation-only.
- **CLEAN-02** — sweep vestigial dead code and close the dead-template guard's blind spot for
  its own unused entry-root literals.

Everything here is CI/test/tooling infrastructure or presentation-only. The "user" is the
project maintainer/operator, not the end user.

</domain>

<decisions>
## Implementation Decisions

### Docs-Drift Gate — what it checks (DOCS-01)
- **D-01:** A phase counts as "passed" only when **both** signals agree: the `- [x]` checkbox
  on its `**Phase NN...**` line in `ROADMAP.md` **AND** its `NN-VERIFICATION.md` exists with
  status/verdict = `passed`. ROADMAP `[x]` alone is not sufficient.
- **D-02:** The cross-check is **bidirectional**. Fail if a passed phase has any mapped
  requirement not marked Complete, **and** fail if a requirement is marked Complete without its
  mapped phase being passed.
- **D-03:** `REQUIREMENTS.md` encodes status twice — the per-requirement `- [ ]/[x]` checkbox
  **and** the Traceability-table `Status` column. The gate requires **both to agree** with each
  other and with phase-pass. A drifted checkbox-vs-table is itself a failure (this is the exact
  gap where one gets updated and the other doesn't).
- **D-04:** Scope = **active milestone + archived milestones**. Validate `.planning/REQUIREMENTS.md`
  against `.planning/ROADMAP.md`, and also re-validate the archived `milestones/vN.M-REQUIREMENTS.md`
  / `vN.M-ROADMAP.md` pairs. ⚠ **Research flag:** archived milestones predate the gsd-verifier and
  their VERIFICATION artifacts may be absent or located under `milestones/vN.M-phases/` rather than
  `.planning/phases/`. The gate must degrade gracefully for archived pairs — the user's intent is
  that archived REQUIREMENTS/ROADMAP/table stay **internally consistent**, not that missing legacy
  VERIFICATION files fail the build. Planning must resolve how the "both-must-agree" rule applies to
  archived phases (likely: internal-consistency-only for archived, full ROADMAP+VERIFICATION for active).
- **D-05:** In-flight phases are tolerated: a not-yet-passed phase with unmarked requirements is the
  expected working state and must PASS. Only genuine drift fails — passed-but-unmarked, or
  marked-but-unpassed. (Phase 66 itself is `[ ]` with its reqs `[ ]` during this work → PASS.)

### Docs-Drift Gate — form & where it runs (DOCS-01)
- **D-06:** Form = a **pytest guard** in the shared bucket, following the existing
  `tests/shared/core/test_docs_ia_current.py` / `test_dead_template_guard.py` pattern (suggested
  name `test_requirements_traceability.py`). No new standalone-script tooling.
- **D-07:** The gate **must run on doc-only PRs**, since that is exactly when drift is introduced.
  CI-04 (Phase 63) skips the heavy build/test jobs on `.planning/`/markdown-only changes — so this
  guard needs a path around that skip. **Implied wiring (planning to resolve):** a lightweight
  always-run CI invocation of just this guard, or add `REQUIREMENTS.md`/`ROADMAP.md`/`.planning/**`
  to the paths that trigger it. Must still preserve CI-04's skip-with-success for the heavy jobs.
- **D-08:** On drift, the gate emits **precise, actionable** messages naming the exact offender —
  e.g. `Phase 65 passed but VER-03 checkbox [ ] unmarked`, `table Status 'Complete' ≠ checkbox [ ]
  for VER-03`, `DOCS-01 marked Complete but Phase 66 not passed`. The whole value is telling the
  maintainer exactly what to fix.

### /saq Re-Link (CLEAN-01) — presentation-only
- **D-09:** Render the link **only when `settings.enable_saq_ui` is true** (exactly when `/saq` is
  actually mounted per Phase 33), to avoid a dead 404 link. `admin_agents.py` does **not** currently
  pass `enable_saq_ui` into the `admin/agents.html` context — CLEAN-01 must add it. This is a
  presentation-only template-context addition, **not** a backend behavior change.
- **D-10:** Placement = a **discreet footer/utility link** on the Agents/Compute page
  (`admin/agents.html`), low visual weight (e.g. small muted "SAQ monitor ↗"). Matches "discreet"
  in CLEAN-01; must not compete with the existing header status strip.
- **D-11:** Opens in a **new tab** — `target="_blank" rel="noopener"` — because `/saq` is a separate
  embedded SAQ sub-app (no in-`/saq` link back to the console) and this keeps the operator's place.

### Dead-Code Sweep (CLEAN-02)
- **D-12:** Scope = **full-repo dead-code hunt** across `src/phaze/` (broader than CLEAN-02's literal
  "surfaced during the v7.0 cutover" wording — the user deliberately chose thoroughness). This is an
  expansion of *thoroughness* within the same requirement, not scope creep into a new capability.
  **Guardrail:** only **confirmed-dead** code is removed — verified against dynamic references and a
  green test suite. Nothing that alters runtime behavior.
- **D-13:** Method = **vulture-assisted + manual verify**. Run vulture over `src/phaze` to generate
  candidates, then verify each against runtime reachability (grep for dynamic refs, run tests) before
  deleting. ⚠ **Research flag:** `vulture` is **not currently installed** and not in `pyproject.toml`
  — it must be added as a dev dependency (subject to the supply-chain `exclude-newer` cooldown; vulture
  is long-stable so its floor is >7d old). Vulture will false-positive on framework-invoked code
  (FastAPI route handlers, Pydantic validators, SQLAlchemy hooks) — plan for a whitelist / ignore list
  so those are not deleted.

### Claude's Discretion
- **D-14 (blind spot mechanism — "you decide"):** Close the dead-template guard's blind spot for its
  own unused entry-root literals. **Leaning (planning free to refine):** add an assertion to
  `test_dead_template_guard.py` that every router-captured `"...html"` entry literal that lives under
  `templates/` resolves to an on-disk template — so a literal pointing at a now-deleted template (the
  `_STAGE_PLACEHOLDER` shape) fails loudly instead of masking an orphan. Alternative considered: an
  AST/vulture unused-assignment check (closer to root cause but heavier, overlaps the D-12/D-13 sweep).
  Whatever the mechanism, the required outcome: **a dead entry-root literal must fail the guard rather
  than mask an orphan.**

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & milestone framing (DOCS-01)
- `.planning/REQUIREMENTS.md` — the DOCS/CLEAN requirements + the Traceability table that DOCS-01
  gates; note the dual checkbox/Status encoding and the "no behavior change" framing.
- `.planning/ROADMAP.md` — the `- [x]` phase-pass markers the gate keys off; per-milestone
  requirement→phase mapping in the phase lines.
- `.planning/milestones/` — archived `vN.M-REQUIREMENTS.md` / `vN.M-ROADMAP.md` pairs in scope
  per D-04; `vN.M-phases/` for archived VERIFICATION locations.

### Dead-code guard & retrospective (CLEAN-02)
- `tests/shared/core/test_dead_template_guard.py` — the SHELL-05 guard whose blind spot D-14 closes;
  entry-literal extraction + reachability closure logic.
- `.planning/RETROSPECTIVE.md` §~209-218 — describes the blind spot: a reachable-set built from
  source-string literals can mask its *own* unused literals (the `_STAGE_PLACEHOLDER` incident).
- `.planning/milestones/v7.0-MILESTONE-AUDIT.md` §~77 — the cutover dead-code findings list.

### /saq re-link (CLEAN-01)
- `src/phaze/main.py` §~134-169 — `/saq` mount gated by `settings.enable_saq_ui` (Phase 33).
- `src/phaze/routers/admin_agents.py` — renders `admin/agents.html`; must add `enable_saq_ui` to context.
- `src/phaze/templates/admin/agents.html` — the Agents/Compute page; home of the discreet link.

### Existing guard patterns (DOCS-01 form)
- `tests/shared/core/test_docs_ia_current.py` — closest analog for a docs-consistency pytest guard.
- `.github/workflows/tests.yml`, `.github/workflows/ci.yml`, `scripts/classify-changed-files.sh` —
  the CI-04 code-change gating this guard must run *around* (D-07).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`test_docs_ia_current.py` / `test_dead_template_guard.py`**: established shared-bucket pytest
  guard pattern — DOCS-01's traceability guard should mirror their structure (repo-root pathing,
  parse-then-assert, precise failure messages).
- **`scripts/coverage_floor.py` + `classify-changed-files.sh`**: existing CI gate/script precedents
  for how a gate is wired and how changed-file classes are computed (relevant to D-07 doc-only routing).
- **`test_dead_template_guard.py` `_entry_templates()` / `_HTML_LITERAL`**: the literal-capture that
  has the blind spot — D-14 extends this same function/regex site.

### Established Patterns
- **CI-04 skip-with-success** (Phase 63): heavy jobs skip on doc-only PRs but required checks still
  report success. D-07's docs-drift gate must live *outside* that skip while not breaking the
  skip-with-success contract for the heavy jobs.
- **`enable_saq_ui` settings gate** (Phase 33): `/saq` mount is conditional; the link must mirror the
  same condition (D-09).
- **Test buckets** (Phase 63): the new guard belongs in the shared/generic bucket; must pass in
  isolation via `just test-bucket <bucket>` (see the CI bucket test-isolation note).

### Integration Points
- `admin_agents.py` `_render` context dict (`current_page: "admin_agents"`) — add `enable_saq_ui`
  (CLEAN-01). Confirm this is the live v7.0 shell Agents route (execution.py also renders an agents
  partial via SSE — verify which is the shell's Agents page before wiring the link).
- `pyproject.toml` `[dependency-groups]` dev — add `vulture` (D-13), alphabetically sorted, cooldown-safe.
- The new traceability guard parses `.planning/` docs — pathing must work from repo root in CI and locally.

</code_context>

<specifics>
## Specific Ideas

- The `_STAGE_PLACEHOLDER` symptom was already removed by PR #191 (`8b323a9`); stale references
  remain only in test docstrings. CLEAN-02's remaining work is the guard **mechanism** (D-14) plus
  the broader sweep (D-12), not re-deleting the placeholder.
- Failure-message examples the maintainer will see (D-08): `Phase 65 passed but VER-03 [ ] unmarked`;
  `table Status 'Complete' ≠ checkbox [ ] for VER-03`.

</specifics>

<deferred>
## Deferred Ideas

- **Rewriting coverage/CI tooling or a full monorepo split** — explicitly out of scope per the
  milestone's "Out of Scope" table; the gate is a job-level/pytest addition only.
- **Multi-cloud backends (MCB-01..)** — the next named milestone (phase 67+), not this cleanup phase.

</deferred>

---

*Phase: 66-docs-drift-gate-dead-code-sweep*
*Context gathered: 2026-07-03*
