# Phase 64: Per-Module Coverage Uplift & Gate Raise - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-02
**Phase:** 64-Per-Module Coverage Uplift & Gate Raise
**Areas discussed:** Gate enforcement model, Target numbers, Uplift scope & depth, Hard-to-test modules & refactors

---

## Gate enforcement model — how to enforce a per-module floor

| Option | Description | Selected |
|--------|-------------|----------|
| Global bump + per-module script | Raise global fail_under AND add a script (parses `coverage json`) failing CI if any module drops below its floor. Two guardrails. | ✓ |
| Global bump only | Just raise global fail_under above 90.38%. Simplest, but a module can silently rot. | |
| Per-module floor only | Enforce only the script, keep global at 85. Lets overall drift. | |

**User's choice:** Global bump + per-module script
**Notes:** Two guardrails — overall can't slide AND no single module can rot.

## Gate enforcement model — floor policy shape

| Option | Description | Selected |
|--------|-------------|----------|
| Single uniform floor | Every tracked module clears one number. Simple pass/fail. | ✓ |
| Per-module ratchet (never-decrease) | Record each module's current % as its floor; fail on any regression. More config. | |
| Uniform floor + ratchet hybrid | Uniform minimum AND ratchet the already-high modules. Most protective, most moving parts. | |

**User's choice:** Single uniform floor

---

## Target numbers — global gate

| Option | Description | Selected |
|--------|-------------|----------|
| 91% | Modest safe bump just above baseline. Little headroom. | |
| 92% | Clear step up; requires uplift to move the overall number. | |
| 93%+ | Aggressive; demands substantial coverage across the tail. | ✓ |

**User's choice:** 93%+ (later refined — see "Exact global" below)

## Target numbers — uniform per-module floor

| Option | Description | Selected |
|--------|-------------|----------|
| 70% | Every module ≥ 70%. Targets true laggards, tolerates 71–78% tail. | |
| 80% | Every module ≥ 80%. Forces the whole tail up too. | |
| 85% | Every module ≥ 85% (matches old global gate). Very demanding; hard modules likely need exemptions. | ✓ |

**User's choice:** 85%
**Notes:** Couples floor to scope — nearly every under-85 module needs work or an exemption.

## Target numbers — exact global finalization

| Option | Description | Selected |
|--------|-------------|----------|
| Set to achieved minus small margin | Measure post-uplift overall, set fail_under ~1 pt below. Avoids brittleness while ratcheting hard above 90.38. | ✓ |
| Hard 93% exactly | Flat fail_under=93. Simple, predictable. | |
| Hard 95% | Flat fail_under=95. Very aggressive; brittleness risk. | |

**User's choice:** Set to achieved minus small margin

---

## Hard-to-test modules & refactors — refactor policy

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, behavior-preserving seams OK | Allow small refactors adding test seams (extract pure fn, inject clock/dep, split loop) with provably unchanged behavior; verifier confirms. | ✓ |
| Tests only, no source edits | Strictly no source changes; unreachable-85% modules exempted with justification. | |
| Tests only, but pragma allowed | No refactors, but liberal `# pragma: no cover` on untestable scaffolding. | |

**User's choice:** Yes, behavior-preserving seams OK
**Notes:** Verifier/reviewer must confirm zero behavior delta.

## Hard-to-test modules & refactors — quality bar / exemption discipline

| Option | Description | Selected |
|--------|-------------|----------|
| Assert behavior; exemptions need written why | Every test asserts an observable outcome; every pragma/omit carries an inline justification; reviewer flags padding. | ✓ |
| Assert behavior, exemptions freely allowed | Behavior-asserting bar, but exemptions without per-line justification. | |
| Coverage-first pragmatic | Prioritize numbers; smoke tests acceptable. Weakest guarantee. | |

**User's choice:** Assert behavior; exemptions need written why

---

## Claude's Discretion

- Exact per-module-floor script implementation and coverage data format; whether an off-the-shelf tool suffices.
- The `just` recipe name/signature for the per-module check.
- The precise final `fail_under` number (measured achieved minus ~1 point).
- Which lines qualify for pragma exemptions vs. testability seams — decided per module during execution, subject to reviewer confirmation.
- Test file organization within the Phase 63 bucket dirs.

## Deferred Ideas

None — discussion stayed within phase scope. Codecov project/patch target retuning noted as advisory follow-up (folded into the gate-raise decision, not a separate deliverable).
