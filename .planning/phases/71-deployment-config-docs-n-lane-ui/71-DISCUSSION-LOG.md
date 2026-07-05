# Phase 71: Deployment, Config, Docs & N-Lane UI - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-04
**Phase:** 71-deployment-config-docs-n-lane-ui
**Areas discussed:** Lane data plumbing, N-lane card layout, Master toggle, Docs + cloud_target deprecation

---

## Lane data plumbing (BEUI-01)

### Data source
| Option | Description | Selected |
|--------|-------------|----------|
| New read-only snapshot service | `get_backend_lane_snapshot(session)`: registry + `cloud_job` COUNT per `backend_id`, degrade-safe, off the drain path | ✓ |
| Reuse drain BackendSlot | Surface the transient in-tick dispatch snapshot to the UI | |

### Availability signal
| Option | Description | Selected |
|--------|-------------|----------|
| Cached signals, no live probe | Derive from agent counts / Redis keys / config | |
| Live `is_available()` probe per poll | Probe each backend on every poll — freshest signal | ✓ |

### Probe safety (follow-up)
| Option | Description | Selected |
|--------|-------------|----------|
| Per-backend timeout + isolation, concurrent | `asyncio.gather`, short timeout + try/except each; hung cluster → offline that poll, fan-out bounded to ~one timeout | ✓ |
| Sequential probes, per-backend guarded | One at a time; total latency = sum of timeouts | |

### Per-lane state attribution
| Option | Description | Selected |
|--------|-------------|----------|
| Snapshot service returns per-lane counts too | GROUP BY `backend_id` for admission/inadmissible/localqueue; each card carries own quota-wait-vs-Inadmissible | ✓ |
| Keep 6 global cards, add per-lane badge only | Smaller per-backend query for a lightweight badge | |

**User's choice:** New snapshot service + live-per-poll probes bounded by concurrent per-backend timeout/isolation + per-lane admission counts from the same read.
**Notes:** Operator chose freshness (live probes) over cached signals despite the latency tradeoff; the bounded-timeout/isolation mitigation makes it safe on the shared poll.

---

## N-lane card layout (BEUI-01)

### Layout
| Option | Description | Selected |
|--------|-------------|----------|
| Responsive auto-fit grid | Wraps at breakpoints; card size unchanged | ✓ |
| Single horizontal scroll row | Compact but hides lanes off-screen | |
| You decide | Planner/UI-phase picks classes | |

### Ordering
| Option | Description | Selected |
|--------|-------------|----------|
| By rank ascending (preferred first) | Local rank-99 last; mirrors scheduler order; tie-break by id | ✓ |
| Local first, then by rank | Pin local as card 1 | |

### Fate of the 6 global cloud-state cards
| Option | Description | Selected |
|--------|-------------|----------|
| Keep as global roll-up below grid | No restyle, OOB ids untouched, preserves cross-lane total | |
| Fold entirely into lanes, remove globals | Cleanest but higher-risk, touches WORK-03 alert cards | |
| You decide | Planner/UI-phase weighs it | ✓ |

**User's choice:** Responsive auto-fit grid; rank-ascending order; 6-card fate deferred to Claude/UI-phase.
**Notes:** Claude's lean recorded in CONTEXT (D-07): keep the 6 as a global roll-up below the grid (lowest risk).

---

## Master toggle (BEUI-02)

### Toggle type
| Option | Description | Selected |
|--------|-------------|----------|
| Live runtime toggle (one-click) | Immediately forces all routing local, no redeploy, reversible | ✓ |
| Config-only, documented | Re-express `cloud_enabled` gate + document restart procedure | |

### Persistence
| Option | Description | Selected |
|--------|-------------|----------|
| DB control row (mirror pause table) | Persist in a control row like per-stage pause/priority; read by router/drain | ✓ |
| Redis flag | Fast cross-process key, but adds Redis dep to routing decision | |
| You decide | Planner picks | |

### Location
| Option | Description | Selected |
|--------|-------------|----------|
| Header status strip (global) | Always visible, one click from anywhere mid-incident | ✓ |
| Analyze workspace (near lanes) | Co-located but single-stage only | |
| Agents / Compute page | Ops mental model but least glanceable | |

**User's choice:** Live one-click runtime toggle, persisted in a DB control row, placed in the global header status strip.
**Notes:** Adds one force-local gate read to the routing path — that read IS the requirement (BEUI-02); routing algorithm/ranks/caps otherwise unchanged. New thin write endpoint.

---

## Docs + cloud_target deprecation (BEUI-03)

> First pass was rejected for clarification. User clarified: **nobody used k8s/cloud burst yet — there is no config to migrate.** Questions were reformulated around that (migration guide collapses to a trivial 1:1 note).

### Docs shape
| Option | Description | Selected |
|--------|-------------|----------|
| New runbook + update configuration.md | New `docs/runbook.md` (toggle/incident/per-lane/spillover/`_FILE` secrets) + `configuration.md` schema + short deprecation note | ✓ |
| Fold into configuration.md only | One expanded file, no separate runbook | |
| You decide | Planner picks layout | |

### Deprecation policy
| Option | Description | Selected |
|--------|-------------|----------|
| Docs-only deprecation | Mark deprecated in docs, shim silent, no removal date | |
| Docs + a one-line deprecation log | Same + startup log-warning when `cloud_target` is set | ✓ |
| Keep indefinitely, no mention | Permanent silent alias | |

**User's choice:** New runbook + configuration.md update; deprecation = docs + a one-line startup deprecation log; no migration guide, no removal date.
**Notes:** Operator: "just deprecate; no one used the k8s or cloud burst yet; no config to migrate." The requirement's migration path collapses to a 1:1 equivalence note for completeness only.

---

## Claude's Discretion

- Fate of the 6 global cloud-state cards (lean: keep as roll-up below the grid).
- Exact responsive grid Tailwind classes + rank presentation within the C3 two-weight contract.
- `is_available()` probe timeout value + concurrency mechanics; local-probe short-circuit.
- Master toggle confirm-vs-instant UX (reversible control — instant-on defensible).
- DB control-row shape/name for the force-local override (follow the pause/priority schema).
- `docs/runbook.md` vs `configuration.md` exact section split.

## Deferred Ideas

- PROV-01: new concrete cloud providers + the `ComputeAgentBackend` `agent_ref → Agent.id` fix (Future Requirements, from Phase 70 D-05).
- Folding the 6 global cards fully into per-lane cards (higher-risk; future UI polish).
- `cloud_target` shim removal (no date this phase; future milestone if friction arises).
