# Phase 73: Per-Agent Dispatch, Liveness, Scratch & Failure Isolation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-05
**Phase:** 73-per-agent-dispatch-liveness-scratch-failure-isolation
**Areas discussed:** Push destination resolution, cloud_job cardinality, /pushed reconcile attribution, Failure isolation & rank/cap

---

## Area selection

All four presented gray areas were selected for discussion. Framing given upfront: the milestone
is parity-only ("reuse Phase 70 verbatim"), so several decisions are near-predetermined by
precedent — the user chose to lock each explicitly.

---

## Push destination resolution (MCOMP-03)

### Q1 — Destination source

| Option | Description | Selected |
|--------|-------------|----------|
| backends.toml ComputeBackend | Add push host (+ssh_user) to the entry; control resolves from cloud_job.backend_id and stamps into payload; single registry. | ✓ |
| Fileserver agent_id→dest map | Agent-side host/scratch map; control stamps only backend_id/agent_ref; splits registry across two surfaces. | |
| Agent record (DB) | Resolve host from the registered Agent row; no new field but couples routing to dynamic check-in data. | |

**User's choice:** backends.toml ComputeBackend (Recommended)
**Notes:** Keeps backends.toml the single registry; control-authoritative, record-don't-rederive.

### Q2 — Control-carried destination vs agent-side SSH credentials

| Option | Description | Selected |
|--------|-------------|----------|
| Host+scratch in payload, key+known_hosts agent-side | Payload gains host+scratch_dir (per-backend); SSH key+known_hosts stay on the fileserver (known_hosts pins all N hosts); retire the single push_ssh_host/cloud_scratch_dir env. | ✓ |
| Everything per-backend in backends.toml | Move host/scratch/ssh_user AND key/known_hosts refs into control config; weaker secret hygiene. | |
| Keep agent env as fallback | Payload authoritative when present, keep agent env as ≤1 fallback; two code paths + lingering ≤1 assumption. | |

**User's choice:** Host+scratch in payload, key+known_hosts agent-side (Recommended)
**Notes:** ssh_user travels with the destination in the payload (defaults to the agent's configured
user if a backend omits it) — captured as a planning detail. Retires the fileserver's single
push_ssh_host/cloud_scratch_dir remote-target env.

---

## cloud_job cardinality (MCOMP-06 — plan-phase research flag)

| Option | Description | Selected |
|--------|-------------|----------|
| Stay one-row-per-file, keyed by backend_id | Keep unique(file_id); backend_id = current target; spill re-stamps via upsert; mirrors Phase 70; no migration. | ✓ |
| Per-(file, backend) rows | Drop unique(file_id), one row per attempt; adds migration + complicates in_flight/terminalization; diverges from precedent. | |

**User's choice:** Stay one-row-per-file, keyed by backend_id (Recommended)
**Notes:** Resolves the roadmap research flag — a file is only ever in-flight to one backend at a
time; attribution derives from the recorded backend_id.

---

## /pushed reconcile attribution (MCOMP-06)

| Option | Description | Selected |
|--------|-------------|----------|
| Validate token agent == dispatched backend's agent_ref; reject on mismatch | Resolve backend_id→agent_ref; reject (4xx) + don't terminalize if the reporter doesn't match. | ✓ |
| Trust recorded backend_id, don't validate the reporter | Resolve scratch/terminalization purely from backend_id, ignore who reported. | |
| Trust the reporting token, re-stamp backend_id | Treat token agent as authoritative and re-attribute; inverts record-don't-rederive. | |

**User's choice:** Validate token agent == dispatched backend's agent_ref; reject on mismatch (Recommended)
**Notes:** Strong "no cross-agent mis-attribution" guarantee; scratch + terminalization key off the
recorded backend_id; never re-stamp from the reporting token.

---

## Failure isolation & rank/cap (MCOMP-04/05)

| Option | Description | Selected |
|--------|-------------|----------|
| Pure verbatim reuse + regression proof | Phase-69 select_backend (rank+cap+spill) + Phase-70 snapshot try/except apply as-is; add N-compute-spread + one-flaky isolation tests; no new scheduler code. | ✓ |
| Compute-specific tweak | A compute-only spill/health/preference behavior; would push beyond strict parity. | |

**User's choice:** Pure verbatim reuse + regression proof (Recommended)
**Notes:** Free arm64 = lower rank, paid x86 = higher rank (operator config, no capability-matching).
Cost-tiering guidance is Phase 74 docs.

---

## Claude's Discretion

- PushFilePayload field shape (flat fields vs nested destination submodel) + validators for the new
  host/scratch fields.
- ssh_user placement (payload vs agent default).
- ComputeBackend host field naming (push_host / host / ssh_host) — follow closest Phase-67/68 idiom.
- Whether config.py `active_compute_scratch_dir` @property is deleted outright once its last reader
  (agent_push.py) is removed.

## Deferred Ideas

None — discussion stayed within phase scope. (N-lane compute UI + runbook + arm64/x86 cost-tiering
docs → Phase 74; capability routing → PROV-02; provisioning → PROV-03; all tracked in
REQUIREMENTS.md.)
