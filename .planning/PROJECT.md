# Phaze

## What This Is

A music collection organizer that ingests ~200K music files (mp3, m4a, ogg, opus) and concert video streams, analyzes them for BPM/mood/style/key, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. As of v4.0, phaze runs as a **two-host distributed system**: an application server (API, UI, Postgres, Redis, fileless workers, no file mounts) and one or more file-server agents that own the music/video files locally, pull jobs from per-agent SAQ queues, and write every state change back over authenticated HTTPS. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

## Core Value

Get 200K messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review. Files stay where they live; decisions stay on one server.

## Last Milestone: 2026.7.0 Engineering Improvements — SHIPPED 2026-07-03

**Next:** planning the **Multi-cloud backends** milestone (phases 67+; design already on `main` via PR #182). The 2026.7.0 goal and target features below are retained as shipped-milestone context.

**Goal (shipped):** Pay down accumulated CI / build / versioning / dead-code engineering debt — faster parallel CI, code-change-gated builds, CalVer release versioning, a docs-drift guard, and small UI/dead-code cleanup — with zero product-behavior change.

**Target features:**
- **Parallel-CI test partition** — split the ~1,750-test suite into workflow-step buckets fanned out across parallel CI jobs; combine `.coverage` shards → one Codecov upload; preserve the 85% gate.
- **Code-change-gated CI** — the full build/test/security pipeline runs only when code changes; docs/`.planning`-only changes skip-with-success so required status checks stay satisfiable (no branch-protection trap).
- **CalVer adoption** — replace `vN.M` with `YYYY.MM.REVISION`, no leading-zero month (first tag `2026.7.0`); update the release procedure, version badges, image tags, and the milestone↔version mapping in ROADMAP/MILESTONES.
- **Docs-drift CI gate** — cross-check REQUIREMENTS.md traceability against passed phases so the traceability table stops going stale after PR merges.
- **Per-module coverage uplift** — overall coverage is healthy (90.38%) but a tail of modules sits well below the gate after v7.0. Raise a per-module coverage floor (prioritizing v7.0-touched + worst offenders: `agent_liveness.py` 12.5%, `routers/shell.py` 39.7%, `services/pipeline.py` 65.5%, `routers/tracklists.py`/`routers/pipeline.py` ~69%, plus the 71–78% tail), and lift the enforced gate.
- **/saq re-link + dead-code sweep** — a discreet in-UI link back to the still-mounted `/saq` SAQ monitor (Agents/Compute page); delete vestigial unused templates/routers/assignments surfaced during the v7.0 cutover. Presentation-only.

**Key context:** Cleanup/infra milestone — **no user-facing feature change, no backend behavior change.** This is the milestone that *adopts* CalVer, so it is the last `vN.M`-numbered planning cycle and its release is the first CalVer tag (`2026.7.0`). Phase numbering continues from v7.0 (starts at Phase 63). Candidates sourced from the ROADMAP Backlog + v7.0 RETROSPECTIVE.

## Prior Milestone: v7.0 UI Redesign — DAG-Centric Hybrid Console — SHIPPED 2026-07-02

**Goal:** Replace the MVP tab-sprawl admin UI with a DAG-centric hybrid console — the pipeline becomes the home and the navigation spine, the local/A1/k8s execution targets are first-class, and every human approval unifies behind one before→after diff/approve gate. An information-architecture + presentation rewrite over the existing routers/services; **no backend behavior change.**

**Target features:**
- Three-column "Hybrid Console" shell — a persistent left **DAG rail** (pipeline stages with live counts) is the navigation spine; clicking a stage swaps the center workspace via HTMX (no tab-jumping); a right per-file pane. `/` renders the shell with **Analyze** selected by default.
- Full legacy tab collapse — the ~10 sibling tabs become pipeline stages; global Search → **⌘K command bar**; Agents/health → header status strip + Agents page. Old per-tab routes redirect into the corresponding shell stage so bookmarks survive.
- Enrich + Analyze workspaces — Discover/Metadata/Fingerprint queues with their existing triggers; an Analyze workspace with three **execution-lane cards (local / A1 / k8s)** showing live capacity (k8s surfaces Kueue quota-wait vs. Inadmissible), and per-file lane + windowed progress.
- Identify workspaces — Track-ID (AcoustID→MusicBrainz match state/confidence) and Tracklist (Search→Scrape→Match shown inline as a visible 3-step).
- Unified Review & Apply — Rename/Tag/Move each as a before→after diff with per-file Approve/Edit/Skip + bulk "approve all high-confidence"; Dedupe keeper-select; Cue preview; every applied change audited and reversible.
- Full per-file record, ⌘K command palette (search files/tracklists/artists + quick commands), an Agents page that models the k8s burst lane as an ephemeral Job-based identity (not perpetually-DEAD), and a first-run empty state.
- Polish & cutover — baseline accessibility (keyboard rail + ⌘K, focus states, skip link, ARIA on the DAG), removal of dead legacy templates/routers, updated docs/README, and a narrow-width rail-collapse.

**Key context:** Aesthetic is **C3 "Evolved phaze"** — preserve the existing brand (Jura headings, blue accent, wave logo, dark `phaze-bg` theme + light toggle); evolve, don't reskin. Stack stays server-rendered: FastAPI + Jinja2 + HTMX + Tailwind + Alpine — **no SPA build**. Design spine is locked in `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` (+ interactive prototype in the co-located assets dir). Depends on and visualizes the v6.0 local/A1/k8s routing targets but does not modify v6.0 backend behavior. 25 requirements (SHELL/WORK/IDENT/REVIEW/RECORD/CUT) across phases 57–62.

## Earlier Milestone: v6.0 Kubernetes Burst Analysis — SHIPPED 2026-06-29

**Next:** v7.0 UI Redesign (DAG-Centric Hybrid Console) is now the active milestone (started 2026-06-29; see Current Milestone above). The v6.0 goal and target features below are retained as shipped-milestone context.

**Goal (shipped):** Offload long-duration audio analysis to a remote **x64 Kubernetes cluster running Kueue** as a third routing target alongside local and the v5.0 OCI A1 — following the v5.0 cloud-burst pattern (duration routing, compute-agent result callback, master toggle), but with the execution unit changed from "persistent host draining a SAQ queue" to "ephemeral, quota-scheduled Kueue batch Job submitted per file."

**Target features:**
- x86 Kueue Job-runner image published to GHCR — reuses the existing x86 essentia stack (the cluster is x64, so no arm64 source build); a one-shot entrypoint: pull file from object storage → analyze → POST result → exit
- Kube-API submission seam: the control plane submits a *suspended* batch `Job` labeled `kueue.x-k8s.io/queue-name`, then watches the `Workload` for admission→completion and reconciles results by `file_id`; Kueue owns quota/admission
- Object-storage staging: the long file is uploaded to an **operator-provided S3-compatible bucket** (reuse existing), the Job downloads it, and the object is cleaned up after analysis (ephemeral). Secrets via the `_FILE` convention. **(Reverses v5.0's "no object storage" decision — see Out of Scope.)**
- Router extension: "K8s" becomes a third cloud target; a single config setting selects the **active cloud target** (local / A1 / K8s). Same long-set routing seam as v5.0 (≥ duration threshold) — long files only, conservative scope
- Result callback reuses v5.0's compute-agent machinery: the Job pod authenticates back to `/api/internal/agent/*` as a registered compute agent
- Transport-agnostic connectivity: Tailscale *or* WireGuard — phaze only consumes operator-provided reachable endpoints (kube API, S3, callback), no mesh-specific code
- Deployment + runbook + config/docs: Kueue admin objects (ResourceFlavor / ClusterQueue / LocalQueue) documented as cluster-admin setup; phaze references a configured LocalQueue name; kubeconfig/service-account token via `_FILE` secret; all behind the existing `cloud_burst_enabled` master toggle with per-target config

**Key context:** Mirrors v5.0's duration-routing + compute-agent + result-reconciliation design, but the execution unit changes from a persistent SAQ-draining host to an ephemeral Kueue Job. x64 hardware removes the arm64-image burden — the existing x86 essentia stack is reused. Two new external dependencies vs. v5.0: object storage (S3-compatible client) and a Kubernetes API client. Connectivity is intentionally transport-agnostic (Tailscale or WireGuard), unlike v5.0's Tailscale-specific pipeline.

## Current State

**2026.7.0 Engineering Improvements (cleanup/infra milestone) — ALL 4 PHASES COMPLETE (63-66); ready for milestone audit + first CalVer release (`2026.7.0`).** First CalVer cycle; no product/backend behavior change. **Phase 63 (Parallel CI & Code-Change Gating, complete 2026-07-02):** the ~2,566-test suite is physically partitioned into 9 workflow-step buckets (`tests/<bucket>/`, canonical list in `tests/buckets.json` consumed by both the CI matrix and a structural partition guard); `tests.yml` fans the buckets across a parallel `fromJSON` matrix and a `combine` job unions per-shard `.coverage.*` (`relative_files=true`) into one Codecov upload with the 85% gate enforced once on the combined number; `ci.yml` skips heavy jobs on doc-only changes (`.planning/**`, `*.md`, `LICENSE`, `docs/`, `.txt`) via a conservative, unit-tested `classify-changed-files.sh` with skip-with-success preserved. Code review + verification hardened the gate beyond plan scope (operator-approved): `aggregate-results` is now a deny-list (a failed/cancelled `detect-changes` can no longer cascade to a green required check), empty diffs fail safe to `code-changed=true`, and the per-bucket coverage gate was deferred to combine (`--cov-fail-under=0`) so the matrix legs actually pass. Verifier 12/12; validated requirements CI-01..CI-04. **Phase 64 (Per-Module Coverage Uplift & Gate Raise, complete 2026-07-03):** COV-01/02 — raised the worst-offender modules to a per-module coverage floor with behavior-asserting tests and lifted the enforced CI gate above the prior 90.38%. **Phase 65 (CalVer Adoption, complete 2026-07-03):** VER-01..04 — replaced `vN.M` with `YYYY.MM.REVISION` (first tag `2026.7.0`) across the release procedure, version badges, image tags, and the milestone↔version mapping, historical record intact. **Phase 66 (Docs-Drift Gate & Dead-Code Sweep, complete 2026-07-03):** DOCS-01/CLEAN-01/CLEAN-02 — a hermetic `just docs-drift` CI guard (in the always-run code-quality job) cross-checking REQUIREMENTS.md traceability against passed phases across 5 drift classes with active-vs-archived degradation and in-flight tolerance (it caught genuine drift on its first run — a stale Phase-65 ROADMAP checkbox — and self-validates this milestone's own completion bookkeeping green), plus a D-14 dead-template entry-literal check; a flag-gated `/saq` shell re-link (`enable_saq_ui` context + `target=_blank rel=noopener` footer anchor on the Agents page, never a dead 404); and `vulture` dead-code tooling (dev-only dep + hand-audited 228-line whitelist + non-blocking `just vulture` recipe) whose one-shot manual-verify sweep confirmed no vestigial dead code remained after the v7.0 cutover (a deliberate no-op behind two human-approved blocking gates). Verifier 16/16; code review 0 blockers (3 advisory guard-robustness warnings). All 13 milestone requirements (CI-01..04, COV-01/02, VER-01..04, DOCS-01, CLEAN-01/02) validated. Next: milestone audit + first CalVer release (`2026.7.0`).

**v7.0 UI Redesign (DAG-Centric Hybrid Console) — SHIPPED 2026-07-02.** All 7 phases (57 → 57.1 → 58 → 59 → 60 → 61 → 62) complete + verified; milestone audit PASSED (28/28 requirements, 7/7 phases verified, 0 broken flows). Next: a cleanup / "engineering basics" milestone (planning) — candidates parked in ROADMAP.md Backlog (partition the test suite for parallel CI, CI-builds-only-when-code-changes, adopt CalVer, re-add the /saq in-UI link).

**Phase 59 (v7.0, complete 2026-07-01):** Identify workspaces — the two Identify-stage fragment workspaces superseding the `trackid`/`tracklist` `_STAGE_PLACEHOLDER`s. Track-ID = one combined read-only per-file identity table (File · audfprint · Panako · Tracklist · Confidence) surfacing existing fingerprint state (per-engine status words keyed on `FingerprintResult.status == "success"` — no fabricated score, D-01/D-02) + rapidfuzz `match_confidence` with matched/candidate/no-match state (D-04); rows inert. Tracklist = three Search·Scrape·Match step cards (lane-card visual, NOT a stepper, D-05) each with an R-4-guarded ALL trigger wired verbatim to the existing `/pipeline/{search,scrape,match}-tracklists` endpoints (D-06), over a per-set N/M track-coverage table scoped to the latest tracklist version (D-07/D-08). Two new degrade-safe read-only helpers `get_trackid_stage_files`/`get_tracklist_set_rows`. `STAGE_PARTIALS` values static literals (T-57-01); no second poll, no new store key/OOB seed, no chain-orchestration endpoint. AcoustID/MusicBrainz re-scoped out (deferred IDENT-03). Presentation-only — zero backend change. 3 plans, 3 sequential waves. Verifier 18/18; code review 0 blockers (WR-01 latest-version coverage bug fixed in-phase; WR-02 pre-existing `fingerprint.done`-always-0 captured as follow-up). Validated requirements: IDENT-01, IDENT-02.

**Phase 58 (v7.0, complete 2026-06-30):** Enrich + Analyze stage workspaces — the shell's first real content. Replaced the Phase-57 bridge with four content-only fragment workspaces swapped into `#stage-workspace`: Discover (recent scans + live discovered/not-yet-enriched sub-count + SCAN/RECOVER), Metadata + Fingerprint (ALL-only triggers wired verbatim to the existing `/pipeline/extract-metadata` & `/pipeline/fingerprint` endpoints — no EXTRACT SELECTED, D-02), and Analyze (three always-render local/A1/k8s lane cards with offline/not-configured states + the six reused v6.0 cloud cards preserving the Kueue quota-wait-vs-Inadmissible `role="alert"` distinction, plus one all-in-stage file table with per-file derived lane badges and windowed progress reading the Phase-57.1 mid-flight `fine_windows_analyzed/total` signal — in-flight rows show `running · N/M windows`, completed show full coverage). All live updates ride the ONE `/pipeline/stats` 5s poll (58-01 wired the shell's first live poll into chrome + a `visibilitychange` shed); no second loop. Presentation-only — zero backend behavior change. 4 plans, 4 sequential waves. The phase branch was merged up to current main mid-execution to pick up 57.1 backend + #181 build-time Tailwind. Validated requirements: WORK-01..05. Verifier 5/5 must-haves; plan-checker caught 2 blockers pre-execution (computeOnline OOB seed target; WORK-04 N/M test guard). Live Playwright UAT (2026-06-30) passed both deployment-gated items and surfaced/fixed 3 quality defects (W-1 base.html/shell.html store divergence; the legacy `#straggler-failed-card` orphan-OOB; all regression-guarded).

**Phase 57.1 (v7.0, complete 2026-06-30):** Incremental window persistence & live analyze progress signal — the one deliberate, scoped backend exception to v7.0's no-backend-change rule. `analyze_file` now bumps `analysis.fine_windows_analyzed/total` incrementally mid-run (counter-only; detail rows still atomic at completion) via a counter-only progress endpoint, idempotent under reboot re-enqueue, gated by a new `analysis_completed_at` discriminator (migration 028) so a partial row never leaks into proposals. Validated requirements: PROG-01..03. Shipped PR #184.

**Phase 57 (v7.0, complete 2026-06-30):** Application shell & DAG rail — the three-column hybrid-console shell, `GET /` + `/s/{stage}` fragment routing, DAG rail, header (⌘K + status strip), and legacy-route redirects. Validated requirements: SHELL-01..05. Shipped PR #179.

**v6.0 Kubernetes Burst Analysis shipped 2026-06-29.** Long sets that can't finish locally now run as ephemeral, quota-scheduled **Kueue batch Jobs** on a remote x64 cluster — a third analysis-routing target selected by a single `cloud_target` (`local`/`a1`/`k8s`) config under the `cloud_burst_enabled` toggle. 5 phases (52-56), 27 plans; the x86 Job-runner image, S3 object-staging leg (DIST-01-preserving control-plane presign), kr8s submit + reconcile cron, the one-branch live-seam routing edit, and the cluster-admin runbook all shipped. All 26 requirements (KJOB/KSTAGE/KSUBMIT/KROUTE/KDEPLOY) validated + 2 bonus. The milestone audit passed after closing one critical cross-phase blocker — JOB-ENV-CONTRACT (the Job manifest didn't inject the pod's runtime env; fixed via quick task 260628-wzq). Live K8s + real-S3 end-to-end verification is deployment-gated on the homelab cluster rollout (see milestones/v6.0-MILESTONE-AUDIT.md + STATE.md Deferred Items) — and must be re-run FIRST after rollout, as it is the test that would have caught JOB-ENV-CONTRACT.

**v5.0 Cloud Burst Analysis shipped 2026-06-26.** Phaze can now offload long-duration audio (≥ a configurable threshold) that times out locally to a free OCI Ampere A1 (arm64) "compute agent" reached over Tailscale — duration-routed, rsync-pushed, sha256-verified, and analyzed unattended, all behind a single `cloud_burst_enabled` master toggle that defaults to all-local. 5 phases (47-51), 23 plans; all requirements (CLOUDIMG/CLOUDAGENT/CLOUDROUTE/CLOUDPIPE/CLOUDDEPLOY) validated. Live end-to-end verification is deployment-gated on the homelab OCI A1 rollout (see milestones/v5.0-MILESTONE-AUDIT.md + STATE.md Deferred Items).

**v4.0 Distributed Agents shipped 2026-05-17.** Phaze runs across two hosts: a control-plane application server and one or more file-server agents.

**Phase 56 (v6.0, complete 2026-06-29):** Deployment, runbook, config & docs — the operator-facing close-out of the Kubernetes burst leg. Ships `docs/k8s-burst.md` (apply-ready Kueue/RBAC/Secret manifests with a least-privilege namespaced Role whose verb floor is machine-asserted against the kr8s call graph; transport-agnostic connectivity notes; apiVersion lockstep rule + v1beta2 upgrade note) and a homelab change-prompt; the full K8s/S3 `_FILE`-secret knob table in `docs/configuration.md`; the single-toggle (`PHAZE_CLOUD_TARGET=local`) revert in `docs/deployment.md`; a live non-fatal LocalQueue-reachability probe at controller startup that writes a cross-process Redis flag surfaced as an amber dashboard alert; an ephemeral-identity Agents-UI note (the one-shot Job never heartbeats → classifies "never", never "dead"); and KDEPLOY-06 pulled forward — the internal CA is mounted at runtime from an operator-created K8s Secret (the image bakes no CA), superseding the old KJOB-05 bake. Validated requirements: KDEPLOY-01..06. Code review surfaced and fixed 2 real probe defects (CR-01: a Redis-down k8s boot could crash the controller, violating the D-05 "boots regardless" invariant; WR-01: a stale dashboard flag persisted across a target switch); both fixed with added coverage. Security audit: 19/19 threats closed. UAT (operator-delegated, live PG+Redis): 3/3 passed. **This completes milestone v6.0.**

**Phase 55 (v6.0, complete 2026-06-28):** Routing, state & ledger integration — wired the Phase 53/54 S3-staging + kube-submit producers into the live duration-routing seam behind `cloud_burst_enabled`, with the scheduling ledger as the single source of truth for what may be recovered. Validated requirements: KROUTE-01..06.

**Phase 54 (v6.0, complete 2026-06-28):** Kube submit/watch + reconcile cron — the Kubernetes (Kueue) burst leg. A pure kr8s `kube_staging` service (the single home of every kube API call, mirroring `s3_staging`) builds and idempotently submits a suspended `batch/v1` Job (`suspend:true`, `backoffLimit:0`, requests-only, `kueue.x-k8s.io/queue-name` label, deterministic `phaze-analyze-<file_id>` name, 900s TTL backstop). A fast `submit_cloud_job` controller-queue producer does one kube POST and returns; a narrow `*/5` `reconcile_cloud_jobs` safety-net cron owns the Job lifecycle — iterating the `cloud_job` sidecar (extended with kueue_workload/attempts/inadmissible columns + migration 026), mapping each Job/Workload condition tuple to an outcome with delete-after-record ordering (D-04), bounded re-drive with a confirm-gone race guard, S3 cleanup on no-callback terminals, and an Inadmissible operator-alert card on the pipeline dashboard — while the out-of-band `/api/internal/agent/analysis/{file_id}` callback stays the sole result writer (KSUBMIT-03). The kube config surface (api url/namespace/local-queue/image/requests + `_FILE`-resolved SA-token/kubeconfig secrets) is optional in Phase 54; fail-fast coupling to `cloud_burst_enabled` is Phase 55/56. Validated requirements: KSUBMIT-01..06. Code review surfaced and fixed 5 real bugs incl. a critical never-clearing Inadmissible alert and an SA-token that never reached the wire (kr8s session-header timing). The submit producer is built but not yet wired into the live routing seam (Phase 55 owns that).

**Phase 53 (v6.0, complete 2026-06-28):** S3 object-staging leg — the control plane presigns S3 multipart PUT/GET URLs and orchestrates cleanup via a pure-aioboto3 `s3_staging` service (the **only** file importing the S3 SDK — DIST-01 boundary CI-enforced), while the file-server agent uploads bytes over httpx to presigned PUT URLs with no SDK or bucket credentials, and the pod fetches a just-in-time presigned GET at startup. A `cloud_job` per-`file_id` sidecar table (migration 025) tracks staging status/`upload_id`; staged objects use `file_id`-scoped keys, are deleted inline on every terminal outcome (success/failure/re-drive) with a bucket lifecycle TTL backstop, and S3 config is operator-provided via `_FILE` secrets against any S3-compatible backend. Validated requirements: KSTAGE-01..05. Code review surfaced and fixed 5 real robustness bugs (idempotent abort/complete, re-drive URL-clobber, empty-ETag, presign readiness guard); CR-01 was a verified false-positive. The producer is built but not yet wired into the live routing seam (Phase 55 owns that).

**Phase 51 (v5.0, complete 2026-06-26):** Deployment, config & docs — `docker-compose.cloud-agent.yml` (arm64, host-Tailscale, no media, named scratch volume), the `cloud_burst_enabled` master toggle gating all three cloud entry points (routing seam, staging cron, backfill), the homelab OCI A1 + Tailscale-ACL + least-privilege Postgres broker provisioning runbook, and the full config/docs surface. Validated requirements: CLOUDDEPLOY-01..04. Post-audit fixes (#161/#162): cloud-agent compose `python3 -m saq` start command, `compute_scratch_dir` fail-fast guard, scratch-dir-skew diagnostic, and the WR-03 push-timeout-coupling guard.

**Phase 30 (post-v4.0 fix, complete 2026-06-10):** Resolved systemic control-plane SAQ queue misrouting — every manually-triggered UI/API enqueue previously targeted a consumer-less unnamed `default` queue (stranded 11,428 jobs in the v4.0.6 incident). All enqueue sites (pipeline, tracklists, scan/ingestion) now route through a shared `enqueue_router.resolve_queue_for_task` helper: controller-bound tasks → named `controller` queue, per-agent tasks → `phaze-agent-<id>` via active-agent selection (0-agent surfaces a 503/empty-state). A static AST guard test prevents recurrence.

**Phase 47 (v5.0, complete 2026-06-24):** Official arm64 essentia agent image — `Dockerfile.agent-arm64` builds essentia from source (the essentia-tensorflow wheel is x86-only) on `python:3.13-slim-bookworm` against TF 2.20.0 with all four spike fixes baked in; CI builds it on a native `ubuntu-24.04-arm` runner and publishes `-arm64`-tagged images to GHCR *only* after a numeric-parity guard compares arm64 `analyze_file` output against an x86 golden. Validated requirements: CLOUDIMG-01/02/03. Unlocks the OCI Ampere A1 free-tier compute agent for Phases 48-51.

**Phase 50 (v5.0, complete 2026-06-26):** Cloud push pipeline — a file-server agent rsyncs a cloud-routed long file to the compute agent's scratch dir over SSH-over-Tailscale (`push_file` task, shell-free argv with `--` terminator + pinned host keys + 0600 secret temp files); the compute agent sha256-verifies the scratch copy against `FileRecord` before analyzing and unlinks it in a `finally` (kept on retryable failure so the SAQ retry can re-verify). New `PUSHING`/`PUSHED` states; a `stage_cloud_window` cron keeps ≤N files staged-or-in-flight ("stay one ahead", default 2) under a pg advisory lock; control-side `pushed`/`mismatch` internal-API callbacks drive the handoff (idempotent, ledger-tracked) with two D-09 dashboard count cards. Validated requirements: CLOUDPIPE-01..05. Live rsync-over-Tailscale transfer to a real compute agent is Phase 51 (deploy/provisioning).

- ~14,300 lines of Python source + ~28,000 lines of tests across 29 phases, 94+ plans total (v1.0–v4.0)
- Tech stack: FastAPI, SQLAlchemy (async), SAQ + Redis (per-agent queues), litellm, essentia-tensorflow, mutagen, rapidfuzz, httpx, watchdog, cryptography (self-signed CA), tenacity, respx, HTMX + Tailwind + Alpine.js
- Two Docker Compose stacks: `docker-compose.yml` (app-server: api with TLS via internal CA, controller worker, postgres, redis with `requirepass` + LAN bind, no file mounts) and `docker-compose.agent.yml` (file-server: agent worker, watcher, audfprint + panako sidecars)
- 14 Alembic migrations, 14 SQLAlchemy models (Agents added in v4.0), per-file-server fingerprint sidecars
- Internal API surface: `/api/internal/agent/*` with token-hash bearer auth, idempotent natural-key upserts, 403-before-state-machine cross-tenant guards, 30s heartbeat
- Admin UI: proposals, duplicates (with cross-FS fingerprint notice), tracklists, pipeline dashboard with **Trigger Scan card**, unified search, Discogs linking, tag review, CUE management, **Agents** page with liveness + queue depth
- Operator workflow: `just up` (app-server), `just up-agent` (each file-server), `just up-all` (single-host dev); full deployment walkthrough in `docs/deployment.md`

## Previous State

<details>
<summary>v3.0 shipped 2026-04-04</summary>

Single-host enrichment milestone: unified FTS search with faceted filtering, Discogs cross-service linking with fuzzy matching and bulk-link, format-aware tag writing with 4-layer cascade (tracklist > discogs > metadata > filename) and verify-after-write, CUE sheet generation with fingerprint-preferred timestamps and Discogs REM enrichment.

- 6 phases, 11 plans
- 13 Alembic migrations, 13 SQLAlchemy models
- TagWriteLog audit, DiscogsLink with confidence scoring, three-entity UNION ALL search (file/tracklist/discogs)

</details>

<details>
<summary>v2.0 shipped 2026-04-02</summary>

Metadata enrichment & tracklist integration. Audio tag extraction (mutagen), AI destination paths with collision detection, duplicate resolution UI, 1001Tracklists integration with monthly cron, dual fingerprint service (audfprint + Panako) with batch ingestion.

- 6 phases, 16 plans, 538 tests passing
- ~5,966 lines of Python added

</details>

<details>
<summary>v1.0 shipped 2026-03-30</summary>

Full pipeline operational: scan → analyze → propose → approve → execute.

- 11 phases, 24 plans, 282 tests passing
- ~7,975 lines of Python
- Tech stack: FastAPI, SQLAlchemy (async), arq, litellm, essentia-tensorflow, HTMX + Tailwind
- 4 Alembic migrations, 6 SQLAlchemy models

</details>

## Requirements

### Validated

- ✓ Containerized backend services running via Docker Compose — v1.0 Phase 1
- ✓ PostgreSQL database for all metadata and state — v1.0 Phase 1
- ✓ Alembic database migrations — v1.0 Phase 1, 10
- ✓ Recursive directory scanning for music/video/companion files — v1.0 Phase 2
- ✓ SHA256 hash computation and storage — v1.0 Phase 2
- ✓ Original filename and path recorded in PostgreSQL — v1.0 Phase 2
- ✓ File type classification (music, video, companion) — v1.0 Phase 2
- ✓ Companion files linked to media files via directory proximity — v1.0 Phase 3
- ✓ Exact duplicate detection via SHA256 hash grouping — v1.0 Phase 3
- ✓ arq + Redis task queue with bounded worker pool, retry with backoff, process pool — v1.0 Phase 4 (replaced by SAQ in v4.0)
- ✓ BPM detection for music files — v1.0 Phase 5
- ✓ Mood and style classification for music files — v1.0 Phase 5
- ✓ Analysis runs in parallel across worker pool — v1.0 Phase 4
- ✓ AI-powered filename proposals via litellm with batch prompting and structured output — v1.0 Phase 6
- ✓ Proposals stored as immutable records in PostgreSQL — v1.0 Phase 6
- ✓ Admin web UI with paginated proposal list, status filtering, bulk actions, keyboard shortcuts — v1.0 Phase 7
- ✓ Admin can approve/reject individual proposals with FileRecord state transition — v1.0 Phase 7, 11
- ✓ Safe file execution via copy-verify-delete protocol with proposed_path routing — v1.0 Phase 8, 11
- ✓ Append-only audit log for all file operations — v1.0 Phase 8
- ✓ Pipeline orchestration: scan→analyze→propose triggers via API endpoints — v1.0 Phase 9

- ✓ Audio tag extraction (ID3/Vorbis/MP4/FLAC/OPUS) feeding richer LLM context — v2.0 Phase 12
- ✓ Shared async engine pool replacing per-invocation engine creation — v2.0 Phase 12
- ✓ AI destination path proposals with collision detection and directory tree preview — v2.0 Phase 13
- ✓ Duplicate resolution UI with auto-scoring, side-by-side comparison, resolve/undo — v2.0 Phase 14
- ✓ 1001Tracklists integration with search, scrape, fuzzy match, periodic refresh — v2.0 Phase 15
- ✓ Dual fingerprint service (audfprint + Panako) with batch ingestion — v2.0 Phase 16
- ✓ Live set scanning with tracklist review, inline editing, approve/reject — v2.0 Phase 17

- ✓ Unified search across files, tracklists, and metadata with faceted filtering — v3.0 Phase 18
- ✓ Discogsography cross-service linking via HTTP API with fuzzy matching and confidence scores — v3.0 Phase 19
- ✓ Write corrected tags to destination copies with review UI, verify-after-write, and audit logging — v3.0 Phase 20
- ✓ CUE sheet generation from tracklist data with fingerprint-preferred timestamps and Discogs REM enrichment — v3.0 Phase 21

- ✓ File servers run agents that own files locally; the application server orchestrates and stores all state — v4.0 Phase 24-29
- ✓ HTTP-only boundary between agents and the application server (no shared filesystem, no shared database access) — v4.0 Phase 25-26
- ✓ Per-agent bearer token auth with `agent_id` derived from token, never from request body — v4.0 Phase 25
- ✓ Continuous file watcher service on each file server that streams new arrivals to the application server — v4.0 Phase 27
- ✓ Distributed approval execution: group approved proposals by agent and dispatch one sub-batch per file server — v4.0 Phase 28
- ✓ Self-signed HTTPS via internal CA + Redis `requirepass` + LAN bind + per-file-server fingerprint sidecars — v4.0 Phase 29
- ✓ Same Docker image for both roles via `PHAZE_ROLE={control,agent}` env; new `docker-compose.agent.yml` for file servers — v4.0 Phase 26, 29
- ✓ 30s heartbeat + Agents admin page with liveness, queue depth, last-seen — v4.0 Phase 29

- ✓ Official arm64 essentia agent image published to GHCR via native arm64 CI build with numeric-parity guard — v5.0 Phase 47
- ✓ Compute-agent type (no scan roots / no media) with duration-based, capability-aware analysis routing — v5.0 Phase 48-49
- ✓ Ledger-scoped backfill of timed-out long files to the cloud agent (no whole-backlog over-enqueue) — v5.0 Phase 49
- ✓ rsync-over-Tailscale "stay one ahead" push pipeline (control-plane orchestrated, ephemeral scratch + sha256 verify) — v5.0 Phase 50
- ✓ Cloud-agent deployment + OCI A1 / Tailscale-ACL runbook; `cloud_burst_enabled` master toggle; `_FILE`-secret-capable config — v5.0 Phase 51

- ✓ x86 Kueue Job-runner image (FROM essentia base, zero new pip deps) + one-shot entrypoint (presign-download → sha256-verify → windowed analyze → POST → exit, honest exit codes, runtime-mounted CA) — v6.0 Phase 52
- ✓ S3 object-staging leg: control-plane aioboto3 presign/cleanup (sole importer, DIST-01 preserved), agent httpx upload, pod just-in-time GET, `file_id`-scoped keys + lifecycle TTL, `_FILE`-secret any-S3 — v6.0 Phase 53
- ✓ Kube submit + `*/5` reconcile cron (kr8s): idempotent suspended Job, fast non-blocking submit, out-of-band callback authoritative, Inadmissible-vs-Pending, bounded re-drive, no `process_file` ledger seed — v6.0 Phase 54
- ✓ K8s as a third cloud target via the `cloud_target` selector (replaces `cloud_burst_enabled` bool) wired as one `stage_cloud_window` branch + AST over-enqueue guard + ledger-scoped backfill — v6.0 Phase 55
- ✓ Cluster-admin Kueue/RBAC/Secret runbook, transport-agnostic endpoints, `_FILE`-secret config + fail-fast validator, LocalQueue startup probe + dashboard alert, ephemeral Job-based Agents identity, single-toggle revert — v6.0 Phase 56

- ✓ Three-column DAG-centric console shell: `GET /` (Analyze default) + `/s/<stage>` HTMX rail nav, ⌘K command palette, header status strip, 8 legacy routes redirect into the shell — v7.0 Phase 57 (SHELL-01..05)
- ✓ Incremental mid-flight analyze-progress counter (`fine_windows_analyzed`) + `analysis_completed_at` discriminator gating partial rows out of proposals (the one scoped backend exception) — v7.0 Phase 57.1 (PROG-01..03)
- ✓ Enrich + Analyze stage workspaces: 3 local/A1/k8s lane cards (Kueue quota-wait vs Inadmissible) + windowed per-file progress, single `/pipeline/stats` poll fanout — v7.0 Phase 58 (WORK-01..05)
- ✓ Identify workspaces — Track-ID (existing audfprint+Panako + rapidfuzz signals; AcoustID/MB deferred to IDENT-03) + Tracklist Search→Scrape→Match 3-step — v7.0 Phase 59 (IDENT-01..02)
- ✓ Unified Review & Apply gate — before→after diff + Approve/Edit/Skip + server-predicate bulk-approve, Dedupe keeper-select, Cue preview, every apply audited + reversible — v7.0 Phase 60 (REVIEW-01..05)
- ✓ Per-file record slide-in, ⌘K command palette, Agents page with ephemeral k8s Job identity, first-run empty state — v7.0 Phase 61 (RECORD-01..04)
- ✓ Baseline accessibility (keyboard rail + ⌘K, focus, skip link, DAG ARIA), narrow-width icon-rail collapse, docs/README refresh, dead-code cutover (20 legacy templates removed, empty guard allowlist) — v7.0 Phase 62 (CUT-01..04)

- ✓ Parallel-CI test partition — ~1,750-test suite split into 9 workflow-step buckets (`tests/buckets.json`), fanned across a matrix, per-shard `.coverage` combined into one Codecov upload, doc-only changes skip-with-success — 2026.7.0 Phase 63 (CI-01..04)
- ✓ Per-module coverage floor + enforced project gate raised above the 90.38% baseline, wired into CI so future regressions fail the build — 2026.7.0 Phase 64 (COV-01/02)
- ✓ CalVer release versioning (`YYYY.MM.REVISION`, no-leading-zero month, first tag `2026.7.0`) across release procedure, badge, image tags, and the milestone↔version mapping; historical `vN.M` record intact — 2026.7.0 Phase 65 (VER-01..04)
- ✓ Docs-drift CI gate cross-checking REQUIREMENTS.md traceability against passed phases (hermetic `just docs-drift`) + `/saq` shell re-link + vulture dead-code tooling (deliberate no-op sweep post-v7.0-cutover) — 2026.7.0 Phase 66 (DOCS-01, CLEAN-01/02)

### Active

**No active milestone — planning the next one.** 2026.7.0 shipped 2026-07-03. The next named milestone is **Multi-cloud backends** (pluggable analysis-backend registry: local + 1+ Kueue + 1+ cloud-compute simultaneously, cost-tiered ranks + caps, static/no-provisioning; phases 67+). Design already merged to `main` (PR #182); promote to an active milestone via `/gsd:new-milestone`. `.planning/REQUIREMENTS.md` was archived at the 2026.7.0 close and will be regenerated for the next milestone.

### Out of Scope

- Cross-file-server fingerprint matching — per-agent fingerprint DB only in v4.0; documented as v4.0 limitation, tracked as XAGENT-01, deferred to a later milestone
- Cross-file-server execution batches (moves spanning hosts) — XAGENT-02, deferred
- Delete / move / rename detection in the file watcher — v4.0 watcher only handles `created` events; tracked as WATCH-05/06, deferred
- Watcher catch-up on startup (rescan files that landed while watcher was down) — WATCH-07; manual user-initiated scan covers this in v4.0
- mTLS in addition to bearer tokens for the agent boundary — OPS-05, deferred
- Multi-tenant agent self-service registration — OPS-06; today operator pre-seeds tokens
- Agent metric scraping endpoint (Prometheus-compatible) — OPS-07, deferred
- Natural language querying across services — deferred
- Acoustic near-duplicate detection via fingerprint similarity — deferred
- Public network access — private LAN only
- Offline mode — real-time server tool, not a desktop app
- Files transferred between application server and file server — v4.0 keeps files local to file servers; transfer would defeat the boundary. **(Narrowed in v5.0: still no app↔file-server transfer, but a file-server agent may push a long file to an ephemeral *cloud compute agent* for analysis-only, then delete it — extra compute, not a data home. v6.0 keeps this: the long file is staged to ephemeral object storage for the Kueue Job, downloaded, analyzed, deleted — analysis-only, not a data home.)**
- Postgres replication / read-replica on file server — agents stay HTTP-only (Option II in v4.0 grilling was rejected)
- ~~Tailscale / mesh networking — plain private LAN chosen in v4.0 (Q10b)~~ **(Reversed in v5.0: Tailscale is the transport for the off-LAN cloud compute agent. Generalized in v6.0: connectivity is transport-agnostic — Tailscale or WireGuard — phaze consumes operator-provided reachable endpoints only.)**
- ~~No object storage — v5.0's cloud agent analyzed from local rsync'd scratch, never a bucket~~ **(Reversed in v6.0: ephemeral Kueue Job pods have no persistent local disk, so the long file is staged to an operator-provided S3-compatible bucket, downloaded by the Job, and deleted after analysis. Ephemeral staging only — not a data home.)**
- GPU / Coral TPU acceleration for the cluster nodes — essentia-tensorflow analysis is CPU-bound on this workload; cluster nodes and Kueue resource requests target CPU, not accelerators (see Key Decisions)

## Context

- v1.0–v4.0 shipped: full pipeline from scan → tag extract → analyze → propose (filename + path) → approve → execute, now distributed across application server + file-server agents
- ~200K files total, mix of music files and full concert video streams
- Concert videos are primarily recordings of live streams (YouTube streams from festivals, etc.)
- FileMetadata fully populated via mutagen tag extraction (ID3/Vorbis/MP4/FLAC/OPUS)
- Dual fingerprint service (audfprint + Panako) per file server with weighted scoring (60/40, 70% single-engine cap); no cross-file-server matching in v4.0
- 1001tracklists integration operational with monthly refresh cron (runs on app-server controller worker)
- This is a personal tool running on a private home LAN, not a multi-user SaaS

## Constraints

- **Language**: Python 3.13 exclusively
- **Package manager**: uv only
- **Deployment**: Docker Compose on private LAN; two-host topology (app-server + file-server agents)
- **Database**: PostgreSQL (app-server only; agents have zero direct DB access)
- **Scale**: Must handle ~200K files efficiently — batch processing and parallelization required
- **Naming format**: Live sets: `{Artist} - Live @ {Venue|Event} {YYYY.MM.DD}.{ext}`, Album tracks: `{Artist} - {Track #} - {Track Title}.{ext}`

**Per-agent fingerprint indices (v4.0).** Each file server's `audfprint` and `panako` sidecars index ONLY that file server's local files. Duplicate audio content landing on different file servers will NOT cross-match. Cross-file-server fingerprint matching is XAGENT-01 (deferred to a post-v4.0 milestone). The Duplicate Resolution admin UI surfaces this constraint as an inline, per-session-dismissible banner on every page load so the operator interprets fingerprint-derived results with this scope in mind.

### Deployment (v4.0 — Distributed Agents)

Phaze v4.0 production runs as **two Docker Compose files on two private-LAN hosts**:

- **Application server** (`docker-compose.yml`): `api` (uvicorn-direct TLS via internal CA), `worker` (fileless controller-role SAQ worker), `postgres`, `redis` (password-auth + LAN-bound port). **No file mounts** beyond `./certs/` — the app-server has no way to read or write music/video file content (DIST-01).
- **File servers** (`docker-compose.agent.yml`, one stack per file-server host): `worker` (agent-role SAQ worker), `watcher` (watchdog-based file event poster), `audfprint` + `panako` (local fingerprint sidecars). Holds the music/video library locally; reaches the app-server over HTTPS for every state change.

Locked invariants (Phase 29):

- All agent → app-server traffic uses **HTTPS** terminated by uvicorn against a self-signed internal CA generated in the app-server's `api` container on first start. Operators distribute the public CA cert (`phaze-ca.crt`) to each file server via scp/rsync; the CA private key (`phaze-ca.key`, mode 0600) never leaves the app-server.
- **Redis** on the app-server requires `requirepass` and is bound to the private LAN IP (or loopback in dev). Agents connect with `redis://default:<password>@<host>:6379`. In `PHAZE_AGENT_ENV=production`, `AgentSettings` rejects a passwordless `redis_url` at boot.
- **0 new pip dependencies** beyond `cryptography` (added Phase 29 for cert generation).
- `docker-compose.agent.yml` enforces `${SCAN_PATH:?SCAN_PATH required}` on all four services — compose parse fails fast on a misconfigured file-server host.
- Operator workflow: `just up` (app-server), `just up-agent` (each file-server), `just up-all` (single-host dev). Full walkthrough in `docs/deployment.md`.

Deferred to a future ops phase: mTLS for the agent boundary, agent self-registration UI, Prometheus metrics scrape endpoint, automated CA rotation. See `.planning/milestones/v4.0-REQUIREMENTS.md` §"Future Requirements → Operational Polish" (OPS-05..OPS-07).

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| PostgreSQL over SQLite | 200K files with complex metadata, relationships, and future cross-service queries need a real RDBMS | ✓ Good — handles async access, complex queries, JSON columns well |
| Organization before search | Getting files organized is the primary win; search/NLQ is a follow-on | ✓ Good — v1.0 delivers complete organization pipeline |
| Human-in-the-loop approval | No file moves without admin review — safety for a large, irreplaceable collection | ✓ Good — approval UI with undo prevents mistakes |
| Containerized services | Clean separation of concerns, reproducible deployment on home server | ✓ Good — Docker Compose with health checks works reliably |
| HTMX over React SPA | Single-user admin tool doesn't need SPA complexity | ✓ Good — zero build step, CDN delivery, full interactivity |
| arq over Celery | Async-first, simple config, Redis-native — single user doesn't need Celery complexity | — Replaced — migrated to SAQ in v4.0 prep; arq was in maintenance mode and SAQ has active development + per-agent queue affordances |
| SAQ over arq (v4.0) | Active maintenance, built-in web UI, native per-queue worker model | ✓ Good — clean fit for per-agent `phaze-agent-<id>` queues |
| essentia-tensorflow for analysis | 34 pre-trained models, BPM/key/mood/style in one library | ✓ Good — baked into Docker image, process pool execution |
| litellm for LLM abstraction | Provider flexibility without vendor lock-in | ⚠️ Revisit — supply chain incident on 1.82.7/1.82.8, pin aggressively |
| copy-verify-delete protocol | Never direct move — SHA256 verification before deleting original | ✓ Good — safety for irreplaceable collection, preserved across the v4.0 HTTP boundary via per-operation PATCH |
| State machine on FileRecord | Explicit state transitions (DISCOVERED→ANALYZED→PROPOSED→APPROVED→EXECUTED→MOVED/UNCHANGED/FAILED) | ✓ Good — enables pipeline dashboard stage counts |
| mutagen for tag read/write | Zero-dependency, supports all major tag formats | ✓ Good — reliable across ID3/Vorbis/MP4/FLAC/OPUS |
| audfprint + Panako hybrid | Complement each other: landmark-based vs tempo-robust | ✓ Good — weighted orchestrator with per-engine results |
| rapidfuzz for fuzzy matching | Fast token_set_ratio for tracklist-to-file matching | ✓ Good — weighted scoring with artist/event/date |
| Long-running fingerprint containers | HTTP API over subprocess calls for fingerprint services | ✓ Good — persistent DBs, Docker Compose integration; now per-file-server in v4.0 |
| Distributed agents (v4.0) | Files stay on file servers; application server owns API, UI, Postgres, Redis | ✓ Good — v4.0 shipped end-to-end; two-host topology operational with strict HTTP-only boundary |
| HTTP-only agent boundary (v4.0) | Agents have zero Postgres access; all writes go through `/api/internal/agent/*` | ✓ Good — `test_agent_worker_does_not_import_phaze_database` subprocess gate enforces the boundary at CI time |
| One SAQ queue per agent (v4.0) | `phaze-agent-<id>` queue per file server; enqueuer picks queue by `FileRecord.agent_id` | ✓ Good — matches SAQ's native pull model, clean per-agent maintenance |
| Per-agent bearer token auth (v4.0) | `agent_id` derived from token lookup on application server, never from request body | ✓ Good — partial-index `ix_agents_token_hash_active WHERE revoked_at IS NULL` gives O(1) lookup; revoke = instant block |
| Per-agent fingerprint DB (v4.0) | Each file server runs its own audfprint+panako sidecars indexing only its files | ⚠️ Revisit — known v4.0 limitation; XAGENT-01 deferred. Operator banner mitigates UX surprise |
| Self-signed internal CA (v4.0) | Generated in api container on first start; public cert distributed by operator via scp | ✓ Good — no DNS dependency, no public ACME, no rotation pain for single-user LAN |
| Redis `requirepass` + LAN bind (v4.0) | App-server Redis is broker + cache; password + interface bind is the minimal credible hardening on a private LAN | ✓ Good — `AgentSettings` fail-fast in production prevents passwordless misconfig |
| Group-by-agent execution dispatch (v4.0) | In-Python `defaultdict(list)` over SQL `GROUP BY` — at 1-5 agents × ≤10K proposals, type-safe path is cheaper than DB aggregation | ✓ Good — preserves write-ahead `ExecutionLog` audit over HTTP boundary via per-operation PATCH |
| Pre-uvicorn entrypoint shim (v4.0) | Cert bootstrap then `execvp uvicorn` so signals + PID-1 propagate cleanly | ✓ Good — clean Docker stop semantics, no double-process tree |
| Two-step Alembic migration (v4.0) | 012 adds + backfills, 013 enforces NOT NULL + swaps UQ — preserves v3.0 data via `legacy-application-server` seed | ✓ Good — round-trip downgrade smoke gate caught the boundary; zero data loss in production migration |
| CPU-only cluster nodes for v6.0 (no GPU / no Coral) | essentia analysis is CPU-bound: wall-clock is dominated by `MonoLoader` decode + native DSP (rhythm/onset/spectral) on long sets; the TF classifier step is a tiny slice. Coral needs int8 TFLite (essentia ships full float TF) and GPU only speeds the negligible inference. Throughput lever is horizontal CPU parallelism across files — Kueue quota delivers exactly that. | ✓ Good — shipped v6.0; Kueue resource requests target `cpu`/`memory` only on a generic x64 CPU node pool; consistent with the v1.0 ProcessPoolExecutor decision |
| Ephemeral Kueue Job as the K8s execution unit (v6.0) | One suspended `batch/v1` Job per long file instead of a persistent SAQ-draining host; Kueue owns quota/admission only, and the analysis result stays out-of-band via the `/api/internal/agent/*` callback reconciled by `file_id` (a dropped kube watch never loses a result) | ✓ Good — v6.0 shipped; the milestone audit (JOB-ENV-CONTRACT) confirmed the manifest→pod env seam needs an explicit contract test; live E2E deployment-gated |
| Object storage for K8s staging (v6.0, reverses v5.0) | Ephemeral Job pods have no persistent local disk, so the long file is staged to an operator-provided S3-compatible bucket, downloaded by the pod, and deleted after analysis; control plane presigns only (DIST-01), agent/pod are credential-free | ✓ Good — v6.0 shipped; aioboto3 confined to one control-plane module, CI-enforced; ephemeral staging only, not a data home |
| CalVer release versioning (2026.7.0) | Decouple milestone names from version numbers; a per-month zero-based REVISION supports multiple same-month releases without a semantic-version argument | ✓ Good — adopted 2026.7.0; `YYYY.MM.REVISION` (no-leading-zero month), historical `vN.M` preserved verbatim |
| Bucketed parallel CI over one serial run (2026.7.0) | Partition the ~1,750-test suite into 9 workflow-step buckets fanned across a matrix, combine per-shard coverage into one gated Codecov upload, skip heavy jobs with success on doc-only changes | ✓ Good — `tests/buckets.json` single source of truth + partition guard; wall-clock CI cut, required checks stay satisfiable on doc-only PRs |
| Docs-drift gate as a hermetic CI test (2026.7.0) | REQUIREMENTS↔ROADMAP↔VERIFICATION traceability went stale after merges across every prior milestone; a `just docs-drift` pytest guard fails CI on drift | ✓ Good — caught a stale Phase-65 ROADMAP checkbox on first run; self-validates milestone bookkeeping |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition:**
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone:**
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-07-03 — Milestone 2026.7.0 Engineering Improvements SHIPPED + archived (13/13 requirements validated → Validated; phases 63-66 detail in `milestones/2026.7.0-ROADMAP.md`, requirements in `milestones/2026.7.0-REQUIREMENTS.md`). CalVer adopted this cycle (`YYYY.MM.REVISION`); v7.0 was the last `vN.M` release. Next: cut the `2026.7.0` tag on the release-PR merge (fires GHCR publish), then plan the Multi-cloud backends milestone (phases 67+) via `/gsd:new-milestone`.*
