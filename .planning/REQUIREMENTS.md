# Requirements: Phaze — 2026.7.2 Multi-Compute Agents

**Defined:** 2026-07-05
**Core Value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata in Postgres — human-in-the-loop approval so nothing moves without review. Files stay on file-server agents; decisions stay on the application server.

**Milestone goal:** Finish the 2026.7.1 registry's deliberate compute-side descope — make **N cloud-compute agents** dispatch / route / reconcile / fail-isolate simultaneously, exactly as N Kueue clusters do today (the direct compute-side twin of Phase 70's multi-Kueue work). Retire the `≤1-compute invariant`. **Parity only** — no new routing semantics, no provisioning. Zero new dependencies expected.

## v1 Requirements

### Multi-Compute Agents (MCOMP)

- [x] **MCOMP-01**: Operator can declare **N `compute` backends** in `backends.toml`, each bound to a specific registered compute Agent, and all N are accepted at boot — the `≤1-compute` fail-fasts (`config.active_compute_scratch_dir`, `services/backends.resolved_non_local_kind`) are retired and generalized for a `local + N-Kueue + N-compute` registry.
- [x] **MCOMP-02**: Each compute backend probes **its own bound agent's** liveness; an offline agent makes only *that* backend unavailable (the file holds or spills to the next eligible backend — it never dispatches to a dead agent). Replaces `ComputeAgentBackend.is_available`'s `select_active_agent(kind="compute")` "the single active compute agent" assumption.
- [x] **MCOMP-03**: A file dispatched to a specific compute backend is pushed to **that agent's** host/scratch destination — the push pipeline (`_enqueue_push_file` → fileserver → rsync) and the `/pushed` callback (`routers/agent_push.py`) resolve the destination per-agent, not from a single global `active_compute_scratch_dir`.
- [x] **MCOMP-04**: The tiered drain scheduler spreads long files across N compute agents by **rank** (free arm64 preferred over paid/trial x86) and **per-agent `cap`**, spilling to the next-eligible backend when one is at cap or offline. Reuses the Phase-69 rank/cap `select_backend` policy — no capability-matching.
- [x] **MCOMP-05**: One flaky or offline compute agent is **isolated** — it degrades to 0 slots without failing the drain tick or blocking dispatch to healthy compute agents (per-backend snapshot try/except, mirroring the Phase 70 MKUE-03 pattern).
- [x] **MCOMP-06**: Each compute backend's **in-flight count and terminalization** (the `/pushed` + `/api/internal/agent/*` reconcile path) are scoped to that backend/agent, so a file's result is attributed to the agent that analyzed it — no cross-agent mis-attribution. Resolves the open question of whether `cloud_job` stays one-row-per-file or needs per-(file,backend).
- [x] **MCOMP-07**: The operator runbook + config docs cover **adding a 2nd+ compute agent** and the mixed arm64/x86 rank/cap cost-tiering; each compute agent renders as its own lane in the N-lane UI (verify the Phase-71 BEUI generalization already covers compute lanes; fix if a gap surfaces).

### Engineering Hygiene (HYG)

Appended cleanup sweep (Phase 75, added 2026-07-06) — a cross-milestone engineering-hygiene backlog that accumulated through 2026.7.0/.1/.2. No user-facing behavior change.

- [ ] **HYG-01**: The Phase-66 traceability guard (`tests/shared/core/test_requirements_traceability.py`) no longer raises `FileNotFoundError` when `.planning/REQUIREMENTS.md` is absent — its active-milestone tests `pytest.skip`/fail-clean in the between-milestones state, and a regression test covers the archived/no-active-milestone case, so the standard milestone-close `git rm REQUIREMENTS.md` keeps the required code-quality check green. **Disposition: already-satisfied by PR #207 (`ec80a53a`, 2026-07-05, landed before Phase 75 was appended).** `_NO_ACTIVE_MILESTONE = not _REQUIREMENTS.exists()` (test L64) skipif-gates all active-milestone tests, and `test_archived_milestones_internally_consistent` covers the no-active-milestone case — the `git rm REQUIREMENTS.md` close path already stays green. No new code, no new test (Phase 75 D-01/D-02). Checkbox stays `[ ]` / Status `Pending` until the standard phase-completion flow flips it.
- [ ] **HYG-02**: The two stale/inert `PHAZE_CLOUD_TARGET` env + comment lines (Phase 67, silently dropped by Pydantic `extra=ignore`) are removed from the docker-compose file(s).
- [ ] **HYG-03**: The `>1`-compute-backend fail-fast fires at boot (`services/backends._validate_registry`) rather than lazily at first `resolved_non_local_kind` invocation — fail-loud with the existing id-tagged message; single-/zero-compute behavior unchanged. **Disposition: SUPERSEDED by Phase 72 (D-03).** The `>1`-compute fail-fast this asks to promote to boot-time was DELETED outright by Phase 72 to enable the N-compute capability that is the entire 2026.7.2 deliverable (MCOMP-01); re-adding it would break Phases 72-74's shipped-and-verified behavior. The correct boot guard already exists — `config.py:_validate_registry` boot-rejects a duplicate `agent_ref` while accepting N distinct compute backends, and `resolved_non_local_kind` returns `"compute"` for any N. No code change (Phase 75 D-05/D-06/D-07).
- [ ] **HYG-04**: The force-local duration-router gate is covered by a committed regression test (`tests/shared/routers/test_pipeline.py`) exercising the 3 gate sites (`pipeline.py:396/718/793`).
- [ ] **HYG-05**: Stale 2026.7.0 tracking is reconciled — `63-UAT` flipped to complete (0 pending scenarios), quick-tasks `260628-wzq` + `260629-eev` marked complete (both already committed).

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
| MCOMP-02 | Phase 73 | Complete |
| MCOMP-03 | Phase 73 | Complete |
| MCOMP-04 | Phase 73 | Complete |
| MCOMP-05 | Phase 73 | Complete |
| MCOMP-06 | Phase 73 | Complete |
| MCOMP-07 | Phase 74 | Complete |
| HYG-01 | Phase 75 | Pending |
| HYG-02 | Phase 75 | Pending |
| HYG-03 | Phase 75 | Pending |
| HYG-04 | Phase 75 | Pending |
| HYG-05 | Phase 75 | Pending |

**Coverage:**
- v1 requirements: 12 total (7 MCOMP + 5 HYG)
- Mapped to phases: 12 ✓ (MCOMP-01→72 · MCOMP-02..06→73 · MCOMP-07→74 · HYG-01..05→75)
- Unmapped: 0 ✓ (no orphans, no duplicates)
- Note: HYG-01..05 are the appended Phase-75 engineering-hygiene sweep (added 2026-07-06); all Pending until Phase 75 executes.
- Reconciliation note (Phase 75, 2026-07-06): HYG-01 is already-satisfied by PR #207 (`ec80a53a`) and HYG-03 is SUPERSEDED by Phase 72 (D-03) — both are no-code dispositions recorded in the requirement descriptions above. Their Traceability rows deliberately stay `Pending` (the docs-drift guard keeps active-phase checkboxes unflipped until Phase 75 is a passed phase); the standard phase-completion flow flips them later.

---
*Requirements defined: 2026-07-05*
*Last updated: 2026-07-05 after roadmap creation (Phases 72-74 mapped)*
