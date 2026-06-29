# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.0 — MVP

**Shipped:** 2026-03-30
**Phases:** 11 | **Plans:** 24 | **Tasks:** 43

### What Was Built
- Full music collection pipeline: scan -> analyze -> propose -> approve -> execute
- Docker Compose stack with FastAPI API, arq workers, PostgreSQL, Redis
- Audio analysis via essentia-tensorflow (34 models for BPM, key, mood, style)
- LLM-powered filename proposals via litellm with batch processing
- Admin web UI (HTMX + Tailwind) with approve/reject, bulk actions, keyboard shortcuts, SSE progress
- Copy-verify-delete execution with append-only audit log
- 282 tests, 7,975 lines of Python

### What Worked
- GSD workflow kept 11 phases organized with clear planning -> execution -> verification gates
- Parallel executor agents (worktree isolation) cut execution time significantly for independent plans
- TDD approach caught integration issues early (e.g., MagicMock truthiness bug in execution tests)
- Milestone audit after Phase 8 surfaced real integration gaps (scan->analyze, analyze->propose triggers) that Phase 9 closed
- Phase branching with PRs kept main clean and provided review checkpoints
- Pre-commit hooks with frozen SHAs caught formatting/linting issues before they accumulated

### What Was Inefficient
- Phase 10 and 11 were gap-closure phases created after the audit — earlier integration testing could have caught these during Phase 8/9
- Some VERIFICATION.md files showed gaps_found but the gaps were already closed by successor phases — verification status should update automatically
- SUMMARY frontmatter requirements-completed fields were inconsistently populated across early phases — establishing the convention earlier would have avoided Phase 11 cleanup
- Phase 10 VERIFICATION gaps (config.json EOF, INF-03 checkbox) were trivial items that shouldn't have required a separate phase

### Patterns Established
- justfile as command runner for all dev tasks (replicated in CI via `just` delegation)
- Phase branching strategy with PRs per phase for code review
- Nyquist validation (VALIDATION.md) for test coverage verification per phase
- 3-source cross-reference for requirements (VERIFICATION + SUMMARY + REQUIREMENTS.md)
- Milestone audit before completion to surface tech debt early

### Key Lessons
1. Run integration checks after every pipeline-connecting phase, not just at milestone end
2. Establish SUMMARY frontmatter conventions (requirements-completed, tech-stack) from Phase 1
3. Trivial doc fixes should be batched into the phase that creates them, not deferred to cleanup phases
4. The milestone audit -> gap closure -> re-audit cycle is effective but adds 2-3 phases — bake integration testing into earlier phases to reduce this
5. Pre-commit hook validation in CI (via pre-commit/action) is more reliable than manual runs

### Cost Observations
- Model mix: ~60% opus (execution), ~30% sonnet (verification, integration checks), ~10% haiku (quick lookups)
- Notable: Parallel worktree agents are the most token-efficient approach for independent plans — each gets a fresh context window without polluting the orchestrator

---

## Milestone: v2.0 — Metadata Enrichment & Tracklist Integration

**Shipped:** 2026-04-02
**Phases:** 6 | **Plans:** 16 | **Tasks:** 31

### What Was Built
- Audio tag extraction (mutagen) populating FileMetadata with artist, title, album, year, genre, track number, duration, bitrate, raw JSONB dump
- AI destination path proposals with collision detection, directory tree preview, and execution gate
- Duplicate resolution UI with auto-scoring (bitrate > tags > path), side-by-side comparison, resolve/undo workflow
- 1001Tracklists integration: async scraper, fuzzy matcher (rapidfuzz), monthly refresh cron
- Dual fingerprint service (audfprint + Panako) as Docker containers with HTTP APIs and batch ingestion
- Live set scanning with tracklist review: inline editing, approve/reject, bulk reject, confidence badges

### What Worked
- Milestone audit after all phases caught only cosmetic/process tech debt — no functional gaps, proving v1.0 lesson about integration testing paid off
- Phase branching with PRs continued to keep main clean (PRs #16-#22)
- Research phases before planning (especially Phase 16 fingerprint architecture) prevented major rework
- HTMX + server-rendered templates kept UI delivery fast without frontend build complexity
- Parallel-capable phases (13, 14, 15 all depend only on 12) gave scheduling flexibility

### What Was Inefficient
- Phase 12 REQUIREMENTS.md checkboxes got lost during branch merge — needed manual sync in tech debt cleanup
- Nyquist VALIDATION.md frontmatter was never toggled to `true` after execution across all 6 phases — process step consistently skipped
- Phase 12 showed as "Not started" in ROADMAP.md progress table despite being complete — merge artifact from phase branch
- Some MILESTONES.md accomplishments are raw summary one-liners rather than curated highlights

### Patterns Established
- Dual fingerprint engine architecture with Protocol-based adapters and weighted orchestrator
- HTMX OOB swaps for inline updates (undo toasts, status transitions) — reusable pattern across all admin pages
- Alpine.js for client-side state that HTMX doesn't handle (filter tabs, radio selection highlighting, scan panel toggle)
- Convergence gate pattern: dual exists() subquery checks before advancing pipeline stage

### Key Lessons
1. Nyquist VALIDATION.md frontmatter finalization should be automated (hook or post-execution step) — manually toggling 6 files is error-prone
2. REQUIREMENTS.md checkbox sync needs to happen on main after PR merge, not just on the phase branch
3. Research phases for unfamiliar domains (fingerprinting, scraping) are high-ROI — Phase 16 research prevented audfprint/Panako integration surprises
4. MILESTONES.md accomplishment extraction should be curated (4-6 highlights), not raw dump of all 16 summary one-liners

### Cost Observations
- Model mix: ~70% opus (execution + planning), ~20% sonnet (verification), ~10% haiku (quick checks)
- Notable: 6 phases in 3 days with 538 tests — velocity improved from v1.0 due to established patterns and infrastructure

---

## Milestone: v4.0 — Distributed Agents

**Shipped:** 2026-05-17
**Phases:** 6 | **Plans:** 47

### What Was Built
- `agents` table + `agent_id` columns on FileRecord/ScanBatch with two-step Alembic migration (012 add+backfill via `legacy-application-server` seed, 013 NOT NULL + UQ swap) preserving v3.0 corpus end-to-end
- `/api/internal/agent/*` HTTP surface (15+ routes: files, metadata, analysis, fingerprint, tracklists, proposals, execution-log, scan-batches, exec-batches, heartbeat, whoami) with token-hash bearer auth deriving `agent_id` from token; 403-before-state-machine cross-tenant guard on every multi-tenant PATCH
- Task code split: `phaze.tasks.controller` (fileless) vs `phaze.tasks.agent_worker` (file-bound) under `PHAZE_ROLE={control,agent}`; per-agent SAQ queue (`phaze-agent-<id>`); subprocess import-boundary test catches `phaze.database` leaks
- `PhazeAgentClient` with tenacity retry funnel + 4-class error hierarchy + respx contract tests; bearer token never instance-attribute (lives in httpx headers only)
- `phaze-agent-watcher` service: watchdog observer + asyncio-owned single-loop sweep with mtime settle (10s default) + stuck-file cap (3600s); LIVE-sentinel ScanBatch per agent; admin "Trigger Scan" form
- Distributed execution dispatch: group-by-`FileRecord.agent_id` in-Python `defaultdict`, one `execute_approved_batch` sub-job per agent under shared parent `batch_id`; per-proposal terminal progress POST with SAQ-meta UUID lift for retry safety; unified SSE aggregated by app-server
- Self-signed internal CA + leaf x509 generated on first start by `phaze.cert_bootstrap`; pre-uvicorn entrypoint shim execvp's uvicorn (clean PID-1 signal propagation); `PhazeAgentClient.verify=` honors `AgentSettings.agent_ca_file`
- Redis `requirepass` + `${REDIS_BIND_IP:-127.0.0.1}` LAN bind; `AgentSettings` rejects passwordless `redis_url` in production at boot
- App-server compose stripped of `SCAN_PATH`/`MODELS_PATH` mounts; new `docker-compose.agent.yml` (4 services); per-file-server `just download-models` + auto-bootstrap; 30s heartbeat cron; `/admin/agents` page with liveness classifier
- Migration from arq → SAQ (built-in web UI, per-queue worker model, active maintenance)

### What Worked
- The discuss-phase questioning loop on Phase 24 ("two-step migration vs single-step") surfaced the `legacy-application-server` backfill strategy BEFORE writing any SQL — saved a full re-plan
- Subprocess import-boundary tests (D-25) are the cheapest possible way to enforce architectural invariants: one test catches every accidental `phaze.database` leak into agent code at CI time
- The 403-before-state-machine guard pattern, repeated across Phases 25-08 / 27-02 / 28-02, is now a project-wide convention for multi-tenant PATCH routes
- Phase 27 watcher: the asyncio-owned single-loop sweep + `loop.call_soon_threadsafe` thread bridge is the entire concurrency story — no locks, no race conditions in tests
- Phase 28 SAQ-meta UUID lift (persist `execution_log_id` + `progress_request_id` in `job.meta` for retry idempotency) closed two latent retry-correctness bugs (L6/L22) that wouldn't surface in unit tests
- Phase 29 entrypoint shim pattern (bootstrap → `execvp uvicorn`) is the canonical answer to "Docker PID-1 signal handling with pre-start work" — no double-process tree
- Per-phase PR convention kept main clean through 6 phases (PRs #52, #56, #57, #59, #62 + this PR for #29)
- Wave-based parallelization with worktree executors gave 3-4x throughput on phases with independent plans (especially Phase 25 wave 3, Phase 26 waves 3-5)

### What Was Inefficient
- Phase 24 plan numbering went from `[ ]` to `[x]` on phase-branch but the ROADMAP.md progress table wasn't synced to main — surfaced again by the v4.0 audit as documentation drift; needed a follow-up commit before milestone close
- REQUIREMENTS.md traceability table was left with 13 stale `| Pending |` rows after Phases 24-28 merged; audit caught it but the drift could have been prevented by a CI gate that checks REQUIREMENTS.md against `find-phase --status passed`
- VERIFICATION.md naming inconsistency: Phase 24 wrote `VERIFICATION.md` (unprefixed) while Phases 25-29 wrote `{N}-VERIFICATION.md` — breaks the `gsd-sdk query find-phase` discovery pattern; convention should be enforced by the verifier agent
- Phase 26 ballooned to 13 plans (split into 6 waves) — the contract gap surfaced in Phase 25 (`/whoami`, `PUT /analysis`, `POST /tracklists`, `PATCH /proposals/{id}/state`) wasn't visible at Phase 25 plan time; a "contract completeness check" between planning waves would have absorbed those 4 plans into Phase 25
- Phase 29 human-UAT was deferred to "verified-docs-only" because real two-host hardware wasn't available — milestone shipped with a documented production-smoke gap rather than a real one
- Compose-template work (docker-compose.agent.yml + .env.example.agent + YAML-parse tests) repeated across Phases 27, 29 — could have lived in a single dedicated infra plan

### Patterns Established
- **Settings split via `get_settings()` factory** (Phase 26-01): `BaseSettings` + `ControlSettings(BaseSettings)` + `AgentSettings(BaseSettings)` with module-level `settings: ControlSettings = ...` for back-compat and call sites that pick via `get_settings()`
- **`AliasChoices(PHAZE_*, bare_field)` per pydantic-settings field** (Phase 26-01): canonical pattern for env-var naming without a global `env_prefix`
- **`Annotated[list[str], NoDecode] + @field_validator(mode="before")`** (Phase 26-01): canonical pattern for comma-split env vars (pydantic-settings v2 does NOT do this natively)
- **Subprocess import-boundary tests** (Phase 26-10, 27-01, 29-01): `subprocess.run([sys.executable, "-c", "import phaze.tasks.agent_worker"])` + assert `phaze.database` not in `sys.modules` — extends per phase as new modules join the agent chain
- **403-before-state-machine cross-tenant guard** (Phases 25-08 / 27-02 / 28-02): handler order is part of the spec; prevents timing side-channel via 409-vs-403 latency difference
- **Idempotent same-state PATCH echoes row with zero DB writes** (Phase 26-08, 27-03): no `updated_at` bump on same-state retry
- **Smoke-app per-router contract test pattern** (Phase 25-04, 26-05): `FastAPI()` + `include_router(...)` + `app.state.X = Y` decouples handler tests from the full main.py wiring
- **Overflow funnel for wire-format fields without a column** (Phase 26-06): non-column response fields merge into existing JSONB column rather than dropping
- **`_render_partial()` helper through `Jinja2Templates.TemplateResponse(...).body.decode()`** (Phase 28-04): Semgrep XSS-lint requires this over bare `Environment.get_template().render()`
- **Pre-uvicorn entrypoint shim** (Phase 29-01): bootstrap-then-`execvp` for clean PID-1 signal propagation
- **`${VAR:?...}` compose fail-fast** (Phase 29-04): forces compose parse failure on misconfigured host before any container starts
- **HTMX poll-partial halt by OMITTING `hx-trigger`** (Phase 27-06): terminal-state markup drops the polling attrs; outerHTML swap replaces the polling element entirely
- **In-Python `defaultdict(list)` over SQL `GROUP BY`** (Phase 28-03): at v4.0 scale (1-5 agents × ≤10K proposals), type-safe path is cheaper than DB aggregation

### Key Lessons
1. **Enforce architectural invariants with subprocess import-boundary tests** — the single test catches every `phaze.database` leak at CI time; the alternative (manual review) does not scale
2. **The discuss-phase questioning loop is highest ROI on schema/migration phases** — getting the two-step migration shape locked in Phase 24 prevented a full re-plan when v3.0 data preservation was raised
3. **Documentation drift gates need automation** — manual REQUIREMENTS.md / ROADMAP.md sync after PR merge consistently lags; a CI gate that cross-checks REQUIREMENTS.md against `find-phase --status passed` would close this
4. **VERIFICATION.md naming convention must be enforced by the verifier agent** — Phase 24's unprefixed `VERIFICATION.md` broke the discovery pattern and required a documentation drift commit at milestone close
5. **Plan a "contract completeness check" between waves on API-heavy phases** — Phase 26 absorbed 4 extra plans (`/whoami`, `PUT /analysis`, `POST /tracklists`, `PATCH /proposals/{id}/state`) that should have lived in Phase 25
6. **Human-UAT defer policy needs explicit acceptance criteria upfront** — Phase 29's "verified-docs-only" exit was the right call given missing hardware, but it should have been declared at plan time, not at verify time
7. **Per-phase PR convention scales to large milestones** — 6 phases, 6 PRs, main never broken; the discipline pays off most on phases that mutate shared modules (config, main.py, docker-compose.yml)
8. **Per-agent SAQ queues fit perfectly when the queue name comes from a stable resource ID** — `phaze-agent-<id>` from `FileRecord.agent_id` made the enqueue path a single field lookup, no routing logic needed

### Cost Observations
- Model mix: ~60% opus (execution + planning on complex phases like 26), ~30% sonnet (verification, contract tests, doc work), ~10% haiku (quick checks, status updates)
- Notable: Phase 26 (13 plans, 6 waves) used the most tokens of any v4.0 phase due to the contract-gap discovery + per-router plan splits — a "contract completeness" pre-check could have collapsed this back to ~9 plans
- Worktree parallelization saved meaningful wall-clock on Phases 25 (wave 3, 5 parallel routers) and 26 (waves 3-5) — orchestrator stays focused on integration while executors work independently

---

## Milestone: v6.0 — Kubernetes Burst Analysis

**Shipped:** 2026-06-29
**Phases:** 5 (52-56) | **Plans:** 27

### What Was Built
K8s as a third analysis-routing target: ephemeral, quota-scheduled Kueue batch Jobs per long file. x86 Job-runner image + one-shot entrypoint (52), a DIST-01-preserving S3 object-staging leg (53), a kr8s submit + `*/5` reconcile cron (54), the one-branch live-seam routing edit that replaced the `cloud_burst_enabled` boolean with a `cloud_target` selector (55), and the cluster-admin runbook + LocalQueue startup probe + ephemeral Agents identity (56).

### What Worked
- The v5.0 spine (image → legs → pipeline → routing seam → deploy) replayed cleanly — every phase had a direct v5.0 precedent, so no phase needed a research-phase.
- Keeping the single live-seam edit (Phase 55) last minimized the partially-integrated window; Phases 52-54 stayed independently unit-testable against fakes (respx / moto / fake-kube), holding the 85% gate at every boundary.
- Confining each external SDK to one module (aioboto3 → `s3_staging`, kr8s → `kube_staging`) plus subprocess import-boundary tests kept DIST-01 enforced for free.

### What Was Inefficient
- The manifest→pod env seam (Phase 54 produces the Job manifest, Phase 52 consumes the env) was never integration-tested: Phase 52 tests injected `PHAZE_JOB_FILE_ID` via fixture, Phase 54 manifest tests asserted structure but not the env contract. The milestone audit's integration-checker caught it (**JOB-ENV-CONTRACT**) — every admitted pod would have exited code 20. A one-function fix, but it should have had a contract test at phase time.
- Deferring the live E2E (the one test that exercises the real seam) let the gap reach milestone close. Deployment-gated deferral is necessary here, but it hides producer↔consumer seam bugs.

### Patterns Established
- For cross-phase producer/consumer artifacts (manifest builder ↔ entrypoint, payload builder ↔ validator), add a contract test on the *actually-produced* artifact, not fixture-injected inputs.
- Run `/gsd:audit-milestone` BEFORE `complete-milestone` — the audit caught a milestone-breaking defect this cycle that all 5 per-phase verifications missed.

### Key Lessons
1. Per-phase VERIFICATION passing ≠ the milestone works end-to-end — cross-phase seams need their own integration check. This is the third time integration-at-boundaries beat unit tests (v1.0 audit gaps, v4.0 cross-tenant guards, v6.0 manifest env).
2. Deployment-gated live E2E is a real outstanding risk, not a formality — re-run it FIRST after rollout (same lesson the v4.0.8 payload incident taught: build in one place, validate in another).

### Cost Observations
- 5 phases / 27 plans / 44 tasks over ~3 days; ~8,450 LOC added across 61 files. The audit + one quick-task fix (260628-wzq) closed the milestone in the same session.

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Phases | Plans | Key Change |
|-----------|--------|-------|------------|
| v1.0 | 11 | 24 | Established GSD workflow, branching strategy, Nyquist validation |
| v2.0 | 6 | 16 | Research phases before planning, dual-service architecture, HTMX patterns matured |
| v3.0 | 6 | 11 | Enrichment layer (search, Discogs, tag writing, CUE) on stable foundation; HTMX OOB swaps + Alpine.js patterns reused everywhere |
| v4.0 | 6 | 47 | Two-host distributed architecture (HTTP-only agent boundary, per-agent SAQ queues, internal CA, settings split via `get_settings()` factory); subprocess import-boundary tests enforce invariants; 4× plan count from contract-heavy API surface |

### Cumulative Quality

| Milestone | Tests | LOC (Python src) | Phases |
|-----------|-------|------------------|--------|
| v1.0 | 282 | 7,975 | 11 |
| v2.0 | 538 | 5,966 added | 6 |
| v3.0 | (unrecorded) | (single-host enrichment) | 6 |
| v4.0 | (full suite passing) | ~14,300 src + ~28,000 tests cumulative; ~23,242 lines added since v3.0 tag | 6 |
| v5.0 | (full suite passing) | (arm64 essentia image + cloud-burst push pipeline) | 5 |
| v6.0 | 2,474 passing | ~8,452 lines added across 61 files since v5.0 tag | 5 |

### Top Lessons (Verified Across Milestones)

1. Integration testing at pipeline boundaries catches gaps that unit tests miss (v1.0 audit gaps, v2.0 clean audit, v4.0 cross-tenant guards)
2. Documentation conventions established early save cleanup phases later (v1.0 SUMMARY frontmatter, v2.0 Nyquist frontmatter, v4.0 VERIFICATION.md prefixing)
3. Research phases for unfamiliar domains prevent rework (v2.0 fingerprint architecture, v4.0 pydantic-settings v2 quirks + cryptography x509 generation)
4. The discuss-phase questioning loop is highest ROI on schema/migration phases (v4.0 Phase 24 two-step migration shape was locked before any SQL was written)
5. Subprocess import-boundary tests are the cheapest enforcement of architectural invariants — established in v4.0, should generalize to any future "this module must not import that module" rule
6. Per-phase PR convention scales — held through 29 phases across 4 milestones, main never broken
7. Documentation drift gates need automation — manual REQUIREMENTS.md / ROADMAP.md sync after PR merge consistently lags; surfaced in v2.0, v3.0, and v4.0 audits
