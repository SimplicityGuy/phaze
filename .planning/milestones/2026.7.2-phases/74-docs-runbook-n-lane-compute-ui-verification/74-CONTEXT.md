# Phase 74: Docs, Runbook & N-Lane Compute UI Verification - Context

**Gathered:** 2026-07-06
**Status:** Ready for planning

<domain>
## Phase Boundary

**MCOMP-07 — the LAST phase of the 2026.7.2 Multi-Compute Agents milestone.** Two deliverables, no new routing behavior:

1. **Operator docs** for **adding a 2nd+ compute agent** and the **mixed arm64/x86 rank/cap cost-tiering** the Phase 72/73 machinery now supports (declare N `compute` backends, each bound to a registered agent, ranked/capped, free arm64 preferred with spill to paid x86).
2. **Verify the Phase-71 BEUI N-lane UI** renders each compute agent as its own lane. Lanes are already registry-derived (`get_backend_lane_snapshot` loops one card per registry backend, rank-ascending — Phase 71 BEUI-01), so a code gap is unlikely; **fix only if a gap surfaces**, and lock the guarantee with a regression test.

**In scope:** new operator doc; generalize any single-compute-agent framing that blocks the 2nd-agent recipe; a worked multi-compute `backends.toml` example; a regression test asserting each compute backend renders its own lane; docs-drift/traceability bookkeeping for MCOMP-07.

**Out of scope:** any change to dispatch/routing/reconcile behavior (delivered in 72/73); capability-aware routing (PROV-02, v2 deferred); on-demand provisioning (PROV-03, v2 deferred); the milestone-close/release itself (`/gsd:complete-milestone` + tag — a separate step after this phase merges).

</domain>

<decisions>
## Implementation Decisions

### Doc home & structure
- **D-01:** The "add a 2nd+ compute agent" recipe lives in a **new dedicated doc** (e.g. `docs/multi-compute.md`), not folded into `cloud-burst.md` or `runbook.md`. `cloud-burst.md` stays the single-A1-agent provisioning walkthrough; the new doc is the "now do it N times, cost-tiered" operator guide. Cross-link from `cloud-burst.md`, `runbook.md`, `configuration.md § Backend registry`, and the docs index/README so it is reachable from the existing multi-cloud nav.

### Cost-tiering example (arm64 vs x86)
- **D-02:** Document a **real x86 compute agent as a deployable tier** — not illustrative, not the Kueue path. The worked example declares **two `compute` backends**: free arm64 (A1) at a **low rank** (preferred) and paid x86 at a **higher rank** (spill target), each with its own `agent_ref`, `scratch_dir`, and `cap`. Show the resulting rank-tiered drain (free arm64 fills first, spills to paid x86, then local rank 99 as final catch).
- **D-03:** Include a **worked `backends.toml`** for the mixed arm64/x86 registry plus a short **cost-tier rationale table** (which tier, why that rank, expected cost posture). Keep the canonical field reference in `configuration.md`; the new doc shows the *scenario*, not a re-stated field table.

### N-lane compute UI verification
- **D-04:** **Verify + add a regression test.** Confirm `get_backend_lane_snapshot` already emits one lane per `compute` backend (each bound agent = its own lane), then add a test asserting **each of N compute backends renders its own lane card** — parity with the Phase 70 MKUE test discipline and the ≥90% per-module coverage floor. Change UI/service code **only if verification surfaces an actual gap**.

### Multi-agent compose artifact
- **D-05:** **Parametrize the existing `docker-compose.cloud-agent.yml`** — document running it **once per compute agent** with distinct `AGENT_ID` / `PHAZE_AGENT_QUEUE` (`phaze-agent-<id>`) / scratch volume / SSH host / compose project-name per agent. **No new compose file.** Note the image-tag wrinkle (see research flag R-1): the arm64 agent pulls the `-arm64` tag; a real x86 agent must pull the **standard x86 tag** — the parametrization must cover the tag swap, not just env.

### Claude's Discretion
- Exact new-doc filename/slug, section order, and mermaid diagram choices — follow the existing docs style (`generated-by: gsd-doc-writer` header, mermaid over ASCII, rank/cap read-outs consistent with `runbook.md`).
- Precise wording of the cost-tier rationale table.
- Where the regression test file lives (candidate: `tests/analyze/services/test_backends.py` next to the existing `get_backend_lane_snapshot` tests).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirement & roadmap
- `.planning/REQUIREMENTS.md` § Multi-Compute Agents (MCOMP) — **MCOMP-07** is this phase; MCOMP-01..06 (72/73) are the shipped machinery this phase documents.
- `.planning/ROADMAP.md` § 2026.7.2 Multi-Compute Agents — Phase 74 line + milestone framing (parity, zero new deps).

### Docs to write / generalize / cross-link
- `docs/cloud-burst.md` — single-A1 `compute` agent provisioning (compose, `agent_ref`, arm64-only image, Tailscale, rank/cap). The **new multi-compute doc builds on this**; generalize any single-agent framing that blocks the 2nd-agent recipe.
- `docs/runbook.md` — the N-lane operator runbook (reading lanes, rank/cap, spillover, force-local). Already N-lane-aware; new doc cross-links here.
- `docs/configuration.md` § Backend registry (`backends.toml`) — canonical `[[backends]]` schema, `agent_ref` (REQUIRED on `compute`), `rank`, `cap`, `scratch_dir`, per-backend fail-fast. **Do not restate the field table** in the new doc; link to it.
- `docs/k8s-burst.md` — Kueue x86 path (context only; D-02 chose a real x86 *compute* agent over the Kueue path).
- `docs/arm64-agent-image.md` — `-arm64` tag naming / no-multi-arch-manifest (directly relevant to R-1).
- `docs/deployment.md` — compose topology (control plane + file server + cloud-agent).

### Design spine
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` — the registry/tiered-drain design (note REG-05 + revised MKUE-02/04 superseded its one-shared-bucket decision).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets (verification targets)
- `src/phaze/services/backends.py:675` `get_backend_lane_snapshot(session)` — the **registry-derived** per-backend lane source (`{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}`). Already loops every registry backend → each compute backend should already yield its own lane. **Primary verification target + regression-test seam.**
- `src/phaze/services/agent_liveness.py:144` `classify_compute_lanes(session)` — compute-lane state (ACTIVE/WAITING/IDLE), the DB-touching per-agent read.
- `src/phaze/templates/pipeline/partials/_analyze_lanes.html` + `_lane_card.html` — the server-rendered loop over the seeded `lanes` list (rank-ascending, offline word-labelled, OOB-swaps as a unit). No per-lane `$store.pipeline` keys.
- `src/phaze/routers/pipeline.py:563` and `:661` — both stats endpoints seed `lanes = await get_backend_lane_snapshot(session)` **identically** so the whole `#analyze-lanes` grid OOB-swaps together.

### Established Patterns
- Docs carry the `generated-by: gsd-doc-writer` marker on line 1, use mermaid diagrams, and keep the canonical field reference in `configuration.md` (feature docs show scenarios, not restated tables).
- Test discipline: parity regression tests (Phase 70 MKUE pattern) + ≥90% per-module coverage floor.
- Phase 66 shipped a `just docs-drift` traceability guard that cross-checks REQUIREMENTS.md ↔ passed phases — ensure MCOMP-07's checkbox/traceability stays green (it caught a stale checkbox on first run before).

### Integration Points
- `docker-compose.cloud-agent.yml` — the single-agent compose to **parametrize** per agent (distinct `AGENT_ID`/`PHAZE_AGENT_QUEUE`/scratch/host/project-name; standard x86 tag for the x86 agent).
- `src/phaze/config.py` / `config_backends.py` — compute backend + agent settings (`agent_ref`, `scratch_dir`, cost-tier `rank`/`cap`).

</code_context>

<specifics>
## Specific Ideas

- Worked example is a **mixed arm64/x86** registry: free arm64 A1 at low rank (preferred), paid x86 at higher rank (spill), local rank 99 (final catch) — show the drain walking that rank order.
- Reuse the `runbook.md` lane-reading vocabulary (RANK {n}, {in_flight}/{cap}, offline word + glyph) so the new doc and the UI read-out speak the same language.

## Open Research Questions (for gsd-phase-researcher / planner)

- **R-1 (x86 compute-agent image path):** The cloud-agent compose pulls the **`-arm64`** tag, and the arm64 image is arm64-only (no multi-arch manifest). A **real x86 compute agent** (D-02) must run the **standard x86 image** as a `kind=compute` worker. Confirm: (a) the standard x86 image runs a media-less `kind=compute` agent that drains its per-agent queue + PUTs results (parity with the arm64 agent), and (b) the compose parametrization cleanly swaps the image tag per agent. If the x86 compute path is not actually deployable, the worked example must be adjusted before the docs commit to it.
- **R-2 (UI verification):** Confirm `get_backend_lane_snapshot` already renders one lane per compute backend for N≥2 compute backends (no dedupe/collapse on `kind`), and identify the best home for the "each compute backend = own lane" regression test.

</specifics>

<deferred>
## Deferred Ideas

- **Milestone close + release** — `/gsd:complete-milestone 2026.7.2` + the CalVer release tag are a **separate step after this phase's PR merges**, not part of Phase 74.
- **PROV-02 (capability-aware routing)** and **PROV-03 (on-demand provisioning)** — explicitly v2-deferred at milestone scoping; out of this parity milestone.

None of the above are new scope for Phase 74.

</deferred>

---

*Phase: 74-docs-runbook-n-lane-compute-ui-verification*
*Context gathered: 2026-07-06*
