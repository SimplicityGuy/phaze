# Phase 49: Duration routing & backfill - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-25
**Phase:** 49-duration-routing-backfill
**Areas discussed:** Awaiting cloud state, Unknown-duration routing, Backfill workflow, Routing & agent selection

---

## Awaiting cloud state

### Q1 — Data-model representation of a held ≥threshold file

| Option | Description | Selected |
|--------|-------------|----------|
| New FileState AWAITING_CLOUD | Explicit, durable, badge-able; String(30) so no migration; must wire into recovery predicate | ✓ |
| Derived (no new state) | Computed query, no migration; less explicit — nothing on the row says "held" | |
| You decide | Defer to codebase conventions | |

**User's choice:** New FileState AWAITING_CLOUD

### Q2 — How held files get released when a compute agent comes online

| Option | Description | Selected |
|--------|-------------|----------|
| Reenqueue cron picks them up | Extend Phase 32/45 controller path (startup + */5) to route AWAITING_CLOUD to compute queue; self-healing | ✓ |
| Operator re-clicks Run analysis | Fully manual; matches DAG-as-single-surface theme | |
| Release on agent heartbeat/registration | Lowest latency; new trigger coupled to liveness path | |

**User's choice:** Reenqueue cron picks them up

### Q3 — Operator visibility

| Option | Description | Selected |
|--------|-------------|----------|
| Count card (like analysis_failed) | Reuses _safe_count + card pattern; drill-down deferred | ✓ |
| Count card + DAG chip | Card plus chip on analyze DAG node; more wiring | |
| You decide | Defer to dashboard conventions | |

**User's choice:** Count card (like analysis_failed)

---

## Unknown-duration routing

### Q1 — What happens to a file with null metadata.duration at routing time

| Option | Description | Selected |
|--------|-------------|----------|
| Route local (treat as short) | Long sets reliably carry tag durations; null ≈ normal short track; no regression vs pre-49 | ✓ |
| Hold as AWAITING_CLOUD | Conservative; risks stranding short files awaiting a never-online agent | |
| Gate analyze on metadata first | Cleanest signal; changes ordering, strands tag-less files | |

**User's choice:** Route local (treat as short)

**Notes:** Threshold knob (`cloud_route_threshold_sec`, default 5400s, `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC`) locked as Claude's discretion — convention match to `straggler_threshold_sec`. Not asked.

---

## Backfill workflow

### Q1 — How the operator triggers the 144-file backfill

| Option | Description | Selected |
|--------|-------------|----------|
| Pipeline dashboard button | Next to Recover/trigger controls; count-confirmed partial | ✓ |
| CLI command | Deliberate one-shot; safer but less discoverable | |
| Dashboard button + dry-run count | Preview before enqueue | |

**User's choice:** Pipeline dashboard button

### Q2 — How the 144 ANALYSIS_FAILED files re-enter the pipeline

| Option | Description | Selected |
|--------|-------------|----------|
| Reset + route through same path | Select ANALYSIS_FAILED ∧ duration≥threshold → reset DISCOVERED → seed ledger → duration-aware router (compute or AWAITING_CLOUD); dedup guards double-click | ✓ |
| Require compute agent online | No-op with message if none online; never produces held bucket | |
| You decide | Defer | |

**User's choice:** Reset + route through same path

---

## Routing & agent selection

### Q1 — How the per-file split is reported by "Run analysis"

| Option | Description | Selected |
|--------|-------------|----------|
| Split counts in response | "Enqueued 50 local, 12 cloud, 5 awaiting cloud" | ✓ |
| Single total (current behavior) | Keep "Enqueued N files"; cards surface buckets | |
| You decide | Defer | |

**User's choice:** Split counts in response

### Q2 — Kind-aware agent selection

| Option | Description | Selected |
|--------|-------------|----------|
| Most-recent-seen, kind-filtered | long→most-recent kind='compute', short→most-recent kind='fileserver'; round-robin deferred | ✓ |
| Compute kind-filtered, local unchanged | Smaller diff; compute agent could wrongly win short-file selection | |
| You decide | Defer | |

**User's choice:** Most-recent-seen, kind-filtered

---

## Claude's Discretion

- Threshold config knob `cloud_route_threshold_sec` (default 5400s = 90 min, `PHAZE_CLOUD_ROUTE_THRESHOLD_SEC`), mirroring `straggler_threshold_sec`.
- Exact wiring of kind-filtered selection and the per-file routing loop location.
- Backfill response-partial copy and count formatting.

## Deferred Ideas

- CLOUDROUTE-05 cost/throughput-aware routing — out of scope this milestone.
- Round-robin / least-loaded dispatch among multiple compute agents.
- Click-through drill-down list for the "Awaiting cloud" count card.
- Backfill dry-run/preview count (offered, not chosen — explicit filter + dedup already guard over-enqueue).
