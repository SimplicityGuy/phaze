# Requirements: Phaze — 2026.7.2 Multi-Compute Agents

**Defined:** 2026-07-05
**Core Value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata in Postgres — human-in-the-loop approval so nothing moves without review. Files stay on file-server agents; decisions stay on the application server.

**Milestone goal:** Finish the 2026.7.1 registry's deliberate compute-side descope — make **N cloud-compute agents** dispatch / route / reconcile / fail-isolate simultaneously, exactly as N Kueue clusters do today (the direct compute-side twin of Phase 70's multi-Kueue work). Retire the `≤1-compute invariant`. **Parity only** — no new routing semantics, no provisioning. Zero new dependencies expected.

## v1 Requirements

### Multi-Compute Agents (MCOMP)

- [x] **MCOMP-01**: Operator can declare **N `compute` backends** in `backends.toml`, each bound to a specific registered compute Agent, and all N are accepted at boot — the `≤1-compute` fail-fasts (`config.active_compute_scratch_dir`, `services/backends.resolved_non_local_kind`) are retired and generalized for a `local + N-Kueue + N-compute` registry.
- [ ] **MCOMP-02**: Each compute backend probes **its own bound agent's** liveness; an offline agent makes only *that* backend unavailable (the file holds or spills to the next eligible backend — it never dispatches to a dead agent). Replaces `ComputeAgentBackend.is_available`'s `select_active_agent(kind="compute")` "the single active compute agent" assumption.
- [ ] **MCOMP-03**: A file dispatched to a specific compute backend is pushed to **that agent's** host/scratch destination — the push pipeline (`_enqueue_push_file` → fileserver → rsync) and the `/pushed` callback (`routers/agent_push.py`) resolve the destination per-agent, not from a single global `active_compute_scratch_dir`.
- [ ] **MCOMP-04**: The tiered drain scheduler spreads long files across N compute agents by **rank** (free arm64 preferred over paid/trial x86) and **per-agent `cap`**, spilling to the next-eligible backend when one is at cap or offline. Reuses the Phase-69 rank/cap `select_backend` policy — no capability-matching.
- [ ] **MCOMP-05**: One flaky or offline compute agent is **isolated** — it degrades to 0 slots without failing the drain tick or blocking dispatch to healthy compute agents (per-backend snapshot try/except, mirroring the Phase 70 MKUE-03 pattern).
- [ ] **MCOMP-06**: Each compute backend's **in-flight count and terminalization** (the `/pushed` + `/api/internal/agent/*` reconcile path) are scoped to that backend/agent, so a file's result is attributed to the agent that analyzed it — no cross-agent mis-attribution. Resolves the open question of whether `cloud_job` stays one-row-per-file or needs per-(file,backend).
- [ ] **MCOMP-07**: The operator runbook + config docs cover **adding a 2nd+ compute agent** and the mixed arm64/x86 rank/cap cost-tiering; each compute agent renders as its own lane in the N-lane UI (verify the Phase-71 BEUI generalization already covers compute lanes; fix if a gap surfaces).

## v2 Requirements

Deferred to a future milestone. Tracked, not in this roadmap.

### Compute Provisioning & Capability Routing (PROV)

- **PROV-02**: Capability-aware routing — route specific files to specific compute agents by capability (arch/label/tag matched against file attributes) rather than pure rank/cap load-spread.
- **PROV-03**: On-demand provisioning — spin compute agents up/down instead of static operator-seeded registration.

## Out of Scope

Explicitly excluded for 2026.7.2. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Capability-aware / arch-matched routing | Parity-only milestone; any compute agent analyzes any file (essentia runs on both arm64 and x86), so rank/cap load-spread is sufficient. Cost-tiering is handled by operator `rank`, not a capability policy. Deferred as PROV-02. |
| Compute-agent provisioning / autoscaling | The whole `backends.toml` registry was designed static (no provisioning) in 2026.7.1; introducing a lifecycle contradicts that. Deferred as PROV-03. |
| New routing semantics beyond rank/cap | The Phase-69 tiered scheduler already delivers rank-first eligible dispatch + per-backend cap + spill; this milestone extends its *reach* to N compute agents, not its *policy*. |
| Kueue-side changes | N-Kueue-cluster dispatch already shipped (Phase 70). This milestone is compute-side only. |
| `2026.7.1` release PR + tag | A separate ship step (bump `pyproject`/`uv.lock` → push the `2026.7.1` tag), not milestone scope. |

## Traceability

Which phases cover which requirements. Populated during roadmap creation (phases continue from 71 → start at Phase 72).

| Requirement | Phase | Status |
|-------------|-------|--------|
| MCOMP-01 | Phase 72 | Complete |
| MCOMP-02 | Phase 73 | Pending |
| MCOMP-03 | Phase 73 | Pending |
| MCOMP-04 | Phase 73 | Pending |
| MCOMP-05 | Phase 73 | Pending |
| MCOMP-06 | Phase 73 | Pending |
| MCOMP-07 | Phase 74 | Pending |

**Coverage:**
- v1 requirements: 7 total
- Mapped to phases: 7 ✓ (MCOMP-01→72 · MCOMP-02..06→73 · MCOMP-07→74)
- Unmapped: 0 ✓ (no orphans, no duplicates)

---
*Requirements defined: 2026-07-05*
*Last updated: 2026-07-05 after roadmap creation (Phases 72-74 mapped)*
