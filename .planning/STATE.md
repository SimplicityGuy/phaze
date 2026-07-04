---
gsd_state_version: 1.0
milestone: 2026.7.1
milestone_name: Multi-Cloud Backends
status: ready_to_plan
last_updated: 2026-07-04T15:30:13.819Z
last_activity: 2026-07-04 -- Phase 69 execution started
progress:
  total_phases: 38
  completed_phases: 7
  total_plans: 29
  completed_plans: 16
  percent: 18
stopped_at: Phase 69 complete (5/5) — ready to discuss Phase 70
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-07-03 — 2026.7.0 Engineering Improvements shipped)

**Core value:** Get 200K messy music and concert files properly named, organized, deduplicated, with rich metadata in Postgres -- human-in-the-loop approval so nothing moves without review. Files stay on file-server agents; decisions stay on the application server.
**Current focus:** Phase 70 — multi kueue (n clusters)

## Current Position

Phase: 70
Plan: Not started
Status: Ready to plan
Last activity: 2026-07-04

Progress: [░░░░░░░░░░] 0%

## Performance Metrics

**v1.0 Velocity:**

- Total plans completed: 147
- Total phases: 11
- Timeline: 4 days (2026-03-27 -> 2026-03-30)
- Tests: 282 passing
- LOC: 7,975 Python

**v2.0 Velocity:**

- Total plans completed: 16
- Total phases: 6
- Timeline: 3 days (2026-03-31 -> 2026-04-02)
- Tests: 538 passing
- LOC: 5,966 Python

**v3.0 Velocity:**

- Total plans completed: 11
- Total phases: 6
- Timeline: 2 days (2026-04-03 -> 2026-04-04)

**v4.0 Velocity:**

- Total plans completed: 47
- Total phases: 6
- Timeline: ~43 days (2026-04-03 -> 2026-05-17 incl. discuss/research/UI design per phase)
- LOC: ~23,242 Python lines added / 1,677 deleted (180 files changed since v3.0 tag)

## Accumulated Context

### Roadmap Evolution

- 2026.7.1 Multi-Cloud Backends roadmap created (2026-07-03): 5 phases (67–71), continuing from 2026.7.0's last phase (66) — **NOT reset to 1**. Requirements-driven, dependency-strict, 1:1 category→phase per REQUIREMENTS.md + research SUMMARY: REG→67 · BACK→68 · SCHED→69 · MKUE→70 · BEUI→71. 21/21 mapped, 0 orphans, 0 duplicates. Generalizes the single `cloud_target` selector into a declarative cost-tiered `backends:` registry draining long files across local + 1+ Kueue + 1+ cloud-compute simultaneously (rank + cap, static routing, no provisioning); **zero new deps**, a pure application-code refactor over v6.0. **67** Backend Registry & Config Model (`backends:` list + per-kind discriminated-union validators + `cloud_target` back-compat shim + REG-05 S3 bucket registry public/shared-vs-cluster-specific; config-model-only, behavior-preserving; REG-01..05). **68** Backend Protocol + 3 impls (`Backend` `is_available`/`in_flight_count`/`dispatch`/`reconcile` re-homing existing bodies + `cloud_job.backend_id` additive migration + uniform per-backend in-flight accounting; behavior-preserving, acceptance-gated by a byte-identical characterization test incl. the GATE-1 compute-vs-Kueue asymmetry; **depends on 67**; BACK-01..04). **69** Tiered Drain Scheduler (rank-first per-file eligible dispatch, per-backend `cap`, spill-when-full, offline→next-eligible re-dispatch + black-hole/cooldown guard, stateless equal-rank tie-break, single-recovery-owner per kind; **first behavior-changing phase; depends on 68** — cap needs 68's per-backend count; **research flag** = drain↔reconcile lock-ordering + attempt-budget/cooldown split; SCHED-01..05). **70** Multi-Kueue N clusters (N concurrent Kueue backends each staging to its REG-05-assigned bucket set with DIST-01 preserved, per-cluster probe + `backend_id`-scoped reconcile + per-backend failure isolation, per-(backend,bucket) cleanup; **depends on 69**; **research flag** = `cloud_job` one-row-per-file-vs-per-(file,backend) + `agent_ref`→`Agent.id` resolution + live multi-cluster kr8s auth/multi-bucket staging; MKUE-01..04). **71** Deployment/Config/Docs & N-Lane UI (N registry-derived per-backend lanes read-only on the existing `/pipeline/stats` poll + master revert-to-all-local toggle + runbook/config docs incl. `cloud_target`→`backends` migration; UI-hinted; **depends on 70**; BEUI-01..03). Build order 67→68→69→70→71, strictly sequential (each hard-depends on the prior). Design spine locked (`docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md`, PR #182); REG-05 + revised MKUE-02/04 supersede its one-shared-bucket decision per operator direction. Version provisional CalVer `2026.7.1`, finalized at release. Each phase = own PR (worktree branch, never direct to main); 67–68 behavior-preserving refactors that de-risk 69.
- 2026.7.0 Engineering Improvements roadmap created (2026-07-02): 4 phases (63-66), continuing from v7.0's last integer phase (62; 57.1 was a decimal insert). Cleanup / engineering-debt paydown — **no product-behavior change, no backend behavior change**; the "user" is the maintainer/operator. 13/13 requirements mapped, 0 orphans, 0 duplicates. **63** Parallel CI & Code-Change Gating (partition the ~1,750-test suite into workflow-step buckets + fan out across parallel jobs + combine per-shard `.coverage` → one Codecov upload + doc-only skip-with-success; the tightly-coupled CI-01/02/03 land together and CI-04 rides the same CI-workflow PR; CI-01..04). **64** Per-Module Coverage Uplift & Gate Raise (raise worst-offender/v7.0-touched modules — agent_liveness/shell/pipeline/tracklists/routers.pipeline/main + the 71–78% tail — to a per-module floor with behavior-asserting tests, then lift the enforced gate above 90.38% wired into CI; **depends on 63** because the combined-across-shards coverage number must be trustworthy before a higher gate enforces on it; COV-01/02). **65** CalVer Adoption (replace `vN.M` with `YYYY.MM.REVISION`, no leading-zero month, first tag `2026.7.0`, across release procedure + badges + image tags + milestone↔version mapping, historical record intact; independent parallel-friendly phase; VER-01..04). **66** Docs-Drift Gate & Dead-Code Sweep (CI gate cross-checking REQUIREMENTS.md traceability vs passed phases + `/saq` re-link in the shell Agents/Compute page + vestigial dead-code removal incl. the dead-template guard's own blind spot; **depends on 63** for the CI-gate slot, CLEAN otherwise independent; UI-hinted; DOCS-01, CLEAN-01/02). Build order 63 → 64, with 65 and 66 parallel-friendly (66's DOCS-01 sequenced after 63). This milestone *adopts* CalVer — the last `vN.M` planning cycle; its release is the first CalVer tag. Candidates sourced from the ROADMAP Backlog + v7.0 RETROSPECTIVE. Each phase = own PR (worktree branch, never direct to main).
- v7.0 UI Redesign (DAG-Centric Hybrid Console) roadmap created (2026-06-29): 6 phases (57-62), the phase structure locked in REQUIREMENTS.md honored exactly, 25/25 requirements mapped (no orphans, no duplicates), dependency-strict build order 57→58→59→60→61→62. **57** Shell & DAG rail (three-column shell, DAG-rail-as-nav, `/`=Analyze default, ⌘K + status strip, brand/theme preserved, ≤1-hop legacy redirects; the load-bearing risk phase — locks `#stage-workspace` swap target, single `/pipeline/stats`→OOB fanout, `$store.pipeline` cross-swap survival, `htmx:historyRestore`, focus/ARIA baseline, seeded dead-template AST guard; stack bumps htmx 2.0.10/Alpine 3.15.12/Tailwind 4.3.2 + SRI recompute; SHELL-01..05). **58** Enrich + Analyze workspaces (Discover/Metadata/Fingerprint + Analyze 3 lane cards local/A1/k8s with Kueue quota-wait vs Inadmissible; reuse stats_bar OOB seed, NO second poll loop; WORK-01..05). **59** Identify workspaces (Track-ID surfaces EXISTING audfprint+Panako + rapidfuzz signals ONLY — IDENT-01 re-scoped off AcoustID/MusicBrainz → deferred IDENT-03; Tracklist Search→Scrape→Match 3-step; IDENT-01..02). **60** Review & Apply (unified before→after diff + per-file Approve/Edit/Skip; bulk approve-high-conf = SERVER-evaluated predicate at a fixed threshold, not a client id-list; dedupe keeper-select; cue preview; audit+reversible; REVIEW-01..05). **61** Full record + ⌘K + Agents (per-file record slide-in, ⌘K over existing search w/ `@alpinejs/focus@3.15.12`, ephemeral Job-based k8s Agents identity, first-run empty state; RECORD-01..04). **62** Polish & cutover (a11y audit, dead-template guard green after removing legacy page wrappers/keep partials, docs/README, narrow rail-collapse; CUT-02 necessarily LAST; CUT-01..04). IA/presentation rewrite over existing routers/services — **no backend behavior change**; visualizes v6.0 local/A1/k8s routing. No phase needs a research-phase (all patterns in-repo; verified 2026-06-29). Each phase = own PR (worktree branch).
- v6.0 Kubernetes Burst Analysis roadmap created (2026-06-27): 5 phases (52-56), one per requirement category, in dependency order mirroring v5.0 (image → legs → pipeline → routing seam → deploy) — **52** Job-runner image & one-shot entrypoint (x86 GHCR image FROM existing essentia base, zero new pip deps; one-shot httpx-GET → windowed analyze → POST `/api/internal/agent/*` reconciled by `file_id` → exit; honest exit codes; internal CA baked in; KJOB-01..05); **53** S3 object-staging leg (control-plane aioboto3 presign PUT/GET + delete, file-server agent httpx-PUT uploads bytes, pod presigned GET — DIST-01 preserved, agent+pod S3-credential-free; `file_id`-scoped keys, cleanup on every terminal outcome + bucket-lifecycle TTL backstop; `endpoint_url` any-S3, `_FILE` secrets; `cloud_job` sidecar Alembic migration; KSTAGE-01..05); **54** Kube submit/watch + reconcile cron (kr8s submit of a suspended `batch/v1` Job labeled `kueue.x-k8s.io/queue-name`, deterministic name keyed to `file_id`; fast submit returns in seconds, periodic `reconcile_k8s_jobs` cron owns lifecycle; out-of-band callback is the ONLY authoritative result; Inadmissible-vs-Pending; bounded max-attempts re-drive → ANALYSIS_FAILED, no cross-target fallback; NO `process_file:<id>` ledger seed — highest-risk phase; KSUBMIT-01..06); **55** Routing/state/ledger integration — the ONE live-seam edit (`cloud_target` Literal["local","a1","k8s"] selector + `stage_cloud_window` K8s branch enqueuing `upload_file_s3`; reuse duration router + AWAITING_CLOUD hold + advisory-locked in-flight window + `PUSHING`/`PUSHED` states + `cloud_phase` column; `enqueue_router` frozenset additions + AST guard against over-enqueue; ledger-scoped backfill; KROUTE-01..05); **56** Deploy/runbook/config/docs (cluster-admin Kueue/RBAC/Secret runbook for objects phaze does NOT create, least-privilege namespaced Role, transport-agnostic Tailscale-OR-WireGuard endpoints, pydantic-settings `_FILE` + fail-fast model validator, startup LocalQueue validation, ephemeral Job-based identity in Agents UI vs perpetually-DEAD, master toggle revert; KDEPLOY-01..05). 26 requirements, 100% coverage, no orphans. **No phase needs a research-phase** — kr8s/aioboto3/Kueue v1beta2 verified same-day against Context7/official docs; each phase has a direct v5.0 precedent. Two new control-plane deps vs v5.0 (`kr8s`, `aioboto3`); zero new pip deps in the Job image. Each phase = own PR.
- v5.0 Cloud Burst Analysis roadmap created (2026-06-24): 5 phases (47-51), one per requirement category, in dependency order — **47** Official arm64 essentia agent image (build from source on a native arm64 CI runner, GHCR publish, parity guard; CLOUDIMG-01..03); **48** Compute-agent type (`kind="compute"` media-less agent, drains per-agent SAQ queue + HTTP result PUT, Agents-page badge/liveness/depth; CLOUDAGENT-01..03); **49** Duration routing & backfill (capability-aware `enqueue_router` on `metadata.duration` threshold default 90min, "awaiting cloud" hold when no compute agent online, backfill the 144 `analysis_failed` long files via the Phase 45 scheduling ledger; CLOUDROUTE-01..04); **50** Push pipeline (control-plane "stay one ahead" orchestrator + file-server `push_file_to_cloud` rsync/SSH-over-Tailscale to A1 scratch, `ProcessFilePayload.ephemeral`, sha256 verify, scratch delete, idempotent re-drive; CLOUDPIPE-01..05); **51** Deployment/config/docs (`docker-compose.cloud-agent.yml` + Tailscale, all pydantic-settings knobs with `_FILE` secrets, OCI A1 + Tailscale-ACL runbook scoping A1→lux:{5432,6379,8000}+nox→A1:22 + least-privilege queue role, master enable toggle; CLOUDDEPLOY-01..04). 18 requirements, 100% coverage, no orphans. Design brainstormed + approved this session. Replaces the "Distributed cloud analysis" backlog item (now narrowed to rsync-over-Tailscale to A1 local disk — no object storage — because arm64 essentia builds from source, proven on `spike/arm64-essentia-analysis`). Each phase = own PR.
- Phase 46 added (2026-06-23): Heartbeat Starvation Fix — decouple the agent liveness heartbeat from the SAQ worker concurrency pool. Surfaced by live incident: agent `nox` showed `DEAD` (last seen 39m ago) while the `phaze-agent-worker` container was healthy and pegged at ~394% CPU. Root cause: `heartbeat_tick` is a SAQ `CronJob` registered in the same agent worker (`agent_worker.py:227`), so it competes for the same `worker_max_jobs=8` concurrency slots as `process_file`. With all 8 slots full of multi-hour analysis jobs (long concert sets, 2–3.6h each), the 30s heartbeat cron could only run when a slot freed (~every 50 min) → `last_seen` exceeded the 300s staleness threshold (`constants.py:61`) → control plane marked the busy agent DEAD, which also blocks new agent-task routing (fingerprint/metadata). Fix: run the heartbeat independent of the job concurrency pool so a saturated worker still reports liveness. Distinct from the Phase 43 analyze-throughput work (that bounds job cost; this guarantees liveness regardless of job cost). NOTE: phase.add mis-numbered it 43 (counted dirs, max=42; collided with shipped text-only 43/44/45) — manually renumbered to 46 + dir renamed to `46-heartbeat-starvation-fix`.
- Phase 45 added (2026-06-18): Scheduling Ledger for Orphan Recovery — surfaced by live incident: clicking "Recover orphaned work" (`recover_orphaned_work(force=True)`) bypassed the loss-detection gate and reconciled the ENTIRE complement-of-done backlog of all 8 stages, detonating the queue to ~44,500 jobs over ~11,400 never-scheduled DISCOVERED files. Root issue: no record anywhere that a stage was ever *scheduled* for an item (pending sets = complement-of-done). Operator principle: recovery must only re-queue work that was previously scheduled and lost; never-scheduled work is not yet orphaned. Approach: durable scheduling ledger written at the single `before_enqueue` chokepoint (`apply_deterministic_key`), cleared on completion (`increment_completed` after_process); `recover_orphaned_work` = ledger − live saq_jobs keys − completed. Survives a saq_jobs truncate (the only real post-Phase-36 loss case). `force` becomes "reconcile the ledger now," not "sweep the backlog." Successor to the Phase 39–42 DAG-manual-control + recovery line. NOTE: phase.add mis-numbered it 43 (collided with existing 43/44, wrong dir tree) — manually renumbered to 45 + dir moved to `.planning/phases/45-...`.
- Phase 30 added (2026-06-09): Fix systemic control-plane SAQ queue misrouting — every manually-triggered enqueue (9 sites across pipeline.py, tracklists.py, scan.py/ingestion.py) targets the consumer-less `default` queue. Surfaced by live incident: "Run analysis" stranded 11,428 `process_file` jobs. See phase CONTEXT.md.
- Phase 31 added (2026-06-10): Windowed Time-Series Audio Analysis — surfaced by live incident after v4.0.9 redeploy: `RhythmExtractor2013` `OnsetDetectionGlobal` buffer overflow crashes whole-file BPM on multi-hour sets (79% of the 11,428-file archive is >50 MB), 0 files analyzed. Fix = stream-decode + per-window analysis (two tiers: BPM/key 30s, mood/style/danceability 3min), queryable `analysis_window` child table + aggregates on `analysis`. Design spec: docs/superpowers/specs/2026-06-10-windowed-analysis-design.md. Brainstormed decisions: scope=everything-as-time-series via two tiers; storage=queryable child table (option B); UI=compact+HTMX-expand timeline (option B). Ships v4.0.10.
- Phase 34 added (2026-06-10): Pipeline Queue-Depth Status & Double-Enqueue Guard — surfaced by live UX bug: operator clicked "Run Analysis", refreshed, and all status vanished (DB shows files as `DISCOVERED` whether or not enqueued; verified 11,429 incomplete `process_file` jobs live on `phaze-agent-nox`, 0 analyzed, button still clickable → double-enqueue risk). Fix = read live SAQ queue depth (`Queue.count`) via `app.state.controller_queue` + per-agent `task_router`, new `get_queue_activity` service, surface through existing 5s `/pipeline/stats` poll, persistent OOB "Processing" card (progress = DB `analyzed`/(analyzed+agent_busy)), coarse Alpine `$store.pipeline` button disable (agent_busy gates Analyze/Fingerprint/Metadata; controller_busy gates Proposals). Brainstormed decisions (operator, 2026-06-10): disable scope = coarse (all agent buttons); indicator = progress bar + counts; progress `done` = DB analyzed count (not SAQ `complete`, survives worker restart). NOTE: numbered 34 because phase.add counted directories (max=31) and collided with the text-only Phase 32/33 entries; renumbered 32→34 + directory renamed. Ships a subsequent v4.0.x.
- Phase 35 added (2026-06-11): Pipeline Determinism, Idempotency & Per-Job-Type Observability — surfaced by the 2026-06-11 queue-doubling incident (random-uuid `process_file` jobs from the pre-Phase-32 "Run Analysis" path couldn't dedup against the new deterministic-key re-enqueue → live queue doubled to ~22,830 jobs over 11,428 files; cleaned via purge + cron rebuild). Generalizes the Phase 32 deterministic-key fix to the WHOLE pipeline. Five items: (1) centralized enqueue-layer deterministic keys `<task>:<natural_id>` for all job types (only `process_file` keyed today); (2) audit/ensure all task DB writes upsert (most already D-26; gaps = proposals, execution_log, tag_write_log); (3) remove auto metadata-extraction from discovery/scan (`agent_files.py:130-161`, `ingestion.py:183-191`) → manual-only; (4) add a "Metadata" stage card between Discovered and Fingerprinted; (5) per-job-type progress bars backed by maintained per-function counters. Locked decisions (operator): (A) centralized key enforcement; (B) maintained per-function counters. Ships a subsequent v4.0.x.
- Phases 36/37/38 added (2026-06-12): Stage Pause + Per-Stage Priority feature, brainstormed and approved (design kept INLINE in conversation — no spec doc; see auto-memory `project_stage_pause_priority_design`). **36** = migrate SAQ queue Redis→Postgres backend (`saq[redis]`→`saq[postgres]`, psycopg3 pool separate from SQLAlchemy/asyncpg, new `PHAZE_QUEUE_URL`) to unlock native Postgres-only per-job `priority`+`scheduled` control; regression-check Phases 32/33/35; includes Step D homelab change-prompt deliverable. **37** = `pipeline_stage_control` table + pause/priority API + enqueue hook, operating on `saq_jobs` via UPDATEs (pause=drain via `scheduled=SENTINEL` park, resume sentinel-guarded; priority default 50 range 0–100 LOWER=sooner=SAQ priority direct, reorders queued backlog live); scope = 3 agent stages (metadata/analyze/fingerprint). **38** = DAG UI pause toggle + priority stepper (▲Higher decrements number, ▼Lower increments) per agent node, extend `/pipeline/stats`, REMOVE the "Rescan Files" anchor (was a duplicate of Start Scan → same `POST /pipeline/scans`). Each phaze phase = own PR. Confirmed SAQ Postgres dequeue `ORDER BY priority, scheduled` + `now>=scheduled` gate in `saq/queue/postgres.py:644-662`.
- Phases 39/40/41/42 added (2026-06-14): **"DAG is the single manual control surface; automation only in recovery"** theme. Surfaced by operator audit of the DAG: the tracklist sub-chain (Scan/Search, Scrape, Match) is display-only on the DAG (triggers live on Tracklists/Proposals pages), the empty-state's "starts automatically" is unwired (`metadata_extraction` does NOT chain to `search_tracklist`; no cron sweeps unmatched files), and `reenqueue_discovered` runs unconditionally every 5 min → effectively auto-runs Analyze. Operator decisions (2026-06-14): run BOTH name-search AND fingerprint-scan over all files, no fallback, as TWO separate phases; Scrape/Match = bulk-over-pending; automatic enqueue ONLY in recovery mode, restoring ALL stages. **39** = Tracklist Search DAG node (bulk `search_tracklist`, gated on Metadata done). **40** = Tracklist Fingerprint-Scan DAG node (bulk `scan_live_set`, gated on discovered + online agent; independent of 39). **41** = Scrape + Match DAG triggers (bulk-over-pending, gated on ≥1 tracklist; depends 39+40). **42** = Recovery-only automation (replace the 5-min `reenqueue_discovered` cron with restart/queue-loss detection reconciling ALL in-flight stages; zero steady-state auto-enqueue; depends Phase 32). Plan order: 39 first. Each phase = own PR.

### Decisions

(Full milestone decision log archived in `.planning/milestones/v4.0-ROADMAP.md` Milestone Summary. Current-cycle decisions accumulate here.)

- [Phase ?]: Phase 37-01: pipeline_stage_control is a standalone app table separate from SAQ-owned saq_jobs; priority SmallInteger with DB CHECK 0-100 keeps stages inside SAQ's 0-32767 dequeue window
- [Phase ?]: Phase 37-01: STAGE_TO_FUNCTION/_FUNCTION_TO_STAGE/SENTINEL=9999999999 live in a DB-free constants module so the agent worker can import them without crossing the ORM import boundary
- [Phase ?]: 37-02: apply_stage_control reads pipeline_stage_control via job.queue.pool (psycopg3), never SQLAlchemy, keeping the agent import boundary intact (T-37-04)
- [Phase ?]: 37-02: 5s TTL cache (single monotonic window) collapses bulk-enqueue control reads; resume keeps AND scheduled=:SENTINEL guard so retry backoffs are never clobbered
- [Phase 37]: 37-03: assert dequeue ORDER + saq_jobs.priority COLUMN, never the deserialized Job.priority (a raw column UPDATE does not rewrite the serialized job blob)
- [Phase 37]: 37-03: shared tests/integration/conftest.py stage_env fixture (real build_pipeline_queue queue + SQLAlchemy session on the same DB + seeded pipeline_stage_control) proves the helpers on the live saq_jobs dequeue/count/row-lock contract
- [Phase ?]: 37-04: control endpoints return {stage, priority, paused} from the PipelineStageControl row (durable intent), never a serialized job's priority (Plan-03 column-vs-blob finding)
- [Phase ?]: 37-04: control-row ORM mutation + service-helper saq_jobs UPDATE land in one session.commit(); unknown stage -> 422 via allowlist guard; priority delta clamped [0,100]; no app-layer auth (reverse-proxy internal-realm)
- [Phase 38]: 38-01: removed the unused discovery cta 'Rescan Files' alongside the dead anchor (mk o={} default makes the omission safe); guarded the deletion with a string-absence render assertion
- [Phase 38]: 38-03: get_stage_controls degrade-safe reader returns paused=False/priority=50 defaults on any failure (mirrors _safe_count); _build_dag_context coerces paused to int 0/1 to hold the all-ints x-init invariant (T-35-11)
- [Phase 38]: 38-03: the 6 stage-control keys ride the existing dag.items() OOB loop with zero stats_bar.html edit; one _NEW_STORE_KEYS edit drives the store-literal, int-key, and OOB-seed tests
- [Phase 38]: 38-02: stage_controls reusable Jinja macro (id=stage-controls-<stage>) on the 3 agent chips; pause/resume = TWO x-show-gated static-hx-post buttons (not a bound :hx-post), authoritative-only @htmx:after-request JSON-parse store write, no optimistic mutation (T-38-OOB)
- [Phase 38]: 38-02: agent-chip NODE_LAYOUT gutter widened 182->276px (h 154->250) for the control row; overlap guard min_chip_height bumped 150->240; canvas/SVG grown 720->1000; col-0/col-2/col-3 nodes re-balanced to incoming-edge midpoints
- [Phase ?]: Phase 46-01: agent liveness heartbeat runs as an asyncio background task launched in agent_worker startup (cancelled in shutdown), NOT a SAQ CronJob — a CronJob competed for worker_max_jobs dispatch slots and was starved by multi-hour process_file jobs (busy-agent-DEAD incident); heartbeat_tick kept as a back-compat shim; one-time DELETE of orphaned cron:heartbeat_tick row from saq_jobs documented for redeploy
- [Phase 58]: 58-01: WORK-05 single-poll wired via htmx 'every 5s [document.visibilityState===visible]' trigger filter + visibilitychange foreground-resume listener (not hx-trigger=none toggle) — avoids htmx reprocess double-timer, keeps one poll element
- [Phase ?]: 58-03: Metadata/Fingerprint ship ALL-only bulk triggers wired VERBATIM to existing POST /pipeline/extract-metadata + /pipeline/fingerprint (D-01); NO EXTRACT SELECTED / checkboxes / row-selection (D-02); zero backend change
- [Phase 58]: 58-04: per-file lane is DERIVED from the cloud_job sidecar (no row->local / cloud_phase NULL->a1 / set->k8s); sound because cloud_job rows are written ONLY in cloud_staging.stage_file_to_s3, so a local file never carries one (RESEARCH A1 confirmed)
- [Phase 58]: 58-04: computeOnline added by extending count_active_agents with an optional kind= filter (no second liveness rule); rides the dag.items() OOB loop onto a pre-mounted dag-seed-computeOnline placeholder (B1)
- [Phase 58]: 58-04: the six v6.0 cloud cards reused VERBATIM preserve the quota-wait-vs-Inadmissible role=alert distinction; in-flight rows render the 57.1 mid-flight N/M signal alongside running (D-04), not a bare running
- [Phase 60]: 60-01: D-03/OQ-1 tag-bulk blank-guard is defensive — compute_proposed_tags copies every non-None metadata field so a server-computed comparison never blanks; predicate factored into _qualifies_for_bulk_write and unit-asserted
- [Phase 60]: 60-01: scope refined 2->4 thin routes (all over unchanged logic) — tag-bulk (D-03) + tag-undo (REVIEW-05) have no existing endpoint; both live in tags.py since tags are computed, not RenameProposal rows
- [Phase ?]: Phase 60-03: tag-write reuses shared _diff_row.html via backward-compatible show_edit/show_skip/show_undo flags; Tag SKIP omitted (no tag-skip route), UNDO surfaced in pending cluster
- [Phase ?]: Phase 60-03: Propose is a GENERATION view (reuses _file_table.html over get_pending_proposal_rows, Model = configured settings.llm_model A1), not a diff
- [Phase ?]: Phase 60-04: dedupe/cue wired to VERIFIED endpoints not the UI-SPEC sketch — resolve uses Form canonical_id + sha256_hash key (not group_id/keeper_id); cue APPROVE posts /cue/{id}/generate (generate IS the write, no /approve route)
- [Phase ?]: Phase 60-04: get_cue_review_cards builds the .cue preview via generate_cue_content ONLY (no write_cue_file) — render never writes disk (T-60-CUE); dedupe UNDO rides the existing resolve_response.html OOB toast file_states round-trip, no new template
- [Phase ?]: Phase 60-04: _STAGE_PLACEHOLDER constant retained in shell.py (unused) so the _stage_placeholder.html literal stays in router source and the dead-template guard keeps it reachable until CUT-02 (Phase 62)
- [Phase ?]: Phase 62-04 (CUT-02): v7.0 dead-code cutover complete — 20 legacy tab-era templates deleted, /pipeline/ + /preview/ pure redirects, base.html reduced to logo+theme, dead-template guard green with empty _ALLOWLIST (closure untouched); kept all live shell HX fragments (D-03b)
- [Phase ?]: Phase 62-04: /saq SAQ-monitor in-UI link is a surfaced supersession gap (D-05) — app stays mounted + URL-reachable but no shell link exists; not fixed (would be new capability)
- [Phase 63]: 63-01: pytest-xdist floor >=3.8.0 approved at the blocking legitimacy gate (pytest-dev official, 3.8.0 ~12mo old clears the 7-day exclude-newer cooldown)
- [Phase 63]: 63-01: relative_files=true added for cross-shard coverage combine; concurrency kept [greenlet,thread] with NO multiprocessing (would corrupt CI-03 baseline); fail_under stays 85 (Phase 64 raises it)
- [Phase 63]: 63-01: tests/buckets.json is the single source of truth for the 9 buckets; test-bucket XDIST defaults serial (DB-safe), -n auto opt-in for DB-free buckets
- [Phase 63]: 63-03: tests.yml runs a setup->bucket-matrix->combine topology; all 9 buckets serial (each has DB-fixture tests), matrix fan-out alone gives CI-02; single combine job yields one coverage.xml + one Codecov upload at --fail-under=85 (CI-03), CODECOV_TOKEN scoped to combine only
- [Phase 63]: 63-04: broadened doc-only CI skip to *.md + .planning/** + LICENSE + docs/** + *.txt; conservative keep-only-non-doc classifier keeps code-changed=true for any non-doc path (security T-63-04-01); classifier extracted to shellcheck-clean scripts/classify-changed-files.sh invoked via `just detect-code-changes` (D-10) + unit-tested by tests/shared/test_change_gate.py; ci.yml SHA edge-case block + aggregate-results skip-with-success contract left byte-for-byte unchanged (no paths-ignore)
- [Phase 66]: 66-03: vulture dead-code sweep was a deliberate NO-OP — `just vulture` (min-confidence 80 + whitelist + --ignore-decorators) exits 0 with zero confirmed-dead symbols in src/phaze; the v7.0 CUT-02 cutover + PR #191 already removed the vestigial dead code (as RESEARCH Deep-Dive 3 anticipated). Durable CLEAN-02 artifact = hand-audited vulture_whitelist.py suppressing 20 grep-verified framework/dynamic false-positives (FastAPI/watchdog callbacks, Pydantic schemas, string-annotation casts, has_prev/has_next, deferred-feature scaffolding, heartbeat_tick shim). vulture stays NON-blocking (just recipe only, never CI/pre-commit — T-66-09). DO-NOT-DELETE trio (build_dashboard_context/get_stage_progress/get_queue_activity) never flagged, kept out of the whitelist. Both blocking checkpoints (package-legitimacy + deletion-review) human-approved.

### Pending Todos

None.

### Blockers/Concerns

- 29-HUMAN-UAT.md: real two-host production smoke is verified-docs-only; deferred until file-server hardware is available
- Tech debt parked in v4.0 audit: WR-01..WR-04 (Phase 29), WR-03 (Phase 28 UI), P28-RACE-01 — see `.planning/milestones/v4.0-MILESTONE-AUDIT.md`

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260410-kco | Add Docker image publishing to GHCR following discogsography pattern | 2026-04-10 | 3f91f93 | [260410-kco-add-docker-image-publishing-to-ghcr-foll](./quick/260410-kco-add-docker-image-publishing-to-ghcr-foll/) |
| 260414-quo | Add Discord notification to docker-publish.yml workflow mirroring discogsography pattern | 2026-04-14 | 9c5cedb | [260414-quo-add-discord-notification-to-docker-publi](./quick/260414-quo-add-discord-notification-to-docker-publi/) |
| 260502-lqb | Remove Discord notification step from docker-publish.yml workflow | 2026-05-02 | ea84be2 | [260502-lqb-remove-discord-notification-step-from-do](./quick/260502-lqb-remove-discord-notification-step-from-do/) |
| 260520-bcl | Dedicated local integration-test database on a non-colliding port (env-configurable URLs + `just integration-test`/`test-db` recipes) | 2026-05-20 | adc2970 | [260520-bcl-dedicated-local-integration-test-databas](./quick/260520-bcl-dedicated-local-integration-test-databas/) |
| 260606-qgu | Fix flaky CDN SRI test: self-host audited Tailwind build (jsDelivr per-edge minification varied bytes); no SRI weakening | 2026-06-06 | b76d725 | [260606-qgu-fix-flaky-cdn-sri-test-jsdelivr-serves-t](./quick/260606-qgu-fix-flaky-cdn-sri-test-jsdelivr-serves-t/) |
| 260606-mpm | Fix release tags not publishing version-tagged Docker images to GHCR (push:tags trigger, tag-ref change detection, strengthened guard test, doc pin fixes) | 2026-06-06 | b811a9e | [260606-mpm-fix-release-tags-not-publishing-version-](./quick/260606-mpm-fix-release-tags-not-publishing-version-/) |
| 260606-pjd | Make ci.yml detect-changes robust to force-push: fall back to origin/main diff when github.event.before is unreachable (+ guard test) | 2026-06-06 | d89a00b | [260606-pjd-make-ci-yml-detect-changes-robust-to-for](./quick/260606-pjd-make-ci-yml-detect-changes-robust-to-for/) |
| 260608-mbc | Fix three scan-incident issues: container uid→1000:1000 (Dockerfile), scan_directory surfaces permission-denied walks as failed, ScanBatch.completed_at + migration 015 so elapsed freezes | 2026-06-08 | cdc3c59 | [260608-mbc-fix-three-scan-incident-issues-in-one-pr](./quick/260608-mbc-fix-three-scan-incident-issues-in-one-pr/) |
| 260606-n0y | Reconcile GHCR image paths: cleanup targets canonical bare `phaze`, orphan `phaze/api` documented as deprecated, publish/cleanup parity guard test | 2026-06-06 | a993aea | [260606-n0y-reconcile-ghcr-image-paths-stop-orphanin](./quick/260606-n0y-reconcile-ghcr-image-paths-stop-orphanin/) |
| 260606-n7g | Switch audfprint/panako sidecars in docker-compose.agent.yml to pull published GHCR images (commented build fallback); update deployment docs + tests | 2026-06-06 | 95cd630 | [260606-n7g-switch-audfprint-panako-sidecars-in-dock](./quick/260606-n7g-switch-audfprint-panako-sidecars-in-dock/) |
| 260622-i0w | Add scanned/deduped/unique reconciliation to pipeline UI (Discovery DAG node + Recent Scans FILES cell); explains the discovery-vs-agent count gap as NFC-collision dedup, not lost work | 2026-06-22 | 8d805bd | [260622-i0w-add-scanned-deduped-unique-reconciliatio](./quick/260622-i0w-add-scanned-deduped-unique-reconciliatio/) |
| 260606-nha | Add `phaze agents add` management CLI (token mint + sha256 hash + id-charset validation + queue-name output) and document PHAZE_AGENT_QUEUE = phaze-agent-<agent_id> convention | 2026-06-06 | 602488a | [260606-nha-add-a-phaze-agents-add-management-cli-ge](./quick/260606-nha-add-a-phaze-agents-add-management-cli-ge/) |
| 260620-jvu | Harden Phase 45 code-review warnings: WR-01 nested swallow+log guard on terminal-ack except blocks (scan match-failure, metadata, fingerprint) so a double-failure re-raises the original error; WR-02 `cleared: Literal[True]` on failure-response schemas | 2026-06-20 | d9123af | [260620-jvu-harden-ledger-ack-warnings](./quick/260620-jvu-harden-ledger-ack-warnings/) |
| 260608-i21 | Harden agent model bootstrap against transient download failures: per-file retry with bounded backoff+jitter, explicit httpx timeouts, atomic os.replace, Content-Length truncation check, fail-fast 4xx / retry 5xx (Verified) | 2026-06-08 | b0ddc4f | [260608-i21-harden-agent-model-bootstrap-against-tra](./quick/260608-i21-harden-agent-model-bootstrap-against-tra/) |
| 260608-jbg | Validate model integrity on bootstrap via per-file HEAD Content-Length size check (size-only); shared bounded-retry+timeout across HEAD+GET so no request can wedge the worker; remove count-only gate (always validate); re-download truncated/corrupt files; correct stale ~150MB estimate to ~3.1GB/34 files (Verified). Extends PR #91. | 2026-06-08 | b86babd | [260608-jbg-validate-model-integrity-on-bootstrap-vi](./quick/260608-jbg-validate-model-integrity-on-bootstrap-vi/) |
| 260609-f96 | Fix scan_directory 10s asyncio.TimeoutError: AgentTaskRouter._queue_for built per-agent SAQ queues without the apply_project_job_defaults before_enqueue hook, so agent-dispatched jobs inherited SAQ's 10s default instead of worker_job_timeout=600. Register the hook on each per-agent queue (3rd call site) + regression test. Found live on nox/lux v4.0.4. | 2026-06-09 | c6c7e20 | [260609-f96-fix-scan-directory-10s-timeouterror-regi](./quick/260609-f96-fix-scan-directory-10s-timeouterror-regi/) |
| 260609-glv | Scan-pipeline reliability bundle (3 fixes, surfaced sequentially on v4.0.5): (1) sanitize PG-invalid chars in mutagen tags — _sanitize_pg_text strips NUL U+0000 + lone surrogates U+D800-U+DFFF in _first_str + _serialize_tags (fixes asyncpg UntranslatableCharacterError 500 on metadata writes; preserves valid controls/noncharacters); (2) scan_directory enqueued with timeout=0 (unbounded; Job.stuck stays False) + retries=0 via AgentTaskRouter timeout/retries pass-through — a fixed 600s SAQ timeout killed healthy bulk scans that then retried from scratch and never finished; (3) config.scan_stall_seconds default 600→86400 (24h) so the progress stall reaper is the sole liveness guard. + regression tests. | 2026-06-09 | 4b37c13 | [260609-glv-fix-metadata-write-500-strip-nul-bytes-f](./quick/260609-glv-fix-metadata-write-500-strip-nul-bytes-f/) |
| 260610-fp9 | Add audio system-deps apt layer to shared Dockerfile (libatomic1 ffmpeg libsndfile1 libchromaprint-tools) — v4.0.8 `python:3.14-slim` image had NO apt layer, so every `process_file` job dead-lettered at `import essentia` (`ImportError: libatomic.so.1`), stranding all 11,428 files in `discovered`. Verified via ldd on live v4.0.8 image: prebuilt essentia-tensorflow wheel bundles its heavy deps; only libatomic1 was unbundled+missing — proven sufficient for full `import essentia`. ffmpeg/fpcalc kept for the broader pipeline (ffprobe video metadata, pyacoustid fingerprinting). Needs v4.0.9 release + nox/lux redeploy. | 2026-06-10 | f5fb6e7 | [260610-fp9-add-audio-system-deps-to-dockerfile-so-e](./quick/260610-fp9-add-audio-system-deps-to-dockerfile-so-e/) |
| 260613-t7k | Two pipeline-DAG fixes: (1) widen NODE_LAYOUT chips 180→240px (re-gridded columns 24/392/760/1128, canvas/SVG 1132→1392×1000) so the Phase-38 control row stops clipping "▼ Lower"→"Low"; (2) replace the global `agentBusy` enqueue gate with per-stage busy counts — new degrade-safe `get_stage_busy_counts` reads `saq_jobs` by deterministic-key prefix, seeded as metadataBusy/analyzeBusy/fingerprintBusy onto the dag map (rides existing dag.items() seed + 5s OOB loop, no stats_bar.html edit), so Metadata/Analyze/Fingerprint run in parallel. SAVEPOINT degrade (not rollback) to avoid 500ing the dashboard. 1755 tests pass, 97.55% cov. | 2026-06-14 | 11fc68f | [260613-t7k-widen-pipeline-dag-node-chips-and-make-m](./quick/260613-t7k-widen-pipeline-dag-node-chips-and-make-m/) |
| 260614-sg8 | Fix trigger_scan dead-letter (Tracklists "Scan" tab): `POST /tracklists/scan` enqueued `scan_live_set` with file_id only → dead-lettered against `ScanLiveSetPayload` (`extra="forbid"`, needs file_id+original_path+agent_id), the v4.0.8 incident class. Now loads each FileRecord and enqueues the full payload via `model_dump(mode="json")`; non-UUID / no-FileRecord ids skipped (never 500); `total` = jobs actually enqueued; routing + central deterministic key unchanged. Follow-up to Phase 40 (which fixed only the bulk pipeline path). 1807 pass, 97.59% cov. | 2026-06-14 | (pending PR) | [260614-sg8-fix-trigger-scan-dead-letter-enqueue-sca](./quick/260614-sg8-fix-trigger-scan-dead-letter-enqueue-sca/) |
| 260615-cyp | Fix pipeline DAG rendering as visible text: a JS comment inside the parent `#pipeline-dag` Alpine `x-data="..."` attribute used double quotes (`"no online agent"`), terminating the HTML attribute at the first inner `"` and dumping the entire `nodes` getter into the DOM as text (Phase-40 `fingerprint_scan` comment regression, live on nox/lux v4.2.0). Single-quote the comment + regression guard `test_xdata_getter_has_no_unescaped_double_quotes` asserting the `#pipeline-dag` x-data value holds zero literal `"`. 30 non-DB tests pass; all hooks clean. | 2026-06-15 | 928d229 | [260615-cyp-fix-dag-canvas-xdata-quote](./quick/260615-cyp-fix-dag-canvas-xdata-quote/) |
| 260618-sx6 | Bridge configured LLM API key into litellm (Bug A): generate_proposals failed every run with litellm AuthenticationError because the file-loaded ControlSettings.anthropic_api_key had zero consumers — litellm reads the bare ANTHROPIC_API_KEY env var, never set. Add config.export_llm_api_keys() (exports present SecretStr keys only when unset, operator wins, never logs) called from controller.startup(); unit + functional wiring-guard tests. Full suite 1888 passed, 97.64% cov. Bug B (nox panako/audfprint host alias) handled separately as a homelab fix. | 2026-06-19 | 9e6dd53 | [260618-sx6-pass-configured-anthropic-openai-api-key](./quick/260618-sx6-pass-configured-anthropic-openai-api-key/) |
| 260627-ktb | Uniform supply-chain cooldown via the canonical relative `[tool.uv] exclude-newer = "7 days"` across root + both service pyproject.toml (satisfies semgrep uv-missing-dependency-cooldown). A relative window only resolves when every floor is ≥7d old, so: reverted yesterday's `chore: update deps` (c8574dc) 7 fresh floors to prior ≥7d-old values (alembic 1.18.4, fastapi 0.138.0, litellm 1.85.6, mutagen 1.47.0, numpy 2.4.6, greenlet 3.5.2, ruff 0.15.18), and relaxed redis `>=8.0.1`→`>=8.0.0` (8.0.1 was a <7d Dependabot bump #160, not a security pin; still redis 8, auto-lifts post-cooldown). Resilience: `.github/dependabot.yml` cooldown.default-days=7 on all ecosystems; update-project.sh `ensure_cooldown_window()` re-asserts the relative window uniformly. Also reorganized root pyproject headings+settings. Why the 4 --major packages stay put: constraint-blocked (litellm <1.86 cap; importlib-metadata <9 via litellm; typer <0.26 via huggingface-hub; pydantic-core 2.47.0 alpha-only). ruff 0.15.18 + mypy green. | 2026-06-27 | a8edbf8 | [260627-ktb-upgrade-litellm-and-transitive-deps-fix-](./quick/260627-ktb-upgrade-litellm-and-transitive-deps-fix-/) |
| 260628-wzq | Fix JOB-ENV-CONTRACT (v6.0 milestone-audit critical blocker): the Kueue Job manifest built by `build_job_manifest` injected only `PHAZE_AGENT_CA_FILE`, so every admitted pod exited `EXIT_CONFIG=20` before analysis (`job_runner` requires `PHAZE_JOB_FILE_ID` + agent env). Inject `{"name":"PHAZE_JOB_FILE_ID","value":str(file_id)}` into the container env + an `envFrom` (configMapRef `kube_env_configmap_name` default `phaze-agent-env`; secretRef `kube_env_secret_name` default `phaze-agent-token`, reusing the existing bearer-token Secret), two new defaulted `ControlSettings` knobs mirroring `kube_ca_secret_name` (no change to `_enforce_kube_config_when_k8s`), the agent-env ConfigMap + envFrom documented in `docs/k8s-burst.md` §6, and `test_build_job_manifest_injects_env_contract` (the regression test that would have caught it). 26 + 127 tests pass, mypy clean. | 2026-06-29 | 5f43aa7 | [260628-wzq-fix-job-env-contract-inject-pod-runtime-](./quick/260628-wzq-fix-job-env-contract-inject-pod-runtime-/) |
| 260629-eev | Convert the two ASCII "Architecture at a glance" diagrams (docs/cloud-burst.md, docs/k8s-burst.md) to `mermaid flowchart LR` — lossless (every host/service/object/port/edge label preserved verbatim), `PHAZE_CLOUD_TARGET=local` caption relocated to an italic line below each block. Scope-locked to the two fenced blocks; trees/tables/CLI/architecture.md/superpowers untouched. | 2026-06-29 | 267109b | [260629-eev-convert-the-two-ascii-architecture-at-a](./quick/260629-eev-convert-the-two-ascii-architecture-at-a/) |
| Phase 34 P01 | 12 min | 2 tasks | 2 files |
| Phase 34 P02 | ~10 min | 3 tasks | 4 files |
| Phase 34 P03 | ~8 min | 2 tasks | 4 files |
| Phase 34 P04 | ~18 min | 3 tasks | 4 files |
| Phase 37 P01 | 3min | 3 tasks | 6 files |
| Phase 37 P02 | 12min | 3 tasks | 6 files |
| Phase 37 P03 | ~20min | 2 tasks | 6 files |
| Phase 37 P04 | ~6min | 3 tasks | 5 files |
| Phase 38 P01 | 3min | 2 tasks | 2 files |
| Phase 38 P03 | ~12min | 3 tasks | 5 files |
| Phase 38 P02 | 8min | 3 tasks | 2 files |
| Phase 45 P05 | ~5 min | 1 tasks | 2 files |
| Phase 45 P06 | ~25 min | 2 tasks | 12 files |
| Phase 46 P01 | ~20min | 3 tasks | 8 files |
| Phase 58 P01 | ~11min | 2 tasks | 3 files |
| Phase 58 P02 | 35m | 3 tasks | 9 files |
| Phase 58 P03 | 25min | 2 tasks | 4 files |
| Phase 58 P04 | 8min | 3 tasks | 9 files |
| Phase 60 P01 | 45min | 3 tasks | 7 files |
| Phase 60 P02 | 20min | 3 tasks | 6 files |
| Phase 60 P03 | 25min | 3 tasks | 6 files |
| Phase 60 P04 | 35min | 3 tasks | 7 files |
| Phase 62 P04 | 95min | 3 tasks | 9 files |
| Phase 63 P01 | 8min | 3 tasks | 4 files |
| Phase 63 P02 | 40min | 3 tasks | 265 files |
| Phase 63 P03 | ~15min | 2 tasks | 1 files |
| Phase 63 P04 | ~20min | 2 tasks | 4 files |

## Deferred Items

Items acknowledged and deferred at the v5.0 milestone close on 2026-06-26. All three are
**deployment-gated** — they unblock on the live OCI A1 + Tailscale rollout (see
`.planning/milestones/v5.0-phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md`).

| Category | Item | Status | Why deferred |
|----------|------|--------|--------------|
| verification | 48-VERIFICATION | human_needed | Live Agents-page compute **badge render**; automated coverage MET — only the visual live check pends a running compute agent |
| uat | 48-UAT | partial | Same live compute-agent badge check |
| uat | 50-UAT | partial | Tests 4-7 (real rsync transfer, sha256 mismatch, recovery re-drive, bounded-window staging) need a live compute agent; logic is green |

These are tracked for the v5.0 deploy; they are NOT blockers for the milestone record.

Items acknowledged and deferred at the **v6.0** milestone close on 2026-06-29. All three are
**deployment-gated** — they unblock on the live x64 Kueue cluster + S3 bucket rollout (see
`.planning/milestones/v6.0-MILESTONE-AUDIT.md` and `docs/k8s-burst.md`).

| Category | Item | Status | Why deferred |
|----------|------|--------|--------------|
| uat | 53-UAT | partial | S3 round-trip verified against moto; the live real-S3 leg pends a real bucket |
| uat | 54-UAT | partial | Kube submit/reconcile verified against a fake kube API; live Kueue admission/eviction pends a real cluster |
| uat | 55-HUMAN-UAT | partial | Test 2 (end-to-end K8s routing) blocked on a live Kueue cluster + real S3 — the test that would have caught JOB-ENV-CONTRACT; **re-run FIRST after rollout**. Tests 1+3 passed in-app. |

These are tracked for the v6.0 deploy; they are NOT blockers for the milestone record. The JOB-ENV-CONTRACT seam fix (quick 260628-wzq) makes the live E2E re-run especially important.

Items acknowledged and deferred at the **v7.0** milestone close on 2026-07-02. Both are
**deployment-gated** — they unblock on the next homelab/cluster rollout (see
`57.1-HUMAN-UAT.md`). `57.1-VERIFICATION.md` is already `passed`; these are live confirmations
of behavior already proven by green automated proxies (57.1-UAT tests 5 + 7).

| Category | Item | Status | Why deferred |
|----------|------|--------|--------------|
| uat | 57.1-UAT test 8 (UAT-57.1-01) | deferred-to-live | Real multi-hour `kill -9` mid-analysis on the local/A1 lane; needs the homelab + a real multi-hour concert file. Proxied by transport kill-safety + `put_analysis`-replace idempotency tests (green on real Postgres) |
| uat | 57.1-UAT test 9 (UAT-57.1-02) | deferred-to-live | Live Kueue k8s-lane mid-flight progress post; needs a live cluster. Proxied by the lane-bridge test (green) |

These are tracked for the next deploy; they are NOT blockers for the v7.0 milestone record. Confirm live, then flip the 57.1 UAT notes to passed.

Items acknowledged and deferred at the **2026.7.0** milestone close on 2026-07-03. Unlike the
deployment-gated items above, all three are **already-completed work with stale tracking status**
(surfaced by `gsd-sdk query audit-open`) — none is genuinely open. Recorded here per the acknowledge-at-close protocol.

| Category | Item | Status | Why deferred |
|----------|------|--------|--------------|
| uat | 63-UAT | partial | Phase 63 UAT has **0 pending scenarios** — status simply never flipped to complete; the parallel-CI work shipped in PR #193 |
| quick_task | 260628-wzq (JOB-ENV-CONTRACT fix) | missing | Committed `5f43aa7` (v6.0 audit fix); quick-task tracking file was never marked complete |
| quick_task | 260629-eev (ASCII→mermaid diagram conversion) | missing | Committed `267109b`; quick-task tracking file was never marked complete |

## Session Continuity

Last session: 2026-07-04T06:17:03.889Z
Stopped at: Phase 69 context gathered
Resume file: .planning/phases/69-tiered-drain-scheduler/69-CONTEXT.md

## Operator Next Steps

- **Merge release PR `release/2026.7.0`** (milestone archive + PROJECT/ROADMAP/STATE/RETROSPECTIVE/MILESTONES updates), then **cut the annotated `2026.7.0` tag on the merge commit and push it** — the tag push fires the GHCR publish (see [[project_release_procedure]]; pyproject/uv.lock were already bumped to `2026.7.0` in Phase 65).
- **`.planning/REQUIREMENTS.md` was intentionally kept** (not `git rm`'d) at this close: the Phase-66 docs-drift guard has no existence check and would fail the required CI check if the file were absent. `/gsd:new-milestone` regenerates/overwrites it. Guard hardening captured in ROADMAP Backlog.
- Start the next milestone with `/gsd-new-milestone` (next named milestone = Multi-cloud backends, phases 67+; design already on `main` via PR #182).
