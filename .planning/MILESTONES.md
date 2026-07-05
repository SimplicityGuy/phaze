# Milestones

## 2026.7.1 Multi-Cloud Backends (Shipped: 2026-07-05)

**Phases completed:** 5 phases, 26 plans, 56 tasks

**Shipped via:** PRs #201, #202, #203, #204, #206 · git range `a818d706..c0184295` · 2026-07-03 → 2026-07-04
**Delivered:** Generalized the single `cloud_target` selector into a declarative, cost-tiered backend registry that drains long, locally-timing-out audio files across local + N Kueue clusters + N cloud-compute agents simultaneously — statically configured, no provisioning, zero new dependencies.

**Key accomplishments:**

- **Declarative backend registry (Phase 67, REG-01..05):** `backends.toml` (id/kind/rank/cap) loaded via a stdlib-`tomllib` before-validator keyed on `PHAZE_BACKENDS_CONFIG_FILE`, with a per-file S3 staging-bucket registry (public/shared vs cluster-specific), whole-registry fail-fast validation, a secret-free boot-log projection, and a zero-config implicit-local default — `cloud_target` and the flat `s3_*`/`kube_*`/`compute_*` fields removed with no back-compat shim.
- **Single `Backend` protocol + 3 implementations (Phase 68, BACK-01..04):** Local/ComputeAgent/Kueue behind one `is_available`/`in_flight_count`/`dispatch`/`reconcile` seam; the `if/elif cloud_target` dispatch fork removed; additive `cloud_job.backend_id` migration (029); behavior-preservation proven by a byte-identical D-01 golden characterization snapshot.
- **Tiered multi-backend drain scheduler (Phase 69, SCHED-01..05):** per-file rank-first eligible dispatch, per-backend `cap` count-and-claim under one advisory lock, staleness spill-to-local, black-hole/attempt guard, deterministic stateless tie-break, single recovery owner per kind, and `FileState.LOCAL_ANALYZING` closing the cross-backend double-dispatch race (CR-01) — the first behavior-changing phase.
- **N-Kueue-cluster dispatch (Phase 70, MKUE-01..04):** distinct constructor-authed kr8s client per cluster (token-hack retired), deterministic restart-stable per-file `pick_bucket`, `cloud_job.staging_bucket` migration (030), per-cluster failure isolation, and concurrency-safe clean-before-flip cross-bucket cleanup under the held advisory lock (closing Pitfall 9).
- **N-lane UI + no-redeploy force-local kill-switch (Phase 71, BEUI-01..03):** N registry-derived read-only backend lanes (rank / in-flight / cap / online-offline / per-lane Kueue admission) on the existing 5s poll, a persisted master force-local toggle that makes the drain + both duration-router triggers + the backfill trigger behave as all-local without a redeploy, plus an operator runbook and reconciled config docs.

**Known deferred items at close:** 4 (see STATE.md Deferred Items) — stale docker-compose `PHAZE_CLOUD_TARGET` comments (67), lazy >1-compute fail-fast / PROV-01 backlog (68), no committed force-local-gate regression test (71/W2), and 70-UAT test 7 deployment-gated live-cluster E2E.

---

## 2026.7.0 Engineering Improvements (Shipped: 2026-07-03)

**Phases completed:** 4 phases (63–66), 13 plans, 19 tasks
**Shipped via:** PRs #193, #194, #197, #198, #199 · git range `9b65cf7..f9949cb` · 2026-07-02 → 2026-07-03
**Delivered:** An engineering-debt paydown milestone — faster parallel CI, a per-module coverage floor, CalVer release versioning, a docs-drift guard, and dead-code cleanup — with **no product or backend behavior change**.

**Key accomplishments:**

- **Parallel CI & code-change gating (Phase 63, CI-01..04):** partitioned the ~1,750-test suite into 9 workflow-step buckets (`tests/buckets.json` as the single source of truth) via a behavior-preserving `git mv` of 205 test files, fanned the buckets out across a setup→matrix→combine CI topology, combined per-shard `.coverage` into one Codecov upload, and gated the heavy jobs to skip-with-success on doc-only changes (shellcheck-clean classifier + partition guard).
- **Per-module coverage uplift & gate raise (Phase 64, COV-01/02):** added `scripts/coverage_floor.py` (fail-closed per-module 85% floor over the combined `coverage.json`, with its own unit test), raised the worst-offender / v7.0-touched modules with behavior-asserting tests, and lifted the enforced project gate above the 90.38% baseline — wired into CI so future regressions fail the build.
- **CalVer adoption (Phase 65, VER-01..04):** replaced `vN.M` with `YYYY.MM.REVISION` (no-leading-zero month, first tag `2026.7.0`, per-month zero-based REVISION) across the release procedure, README badge, image tags, `ci.yml` tag glob, and the milestone↔version mapping — bumping pyproject/uv.lock to `2026.7.0` while leaving every historical `vN.M` record and the docker-publish machinery intact.
- **Docs-drift CI gate (Phase 66, DOCS-01):** a hermetic pytest traceability guard cross-checking REQUIREMENTS/ROADMAP/VERIFICATION for 5 drift classes (plus a dead entry-root-literal assertion), wired as a `just docs-drift` step into the always-run code-quality job so drift fails CI even on doc-only PRs — it caught and fixed a stale Phase-65 ROADMAP checkbox on its first run, and was hardened post-merge (PR #199: section-scoped parser, fail-loud on duplicate rows, flag `[x]` reqs missing from the table, escape-hatch seam).
- **`/saq` re-link & dead-code sweep (Phase 66, CLEAN-01/02):** restored a discreet flag-gated `/saq` SAQ-monitor link to the shell Agents page (`rel="noopener"`, gated on `enable_saq_ui`), and added `vulture>=2.16` as a non-blocking `just vulture` recipe with a hand-audited `vulture_whitelist.py` (20 grep-verified framework false-positives suppressed) — the confirmed-dead sweep was a deliberate no-op since the v7.0 CUT-02 cutover already removed the vestigial dead code.

**Known deferred items at close:** 3 (see STATE.md Deferred Items) — all already-completed work with stale tracking status (Phase 63 UAT 0-pending; quick tasks 260628-wzq, 260629-eev committed), acknowledged at close.

---

## Milestone ↔ Version Mapping

Milestones are **named**; releases are **dated** CalVer. Every milestone from `v1.0`
through `v7.0` shipped under the legacy `vN.M` scheme (preserved verbatim below).
Starting with the Engineering Improvements milestone the project adopts CalVer
`YYYY.MM.REVISION` (e.g. `2026.7.0`) with a **no-leading-zero month** (`2026.7.0`,
not `2026.07.0`). REVISION is a **per-month zero-based** counter — the Nth release
within a given `YYYY.MM`, starting at `0` and **resetting each calendar month** — so
milestone NAMES are fully decoupled from version NUMBERS: multiple milestones landing
in the same month, or same-month patch releases, simply increment REVISION within the
shared `YYYY.MM`.

| Milestone | Version | Date |
|-----------|---------|------|
| MVP | v1.0 | 2026-03-30 |
| Metadata Enrichment & Tracklist Integration | v2.0 | 2026-04-02 |
| Cross-Service Intelligence & File Enrichment | v3.0 | 2026-04-04 |
| Distributed Agents | v4.0 | 2026-05-17 |
| Cloud Burst Analysis | v5.0 | 2026-06-26 |
| Kubernetes Burst Analysis | v6.0 | 2026-06-29 |
| UI Redesign — DAG-Centric Hybrid Console | v7.0 | 2026-07-02 |
| Engineering Improvements | 2026.7.0 | 2026-07-03 |

---

## v7.0 UI Redesign — DAG-Centric Hybrid Console (Shipped: 2026-07-02)

**Phases completed:** 7 phases, 28 plans, 69 tasks

**Key accomplishments:**

- htmx 2.0.10 / Alpine 3.15.12 SRI-repinned (live-CDN-verified) + Tailwind 4.3.2 vendored, with a jinja2.meta dead-template AST guard and a collectible SHELL-01..04 test stub both seeded green.
- The v7.0 shell spine: a prefix-less `GET /` (Analyze default) + `GET /s/{stage}` router with the HX fragment/full fork and a 12-id stage whitelist, a three-column `shell.html` that lifts the base.html theme/`$store.pipeline` machinery verbatim around a single `#stage-workspace` swap target, a shared `build_dashboard_context` bridging the Analyze node to the existing DAG canvas, and the `/pipeline/`→`/` rename-redirect.
- The navigation chrome that makes the v7.0 shell usable: a DAG rail nav spine (12 prototype-order nodes, each HTMX-swapping only `#stage-workspace` with `hx-push-url`, `aria-current` active state, and live counts bound to existing `$store.pipeline` keys), a header carrying the wave logo + ⌘K affordance + D-05 agent status strip, and an Alpine ⌘K skeleton modal — all wired into the Plan-02 shell with `syncRailSelection` completed and SHELL-02/03 proven by tests.
- Every legacy tab URL now resolves into the v7.0 shell in ≤1 hop: a conditional 302 at the top of each of the 7 legacy GET handlers (`HX-Request != "true"` → static `/s/<stage>` or `/?palette=1`) moves plain navigations/bookmarks to the canonical shell URL while leaving each handler's in-page HX-filter branch untouched (D-01), proven by an 8-route ≤1-hop redirect-resolution test plus an HX-filter-not-redirected guard.
- Settled the pebble progress transport (Option A parent-side Queue-drainer, SIGKILL-safe) and proved crash-mid-run idempotency byte-identical to a control on real Postgres — both via a non-invasive spike that touches zero production modules; the real multi-hour kill -9 + k8s live-progress proof is operator-approved as deferred-to-live.
- Added `analysis_completed_at` (migration 028), stamped only in the existing `put_analysis` completion branch, and tightened the proposal convergence gate to require it — so a partial in-flight analysis row (written at START by D-03) can never leak into `generate_proposals` with NULL aggregates.
- Built the single lane-agnostic HTTP surface for mid-flight analyze progress: `AnalysisProgressPayload`, a counter-only `post_analysis_progress` sibling handler on the existing `agent_analysis` router that upserts ONLY the two fine-window counts (with zero completion side effects), and a best-effort `agent_client.post_analysis_progress` method that returns None on failure so a dropped POST never fails the analysis job.
- Wired the live analyze progress signal end-to-end: `analyze_file` now fires a sync `progress_cb` (START + per-window bump, `len(natural)` denominator) that stays HTTP/pickle-free, the pebble (local + A1) lane bridges it via a picklable `multiprocessing.Manager().Queue()` sink → a kill-safe parent-side drainer → `post_analysis_progress` (throttled, best-effort), and the k8s one-shot lane runs `analyze_file` in `asyncio.to_thread` with a `run_coroutine_threadsafe` fire-and-forget cb — all throttled by a new `analysis_progress_interval_sec` knob, with a dropped progress POST never failing the job and never altering the EXIT_ANALYSIS/EXIT_CALLBACK contract.
- The v7.0 shell now live-refreshes through exactly one persistent `/pipeline/stats` poll wired into chrome (with a `visibilitychange` shed), and the single Phase-58 test file is seeded with filled foundation tests + xfail workspace stubs.
- Built the three shared presentation partials every v7.0 workspace composes against (scaffold macro, generic file table, persistent OOB seed-target host) and shipped the first real workspace — Discover — as a content-only fragment with live recent scans, a derived not-yet-enriched backlog count, and SCAN/RECOVER, all riding the single Plan-01 chrome poll with no second loop.
- Replaced the Phase-57 placeholders for the Metadata and Fingerprint stages with their real workspaces — sibling content-only fragments of identical shape (scaffold + queue table + a single ALL-only bulk trigger), each wired VERBATIM to its existing enqueue endpoint with the R-4 double-enqueue guard, live via the one chrome poll, with zero backend behavior change.
- Replaced the Phase-57 bridged Analyze placeholder (dag_canvas.html) with the real Analyze workspace — three always-present execution-lane cards (local / A1 / k8s) with offline-vs-not-configured labels and the reused v6.0 quota-wait-vs-Inadmissible cloud cards, plus ONE table of every in-stage file carrying a derived lane badge and the live 57.1 mid-flight windowed-progress signal — all riding the single chrome poll with zero backend behavior change (one read-only multi-state SELECT + one derived seed).
- Phase-59 test scaffold plus two degrade-safe read-only helpers (get_trackid_stage_files / get_tracklist_set_rows) that assemble the per-file Track-ID identity rows and per-set Tracklist coverage rows Plans 02/03 will render.
- The Track-ID stage now serves one combined, read-only per-file identity table (File · audfprint · Panako · Tracklist · Confidence) that surfaces the existing audfprint/Panako `FingerprintResult` state and the linked/best-candidate tracklist match + confidence, superseding the `trackid` placeholder.
- The Tracklist stage now serves three sequential Search·Scrape·Match step cards — each with its own R-4-guarded ALL trigger wired verbatim to the existing bulk endpoint — over a per-set table showing N/M track coverage, superseding the `tracklist` placeholder with no backend change.
- Four thin routes over unchanged apply/generation logic — D-02 server-predicate bulk-approve, D-05 validated inline-edit, D-03/OQ-1 no-discrepancy tag-bulk, and REVIEW-05 tag-undo — plus the Wave-0 test scaffold + seed factories that gate every later Review workspace plan.
- The ONE shared before→after `_diff_row.html` partial (D-06) plus the Rename/Path and Move-files review diff workspaces — per-file Approve/Edit/Skip over verified PATCH routes and the id-less server-predicate bulk-approve header — wired at `/s/rename` and `/s/move` over a degrade-safe pending-proposal read.
- The D-01 Propose generation view (pending-proposal list + configured Model + Conf + GENERATE ALL over the existing `/pipeline/proposals` trigger — NOT a diff) plus the Tag-write review diff workspace that reuses the ONE shared `_diff_row.html` over the computed tag comparison, applying via `/tags/{id}/write` + `/tags/{id}/undo` and the id-less D-03 `/tags/bulk-write-no-discrepancies` bulk header, over a new EXECUTED-only degrade-safe read.
- The final two Review & Apply placeholders superseded over existing endpoints with zero backend change: the D-07 Dedupe keeper-select workspace (a radio keeper posting the VERIFIED `/duplicates/{sha256_hash}/resolve` `canonical_id` contract with a page-scoped AUTO-KEEP and the stateful `file_states` undo round-trip) and the D-08 Cue preview workspace (in-memory `.cue` previews built with `generate_cue_content` — no disk write — with APPROVE wired to `/cue/{id}/generate` as the write and visibly gated ineligible cards), completing the Review & Apply gate with all six workspaces live.
- Landed @alpinejs/focus@3.15.12 (SRI-pinned, before Alpine core) in both shell.html and base.html, extended the SRI gate to shell.html, and stood up the 11-test RED behavior scaffold + conftest factories that Plans 02-05 turn green.
- Built `GET /record/{file_id}` — a typed-uuid, strictly file_id-scoped, read-only bare HTMX fragment composing the file's windowed timeline, metadata diff, identity, inline-approvable pending approvals, and history into a persistent `x-trap` focus-trapped slide-in over the shell, opened from Analyze file rows (and, by contract, ⌘K).
- Made the Phase 57 ⌘K skeleton functional (RECORD-02): the /search/ HX branch is now a grouped Files/Tracklists/Discogs/Artists/Commands listbox over the existing search service plus a new read-only `distinct_artists()` facet, with a fully keyboard-navigable, `x-trap` focus-trapped palette (roving ↑/↓, Enter activation, ARIA combobox/listbox, debounced hx-get).
- Turned the Agents page into the RECORD-03 two-section surface — Section 1 the existing heartbeating agents (local/A1) reused verbatim, Section 2 a distinct "Compute / burst lanes · ephemeral" k8s section driven by a new read-only `classify_compute_lanes()` CloudJob aggregation (Active/Waiting/Idle, never a perpetually-DEAD agent), both refreshing on the existing single 5s self-poll.
- When no files exist, the home/Analyze workspace now renders a centered first-run guide that lists each registered agent's already-configured scan_roots with a per-root "Scan {agent}" button posting the discovery scan — zero new input surface — while the single existing poll stays clean.
- Locked the WCAG-2.1-AA CUT-01 baseline with a browser-free pytest structural guard, closed the one real gap (⌘K combobox accessible name), and removed the dead detail-pane aside — no new dependency, no logic change.
- Task 1 — rail.html collapse + glyphs (commit `ad2ae59`)
- Refreshed README + docs/architecture.md + docs/project-structure.md + docs/quick-start.md to describe the v7.0 DAG-centric three-column console (rail-as-nav, /s/<stage> HTMX stage swaps, ⌘K command palette, header status strip, per-file record slide-in), locked by a new pure-filesystem docs-currency guard.
- v7.0 tab-era UI removed: 20 legacy wrapper/partial templates deleted, /pipeline/ + /preview/ made pure redirects, base.html reduced to logo + theme, and the dead-template guard is green with an empty allowlist — while every live shell/record HX fragment was kept.

---

## v6.0 Kubernetes Burst Analysis (Shipped: 2026-06-29)

**Phases completed:** 5 phases (52–56), 27 plans, 44 tasks

**Delivered:** Long sets that can't finish locally now run as ephemeral, quota-scheduled **Kueue batch Jobs** on a remote x64 cluster — a third analysis-routing target alongside local and the v5.0 OCI A1 — reusing the v5.0 control-plane choreography with the execution unit changed from a persistent SAQ-draining host to a one-shot per-file Job.

**Key accomplishments:**

- **K8s as a third analysis target (Phase 55):** a single `cloud_target` selector (`local` / `a1` / `k8s`) under the `cloud_burst_enabled` toggle routes ≥threshold long files through the Kueue path, wired as ONE new branch in the existing duration router / advisory-locked `stage_cloud_window` in-flight window, reusing PUSHING/PUSHED states + a `cloud_phase` sidecar column, a static AST over-enqueue guard, and ledger-scoped backfill (the `cloud_burst_enabled` boolean was replaced by the selector).
- **x86 Job-runner image + one-shot entrypoint (Phase 52):** built FROM the existing essentia base with **zero new pip deps**; presign-download → sha256-verify → windowed analyze → POST `/api/internal/agent/*` (reconciled by `file_id`) → exit, with an honest distinct-exit-code contract and a runtime-mounted internal CA (no bake, no `verify=False`).
- **S3 object-staging leg (Phase 53):** control-plane aioboto3 presign/cleanup is the **sole S3 importer** (preserving the CI-enforced DIST-01 no-media boundary); the file-server agent uploads bytes over httpx with no SDK/creds; the pod fetches a just-in-time presigned GET; `file_id`-scoped objects are deleted on every terminal outcome + a bucket lifecycle TTL backstop; any S3-compatible backend via `_FILE` secrets.
- **Kube submit + reconcile cron (Phase 54):** a pure kr8s seam idempotently submits a suspended `batch/v1` Job (deterministic `phaze-analyze-<file_id>` name); a fast non-blocking submit + a `*/5` reconcile cron own the Workload lifecycle, Inadmissible-vs-Pending detection, bounded re-drive, and cleanup — with the out-of-band callback as the **sole** authoritative result and **no `process_file` ledger seed** for k8s files (so `recover_orphaned_work` never re-enqueues them onto an agent queue).
- **Operability (Phase 56):** cluster-admin Kueue/RBAC/Secret runbook (`docs/k8s-burst.md`, least-privilege namespaced Role), full `_FILE`-secret config knob table, transport-agnostic (Tailscale *or* WireGuard) endpoints, a non-fatal LocalQueue startup probe surfaced as an amber dashboard alert, an ephemeral Job-based Agents-UI identity (never perpetually-DEAD), and a single-toggle revert to all-local.

**Stats:** ~8,450 LOC added across 61 files since v5.0; 27 plans, 44 tasks; ~3 days (2026-06-27 → 2026-06-29). 3 additive migrations (025/026/027 — the `cloud_job` sidecar). Two new control-plane deps (`kr8s`, `aioboto3`); zero new deps in the Job image. Full suite green (2474 passed). 26/26 requirements (KJOB/KSTAGE/KSUBMIT/KROUTE/KDEPLOY) validated, +2 bonus (KROUTE-06, KDEPLOY-06).

**Milestone audit:** Passed after closing one critical cross-phase blocker — **JOB-ENV-CONTRACT** (the Kueue Job manifest injected only the CA env, so every admitted pod would have exited code 20 before analysis); fixed inline via quick task `260628-wzq` (inject `PHAZE_JOB_FILE_ID` + `envFrom`). See `milestones/v6.0-MILESTONE-AUDIT.md`.

**Known deferred items at close: 3** (deployment-gated live K8s + real-S3 E2E — UAT phases 53/54/55; see STATE.md Deferred Items). The live E2E must be re-run FIRST after the live rollout — it is the test that would have caught JOB-ENV-CONTRACT.

---

## v5.0 Cloud Burst Analysis (Shipped: 2026-06-26)

**Phases completed:** 5 phases, 23 plans, 39 tasks
**Requirements:** 19/19 satisfied (CLOUDIMG, CLOUDAGENT, CLOUDROUTE, CLOUDPIPE, CLOUDDEPLOY)

**Delivered:** Long-duration audio that times out locally is now analyzed unattended on a free OCI Ampere A1 (arm64) compute agent over Tailscale — duration-routed, rsync-pushed, sha256-verified, and reverted to all-local by a single master toggle.

**Key accomplishments:**

- **Phase 47 — Official arm64 essentia image:** `Dockerfile.agent-arm64` builds essentia from source (the wheel is x86-only) on Python 3.13 + TF 2.20.0, built on a native `ubuntu-24.04-arm` CI runner (no QEMU) and published to GHCR only after a numeric-parity guard compares arm64 `analyze_file` output against an x86 golden (BPM/key exact, model scores within epsilon).
- **Phase 48 — Compute-agent type:** a media-less `kind="compute"` agent (empty scan roots, no app-ORM access, DIST-04) that drains its per-agent SAQ queue and PUTs results over HTTP, surfaced on the Agents admin page with a kind badge.
- **Phase 49 — Duration routing & backfill:** a per-file duration router holds long files in `AWAITING_CLOUD` (never silently analyzed locally) and a ledger-scoped backfill re-drives the timed-out long files — no whole-backlog over-enqueue.
- **Phase 50 — Push pipeline:** a `stage_cloud_window` cron keeps ≤N files staged-or-in-flight; `push_file` rsyncs over SSH-over-Tailscale (shell-free argv, pinned host keys, 0600 secret temps); the compute agent sha256-verifies the scratch copy before analyzing and cleans it up; idempotent, ledger-tracked re-drive.
- **Phase 51 — Deployment, config & docs:** `docker-compose.cloud-agent.yml` (arm64, host-Tailscale, no media, named scratch), the `cloud_burst_enabled` master toggle gating all three cloud entry points, the homelab OCI A1 + Tailscale-ACL + least-privilege Postgres broker runbook, and the full config/docs surface.

**Post-audit hardening (PRs #161/#162):** cloud-agent compose `python3 -m saq` start command (the `uv run` override would have prevented the arm64 container from starting), `compute_scratch_dir` fail-fast guard, scratch-dir-skew diagnostic, and the WR-03 push-timeout-coupling guard.

**Deferred (deployment-gated, unblock on the live OCI A1 rollout):** 48 live admin-badge render, 50-UAT tests 4-7 (real rsync transfer / mismatch / recovery). See STATE.md Deferred Items.

---

## v4.0 Distributed Agents (Shipped: 2026-05-17)

**Phases completed:** 6 phases, 47 plans

**Delivered:** Phaze is now a two-host system — an application-server control plane (API, UI, Postgres, Redis, fileless workers; no file mounts) and one or more file-server agents that own music/video files locally, pull jobs from per-agent SAQ queues, and write every state change back over authenticated HTTPS.

**Key accomplishments:**

- `agents` table + `agent_id` columns on FileRecord/ScanBatch, two-step Alembic migration (012 add+backfill, 013 NOT NULL+UQ swap) with `legacy-application-server` seed preserving v3.0 corpus end-to-end
- Internal `/api/internal/agent/*` HTTP surface (files, metadata, fingerprint, analysis, tracklists, proposals, execution-log, scan-batches, exec-batches, heartbeat, whoami) with token-hash auth deriving `agent_id` from bearer token — never from request body — and 403-before-state-machine cross-tenant guard on every multi-tenant route
- Idempotent natural-key upserts across the agent surface: `(agent_id, original_path)`, `file_id`, `proposal_id`, agent-generated log UUIDs; replays produce zero duplicate rows and zero same-state DB writes
- Task code split: `phaze.tasks.controller` (fileless: generate_proposals, tracklist scrapers, refresh cron) vs `phaze.tasks.agent_worker` (file-bound: process_file, extract_file_metadata, fingerprint_file, scan_live_set, execute_approved_batch); subprocess import-boundary test enforces no `phaze.database` in the agent chain
- `PHAZE_ROLE={control,agent}` env-driven settings split (ControlSettings vs AgentSettings via `get_settings()` factory); same Docker image for both roles; per-agent SAQ queue (`phaze-agent-<id>`); AgentTaskRouter picks queue from `FileRecord.agent_id`
- `PhazeAgentClient` with tenacity retry funnel, 4-class error hierarchy, bearer token never stored as instance attribute (lives only in httpx headers); respx contract tests across all routes
- `phaze-agent-watcher` service: watchdog observer + asyncio-owned single-loop sweep, mtime settle (10s default) + stuck-file cap (3600s); LIVE-sentinel ScanBatch per agent; admin "Trigger Scan" form with HTMX agent-roots swap + 2s/5s polling partials
- `scan_directory` agent task with chunked HTTP upserts (500/chunk), per-chunk PATCH progress, terminal PATCH; same `/files` endpoint serves bulk scans and per-file watcher events
- Distributed execution dispatch: group-by-`FileRecord.agent_id` (in-Python `defaultdict`), one `execute_approved_batch` sub-job per affected agent under shared parent `batch_id`; per-proposal terminal progress POST; SAQ-meta UUID lift for retry-safe `execution_log_id` and `progress_request_id`
- Unified SSE progress aggregating across agents (3 Jinja partials rendered via `_render_partial()` for Semgrep XSS compliance); per-agent breakdown table; revoked-agent banner
- Per-file-server fingerprint sidecars (audfprint + panako allow-list validator blocks non-localhost URLs at config load); cross-file-server fingerprint matching documented as v4.0 limitation with dismissible banner on Duplicate Resolution page
- Self-signed internal CA + leaf x509 generated on first start in the api container via `phaze.cert_bootstrap` + pre-uvicorn entrypoint shim (signals/PID-1 propagate cleanly); `PhazeAgentClient` honors `verify=` kwarg defaulting to `AgentSettings.agent_ca_file`; wrong-CA → ConnectError integration test
- Redis hardening: `requirepass` + `${REDIS_BIND_IP:-127.0.0.1}` LAN bind on app-server compose; `AgentSettings` rejects passwordless `redis_url` at boot when `PHAZE_AGENT_ENV=production`
- Application-server `docker-compose.yml` stripped of `SCAN_PATH`/`MODELS_PATH` mounts and watcher/audfprint/panako services; YAML-parse tests enforce filesystem isolation
- New `docker-compose.agent.yml` (4 services: worker, watcher, audfprint, panako) + `.env.example.agent`; `${SCAN_PATH:?...}` fail-fast on misconfigured file-server hosts; docker-publish.yml extended for both compose-file image tags
- `phaze.scripts.download_models` Python helper + `phaze.tasks._shared.model_bootstrap` wired into agent_worker/watcher startup (rejects partial-download `.part` state); `just download-models` populates per-file-server `/models` volume
- 30-second SAQ CronJob heartbeat from each agent updating `agents.last_seen_at`; Agents admin page (`/admin/agents`) with liveness classifier (alive/stale/revoked), queue depth, last-seen humanize helper; HTMX 5s auto-refresh
- Operator workflow: `just up` (app-server), `just up-agent` (each file-server), `just up-all` (single-host dev); full deployment walkthrough in `docs/deployment.md`; PROJECT.md Constraints + Deployment subsections updated

---

## v3.0 Cross-Service Intelligence & File Enrichment (Shipped: 2026-04-04)

**Phases completed:** 4 phases, 11 plans, 22 tasks

**Key accomplishments:**

- PostgreSQL full-text search with tsvector GENERATED columns, GIN indexes, and cross-entity UNION ALL search service returning ranked, paginated results from files and tracklists
- Search page with FastAPI router, HTMX partial swaps, Alpine.js collapsible filters, type-badged results table, and nav bar integration as first tab
- DiscogsLink model, discogsography HTTP adapter with rapidfuzz confidence scoring, and SAQ background task for batch matching tracklist tracks to Discogs releases
- Five HTMX endpoints and three template partials for Discogs match triggering, inline candidate review with accept/dismiss, and bulk-link functionality
- Discogs release UNION ALL branch in unified search with purple pill badges and accepted-only filtering per D-09
- TagWriteLog audit model, tag proposal cascade merge (tracklist > metadata > filename), and format-aware tag writer with verify-after-write for MP3/OGG/FLAC/OPUS/M4A via mutagen
- Tag review page with side-by-side comparison, inline editing of proposed values, Write Tags CTA, format/status badges, and 10 integration tests
- Fixed two HTMX wiring bugs: collapsed Write Tags button now computes proposed tags server-side, post-write response targets main row by stable ID with OOB detail row cleanup
- Pure-Python CUE sheet generator with 75fps timestamp conversion, Discogs REM enrichment, version suffix naming, and UTF-8 BOM file writing
- CUE management page with stats, batch generation, inline tracklist card buttons, and nav tab integration
- Source badges on CUE management rows with fingerprint-first sorting, and Regenerate CUE button state on tracklist cards via HX-Target detection

---

## v2.0 Metadata Enrichment & Tracklist Integration (Shipped: 2026-04-02)

**Phases completed:** 6 phases, 16 plans, 31 tasks

**Key accomplishments:**

- Shared async engine pool for arq workers with FileMetadata column expansion and METADATA_EXTRACTED pipeline stage
- 1. [Rule 3 - Blocking] Added track_number/duration/bitrate to FileMetadata model
- Tag data piped to LLM context via build_file_context, dual-state convergence gate prevents proposal generation until both metadata extraction and audio analysis complete
- Extended LLM prompt with 3-step directory path decision tree and added proposed_path field to FileProposalResponse with slash normalization in store_proposals
- SQL collision detection service, recursive tree builder, and /preview/ route with collapsible directory tree for approved proposals
- Wired collision detection and proposed_path display into the approval table and execution router, adding a Destination column with three visual states and an execution gate that blocks batch start when duplicate destination paths exist
- Duplicate resolution backend with auto-selection scoring (bitrate > tags > path), metadata-enriched queries, resolve/undo state machine, and stats aggregation
- FastAPI router + 9 Jinja2 templates delivering full duplicate resolution workflow: card-per-group layout, expandable comparison tables with green best-value highlighting, radio pre-selection, resolve/undo via HTMX OOB swaps, 10-second undo toast, bulk Accept All, and nav integration
- Three-table tracklist data model with async scraper (rate-limited) and weighted fuzzy matcher using rapidfuzz token_set_ratio
- arq task functions for tracklist search/scrape/refresh with monthly cron job, plus full HTMX admin UI with card layout, filter tabs, expand/collapse tracks, and undo toasts
- Two Docker containers (audfprint + Panako) with FastAPI HTTP APIs exposing /ingest, /query, /health endpoints, integrated into Docker Compose with named volumes and internal networking
- FingerprintEngine Protocol with httpx adapters, weighted orchestrator (60/40, 70% single-engine cap), FingerprintResult model, and Alembic migration
- arq fingerprint_file task with per-engine result storage, pipeline trigger/progress endpoints, FINGERPRINTED stage in pipeline stats, and justfile commands
- Tracklist source/status columns, track confidence, fingerprint dataclass extensions, and scan_live_set arq task for fingerprint-to-tracklist pipeline
- Scan tab with batch file selection, arq-based fingerprint scanning with polling progress, and source/status badge partials on tracklist cards
- HTMX inline editing, approve/reject status transitions, bulk reject low-confidence tracks, and fingerprint track detail with color-coded confidence badges

---

## v1.0 MVP (Shipped: 2026-03-30)

**Phases completed:** 11 phases, 24 plans, 43 tasks

**Key accomplishments:**

- Python 3.13 project skeleton with pyproject.toml (ruff/mypy/pytest config), pre-commit hooks with frozen SHAs, Docker Compose stack (api/worker/postgres/redis), and justfile developer commands
- FastAPI app with health endpoint, 5 SQLAlchemy models (files/metadata/analysis/proposals/execution_log), async DB layer with pydantic-settings config, and Alembic initial migration creating the full v1 schema
- Directory scanning with chunked SHA-256 hashing, NFC path normalization, extension classification, and PostgreSQL bulk upsert with ON CONFLICT resumability
- REST API endpoints for triggering file discovery scans and querying status, with Pydantic schemas, background task management, and path validation
- FileCompanion join table with directory-based companion association and SHA256 duplicate group detection services
- REST API endpoints for companion association (POST) and duplicate detection (GET) with paginated responses and full integration tests
- arq task queue with WorkerSettings, skeleton process_file with exponential retry backoff, and ProcessPoolExecutor for CPU-bound audio analysis
- ArqRedis pool wired into FastAPI lifespan for job enqueuing, docker-compose worker placeholder replaced with real arq command, justfile worker management commands added
- essentia-tensorflow dependency with 68-file model download script baked into Docker image, plus models_path config
- Essentia-based audio analysis service with 34 model registry (33 characteristic + 1 genre), BPM/key/mood/style detection, wired into arq worker via process pool
- litellm dependency pinned, Settings extended with 5 LLM config fields, Pydantic response models for structured output, naming prompt template with live set and album track rules, and companion cleaning + context building helpers tested
- ProposalService calling litellm acompletion with structured output, Redis rate limiting with configurable RPM, immutable proposal storage, and generate_proposals arq batch job wired into WorkerSettings
- Read-only proposal list UI with HTMX-powered filtering, search, sorting, pagination, and stats bar using Jinja2 templates and Tailwind CSS
- HTMX approve/reject/undo with OOB stats updates, expandable row details, bulk actions, keyboard navigation, and toast notifications
- Execution UI with SSE live progress, paginated audit log, execute button, and navigation bar connecting Proposals and Audit Log pages
- Pipeline trigger endpoints and dashboard wiring scan->analyze->propose flow via API with background enqueue for 200K+ file scale
- ORM model fix to match DB-level constraint from migration 002
- Fixed four v1.0 audit gaps: APPROVED state transition, .opus extension, proposed_path execution routing, and settings_batch_size dashboard injection
- Synced VERIFICATION statuses, SUMMARY requirements-completed fields, Phase 9 Nyquist validation, and config.json EOF to match actual implementation state
- Phase 10 Nyquist VALIDATION.md created and full quality gate sweep confirmed green (282 tests, 17 pre-commit hooks, ruff, mypy)

---
