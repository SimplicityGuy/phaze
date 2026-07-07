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

## Milestone: v7.0 — UI Redesign (DAG-Centric Hybrid Console)

**Shipped:** 2026-07-02
**Phases:** 7 (57, 57.1, 58-62) | **Plans:** 28

### What Was Built
An IA/presentation rewrite turning the tab-sprawl admin UI into a DAG-centric three-column console: the shell + rail-as-nav spine with `/s/<stage>` HTMX swaps (57), the one scoped backend exception — a mid-flight analyze-progress counter gated by an `analysis_completed_at` discriminator (57.1) — then the stage workspaces (Enrich/Analyze 58, Identify 59, unified Review & Apply gate 60), the per-file record slide-in + ⌘K palette + Agents page (61), and the polish/a11y + dead-code cutover that deleted 20 legacy templates and drained the dead-template guard to an empty allowlist (62). No backend behavior change outside 57.1.

### What Worked
- **The seeded dead-template AST guard (Phase 57) held green through the entire milestone**, making the Phase-62 CUT-02 dead-code removal safe and mechanical — the whole "supersede-in-place, cut last" strategy paid off.
- **Browser-free filesystem structural guards** (a11y tree, rail-collapse class strings, docs-currency vocabulary) proved presentation contracts with zero browser/axe dependency, running in the fast lane.
- **Live Playwright-driven UAT on a fresh `phaze_uat` DB** repeatedly caught real defects that per-workspace verification missed — and accessibility-tree box snapshots turned out to be *more* precise evidence than screenshots for layout contracts.

### What Was Inefficient
- **Orphan-OOB bugs recurred across phases 58 and 60**: the single `/pipeline/stats` poll re-emits OOB cards that only have targets on some workspaces, logging `htmx:oobErrorNoTarget` on the others. Same bug class, found multiple times — a shared sink-guard should have landed the first time.
- **A dead `_STAGE_PLACEHOLDER` survived the CUT-02 cutover** because the dead-template guard builds its reachable-set from quoted `"...html"` literals in router source, so the unused assignment masked its own template. The guard meant to remove dead code had a blind spot for its own unused entry-root literals (fixed post-milestone, PR #191).
- **Two-worktree friction at milestone close**: the session lived in the merged per-phase worktree while the release work (tag + archives) belonged on main, and a stale *bundled* audit false-positived on the canonical `<id>-SUMMARY.md` quick-task filename.

### Patterns Established
- Filesystem structural guards for presentation contracts (a11y tree, responsive class strings, docs vocabulary) — the browser-free complement to the dead-template AST guard.
- Live browser a11y-tree box snapshots as layout evidence (exact geometry) over screenshots.
- Supersede-in-place: keep legacy templates reachable behind redirects until one final cutover phase drains the guard allowlist to empty.

### Key Lessons
1. A dead-code guard whose reachable-set comes from source-string literals can mask its *own* unused literals — audit the guard's blind spots at cutover, don't just trust it green.
2. Exercise every stage/workspace, not just the default route — OOB-fanout bugs only surface where the emitted target is absent.
3. Run the release/milestone-close against `main`; when the session is anchored in a per-phase worktree, GSD tooling's cwd/root resolution can point at the wrong `.planning`.

### Cost Observations
- 7 phases (incl. the 57.1 decimal insert) / 28 plans / ~69 tasks over 4 days (2026-06-29 → 2026-07-02); ~14 squash-merged commits on main. The audit + live UAT + two follow-up PRs (#189 fingerprint counter, #191 dead-code + /saq backlog) all closed in the same session.

---

## Milestone: 2026.7.0 — Engineering Improvements

**Shipped:** 2026-07-03
**Phases:** 4 (63-66) | **Plans:** 13

### What Was Built
An engineering-debt paydown milestone with **zero product/backend behavior change**: parallel CI over a 9-bucket test partition with combined-across-shards coverage + doc-only skip-with-success (63), a fail-closed per-module coverage floor that lifted the enforced gate above the 90.38% baseline (64), CalVer release versioning (`YYYY.MM.REVISION`, first tag `2026.7.0`) across the whole release surface with the historical `vN.M` record intact (65), and a hermetic docs-drift CI guard + `/saq` shell re-link + vulture dead-code tooling (66). The docs-drift gate directly closes the "documentation drift needs automation" lesson that recurred across the v2.0/v3.0/v4.0/v7.0 audits.

### What Worked
- **The docs-drift gate caught real drift on its very first run** — a stale Phase-65 ROADMAP checkbox — then self-validated this milestone's own completion bookkeeping green. A lesson logged for four milestones finally became an enforced test.
- **A behavior-preserving `git mv` of 205 test files into 9 buckets kept all 2,566 tests green**, with a partition guard failing CI on any unbucketed test — a large reorg at zero behavior risk.
- **The vulture sweep being a deliberate no-op validated the v7.0 CUT-02 cutover**: the confirmed-dead pass found nothing, proving the earlier dead-code removal was complete; the durable artifact is a hand-audited whitelist, not a deletion.
- **Two blocking human-approval gates on the dead-code phase** (package-legitimacy + deletion-review) kept an inherently-risky sweep safe.

### What Was Inefficient
- **CI-gate hardening spilled past plan scope in Phase 63** (aggregate-results deny-list, empty-diff fail-safe, per-bucket gate deferred to combine) — real improvements, but code-review/verification discoveries rather than planned work; the plan under-modeled the branch-protection interaction.
- **Guard robustness needed a follow-up PR (#199)** after the Phase 66 merge: the traceability parser needed section-scoping, fail-loud-on-duplicate-rows, and a missing-from-table check. The first cut of a drift guard has its own drift blind spots.
- **The two-worktree close friction recurred** from v7.0 — the session lived in the merged per-phase worktree while the tag + archives belonged on main; resolved by running the close from the main worktree on a `release/2026.7.0` branch.

### Patterns Established
- CI-load-bearing scripts (`classify-changed-files.sh`, `coverage_floor.py`) each ship with their own `tests/shared/` unit test — a script that gates CI is itself under test.
- `tests/buckets.json` as a single-source-of-truth partition consumed by BOTH the CI matrix and a structural guard — the partition can't silently drift from what CI runs.
- Hermetic doc-drift gate: a pytest reading REQUIREMENTS/ROADMAP/VERIFICATION that fails on traceability drift, wired into the always-run code-quality job so it fires even on doc-only PRs.
- Fail-closed gates: coverage/drift scripts raise → non-zero exit on missing/unparseable input, never a silent 0.

### Key Lessons
1. A drift guard has its own drift — section-scope its parsers, fail loud on duplicate/ambiguous rows, check both directions (passed-phase-unmarked AND marked-without-passed-phase). Budget a hardening follow-up.
2. Adopt CalVer at a milestone boundary, not mid-cycle — decoupling names from numbers is a clean cutover once the last `vN.M` has shipped.
3. Run the milestone close from the `main` worktree on a `release/*` branch — the two-worktree cwd/root ambiguity bit both v7.0 and 2026.7.0.
4. A dead-code sweep that finds nothing is a positive result, not wasted work — it proves a prior cutover was complete; keep the whitelist as the durable artifact.

### Cost Observations
- 4 phases / 13 plans / 19 tasks over 2 days (2026-07-02 → 2026-07-03); 5 squash-merged PRs (#193/#194/#197/#198 + the #199 guard-hardening follow-up). Net −9,314 lines (12,669 added / 21,983 deleted) — the deletions are the test-bucket reorg + dead-code confirmation, not feature loss.

---

## Milestone: 2026.7.1 — Multi-Cloud Backends

**Shipped:** 2026-07-05
**Phases:** 5 (67-71) | **Plans:** 26

### What Was Built
Generalized the single `cloud_target` selector into a declarative, cost-tiered `backends.toml` registry draining long files across **local + N Kueue clusters + N cloud-compute agents simultaneously**, statically configured with zero new dependencies. Shipped as a dependency-strict chain: a config-only registry with a per-file S3 bucket registry and no back-compat shim (67), a single `Backend` protocol + 3 implementations proven byte-identical by a D-01 golden characterization gate (68), a tiered per-file rank-first drain scheduler with per-backend caps under one advisory lock + staleness/black-hole guards (69, the first behavior-changing phase), N distinct-kr8s-client Kueue clusters with deterministic per-file buckets and concurrency-safe clean-before-flip cleanup (70), and an N-lane read-only UI + no-redeploy force-local kill-switch (71). Milestone audit PASSED 21/21 reqs, 5/5 cross-phase flows.

### What Worked
- **Refactor-first with a byte-identical golden gate paid off exactly as designed.** 67→68 were behavior-preserving (68 gated by a D-01 characterization snapshot that changed exactly one deliberate field); when 69 introduced multiplicity, any behavior diff was unambiguously attributable to the new scheduler, not an accidental regression.
- **The integration checker live-verified the force-local gate end-to-end** (real Postgres + FastAPI app), catching that the drain + both duration-router callers + backfill all fold on `get_route_control` — a confidence the static traceability table alone couldn't give.
- **Reversing the design's one-shared-bucket decision (REG-05) mid-milestone was clean** because dispatch RECORDS `staging_bucket` and every reader reads the recorded value — the per-file `pick_bucket` selection never has to be re-derived, which also made the clean-before-flip cleanup concurrency-safe (closed Pitfall 9).
- **Per-cluster failure isolation via a per-backend snapshot try/except** meant one flaky Kueue cluster degrades to 0 slots without poisoning the whole drain tick.

### What Was Inefficient
- **Phase 70 flipped the MKUE-01 requirement checkbox to Complete mid-phase**, before its VERIFICATION passed — which tripped the 2026.7.0 docs-drift guard's D-02 invariant and left the branch red-suite until the verifier reconciled it. Every prior phase in the milestone flipped checkboxes atomically at phase-completion; 70 deviated for one requirement.
- **Phase 69 needed a gap-closure plan (69-05, CR-01)** after code-review found a cross-backend double-dispatch race — a locally-spilled file stayed in the drain-candidate set; fixed by adding `FileState.LOCAL_ANALYZING`. Phase 70 code-review also found 2 BLOCKERs (autoflush-strand on Kueue dispatch, drain rollback discipline).
- **Coverage-floor CI failed right after the Phase 70 merge** on `cloud_staging`, forcing a follow-up that raised all modules ≥90% and lifted the per-module floor 85→90 — the per-module gate from 2026.7.0 fired on new low-coverage modules the phase added.
- **The force-local gate shipped without a committed regression test** at the two duration-router sites (live-verified only in the audit) — carried as deferred W2.

### Patterns Established
- **Byte-identical characterization gate before any behavior change**: land the protocol/refactor with a golden snapshot proving zero behavior diff, THEN change behavior — isolates attribution.
- **Record-don't-rederive for per-file routing**: stamp the selected bucket/backend on the row at dispatch; every downstream read (presign/delete/reconcile/callback) reads the recorded value, never recomputes — makes concurrent cleanup safe.
- **No-redeploy operational kill-switch as a persisted one-row flag** with a degrade-safe reader (False on absent row / any DB error) gating every enqueue site — an operator can revert to safe-mode instantly.
- **Per-backend snapshot-and-isolate**: wrap each backend's probe in try/except so one backend's failure is a local 0-slot degrade, not a tick-wide raise.

### Key Lessons
1. Flip requirement checkboxes atomically at phase close, AFTER verification passes — Phase 70's early MKUE-01 flip tripped the docs-drift guard and left a red branch. The guard from the prior milestone did its job.
2. A per-module coverage floor will fire on the next phase that adds a low-coverage module — budget coverage work into phases that add new modules, don't let it surface as a post-merge CI failure.
3. When a gate is verified live during the audit but has no committed test (force-local W2), commit the test in the same pass — "live-verified" doesn't survive into the regression suite.
4. Refactor-first ordering with a golden gate is the right shape for a risky behavior change — it made the milestone's one behavior-changing phase (69) debuggable.

### Cost Observations
- 5 phases / 26 plans / 56 tasks over 2 days (2026-07-03 → 2026-07-04); 5 squash-merged PRs (#201/#202/#203/#204/#206). Code-review caught real blockers in 69 (CR-01 double-dispatch) and 70 (2 BLOCKERs); a post-70 coverage follow-up raised all modules ≥90%. Net +29,032 / −13,211 lines across the range (includes the protocol re-home + planning docs).

---

## Milestone: 2026.7.2 — Multi-Compute Agents (N Cloud-Compute Backends)

**Shipped:** 2026-07-06
**Phases:** 5 (72-76) | **Plans:** 17

### What Was Built
Finished the 2026.7.1 registry's deliberate compute-side descope: **N cloud-compute agents** now dispatch / route / reconcile / fail-isolate simultaneously — the direct compute-side twin of Phase 70's multi-Kueue work — with the `≤1-compute` fail-fasts retired and **zero new dependencies**. Shipped as a dependency-strict chain: per-entry `agent_ref`→`Agent.id` compute binding + fail-fast retirement, golden-gated for the ≤1-compute path (72); the behavior core — per-agent liveness, record-don't-rederive push/scratch destination through rsync → `/pushed`, Phase-69 rank/cap load-spread across N agents, per-backend snapshot isolation, `backend_id`-scoped terminalization (73); operator runbook + N-lane compute UI verification (74); then two appended sweeps — engineering hygiene (75, HYG-01..05) and compute/push hardening (76, HARD-01..03) closing carried review items each with a regression test. Milestone audit PASSED 15/15 reqs, 5/5 phases, 6/6 integration seams, E2E flow complete.

### What Worked
- **Parity-by-reuse.** MCOMP-04 (rank/cap load-spread) and MCOMP-05 (one-flaky isolation) added *no new scheduler policy* — they reused the Phase-69 `select_backend` and the Phase-70 per-backend snapshot try/except, adding only N-compute-labelled regressions. The registry/protocol groundwork from 2026.7.1 made the compute-side twin a thin extension.
- **Record-don't-rederive extended cleanly to compute dispatch.** Destination (host/scratch/ssh_user) stamped on `PushFilePayload` at dispatch and read verbatim by rsync + `/pushed` (`resolve_compute_backend(backend_id)`) — the same pattern that made 2026.7.1's bucket routing concurrency-safe resolved the "cloud_job one-row-per-file vs per-(file,backend)" question in favor of one row keyed by `backend_id`, no migration.
- **Appended hardening/hygiene phases (75, 76) as a milestone-close mechanism** turned a pile of accepted-risk / review items into shipped, regression-tested fixes before close — Phase 76 alone closed the three biggest carried items from 72–74 (WR-01 probe-session race → HARD-01 structural fix; WR-04 ledger RMW race → HARD-02; AR-30-03 agent_id validation → HARD-03).
- **The milestone audit's integration checker caught a cross-phase gap per-phase verification missed** (GAP-01: an N-compute-unaware orphan-recovery path in `reenqueue.py` untouched by 72–76) — exactly the class of finding a milestone-level audit exists to surface.

### What Was Inefficient
- **Scope grew twice after the "last phase."** Phase 74 was planned as the milestone's final phase; Phases 75 (hygiene) and 76 (hardening) were both appended mid-milestone (2026-07-06). Legitimate close-out work, but it meant the ROADMAP milestone header was re-extended twice (72-74 → 72-75 → 72-76).
- **A plan-specified lock primitive self-deadlocked.** HARD-02's plan specified `with_for_update()`; code review (76-REVIEW CR-01) found it self-deadlocks against the `push_file` before_enqueue hook (`apply_deterministic_key` upserts the same ledger row from a nested session). Required an operator-approved supersession to `pg_advisory_xact_lock` — caught in review, but the plan should have checked the primitive against nested-session hooks.
- **SUMMARY `requirements_completed` frontmatter was mostly empty**, so the milestone audit's 3-source cross-reference fell back to manual verification against each phase's VERIFICATION coverage table + the traceability table. A documentation-lag, not a coverage gap, but it weakened one of the audit's three independent sources.
- **Four low-severity/cosmetic review items (73-WR-03/IN-01, 74-IN-01/IN-02) reached milestone close still open** — closed at close-out via quick `260706-odc` rather than in-phase.

### Patterns Established
- **Appended close-out sweep phases** (hygiene + hardening) as a deliberate mechanism to convert accepted-risk/review debt into shipped, regression-tested fixes before archiving a milestone.
- **Advisory lock over row lock when a nested-session enqueue hook shares the row**: `pg_advisory_xact_lock(hashtext(key))` serializes an RMW in a different lock space than the hook's `INSERT...ON CONFLICT`, avoiding self-deadlock where `with_for_update()` would block.
- **Structural-over-empirical for concurrency guarantees**: HARD-01 replaced Phase 74's "empirically race-free" shared-session probe fan-out with a sequential loop that is race-free *by construction* — and reworded the docstring to match.

### Key Lessons
1. A milestone-level audit with an integration checker earns its keep — it caught GAP-01 (a cross-phase, N-compute-unaware recovery path) that five green per-phase verifications did not.
2. When a plan names a lock primitive (`with_for_update()`), verify it against every nested session that touches the same row before execution — the deadlock was structural, not a race.
3. Close carried review items in-phase, or budget an explicit close-out sweep — deferring four to milestone close meant a quick task in the completion flow.
4. Prefer structural guarantees over "empirically observed" ones for concurrency: an arbiter test that passes 6× is weaker than a loop that cannot race by construction.

### Cost Observations
- 5 phases / 17 plans over 2 days (2026-07-05 → 2026-07-06); 5 squash-merged PRs (#209/#210/#211/#213/#214) + a close-out quick task (`260706-odc`, 116 tests green). Two phases (75, 76) appended mid-milestone. Code review caught real issues in every behavior phase (73 CR-01 PUSHING-CAS guard; 76 CR-01 with_for_update self-deadlock). GAP-01 deferred to v2 PROV-01.

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Phases | Plans | Key Change |
|-----------|--------|-------|------------|
| v1.0 | 11 | 24 | Established GSD workflow, branching strategy, Nyquist validation |
| v2.0 | 6 | 16 | Research phases before planning, dual-service architecture, HTMX patterns matured |
| v3.0 | 6 | 11 | Enrichment layer (search, Discogs, tag writing, CUE) on stable foundation; HTMX OOB swaps + Alpine.js patterns reused everywhere |
| v4.0 | 6 | 47 | Two-host distributed architecture (HTTP-only agent boundary, per-agent SAQ queues, internal CA, settings split via `get_settings()` factory); subprocess import-boundary tests enforce invariants; 4× plan count from contract-heavy API surface |
| 2026.7.0 | 4 | 13 | First CalVer milestone (names decoupled from versions); parallel-CI bucket partition + combined coverage; docs-drift lesson finally automated as a hermetic CI gate; engineering-debt paydown, zero behavior change |
| 2026.7.1 | 5 | 26 | Pluggable multi-backend registry (local + N Kueue + N compute simultaneously); refactor-first with a byte-identical golden characterization gate isolating the one behavior-changing phase; integration checker live-verified cross-phase flows; per-module coverage floor raised to 90 |
| 2026.7.2 | 5 | 17 | Parity-by-reuse (N-compute twin of N-Kueue reused Phase-69/70 scheduler + isolation, no new policy); two appended close-out sweep phases (hygiene + hardening) converting accepted-risk/review debt into regression-tested fixes; milestone audit's integration checker caught a cross-phase gap (GAP-01) five green per-phase verifications missed; zero new dependencies |

### Cumulative Quality

| Milestone | Tests | LOC (Python src) | Phases |
|-----------|-------|------------------|--------|
| v1.0 | 282 | 7,975 | 11 |
| v2.0 | 538 | 5,966 added | 6 |
| v3.0 | (unrecorded) | (single-host enrichment) | 6 |
| v4.0 | (full suite passing) | ~14,300 src + ~28,000 tests cumulative; ~23,242 lines added since v3.0 tag | 6 |
| v5.0 | (full suite passing) | (arm64 essentia image + cloud-burst push pipeline) | 5 |
| v6.0 | 2,474 passing | ~8,452 lines added across 61 files since v5.0 tag | 5 |
| 2026.7.0 | 2,566 passing (9 buckets) | net −9,314 (test-bucket reorg + dead-code confirmation) | 4 |
| 2026.7.1 | 2,637+ passing (all modules ≥90%) | net +15,821 across range (protocol re-home + N-backend registry) | 5 |
| 2026.7.2 | (full suite passing; 321 targeted green in audit) | ~28,401 Python src LOC; net +13,433 / −15,685 across range (incl. planning churn) | 5 |

### Top Lessons (Verified Across Milestones)

1. Integration testing at pipeline boundaries catches gaps that unit tests miss (v1.0 audit gaps, v2.0 clean audit, v4.0 cross-tenant guards)
2. Documentation conventions established early save cleanup phases later (v1.0 SUMMARY frontmatter, v2.0 Nyquist frontmatter, v4.0 VERIFICATION.md prefixing)
3. Research phases for unfamiliar domains prevent rework (v2.0 fingerprint architecture, v4.0 pydantic-settings v2 quirks + cryptography x509 generation)
4. The discuss-phase questioning loop is highest ROI on schema/migration phases (v4.0 Phase 24 two-step migration shape was locked before any SQL was written)
5. Subprocess import-boundary tests are the cheapest enforcement of architectural invariants — established in v4.0, should generalize to any future "this module must not import that module" rule
6. Per-phase PR convention scales — held through 29 phases across 4 milestones, main never broken
7. Documentation drift gates need automation — manual REQUIREMENTS.md / ROADMAP.md sync after PR merge consistently lags; surfaced in v2.0, v3.0, v4.0, and v7.0 audits → **RESOLVED in 2026.7.0 (Phase 66): a hermetic `just docs-drift` CI guard now fails the build on traceability drift, and caught real drift on its first run**
