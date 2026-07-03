# Phase 65: CalVer Adoption - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-02
**Phase:** 65-CalVer Adoption
**Areas discussed:** Tag `v` prefix, REVISION semantics, Milestone↔version story, Historical-string boundary, CI publish trigger, Publish invariant

---

## Tag `v` prefix

| Option | Description | Selected |
|--------|-------------|----------|
| Keep `v` → `v2026.7.0` | Git tag stays `v2026.7.0`; CI machinery (trigger `v*.*.*`, `type=ref,event=tag`, guard test) untouched; lowest risk. | |
| Drop `v` → `2026.7.0` | Bare tag matching calver.org + the requirement text literally; forces ci.yml trigger + guard-test rewrites. | ✓ |

**User's choice:** Drop `v` → `2026.7.0`
**Notes:** Higher-surface option chosen deliberately. Drives the CI-trigger and guard-test follow-ups below.

---

## REVISION semantics

| Option | Description | Selected |
|--------|-------------|----------|
| Per-month, zero-based counter | Nth release within `YYYY.MM` starting at 0; resets each month; milestone name decoupled from number. | ✓ |
| Per-month, one-based counter | Same reset behavior but first = `.1`; conflicts with required `2026.7.0`. | |
| Global monotonic counter | Never resets; lifetime index; diverges from standard CalVer. | |

**User's choice:** Per-month, zero-based counter (Recommended)
**Notes:** Supports the prior v4.0.x same-month patch cadence (VER-01).

---

## Milestone↔version story (VER-04)

| Option | Description | Selected |
|--------|-------------|----------|
| Named milestones + mapping table | Pure-named milestones going forward; MILESTONES.md table (Milestone \| Version \| Date); historical `vN.M` kept verbatim; current milestone referenced by name, releases as 2026.7.0. | ✓ |
| Keep compound name for this one | Leave "2026.7.0 Engineering Improvements" as-is; pure-named only from next milestone. | |
| Rename current milestone to pure name | Actively rename across all docs now; cleanest end-state, most churn. | |

**User's choice:** Named milestones + mapping table (Recommended)
**Notes:** No retro-rename churn; the "named + table" presentation carries VER-04.

---

## Historical-string boundary

| Option | Description | Selected |
|--------|-------------|----------|
| Forward-looking procedure only | Rewrite only next-release instruction/example text; leave all historical record verbatim. Rule: instructs next release → update; records past event → leave. | ✓ |
| Every version string | Sweep all `vN.M`/`vN.M.P` project-wide; against VER-04 "historical record intact." | |
| Only the release procedure doc | Touch only canonical procedure + README badge; leaves live operator examples stale. | |

**User's choice:** Forward-looking procedure only (Recommended)
**Notes:** Consistent with VER-04's "historical record intact."

---

## CI publish trigger (follow-up to bare-tag choice)

| Option | Description | Selected |
|--------|-------------|----------|
| CalVer + keep legacy `v*.*.*` | Trigger matches both a CalVer glob and existing `v*.*.*`; future re-tag of a historical release still fires. | |
| CalVer-only | Replace `v*.*.*` outright with a CalVer glob; a future re-tag of a historical `vN.M.P` no longer publishes (acceptable — retro-tagging out of scope). | ✓ |

**User's choice:** CalVer-only
**Notes:** Single-scheme end-state; retro-tagging is explicitly out of scope so the dropped legacy path is acceptable.

---

## Publish invariant (follow-up to bare-tag choice)

| Option | Description | Selected |
|--------|-------------|----------|
| Preserve `:latest` + `:<calver>` | Tagged release publishes `:latest` (main) + version-pinnable `:2026.7.0` + `2026.7` month-rolling; update guard test to CalVer form. | ✓ |
| Let the planner decide the exact tag set | Lock only "a version-pinnable tag exists + guard stays green"; leave precise tag list to planning. | |

**User's choice:** Preserve `:latest` + `:<calver>` (Recommended)
**Notes:** Keeps `PHAZE_IMAGE_TAG` pinning + rollback working; guard test retargeted, not weakened.

---

## Claude's Discretion

- Exact CI trigger glob regex that matches `2026.7.0` and rejects noise (policy = CalVer-only is fixed).
- Precise columns/wording of the MILESTONES.md mapping table.
- Whether `pyproject.toml` `version` is bumped to `2026.7.0` within this phase vs at milestone-release time.

## Deferred Ideas

None — discussion stayed within phase scope. Retroactive re-tagging of historical `vN.M` releases is explicitly out of scope (ROADMAP notes), not a deferral.
