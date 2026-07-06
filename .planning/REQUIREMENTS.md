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

- [x] **HYG-01**: The Phase-66 traceability guard (`tests/shared/core/test_requirements_traceability.py`) no longer raises `FileNotFoundError` when `.planning/REQUIREMENTS.md` is absent — its active-milestone tests `pytest.skip`/fail-clean in the between-milestones state, and a regression test covers the archived/no-active-milestone case, so the standard milestone-close `git rm REQUIREMENTS.md` keeps the required code-quality check green. **Disposition: already-satisfied by PR #207 (`ec80a53a`, 2026-07-05, landed before Phase 75 was appended).** `_NO_ACTIVE_MILESTONE = not _REQUIREMENTS.exists()` (test L64) skipif-gates all active-milestone tests, and `test_archived_milestones_internally_consistent` covers the no-active-milestone case — the `git rm REQUIREMENTS.md` close path already stays green. No new code, no new test (Phase 75 D-01/D-02). Checkbox stays `[ ]` / Status `Pending` until the standard phase-completion flow flips it.
- [x] **HYG-02**: The two stale/inert `PHAZE_CLOUD_TARGET` env + comment lines (Phase 67, silently dropped by Pydantic `extra=ignore`) are removed from the docker-compose file(s).
- [x] **HYG-03**: The `>1`-compute-backend fail-fast fires at boot (`services/backends._validate_registry`) rather than lazily at first `resolved_non_local_kind` invocation — fail-loud with the existing id-tagged message; single-/zero-compute behavior unchanged. **Disposition: SUPERSEDED by Phase 72 (D-03).** The `>1`-compute fail-fast this asks to promote to boot-time was DELETED outright by Phase 72 to enable the N-compute capability that is the entire 2026.7.2 deliverable (MCOMP-01); re-adding it would break Phases 72-74's shipped-and-verified behavior. The correct boot guard already exists — `config.py:_validate_registry` boot-rejects a duplicate `agent_ref` while accepting N distinct compute backends, and `resolved_non_local_kind` returns `"compute"` for any N. No code change (Phase 75 D-05/D-06/D-07).
- [x] **HYG-04**: The force-local duration-router gate is covered by a committed regression test (`tests/shared/routers/test_pipeline.py`) exercising the 3 gate sites (`pipeline.py:396/718/793`). **Disposition: satisfied by Phase 75 plan 75-02** — a 4-case force-local region in `tests/shared/routers/test_pipeline.py` covers L396 (`POST /api/v1/analyze`), L718 (`POST /pipeline/analyze`), L793 (`POST /pipeline/backfill-cloud` zero-mutation no-op), plus a force-local-False control. Checkbox stays `[ ]` / Status `Pending` until the standard phase-completion flow flips it (the docs-drift guard keeps active-phase checkboxes unflipped until Phase 75 is a passed phase).
- [x] **HYG-05**: Stale 2026.7.0 tracking is reconciled — `63-UAT` flipped to complete (0 pending scenarios), quick-tasks `260628-wzq` + `260629-eev` marked complete (both already committed).

### Compute/Push Hardening (HARD)

Appended correctness sweep (Phase 76, added 2026-07-06) — three self-contained fixes in the N-compute dispatch/push path, each closing an accepted-risk or code-review item surfaced during Phases 72-74. No new dependencies; each fix ships with a regression test. Category HARD.

- [x] **HARD-01**: The N-compute liveness probe (`services/backends._probe_availability`) no longer fans `_probe_one` over N backends through a **single shared `AsyncSession`** — the probes are serialized (awaited one at a time; N is tiny) or each `_probe_one` gets its own session from the sessionmaker, so N≥2 concurrent compute backends yield correct, **deterministic** per-backend `available` with no SQLAlchemy concurrent-use hazard. The bounded `_PROBE_TIMEOUT_SEC=1.5` `wait_for` is preserved, and the docstring/comment is reworded from an empirical ("Pitfall 1 / empirically race-free") claim to a structural guarantee. **Closes WR-01 (`74-REVIEW.md`).** Impact was bounded (a raced probe flapped one lane's `available` for a single 5s poll and self-healed; no data loss; never touched boot/golden/≤1-compute).
- [x] **HARD-02**: The `push_attempt` read-modify-write on the `push_file:<file_id>` ledger row in `routers/agent_push.py` `/mismatch` selects the row **`with_for_update()`**, making the increment atomic so two concurrent `/mismatch` for one file increment `push_attempt` **exactly twice** (no lost update) and the bounded `push_max_attempts` cap (`config.py`, `gt=0 lt=20`) still trips correctly. **Closes AR-73-02 / T-73-13 / WR-04.** Contained today by the deterministic `push_file:<id>` job-key dedup + bounded cap + the D-07 reporter gate; this makes the increment structurally correct.
- [x] **HARD-03**: The scan-status endpoint's `agent_id` query param (`routers/pipeline_scans.py`) is constrained at the HTTP boundary with `Query(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)` — the agent-id shape used elsewhere — so a malformed `agent_id` returns **422**, not a silently-empty `200` poll. **Closes AR-30-03 / Phase-30 REVIEW IN-01.**

## v2 Requirements

Deferred to a future milestone. Tracked, not in this roadmap.

### Compute Provisioning & Capability Routing (PROV)

- **PROV-01**: N-compute per-agent orphan recovery — generalize `recover_orphaned_work` so orphaned in-flight work is recovered **per compute agent** (each agent's stranded pushes/jobs re-dispatched to that agent or spilled to the next eligible backend), rather than the current single-compute-agent recovery assumption. Folds in AR-73-01 (deferred from Phase 76 as a feature, not a fix — carries Phase-45-class over-enqueue risk, so it needs a scheduling-ledger scoping design, not a one-line change). No milestone yet.
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
| HYG-01 | Phase 75 | Complete |
| HYG-02 | Phase 75 | Complete |
| HYG-03 | Phase 75 | Complete |
| HYG-04 | Phase 75 | Complete |
| HYG-05 | Phase 75 | Complete |
| HARD-01 | Phase 76 | Complete |
| HARD-02 | Phase 76 | Complete |
| HARD-03 | Phase 76 | Complete |

**Coverage:**
- v1 requirements: 15 total (7 MCOMP + 5 HYG + 3 HARD)
- Mapped to phases: 15 ✓ (MCOMP-01→72 · MCOMP-02..06→73 · MCOMP-07→74 · HYG-01..05→75 · HARD-01..03→76)
- Unmapped: 0 ✓ (no orphans, no duplicates)
- v2 requirements: 3 tracked, not in this milestone (PROV-01 N-compute per-agent orphan recovery [folds in AR-73-01] · PROV-02 capability routing · PROV-03 provisioning).
- Note: HYG-01..05 are the appended Phase-75 engineering-hygiene sweep (added 2026-07-06). HARD-01..03 are the appended Phase-76 compute/push-hardening sweep (added 2026-07-06); all Pending until Phase 76 executes.
- Reconciliation note (Phase 75, 2026-07-06): HYG-01 is already-satisfied by PR #207 (`ec80a53a`) and HYG-03 is SUPERSEDED by Phase 72 (D-03) — both are no-code dispositions recorded in the requirement descriptions above. Their Traceability rows deliberately stay `Pending` (the docs-drift guard keeps active-phase checkboxes unflipped until Phase 75 is a passed phase); the standard phase-completion flow flips them later.

---
*Requirements defined: 2026-07-05*
*Last updated: 2026-07-06 — appended Phase 76 Compute/Push Hardening (HARD-01..03) + formalized v2 PROV-01*
