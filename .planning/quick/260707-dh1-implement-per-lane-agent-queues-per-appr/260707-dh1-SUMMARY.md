---
task: 260707-dh1
title: Implement per-lane agent queues (nox agent) per the approved design spec
status: complete
mode: quick-full
date: 2026-07-07
spec: docs/superpowers/specs/2026-07-07-agent-queue-lanes-design.md
tasks_completed: 6
tasks_total: 6
key_files:
  created:
    - docs/agent-queue-lanes.md
    - tests/agents/tasks/test_agent_worker_lanes.py
  modified:
    - src/phaze/services/enqueue_router.py
    - src/phaze/services/agent_task_router.py
    - src/phaze/tasks/agent_worker.py
    - src/phaze/config.py
    - src/phaze/routers/agent_push.py
    - src/phaze/routers/pipeline.py
    - src/phaze/routers/tracklists.py
    - src/phaze/services/cloud_staging.py
    - src/phaze/services/backends.py
    - src/phaze/tasks/reenqueue.py
    - src/phaze/services/pipeline.py
    - src/phaze/main.py
    - docker-compose.agent.yml
    - docker-compose.cloud-agent.yml
    - README.md
    - docs/README.md
metrics:
  full_suite: "2254 passed, 4 skipped"
  changed_module_coverage: "96.97% (enqueue_router 100%, agent_worker 97.22%, pipeline 96.89%, config 96.54%, agent_task_router 95.65%)"
---

# Quick 260707-dh1: Per-Lane Agent Queues Summary

Split the nox file-server agent's single shared SAQ worker into 4 per-lane workers
(analyze / fingerprint / meta / io) so I/O offload and cheap analysis stop being
head-of-line-blocked behind CPU-bound essentia backlog — implemented exactly to the
approved design (`docs/superpowers/specs/2026-07-07-agent-queue-lanes-design.md`), no
redesign.

## What shipped (per task)

1. **`LANE_TASKS` map + `lane_for_task` + config knobs** (`2044ce4b`) — canonical
   `dict[str, frozenset[str]]` partition (analyze/fingerprint/meta/io) as the single source
   of truth; `AGENT_TASKS` derived as its union; `LANES` tuple; `lane_for_task` raises
   `ValueError` on any non-agent / unmapped name (the fail-loud guard). Config gains
   `PHAZE_LANE_{ANALYZE,FINGERPRINT,META,IO}_CONCURRENCY` (4/2/2/4) + `PHAZE_AGENT_HEARTBEAT`.
2. **Lane-parametrized `agent_worker` + single-lane heartbeat** (`c42ceb64`) — resolves
   `PHAZE_AGENT_LANE` at import → `phaze-agent-<base>-<lane>` queue, only `LANE_TASKS[lane]`
   functions, lane concurrency; all-mode (unset) preserves today's base queue + all 8 functions
   + `worker_max_jobs`; invalid lane raises `RuntimeError` at import; heartbeat launch gated on
   `agent_heartbeat_enabled`. Import boundary stays Postgres-free.
3. **Lane-aware queue naming + producer/reader rewiring** (`44dbf3b8`) — `queue_for(agent_id,
   lane)` REQUIRED lane (no default); `all_lane_queues` + read-only `legacy_base_queue`;
   `enqueue_for_agent`/`resolve_queue_for_task` derive the lane; ALL producers rewired
   (agent_push analyze/io, pipeline analysis, cloud_staging s3_upload io, backends LocalBackend
   analyze + ComputeAgentBackend io, reenqueue analyze/io/per-row `lane_for_task`, tracklists poll
   meta); depth readers sum all 4 lanes + legacy base. New no-default-queue regression proves every
   producer resolves to a `-<lane>` name.
4. **Homelab 4-lane compose split + drain + compute single-lane** (`08282c4d`) — 4 lane services
   from one image (identical command, env-only diff), CPU lanes pin `OMP_NUM_THREADS=1` +
   TF intra/inter-op=1; heartbeat=true on `worker-analyze` only; off-by-default `worker-drain`
   (profile-gated, all-mode, heartbeat off); cloud-agent adopts `PHAZE_AGENT_LANE=analyze`.
5. **Mirror-contract test + docs** (`78ec3aad`) — subprocess per-lane assertion in
   `test_task_split.py` (registered SAQ names == `LANE_TASKS[lane]`, union == `AGENT_TASKS`,
   all-mode = all 8); `docs/agent-queue-lanes.md` (topology, core budget, thread pinning,
   heartbeat A1 caveat, migration/drain runbook); README + docs index updated.
6. **Full-suite gate + collateral** (`d8fea898`) — fixed all lane-routing test collateral across
   the shared/agents/analyze/identify/discovery buckets.

## Verification (full gate — all green)

- **Tests:** `2254 passed, 4 skipped` across `tests/shared tests/agents tests/analyze tests/identify tests/discovery`.
- **Coverage (changed modules):** 96.97% total — `enqueue_router` 100%, `agent_worker` 97.22%,
  `pipeline` 96.89%, `config` 96.54%, `agent_task_router` 95.65% (all above the 90% floor / 95% gate).
- **`uv run ruff check .`** — All checks passed. **`ruff format --check .`** — 484 files formatted.
- **`uv run mypy .`** — clean, 196 source files. This is the routing-completeness backstop:
  `queue_for` requires a lane, so a mypy pass proves every call site supplies one.
- **`pre-commit run --all-files`** — every hook Passed (no `--no-verify` anywhere).

## Explicit confirmations (requested)

- **(a) mypy clean with `queue_for` requiring a lane** — yes; the full `uv run mypy .` passes,
  meaning every `queue_for(` call site supplies an explicit lane (producers via `lane_for_task`,
  readers via `all_lane_queues` / `legacy_base_queue`). No default lane was added to silence mypy.
- **(b) no-default-queue regression passes** — yes; `tests/shared/core/test_no_default_queue_producers.py`
  proves every agent-task producer resolves to `phaze-agent-<id>-(analyze|fingerprint|meta|io)`,
  never the bare base.
- **(c) exactly one heartbeat per agent** — yes; the heartbeat launch is gated on
  `agent_heartbeat_enabled`, compose sets it true on `worker-analyze` only (guard test
  `test_exactly_one_heartbeat_enabled`), and the compute agent (single lane) keeps its lone heartbeat.

## Incident guards preserved

Phase-30 no-default-queue routing (unmapped names RAISE via `lane_for_task`, controller routing
unchanged), v4.0.8 full payloads (untouched — producers still build complete payloads), deterministic-key
dedup + the `before_enqueue` hook chain (per lane via `build_pipeline_queue`, unchanged), dashboard
queue-depth counters (now sum all lanes + legacy base), Phase-46 heartbeat-as-asyncio-task (one lane only).

## Deviations from plan

### Auto-fixed / blocking (Rule 3)

**1. Lane-aware test doubles + collateral assertions.** `queue_for` gaining a REQUIRED `lane` forced
`tests/_queue_fakes.py` (FakeTaskRouter / DedupFakeTaskRouter / stub_app_state router) to become
lane-aware (`_lane_key`/`_lane_name`, `all_lane_queues`, `legacy_base_queue`), and ~40 collateral
assertions across `test_pipeline`, `test_routing_seam`, `test_main_lifespan`, `test_agent_push`,
`test_tracklists`, `test_recovery`, `test_agent_task_router`, `test_reenqueue`, `test_staging_cron`,
`test_backends`, `test_cloud_staging` were repointed to the correct lane queue key
(`nox-analyze` / `nox-meta` / `nox-io`, etc.). These files were not in the plan's `files_modified`
but were required for the suite to stay green under the new routing.

## Environmental note

`docker compose config -q` (Task 4 verify) could **not** be run — the docker compose plugin is
absent in this sandbox (only bare `docker` is available; `docker-compose` v1 is also absent). The
compose YAML was instead validated via `yaml.safe_load` (both files parse; every service declares an
image) plus the 28 compose guard tests (`test_agent_compose.py` + `test_cloud_agent_compose.py`, all
passing). The structural invariants the `config -q` check would catch are covered by those guard tests.

## Ops follow-up (not code)

Landing this in prod needs a homelab redeploy: bring up the 4 lane workers, start `worker-drain`
(`--profile drain`), and remove it once `phaze-agent-nox` reports 0 queued + 0 active. Runbook in
`docs/agent-queue-lanes.md`.

## Self-Check: PASSED

- Created files present: `docs/agent-queue-lanes.md`, `tests/agents/tasks/test_agent_worker_lanes.py` — FOUND.
- All 6 task commits present in `git log`: `2044ce4b`, `c42ceb64`, `44dbf3b8`, `08282c4d`, `78ec3aad`, `d8fea898` — FOUND.
