# Phase 69: Tiered Drain Scheduler - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-04
**Phase:** 69-tiered-drain-scheduler
**Areas discussed:** Local spillover timing, Black-hole / hard-fail policy, Global concurrency ceiling, Failed-file re-dispatch target

---

## Local spillover timing (staleness guard)

| Option | Description | Selected |
|--------|-------------|----------|
| Immediate spill (design default) | File dispatches to local as soon as no higher-ranked backend has a free slot; local's small cap throttles it; no staleness logic | |
| Staleness threshold first | File must sit in AWAITING_CLOUD beyond a wait threshold before local becomes eligible, so a momentary cloud-full blip doesn't dump long files onto slow local | ✓ |

**User's choice:** Staleness threshold first (diverges from design default).

### Sub-decision: threshold source & tuning
| Option | Description | Selected |
|--------|-------------|----------|
| Operator-configurable, sensible default | Config knob (e.g. cloud_spill_to_local_after_seconds, ~15 min default), tunable without logic redeploy | ✓ |
| Fixed constant in code | Hardcoded, no knob | |
| Derived from file duration | Scale wait to file length | |

**User's choice:** Operator-configurable with sensible default.

### Sub-decision: wait scope (offline vs full)
| Option | Description | Selected |
|--------|-------------|----------|
| Only when FULL; offline spills at once | Guard rides out busy-backlog blips; genuine outage releases to local immediately | ✓ |
| Wait in both cases | Even during an outage the file waits the threshold before local | |

**User's choice:** Only when FULL; offline spills immediately.

---

## Black-hole / hard-fail policy

| Option | Description | Selected |
|--------|-------------|----------|
| Local is the safety net | Bounded per-backend attempts (reuse cloud_submit_max_attempts) stop thrash; exhausted file falls to local; ANALYSIS_FAILED only if local fails or a global attempt cap is hit | ✓ |
| Global attempt cap hard-fails regardless | After N total attempts incl. local, file → ANALYSIS_FAILED even if local is up | |

**User's choice:** Local is the safety net.

---

## Global concurrency ceiling

| Option | Description | Selected |
|--------|-------------|----------|
| Purely per-backend caps | Sum of caps is the only ceiling; retire global cloud_max_in_flight | ✓ |
| Keep a global master ceiling too | Retain a global cap on total concurrent cloud dispatch on top of per-backend caps | |

**User's choice:** Purely per-backend caps.

---

## Failed-file re-dispatch target

| Option | Description | Selected |
|--------|-------------|----------|
| Stateless re-rank | Next tick re-picks lowest-rank-available normally; per-backend attempt cap bounds thrash; no per-file failure memory | ✓ |
| Exclude the backend that just failed | Record failed backend_id and skip it for that file next dispatch | |

**User's choice:** Stateless re-rank.

---

## Claude's Discretion

- Exact new config field name + default for the staleness threshold.
- Whether the "waited-since" staleness signal reads an existing timestamp or needs a new one (least-invasive source; flagged as a research/plan-time open question).

## Deferred Ideas

- Duration-scaled staleness threshold — deferred as over-engineering.
- Per-file "avoid last-failed backend" memory — deferred (stateless re-rank chosen).
- Global master ceiling on total concurrent cloud dispatch — rejected in favor of purely per-backend caps.
