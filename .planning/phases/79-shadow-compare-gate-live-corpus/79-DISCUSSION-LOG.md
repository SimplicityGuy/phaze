# Phase 79: Shadow-Compare Gate (live corpus) - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-08
**Phase:** 79-shadow-compare-gate-live-corpus
**Areas discussed:** Check form + live-run, Derived-side source, Invariant scope, Divergence output & fail

---

## Check form / harness

| Option | Description | Selected |
|--------|-------------|----------|
| Both: shared core + two entry points | One assertion core; hermetic pytest over a fixture corpus (integration bucket, standing CI gate) + a thin CLI/`just` entry running the same core against any DB. No logic duplicated. | ✓ |
| Pytest only (fixture corpus) | Just a pytest over a fixture corpus; no first-class live-DB path. | |
| CLI/script only (against a DB) | A runnable script/`just` recipe against any DB; CI then needs a seeded fixture DB. | |

**User's choice:** Both: shared core + two entry points
**Notes:** Phases 80–90 each require the gate to "stay green," so a CI-runnable form is mandatory; SC-3 requires a live-corpus restore run, so a real-DB entry point is mandatory too.

## Live-run: now vs deferred

| Option | Description | Selected |
|--------|-------------|----------|
| Defer live run to homelab rollout | Ship check + hermetic tests green now; record the 200K-restore run in VERIFICATION at the next rollout. | ✓ |
| Run against a live-corpus restore now | Restore the live corpus into `:5433`, run the gate, capture output in VERIFICATION now. | |

**User's choice:** Defer live run to homelab rollout
**Notes:** Consistent with other deployment-gated UAT items in the project. The gate stays a hard precondition for `033` (phase 90) regardless.

---

## Derived-side source

| Option | Description | Selected |
|--------|-------------|----------|
| Reuse Phase 78 predicates | Build the derived side from `services/stage_status.py` / `enums/stage.py`; the gate also guards the derivation layer. | ✓ |
| Raw output-table columns, independent | Hand-assert raw columns with SQL written independently of Phase 78; zero circularity but duplicates logic and doesn't guard derivation. | |
| Both sides asserted | Assert raw columns AND that stage_status agrees; largely redundant with Phase 78's DERIV-04 equivalence test. | |

**User's choice:** Reuse Phase 78 predicates
**Notes:** Accepted the residual circularity — completion states (ANALYZED/METADATA/PROPOSAL/apply) derive from pre-existing output rows, so the gate still catches real drift; and reuse makes the gate double as a derivation-layer guard for phases 80–90.

---

## Invariant scope

| Option | Description | Selected |
|--------|-------------|----------|
| Comprehensive: every FileState | Assert an implication per every §6.1 state, incl. the no-backfill completion states. | ✓ |
| Risky/backfilled subset only | Only the 6 the design lists explicitly. | |

**User's choice:** Comprehensive: every FileState
**Notes:** This is *the* gate before dropping the column — a completion state with no backing row is exactly the drift worth catching.

---

## Divergence output & fail semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Per-invariant summary + sample IDs, nonzero exit | Per-invariant count + capped sample file_ids; nonzero exit / pytest fail on hard-fail; `--verbose` full dump. | ✓ |
| Full divergence dump | Emit every divergent row. Noisy at 200K scale. | |
| Count-only pass/fail | Per-invariant counts + pass/fail, no file_ids. | |

**User's choice:** Per-invariant summary + sample IDs, nonzero exit

## Soft-case classification

| Option | Description | Selected |
|--------|-------------|----------|
| Explicit allowlist, reported not failed | FINGERPRINTED + LOCAL_ANALYZING code-commented allowlist (§6.1); counted + printed as "expected divergence", never flip exit; every other divergence hard-fails. | ✓ |
| FINGERPRINTED only; LOCAL_ANALYZING hard-fails | Only whitelist FINGERPRINTED; treat LOCAL_ANALYZING as a real invariant. Risks a spurious fail. | |
| You decide during planning | Leave LOCAL_ANALYZING to the researcher/planner. | |

**User's choice:** Explicit allowlist, reported not failed
**Notes:** Left a research follow-up to confirm LOCAL_ANALYZING's real writer behavior against `routers/backends`/push code before locking the allowlist entry.

---

## Claude's Discretion

- Exact fixture-corpus construction, the internal signature/shape of the shared assertion core, the precise `just`/CLI invocation surface, and the sample-cap number.
- Verify `LOCAL_ANALYZING`'s real writer behavior during research to confirm the D-06 allowlist entry.

## Deferred Ideas

- Live 200K-corpus restore run + VERIFICATION evidence — deferred to the next homelab rollout (D-02).
- Cloud-push lane drain (`--profile drain`) quiesce before `033` — belongs to Phase 90's rollout runbook.
