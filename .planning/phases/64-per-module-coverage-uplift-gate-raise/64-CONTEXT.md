# Phase 64: Per-Module Coverage Uplift & Gate Raise - Context

**Gathered:** 2026-07-02
**Status:** Ready for planning

<domain>
## Phase Boundary

Raise the worst-offender source modules to a per-module coverage floor with **behavior-asserting** tests, and lift the enforced coverage gate above today's 90.38% baseline — wired into CI so future regressions fail the build (COV-01, COV-02).

**Milestone constraint (hard):** No user-facing feature change, **no backend behavior change.** This phase is tests + CI/coverage configuration. The one sanctioned exception is *behavior-preserving testability seams* (see D-08), which must be provably behavior-neutral.

Priority targets (measured post-v7.0 reorg): `services/agent_liveness.py` 12.5%, `routers/shell.py` 39.7%, `services/pipeline.py` 65.5%, `routers/tracklists.py` / `routers/pipeline.py` ~69%, `main.py`, plus the 71–78% tail.

</domain>

<decisions>
## Implementation Decisions

### Gate enforcement model (COV-01 / COV-02 mechanism)
- **D-01:** **Two guardrails: global bump + per-module floor script.** Raise the global `fail_under` (COV-02) AND add a per-module floor check (COV-01) that fails CI if any tracked module drops below the floor. Both are enforced. Rationale: raising only the global number lets a single module silently rot as long as the aggregate stays high — which is exactly the failure this phase fixes.
- **D-02:** **Per-module floor is enforced by a small script**, since coverage.py has **no native per-file `fail_under`.** The script parses coverage data (e.g. `coverage json`) and fails on any tracked module below the floor. It runs in the **combine step** (where the authoritative combined `.coverage` exists — see Phase 63 D-02), delegated via a **`just` recipe** per project convention ([[feedback-workflows-use-just]]). Planner/researcher determine the exact script shape and whether an existing tool covers it, but the policy is: custom per-module check over the combined coverage data.
- **D-03:** **Single uniform floor** — every tracked module must clear one number. No per-module ratchet / recorded-baseline map (simpler to state and enforce; a module either passes or is explicitly exempted). Modules genuinely below the floor are raised by this phase's tests or exempted with written justification (see D-09).

### Target numbers (COV-02)
- **D-04:** **Uniform per-module floor = 85%.** Every tracked module ≥ 85% or explicitly exempted. This scopes the uplift: it forces the whole 71–78% tail up *and* the named worst offenders, consistent with "no module sits far below the global gate."
- **D-05:** **Global gate: set to the measured post-uplift overall minus a small (~1-point) margin.** Do the uplift, measure the achieved overall % (expected well above 93 given the 85% per-module floor pushes the tail up), then set `fail_under` to ~1 point below achieved. This ratchets hard above the 90.38% baseline while avoiding a brittle gate that blocks unrelated future PRs. **The exact number is pinned at execute time from the measured number — it must be strictly > 90.38 and target the low-90s-or-higher.** (User selected "93%+" then refined to achieved-minus-margin.)

### Uplift scope & depth (COV-01)
- **D-06:** **The 85% per-module floor IS the scope.** Every tracked module reaches 85% or is exempted — not just the ~6 named worst offenders. Prioritize v7.0-touched modules and the worst offenders first (agent_liveness, shell, pipeline, tracklists, routers/pipeline, main.py), then clear the 71–78% tail to the floor.
- **D-07:** **Behavior-asserting quality bar.** Every added test must assert an **observable outcome** — return value, DB/ORM state, HTTP status/response body, emitted log or side-effect. No "call it and assert no exception" coverage-padding. Reviewer/verifier flags padding as a defect.

### Hard-to-test modules & refactor policy
- **D-08:** **Behavior-preserving testability seams are allowed** despite the "no behavior change" milestone rule. Small refactors that add test seams — extract a pure function, inject a clock/dependency, split a loop body — are permitted **only when runtime behavior is provably unchanged.** The verifier/reviewer must confirm zero behavior delta (git-diff-level reasoning, as in prior phases). This lets the genuinely-hard modules (`agent_liveness.py`'s background asyncio heartbeat, `main.py`'s app bootstrap) be honestly tested rather than blanket-exempted.
- **D-09:** **Exemptions (`# pragma: no cover` / coverage `omit`) require written justification.** Any line/module excluded to hit the floor carries an inline comment explaining *why the code is genuinely untestable* (e.g. bootstrap wiring, `if __name__ == "__main__"`, unreachable background-loop scaffolding). Exemptions should be rare given seams are allowed (D-08). No quietly excluding testable code to reach the number.

### Claude's Discretion
- Exact per-module-floor script implementation (language/shape), the coverage data format it reads (`coverage json` vs. parsing `.coverage`), and whether any existing off-the-shelf tool satisfies it — as long as it enforces D-01/D-02/D-03 over the combined coverage.
- The exact `just` recipe name/signature for the per-module check (fold into `coverage-combine` vs. a separate `coverage-floor` recipe).
- The precise final `fail_under` number (D-05: measured achieved minus ~1 point).
- Which specific lines qualify for D-09 exemptions vs. D-08 seams — decided per module during execution, subject to reviewer confirmation.
- Per-test file organization within the Phase 63 bucket directories (`tests/<bucket>/`).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & roadmap
- `.planning/REQUIREMENTS.md` §COV-01, §COV-02 — the per-module-floor + gate-raise requirements and the milestone "no backend behavior change" framing.
- `.planning/ROADMAP.md` §"Phase 64" — goal + the priority worst-offender module list.
- `.planning/PROJECT.md` §"Current Milestone" — the 90.38% baseline number and per-module offender percentages (agent_liveness 12.5%, shell 39.7%, pipeline 65.5%, ~69% routers, 71–78% tail).

### Coverage & gate wiring (what this phase edits)
- `pyproject.toml` §`[tool.coverage.report]` (`fail_under = 85`, `precision = 2`, `show_missing`) and §`[tool.coverage.run]` (`source = ["phaze"]`, `omit = ["tests/*"]`, `relative_files = true`, `concurrency`) — the global gate + coverage config.
- `justfile` §`coverage-combine` (lines ~107–110: `coverage combine` → `coverage xml` → `coverage report --fail-under=85`) — **the single place the combined gate is enforced** (Phase 63). Also `test-bucket` (line ~102, uses `--cov-fail-under=0` per leg) and `test-cov` / `test-ci`.
- `.github/workflows/tests.yml` §`combine` job (lines ~114–154) — runs `just coverage-combine` then the single Codecov upload. Where the new per-module floor check lands in CI.

### Phase 63 decisions this phase depends on
- `.planning/phases/63-parallel-ci-code-change-gating/63-CONTEXT.md` — D-02 (two-stage coverage combine; gate enforced once on the combined number), D-05 (`tests/<bucket>/` directory layout), D-10 (`just`-delegation convention). Phase 64 raises the gate that Phase 63 built the trustworthy combined-coverage plumbing for.

### Reference / conventions
- `codecov.yml` (if present) — project/patch targets (precision 2, project auto+1%, patch 80%+5% per CLAUDE.md) — verify alignment when the global gate moves; Codecov targets are advisory, the CI hard gate is `just coverage-combine`.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`just coverage-combine`**: the existing combine-and-gate recipe. The per-module floor check (D-02) extends this seam — it already owns the combined `.coverage` and the `--fail-under` enforcement point.
- **`just test-bucket <name>`**: Phase 63 per-bucket runner (`--cov-fail-under=0`). New tests land in the `tests/<bucket>/` dirs and are picked up by the existing matrix automatically (Phase 63 D-05/D-06 partition guard enforces placement).
- **Phase 63 combine job in `tests.yml`**: the place to add a per-module-floor CI step (after combine, before/around the gate).

### Established Patterns
- **Gate enforced once on the combined number** (Phase 63 D-02): per-module floor must likewise run against the combined coverage, not per-bucket shards.
- **`# pragma: no cover` / coverage `omit`** already used for `tests/*` omit; D-09 extends this pattern with justification comments.
- **Verifier confirms "logic unchanged" via git diff** (used in Phase 60/62): the mechanism for enforcing D-08's behavior-neutral seams.

### Integration Points
- Two edit sites for the gate raise (D-05): `pyproject.toml [tool.coverage.report] fail_under` and `justfile coverage-combine` (`coverage report --fail-under=...`). Keep them consistent.
- Test files live under `tests/<bucket>/` (Phase 63) — new tests must respect the bucket partition guard.

</code_context>

<specifics>
## Specific Ideas

- User consistently chose the **thorough / strongest-guarantee** option at every fork: two guardrails (not one), 85% per-module floor (the demanding end), 93%+ global, behavior-asserting tests with justified-only exemptions. Downstream agents should bias toward rigor over minimal-effort-to-green.
- The 85% floor + 93%+ global together mean **substantial new test volume** — expect this to be a multi-plan phase (likely partitioned by module cluster / bucket).

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope. (Codecov project/patch target retuning was raised as a possible follow-up but is advisory-only and folded into D-05's "verify alignment" note, not a separate deliverable.)

</deferred>

---

*Phase: 64-Per-Module Coverage Uplift & Gate Raise*
*Context gathered: 2026-07-02*
