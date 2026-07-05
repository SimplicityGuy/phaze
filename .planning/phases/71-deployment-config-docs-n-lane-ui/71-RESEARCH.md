# Phase 71: Deployment, Config, Docs & N-Lane UI - Research

**Researched:** 2026-07-04
**Domain:** FastAPI + Jinja2 + HTMX presentation/ops layer over the shipped multi-cloud backend registry + tiered drain scheduler (Phases 67–70). Server-rendered, single-poll, degrade-safe.
**Confidence:** HIGH (this is a code-grounded investigation of existing paths; every mechanic below is cited by `file:line` from the real repo, not inferred)

## Summary

Phase 71 is a **presentation + ops close-out** over already-proven code. Three surfaces close BEUI-01/02/03: an **N-lane read-only grid** (generalizing the fixed 3-card Analyze grid to one card per registry backend), a **live force-local master toggle** (incident-response revert), and **docs** (a runbook + `configuration.md` coverage of the `backends:` schema). The routing algorithm, per-backend caps, and per-cluster reconcile are frozen — the ONE sanctioned new routing-path control is the BEUI-02 force-local gate.

The codebase already gives you every ingredient: a uniform per-backend `in_flight_count` COUNT keyed by `backend_id` (`services/backends.py:164`), per-backend `is_available()` probes (local always-True cheap; compute agent-gate; Kueue live kr8s API call), a per-`backend_id` reconcile (Phase 70) that makes GROUP-BY admission attribution possible, a proven **DB control-row pattern** (`pipeline_stage_control` + `services/stage_control.py` + `routers/pipeline_stages.py`) to mirror verbatim for the force-local flag, the **seeded-identically dual-context discipline** (`build_dashboard_context` + `pipeline_stats_partial`) plus the **OOB-swap-of-a-whole-partial** mechanism (the 6 cloud cards) to ride the existing 5s poll with no second loop, and the **never-500 `_safe_count`/SAVEPOINT idiom** the new snapshot read must follow.

**Primary recommendation:** Add a degrade-safe `get_backend_lane_snapshot(session)` service (returns `[]` on error), seed it identically into both context builders, extract `#analyze-lanes` into an OOB-swappable partial included by both `analyze_workspace.html` (initial) and `stats_bar.html` (poll, `oob=True`), loop `_lane_card.html` over it rank-ascending. For BEUI-02, add a one-row `route_control` table mirroring `pipeline_stage_control`, a thin `POST /pipeline/routing/force-local` (+ revert) endpoint mirroring `pipeline_stages.py`, a header pill, and gate the drain + duration-router the same way `cloud_enabled` already gates. **Critical correction below (§Assumptions / §Docs): the `cloud_target` back-compat shim assumed by CONTEXT D-12/D-13 does NOT exist — Phase 67 removed `cloud_target` outright "with no shim."** The planner must reconcile D-13 against this before writing a task.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions (verbatim from 71-CONTEXT.md `<decisions>`)

**Lane data plumbing — BEUI-01**
- **D-01 (new read-only snapshot service):** Add a dedicated `get_backend_lane_snapshot(session)` service that reads the Phase-67 `backends` registry, COUNTs `cloud_job` per `backend_id` (the same D-02 in-flight substrate `select_backend` uses), and returns a **list of lane dicts** — one per registry backend. Pure read, degrade-safe (returns `[]` on any DB error, matching the never-500 `_safe_count` idiom), **zero coupling to the advisory-locked drain tick**. Chosen over reusing the drain's transient `BackendSlot`.
- **D-02 (live `is_available()` probe per poll, bounded + isolated):** Each lane's available/offline status comes from a **live `is_available()` probe on every poll**. Probes are fired **concurrently** (`asyncio.gather`), each wrapped in **its own short timeout (~1–2s) + try/except** (extends Phase 70 D-07 per-backend isolation). A timing-out/raising backend renders **`offline` for that poll** while every other lane is unaffected; the whole fan-out is bounded to ~one timeout so a hung cluster can **never stall the shared `/pipeline/stats` poll**. Planner must confirm the exact timeout value + that the local backend's cheap `is_available()` isn't penalized.
- **D-03 (per-lane admission attribution):** The snapshot service ALSO returns **per-`backend_id`** admission state (cloud_phase counts / inadmissible / localqueue-unreachable, GROUP BY `backend_id` — now possible because Phase 70 made reconcile per-`backend_id`), so **each lane card carries its own quota-wait-vs-Inadmissible attribution**.
- **D-04 (join into BOTH context builders):** The lane list must be seeded **identically** in `build_dashboard_context()` (full-page load) AND `pipeline_stats_partial()` (the 5s OOB re-push), matching the existing seeded-identically discipline — no second loop, no new backend endpoint for reads.

**N-lane card layout — BEUI-01**
- **D-05 (responsive auto-fit wrapping grid):** Replace the fixed `grid grid-cols-3` in `analyze_workspace.html` with a **responsive wrapping grid**. Card size/rhythm unchanged from Phase 58 (`_lane_card.html` stays the per-card unit, now `{% for %}`-looped). Exact Tailwind classes are planner/UI-phase discretion within the C3 two-weight contract.
- **D-06 (order by rank ascending):** Lane cards render **lowest-rank first**, **tie-broken by `id`**. Each card shows **rank** alongside in-flight/cap + the available/offline word-label (never hue-only — WCAG 1.4.1).
- **D-07 (6 global cloud-state cards → Claude's discretion, lean keep-as-roll-up):** Recommended lean: **keep them as an overall cross-lane roll-up below the lane grid** (lowest risk — no restyle, WORK-03 alert cards + their OOB swap ids untouched). Folding them entirely is the higher-risk alternative and NOT the default.

**Master toggle — BEUI-02**
- **D-08 (live one-click runtime toggle):** A **live UI toggle that immediately forces all routing to local** — no redeploy, reversible. Adds **one force-local gate read** to the routing path; that read IS the requirement (algorithm, ranks, caps otherwise unchanged). Chosen over a config-only "edit registry all-local & restart" gate.
- **D-09 (persist in a DB control row):** Persisted in a **small DB control row**, mirroring the existing **per-stage pause/priority control-table pattern** (`services/pipeline.py` `_DEFAULT_CONTROLS` / the Phase 37/38 control table). Read in `select_backend`/the router the same way `paused` is read. Chosen over a Redis flag.
- **D-10 (global header status strip):** The toggle lives in the **persistent header status strip**. Writing it is a **new thin write endpoint** (the sanctioned Review-&-Apply-era thin-endpoint pattern). Planner decides confirm-vs-instant UX; note it's reversible (instant-on defensible).

**Docs & deprecation — BEUI-03**
- **D-11 (new runbook + update configuration.md):** Add a **new `docs/runbook.md`** (master toggle / incident revert procedure, how to read the N lanes, spillover behavior, per-backend `_FILE` secrets) AND update **`docs/configuration.md`** with the `backends:` schema + `_FILE` secrets + a **short** deprecation note. `docs/cloud-burst.md` / `docs/k8s-burst.md` get pointers to the unified `backends` model.
- **D-12 (`cloud_target` = trivial deprecation, no migration guide):** Per operator: **no one ever deployed `cloud_target=a1|k8s` live — there is no config to migrate.** Document `backends:` as the way, show the trivial **1:1 `cloud_target`→`backends` equivalence** for completeness, mark `cloud_target` **deprecated**. No migration runbook, no removal date; the back-compat shim stays (harmless).
- **D-13 (docs + one-line startup deprecation log):** Deprecation is **docs + a single deprecation log-warning emitted at startup when `cloud_target` is set** (tiny code touch). The shim keeps working silently otherwise; no removal version pinned.

### Claude's Discretion (verbatim)
- Fate of the 6 global cloud-state cards (D-07) — lean keep-as-roll-up, but planner/UI-phase may fold.
- Exact responsive grid Tailwind classes (D-05) and card rank presentation within the C3 two-weight contract.
- Exact `is_available()` probe timeout value + concurrency mechanics (D-02); whether the local backend probe is short-circuited.
- Master toggle confirm-vs-instant UX (D-10) — reversible control, instant-on defensible.
- Exact DB control-row shape/name for the force-local override (D-09) — follow the pause/priority control-table schema.
- `docs/runbook.md` vs `configuration.md` exact section split (D-11).

### Deferred Ideas (OUT OF SCOPE)
- **PROV-01** — new concrete cloud providers + the `ComputeAgentBackend` `agent_ref → Agent.id` resolution fix (Future Requirements, deferred from Phase 70 D-05).
- **Folding the 6 global cloud-state cards fully into per-lane cards** (removing the global roll-up) — deferred as higher-risk; default keeps them as a roll-up.
- **`cloud_target` shim removal** — no removal date this phase.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| BEUI-01 | Operators can see all N backend lanes (available/offline, in-flight/cap, rank, per-lane Kueue quota-wait-vs-Inadmissible) | New `get_backend_lane_snapshot()` (§Pattern 1) over `_BaseBackend.in_flight_count` (`backends.py:164`) + concurrent bounded `is_available()` probes (§Pattern 2) + per-`backend_id` admission GROUP BY (§Pattern 3); rendered by looping `_lane_card.html`, OOB-swapped whole-grid (§Pattern 4) seeded into both context builders (`pipeline.py:455`/`:598`) |
| BEUI-02 | Revert everything to local for incident response (live master toggle) | New `route_control` DB row mirroring `pipeline_stage_control` (`models/pipeline_stage_control.py`) + thin write endpoint mirroring `routers/pipeline_stages.py` + gate at the same call sites `cloud_enabled` gates (drain `release_awaiting_cloud.py:112`, router `pipeline.py:335`) — §Pattern 5 |
| BEUI-03 | Runbook for the `backends:` schema and the `cloud_target`→`backends` migration | Existing `docs/configuration.md` (already documents the registry + marks `cloud_target` superseded, line 123) + new `docs/runbook.md`; **D-13 startup deprecation-log needs reconciliation — see §Assumptions Log A1** |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Backend lane snapshot (registry + counts + probes) | API / Backend (service `services/backends.py` or a new `services/lane_snapshot.py`) | Database (COUNT `cloud_job` GROUP BY `backend_id`) | Pure read over the registry + `cloud_job` table; belongs with the other degrade-safe pipeline reads, NOT in the template |
| Lane grid render (N cards, rank order, states) | Frontend Server (SSR Jinja) | — | Server-rendered loop of `_lane_card.html`; ordering is server-side (pre-sorted list), template loops verbatim |
| Lane grid live refresh | Frontend Server (OOB swap on existing poll) | Browser (HTMX swap) | Rides `/pipeline/stats` 5s poll; whole `#analyze-lanes` OOB-swapped as a unit — NOT per-lane Alpine store keys |
| Force-local flag persistence | Database (control row) | — | Survives restart; single source both router + drain read via their existing session |
| Force-local write | API / Backend (thin POST endpoint) | Browser (HTMX `hx-post` from header pill) | Mirrors the sanctioned pause/resume thin-endpoint pattern |
| Force-local routing gate | API / Backend (drain cron + duration router) | — | The one new routing-path read; gates identically to `cloud_enabled` |
| Header pill state | Frontend Server (SSR initial) + API (authoritative write response) | — | Global chrome; initial `aria-checked` seeded server-side, authoritative state from the write response (no optimistic mutation) |
| `cloud_target` deprecation log | API / Backend (config/startup) | — | See §Assumptions A1 — the assumed shim location does not exist |
| Docs (runbook + configuration) | Docs (prose) | — | No code surface (D-11..D-13 prose, minus the D-13 log line) |

## Standard Stack

**No new dependencies.** The stack is LOCKED by the milestone constraint (zero new deps) and by the UI-SPEC. Everything below is already pinned in `pyproject.toml` and in use.

### Core (existing, verified in repo)
| Library | Version (pyproject) | Purpose | Why Standard |
|---------|--------------------|---------|--------------|
| Python | `>=3.14,<3.15` `[VERIFIED: pyproject.toml:10]` | Runtime | Project constraint. `asyncio.timeout()` / `asyncio.wait_for()` both available (3.11+) for the D-02 bounded probes |
| FastAPI | `>=0.138.0` `[VERIFIED: pyproject.toml:21]` | Router + `Jinja2Templates` | Already the API framework; the thin endpoint + partials plug into existing routers |
| Jinja2 | (FastAPI dep) | Server-rendered partials | `_lane_card.html`, `analyze_workspace.html`, `header.html` are all Jinja |
| SQLAlchemy 2 async + asyncpg | in-repo | `cloud_job` COUNT + control-row read/write | The snapshot COUNT and the control row use the existing async session |
| HTMX 2 / Alpine 3 / Tailwind (self-hosted) | in-repo (`assets/`, `base.html`) | OOB swaps, `$store.pipeline`, styling | UI-SPEC-locked; no CDN, no build change |
| python-multipart | `>=0.0.32` `[VERIFIED: pyproject.toml:48]` | Form-body parsing for the thin POST | The pause/priority endpoints post `application/x-www-form-urlencoded` via HTMX — the toggle mirrors this |

### Testing (existing)
| Library | Version | Purpose |
|---------|---------|---------|
| pytest-asyncio | `>=1.4.0` `[VERIFIED: pyproject.toml:233]` | async router/service tests |
| httpx `AsyncClient` + `ASGITransport` | in-repo (`tests/conftest.py:213`) | `client` fixture hits the real app |

**Installation:** none — `uv sync` already provides everything. Do NOT add packages (milestone zero-new-deps constraint).

## Package Legitimacy Audit

**Not applicable.** This phase installs **zero** external packages (milestone constraint: no new dependency; UI-SPEC "Registry Safety: not applicable — server-rendered Jinja partials, zero new dependency"). No slopcheck / registry verification is required. Every library used is already resolved in `uv.lock`.

## Architecture Patterns

### System Architecture Diagram

```
                          ┌─────────────────────────────────────────────┐
  Browser (HTMX 2 +       │  GET / or /s/analyze   ──► shell.py           │
  Alpine 3, single 5s     │     _render_stage() ──► build_dashboard_      │
  poll from chrome)       │        context()  [seeds lanes + force_local] │
        │                 └─────────────────────────────────────────────┘
        │ every 5s                     │ seeded IDENTICALLY (D-04)
        ▼                              ▼
  GET /pipeline/stats  ──►  pipeline_stats_partial()  ──► stats_bar.html
        │                        │                            │
        │                        │  get_backend_lane_          │ include _analyze_lanes.html
        │                        │  snapshot(session)          │   with oob=True  (hx-swap-oob)
        │                        ▼                            ▼
        │            ┌───────────────────────────┐   #analyze-lanes grid OOB-swaps
        │            │ services: registry read + │   (loops _lane_card.html rank-asc)
        │            │  COUNT cloud_job GROUP BY │
        │            │  backend_id  +  concurrent│   6 global cloud cards stay as
        │            │  is_available() probes    │   roll-up below (D-07, unchanged ids)
        │            │  (asyncio.gather + per-   │
        │            │   backend wait_for/try)   │
        │            └───────────────────────────┘
        │
        ▼ (BEUI-02)
  POST /pipeline/routing/force-local  ──► thin endpoint ──► route_control row (DB)
        │  (header pill hx-post)              │                    │
        │                                     │   read by:         ▼
        └──────── OOB pill + toast ◄──────────┘   drain stage_cloud_window() (early no-op)
                                                  duration router _route_discovered_by_duration()
                                                  (effective cloud_enabled = cloud_enabled AND NOT forced)
```

File-to-implementation mapping is in Component Responsibilities (§Integration Points below), not the diagram.

### Recommended Project Structure (touch-list only — no new top-level dirs)
```
src/phaze/
├── services/
│   ├── backends.py            # ADD get_backend_lane_snapshot() here OR a new lane_snapshot.py; retire resolved_non_local_kind uses
│   └── (new) route_control.py # OR fold force-local read/write helpers into an existing service (mirror stage_control.py)
├── models/
│   └── (new) route_control.py # one-row control model, mirror pipeline_stage_control.py
├── routers/
│   ├── pipeline.py            # seed lanes + force_local into BOTH context builders (D-04)
│   └── (new) routing.py       # OR add to pipeline_stages.py: thin POST force-local/revert (mirror set_priority/pause)
├── templates/
│   ├── pipeline/partials/
│   │   ├── _lane_card.html         # extend: rank caption + {in_flight}/{cap} + per-lane admission caption
│   │   ├── (new) _analyze_lanes.html  # extracted OOB-swappable grid (loops _lane_card.html)
│   │   ├── analyze_workspace.html  # replace inline 3-card grid with include of _analyze_lanes.html
│   │   └── stats_bar.html          # include _analyze_lanes.html with oob=True (poll re-push)
│   └── shell/partials/header.html  # add the force-local pill left of the Agents pill
├── config.py                  # D-13 deprecation log — SEE §Assumptions A1 (assumed shim absent)
alembic/versions/
└── 031_add_route_control.py   # new migration (next number after 030) — mirror 020_add_pipeline_stage_control.py
docs/
├── runbook.md (new) · configuration.md (update) · cloud-burst.md/k8s-burst.md (pointers)
```

### Pattern 1: Degrade-safe backend-lane snapshot (D-01)
**What:** A pure read returning one dict per registry backend, `[]` on any DB error.
**When to use:** Seeded into both context builders; consumed by the looped card.
**Key mechanics (all verified in-repo):**
- Registry: `resolve_backends(settings)` returns `list[Backend]` — one impl per entry, N non-local supported `[VERIFIED: services/backends.py:442]`. Each carries `id`/`rank`/`cap` (`Backend` protocol `backends.py:120-122`).
- In-flight: `_BaseBackend.in_flight_count(session)` = `COUNT(cloud_job WHERE backend_id==self.id AND status IN {UPLOADING,UPLOADED,SUBMITTED,RUNNING})` `[VERIFIED: services/backends.py:164-176]`. `LocalBackend.in_flight_count` is hard-0 `[VERIFIED: backends.py:192]`.
- Degrade contract: wrap the COUNTs in the `_safe_count` idiom (`services/pipeline.py:275`) — `try/except → log → guarded rollback → 0`; return `[]` for the whole snapshot on a top-level failure, mirroring `get_analyze_stage_files` returning `[]` inside a SAVEPOINT (`pipeline.py:790`). **Order matters:** per-backend COUNTs that share one session must each roll back on failure so a poisoned transaction doesn't cascade (the `_safe_count` rollback is exactly for this).
```python
# Source: pattern derived from services/backends.py:164 + services/pipeline.py:275,416
async def get_backend_lane_snapshot(session: AsyncSession) -> list[dict[str, Any]]:
    """One dict per registry backend, rank-ascending, degrade-safe ([] on error)."""
    try:
        backends = resolve_backends(cast("ControlSettings", get_settings()))
        admission = await _admission_by_backend_id(session)      # D-03, GROUP BY backend_id
        availability = await _probe_availability(session, backends)  # D-02, bounded concurrent
        lanes = []
        for be in backends:
            lanes.append({
                "id": be.id, "kind": _kind_of(be), "rank": be.rank, "cap": be.cap,
                "in_flight": await be.in_flight_count(session),
                "available": availability.get(be.id, False),
                **admission.get(be.id, _ZERO_ADMISSION),
            })
        lanes.sort(key=lambda l: (l["rank"], l["id"]))           # D-06
        return lanes
    except Exception:
        logger.warning("backend_lane_snapshot_degraded", exc_info=True)
        try: await session.rollback()
        except Exception: logger.warning("lane_snapshot_rollback_failed", exc_info=True)
        return []
```

### Pattern 2: Bounded, isolated concurrent `is_available()` probes (D-02)
**What:** Fire every backend's `is_available()` at once, each bounded by its own timeout + try/except, so a hung Kueue cluster renders that one lane `offline` for the poll without stalling the shared request.
**Why load-bearing — the latency risk is real:** `KueueBackend.is_available` calls `kube_staging.get_local_queue(self._kube())`, a **live kr8s API call to a remote cluster** `[VERIFIED: services/backends.py:325-339 + services/kube_staging.py:274 get_local_queue → _api() → kr8s.asyncio.api]`. An unreachable/slow cluster blocks for the kr8s/httpx default timeout (seconds to tens of seconds). `LocalBackend.is_available` is unconditionally `True` — no I/O `[VERIFIED: backends.py:188-190]` (D-02 short-circuit is safe/free). `ComputeAgentBackend.is_available` is a single DB `select_active_agent` `[VERIFIED: backends.py:244-254]` (cheap).
**Codebase precedent for the timeout idiom** (cite in the plan): `asyncio.wait_for(..., timeout=...)` guards already wrap remote I/O in `tasks/s3_upload.py:126` and `tasks/push.py:184`; `asyncio.gather` fan-out already used in `tasks/discogs.py:66`. Python 3.14 also supports `async with asyncio.timeout(...)`.
```python
# Source: idiom from tasks/s3_upload.py:126 (wait_for) + tasks/discogs.py:66 (gather) + backends.py:143-148 (drain's per-backend isolation)
_PROBE_TIMEOUT_SEC = 1.5   # D-02: planner confirms exact value; keep well under the 5s poll
async def _probe_one(session, be) -> tuple[str, bool]:
    if isinstance(be, LocalBackend):        # D-02 short-circuit: no network dep, never penalize
        return be.id, True
    try:
        return be.id, await asyncio.wait_for(be.is_available(session), _PROBE_TIMEOUT_SEC)
    except Exception:                        # TimeoutError OR any probe raise -> offline this poll
        logger.info("lane probe failed/timed out -> offline", backend_id=be.id)
        return be.id, False
async def _probe_availability(session, backends) -> dict[str, bool]:
    results = await asyncio.gather(*(_probe_one(session, be) for be in backends))
    return dict(results)
```
**Session caveat (planner must weigh):** all probes here share ONE `AsyncSession`. `ComputeAgentBackend.is_available` runs a DB query on it; concurrent use of a single AsyncSession is not safe. Options: (a) short-circuit local (free), run the single compute probe and the Kueue probes — Kueue probes are kr8s (no DB session use, they use kr8s clients not the SQLAlchemy session, verified `kube_staging` builds its own kr8s api), so only the compute probe touches the session and there is at most 1 compute backend (D-05 ≤1-compute invariant) → no concurrent-session contention in practice; or (b) probe availability in a separate short-lived session/`asyncio.gather` over independent sessions. Confirm (a) holds: Kueue `is_available` takes `session` in its signature but **ignores it** (`# noqa: ARG002 — kueue probes the cluster, not a DB agent`, `backends.py:325`), so only the lone compute probe uses the session — (a) is safe. **This is the recommended, simplest reading.**

### Pattern 3: Per-`backend_id` admission attribution (D-03)
**What:** GROUP BY `backend_id` over `cloud_job` to give each Kueue lane its own quota-wait-vs-Inadmissible counts.
**Now possible because:** Phase 70 made reconcile per-`backend_id`, and `cloud_job` carries `backend_id` (migration `029_add_cloud_job_backend_id.py`) + `cloud_phase` (migration `027`). The existing GLOBAL reads to mirror: `get_cloud_phase_counts` (four `_safe_count` COUNTs of `cloud_job.cloud_phase` `[VERIFIED: services/pipeline.py:1165-1199]`), `get_inadmissible_count` (`pipeline.py:1122`), `get_localqueue_unreachable` (Redis flag, cross-process, NOT per-backend `[VERIFIED: pipeline.py:1145]`).
**Attribution semantics:** `cloud_phase` is NULL for local/compute rows (admission is Kueue-only, `pipeline.py:1175`), so a GROUP BY naturally attributes counts only to Kueue lanes. `quota_wait` = `cloud_phase == QUEUED_BEHIND_QUOTA`; Inadmissible is `get_inadmissible_count`'s predicate (the reconcile-flagged Inadmissible marker) filtered by `backend_id`. **Caveat:** `localqueue_unreachable` is a single cross-process Redis boolean written by the controller startup probe — it is NOT per-`backend_id` today; with N Kueue clusters it can't distinguish which cluster is unreachable. Either (a) leave it as the global roll-up signal (D-07 roll-up keeps it), or (b) derive per-lane offline purely from the D-02 live probe (recommended — the live probe already tells you per-cluster reachability, making the global Redis flag redundant for the per-lane card). **Recommend (b) for per-lane state, keep the Redis flag only in the global roll-up.** Flag for the planner.

### Pattern 4: Whole-grid OOB swap on the existing poll (D-04, WORK-05/R-2)
**What:** Extract `#analyze-lanes` into `_analyze_lanes.html`; include it in `analyze_workspace.html` (initial, no oob) and in `stats_bar.html` with `oob=True`. The grid element emits `hx-swap-oob="true"` only when `oob` is set — **exactly** how the 6 cloud cards do it.
**Verified mechanism:**
- The dual-context discipline: `build_dashboard_context()` (`pipeline.py:455`) and `pipeline_stats_partial()` (`pipeline.py:598`) seed the SAME keys; the cloud counts appear in both `[VERIFIED: pipeline.py:534 vs :644]`. Add `"lanes": await get_backend_lane_snapshot(session)` to BOTH.
- The OOB pattern: `admission_state_card.html:27` renders `{% if oob %}hx-swap-oob="true"{% endif %}` on its `<section id="admission-state-card">`; `stats_bar.html:103` includes it with `{% with oob = True %}` `[VERIFIED]`. Mirror this for `_analyze_lanes.html` on `#analyze-lanes`.
- OOB lands only on ids already in the DOM (`_workspace_poll_seeds.html` header comment). `#analyze-lanes` is present on the Analyze stage → swap lands; on other stages it's absent → swap harmlessly no-ops (same as the cloud cards).
- The `oob_counts=False` initial-render discipline: `shell.py:164`/`_render_stage` sets `oob_counts=False` so the initial load never emits OOB (Pitfall 5). The lane grid must follow the SAME gate — initial include passes no `oob`, poll include passes `oob=True`.
**Why whole-grid OOB, not per-lane Alpine store keys:** N is dynamic and every datum changes per poll; k8s counts already arrive as OOB swaps not store keys (UI-SPEC Pitfall 2). Inventing N per-lane `$store.pipeline` keys is unsafe. `_lane_card.html` stays pure presentation (no `hx-trigger`/`setInterval` — verified `_lane_card.html:30`).

### Pattern 5: Force-local control row + gate (D-08/D-09/D-10) — mirror the pause/priority plane VERBATIM
**The exact model to mirror:** `PipelineStageControl` `[VERIFIED: models/pipeline_stage_control.py]` — `__tablename__`, PK column, `Boolean server_default=text("false")`, `TimestampMixin`. Migration `020_add_pipeline_stage_control.py` is the migration template; the new migration is `031` (last is `030_add_cloud_job_staging_bucket.py`).
**Recommended shape (planner discretion on exact name, D-09):** a single-row table, e.g. `route_control(id text PK default 'global', force_local bool not null default false)`, or reuse a key/value shape. A single global row (like the 3-row `pipeline_stage_control`) is the cleanest.
**The read helper to mirror:** `get_stage_controls(session)` — reads the row(s), overlays onto `_DEFAULT_CONTROLS`, and degrades to defaults on ANY error so the hot 5s poll never 500s `[VERIFIED: services/pipeline.py:416-446]`. Build `get_route_control(session) -> bool` the same way (default `False` on error/absent).
**The write endpoint to mirror:** `routers/pipeline_stages.py` — `_load_control_row` (`session.get` PK, defensive-create if absent, `with_for_update` for read-modify-write, `pipeline_stages.py:59-74`), `pause`/`resume` (`row.paused = True/False; await …; await session.commit(); return _response(row)`, `pipeline_stages.py:103-128`). The toggle endpoint sets `row.force_local = True/False`, commits, returns the new state. Form-body encoding matches HTMX (`Annotated[int, Form()]` precedent at `pipeline_stages.py:85`; the toggle likely needs no body or a `force: bool` form field).
**The gate insertion points (behavior-preserving — "behave exactly like `cloud_enabled=False`"):**
- **Drain (primary):** `stage_cloud_window` early-returns `{"staged":0,"skipped":0}` when `not cfg.cloud_enabled` BEFORE the advisory lock/snapshot `[VERIFIED: tasks/release_awaiting_cloud.py:110-113]`. But that check is BEFORE the session opens (`:123`). Add the force-local read just after the session opens, before the advisory lock: if `await get_route_control(session)` → same early no-op return. This stops ALL new cloud/Kueue dispatch instantly.
- **Duration router (secondary):** `_route_discovered_by_duration` computes `is_long = cloud_enabled and duration is not None and duration >= threshold` `[VERIFIED: pipeline.py:335]`; the callers pass `settings.cloud_enabled` (`pipeline.py:396,703`). Make the effective flag `cloud_enabled AND NOT force_local` so NEW long files route local immediately instead of being HELD in `AWAITING_CLOUD` while forced. This requires threading the force-local read into `trigger_analysis` / `trigger_analysis_ui` (they have a session).
- `select_backend` itself is **pure** (no DB/config beyond bounded knobs, `backend_selection.py:80-119`) — do NOT inject the flag there; gate at the two callers that already read `cloud_enabled`. **Planner decision:** whether to gate the router leg too, or only the drain. Gating only the drain is the minimal change (new long files still get HELD, then stay held until unforced or spill via staleness — but the drain no-op means no spill happens either); gating both is truer to "revert to local." **Recommend gating both**; document the held-file behavior in the runbook.
**Header pill initial state (D-10):** the header (`shell/partials/header.html`) renders on every full-page shell load and is GLOBAL (all `/s/{stage}`), whereas `build_dashboard_context` only runs for the Analyze stage (`shell.py:166`). To seed the pill's initial `aria-checked` on EVERY page, read `get_route_control(session)` in `_render_stage` unconditionally (one cheap PK get) and add `force_local` to the base shell context — do NOT rely only on the Analyze-only dashboard context. The authoritative post-click state comes from the write response (no optimistic mutation — mirror the Phase-38 `@htmx:after-request` authoritative-write pattern noted in the UI-SPEC).

### Anti-Patterns to Avoid
- **Second poll loop / new read endpoint for lanes.** Forbidden by WORK-05/R-2/D-04. Ride `/pipeline/stats`.
- **Per-lane `$store.pipeline` keys.** N is dynamic; use whole-grid OOB (Pattern 4).
- **Coupling the lane read to the drain's `BackendSlot`.** That snapshot is advisory-locked, mid-tick only (D-01). Build an independent read.
- **Emitting `hx-swap-oob` on the initial full-page render.** Collides on duplicate ids; gate behind `oob`/`oob_counts` (Pitfall 5, `shell.py:164`).
- **Injecting the force-local flag into pure `select_backend`.** Gate at the callers; keep the policy pure.
- **A third UI type weight / new spacing token.** UI-SPEC C3 two-weight contract; only inherited `mt-3` is non-standard (frozen).
- **Reintroducing the "one shared bucket" framing in docs.** Superseded by REG-05 (CONTEXT canonical-refs note on the design doc §6/§7).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-backend in-flight count | A new COUNT query | `_BaseBackend.in_flight_count` (`backends.py:164`) | Already the exact D-02/D-10 substrate the drain uses; identical semantics avoids UI-vs-scheduler drift |
| Degrade-safe read | A bespoke try/except | `_safe_count` / SAVEPOINT idiom (`pipeline.py:275`, `:790`) | The never-500 contract the whole 5s poll depends on |
| Runtime-mutable control flag | A Redis key or env var | The `pipeline_stage_control` table pattern (model + `stage_control.py` + `pipeline_stages.py`) | Survives restart, one DB source both drain + router already read, testable, no new dependency (D-09 rejected Redis) |
| Live grid refresh | `setInterval`/`hx-trigger` on the card | Whole-partial OOB swap on the existing poll (cloud-card pattern, `stats_bar.html:73-103`) | Single-poll discipline; the card stays pure presentation |
| Availability isolation | A bare `await` per probe | `asyncio.gather` + per-probe `asyncio.wait_for` + try/except | A hung kr8s call would otherwise stall the shared poll (Pattern 2) |
| Rank ordering in the template | Jinja sort filters | Server-side `list.sort(key=(rank,id))` in the snapshot (D-06) | Template loops verbatim; ordering is a data concern |

**Key insight:** Every "new" capability here is a thin recombination of shipped, tested primitives. The risk is not building something novel — it's drifting from an existing contract (the in-flight COUNT, the degrade idiom, the OOB gate, the control-table pattern). Mirror, don't reinvent.

## Runtime State Inventory

> This is a presentation/ops phase, not a rename/refactor. Still, one **new** runtime-state artifact is introduced (the force-local control row) and one legacy env var must be considered.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | **NEW:** `route_control` DB row (force-local flag). Existing `cloud_job.backend_id` / `cloud_phase` columns (migrations 029/027) are READ, not migrated. | New Alembic migration `031` seeding one default-`false` row (mirror `020`). No data migration of existing rows. |
| Live service config | `backends.toml` (`PHAZE_BACKENDS_CONFIG_FILE`, default `/etc/phaze/backends.toml`) is the sole registry source; documented in this phase (D-11) but NOT changed. | Docs only. |
| OS-registered state | None — verified: no Task Scheduler / systemd / pm2 artifacts touched by a UI/docs phase. | None. |
| Secrets/env vars | Per-backend `*_file` inline secret pointers in `backends.toml` (kube token, S3 keys) + control-plane `<VAR>_FILE` env secrets (`config.py:94,366`). **Legacy `PHAZE_CLOUD_TARGET`** env var: `model_config = extra="ignore"` `[VERIFIED: config.py:87]` → a set `PHAZE_CLOUD_TARGET` is **silently dropped** (not a model field). | Docs cover `_FILE` secrets (D-11). D-13 startup log requires ACTIVE detection of the legacy env var — see §Assumptions A1. |
| Build artifacts | None — no package rename, no egg-info staleness. | None. |

**The canonical question (after every file is updated, what runtime systems still hold old state?):** Nothing stale — this phase ADDS a control row and READS existing columns. The only legacy-state concern is the ignored `PHAZE_CLOUD_TARGET` env var (A1).

## Common Pitfalls

### Pitfall 1: The `cloud_target` back-compat shim CONTEXT assumes does not exist
**What goes wrong:** D-12/D-13 (and the design doc §4.1/§111) describe a `cloud_target` back-compat shim in `config.py` that "synthesizes a one-entry `backends` list" and a "startup deprecation-log when `cloud_target` is set." **There is no such shim.** `configuration.md:123` states the flat `cloud_target` was "**removed with no shim**"; `grep` finds ZERO `cloud_target` references in `src/phaze/` (only tests asserting its ABSENCE, e.g. `test_pipeline.py:1041 assert "cloud_target" not in ctx_local`, and docs). `model_config` is `extra="ignore"` (`config.py:87`), so `PHAZE_CLOUD_TARGET` is silently dropped, not read.
**Why it happens:** CONTEXT was written against the design doc's *proposed* shim (design §111), but Phase 67 as implemented removed `cloud_target` outright.
**How to avoid:** The planner must reconcile D-13 before writing the code task. Options: (a) collapse D-13 to **docs-only** (nothing in code reads `cloud_target`, so there's nothing to warn about at the shim); (b) if the "nudge legacy deploys" intent is kept, ADD an explicit startup check that reads the raw `PHAZE_CLOUD_TARGET` env var (via `_resolution_env`, `config.py:56`) and logs a one-line deprecation warning — a genuinely new tiny code touch, not attaching to a nonexistent shim. **Recommend surfacing to the operator in discuss/plan; do not silently pick.** (§Assumptions A1)
**Warning signs:** any task that says "edit the `cloud_target` shim in config.py" — there is no shim to edit.

### Pitfall 2: Concurrent AsyncSession use during probe fan-out
**What goes wrong:** `asyncio.gather` over probes that each run a query on ONE shared `AsyncSession` corrupts the session (SQLAlchemy async sessions are not concurrency-safe).
**Why it happens:** naive `gather(*(be.is_available(session) …))`.
**How to avoid:** Only `ComputeAgentBackend.is_available` uses the session, and there is ≤1 compute backend (D-05 invariant); `KueueBackend.is_available` ignores its `session` arg (`backends.py:325 noqa ARG002`) and uses its own kr8s client; `LocalBackend` is short-circuited. So at most one probe touches the session — safe. **Verify this invariant holds in the plan** (if a future multi-compute lands via PROV-01 it breaks — but PROV-01 is out of scope).
**Warning signs:** "InterfaceError: another operation is in progress" in tests.

### Pitfall 3: Losing the never-500 degrade contract
**What goes wrong:** The lane snapshot or force-local read raises into `/pipeline/stats`, 500ing the whole 5s poll (kills the entire dashboard, not just the lanes).
**Why it happens:** forgetting the `_safe_count`-style rollback, or letting a probe timeout escape.
**How to avoid:** `[]` on any snapshot error (D-01); `False` default for the force-local read (mirror `get_stage_controls` `pipeline.py:440-446`); per-probe try/except (Pattern 2). The UI-SPEC empty/degrade state (`Lane status unavailable` panel) renders on `[]`.
**Warning signs:** a test that asserts 200 on a forced DB error fails.

### Pitfall 4: OOB swap emitted on initial render / missing DOM target
**What goes wrong:** the grid double-renders or the poll swap no-ops.
**How to avoid:** gate `hx-swap-oob` behind `oob` (initial render passes none — mirror the cloud cards + `oob_counts=False` at `shell.py:164`); ensure `#analyze-lanes` exists in the initial Analyze render so the poll swap lands.

### Pitfall 5: Test non-hermeticity (CI per-bucket isolation)
**What goes wrong:** BEUI tests pass in the full suite but fail under `just test-bucket <bucket>` due to the `get_settings` `lru_cache` leak / `saq_jobs` stub poison (prior-phase reference).
**Why it happens:** `get_settings()` is `@lru_cache(maxsize=1)`; a cached singleton leaks across tests unless cleared.
**How to avoid:** the `conftest.py` autouse fixture already `get_settings.cache_clear()`s before each test (`conftest.py:69`) and the `backends_toml_env` fixture clears before AND after (`conftest.py:174-179`). New tests that set `settings.backends` should use `monkeypatch.setattr(settings, "backends", [...])` (the established pattern, `test_pipeline.py:1038`) or the `backends_toml_env` fixture — never mutate the global un-monkeypatched. Run new tests in isolation, not just the full suite.
**Warning signs:** green full-suite, red isolated bucket.

## Code Examples

### Reading the force-local flag degrade-safe (mirror get_stage_controls)
```python
# Source: services/pipeline.py:416-446 (get_stage_controls degrade pattern)
async def get_route_control(session: AsyncSession) -> bool:
    """True iff routing is forced-local. Degrades to False (cloud-enabled) on any error."""
    try:
        row = await session.get(RouteControl, "global")
        return bool(row.force_local) if row is not None else False
    except Exception:
        logger.warning("route_control_degraded", exc_info=True)
        try: await session.rollback()
        except Exception: logger.warning("route_control_rollback_failed", exc_info=True)
        return False
```

### Thin write endpoint (mirror pipeline_stages.pause/resume)
```python
# Source: routers/pipeline_stages.py:103-128
@router.post("/pipeline/routing/force-local", response_class=HTMLResponse)
async def force_local(request: Request, engage: Annotated[bool, Form()],
                      session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    row = await session.get(RouteControl, "global") or RouteControl(id="global", force_local=False)
    session.add(row)
    row.force_local = engage
    await session.commit()
    # return the pill partial (swapped in place) + an OOB polite-aria-live toast (UI-SPEC copy)
    return templates.TemplateResponse(request=request, name="shell/partials/_force_local_pill.html",
                                      context={"request": request, "force_local": row.force_local})
```

### Drain gate (mirror the cloud_enabled early no-op)
```python
# Source: tasks/release_awaiting_cloud.py:110-127
async with ctx["async_session"]() as session:
    if await get_route_control(session):            # BEUI-02 force-local: same no-op as all-local
        return {"staged": 0, "skipped": 0}
    await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
    ...  # snapshot + select_backend loop unchanged
```

## State of the Art

| Old Approach (pre-71) | Current Approach (71) | When Changed | Impact |
|-----------------------|-----------------------|--------------|--------|
| Fixed 3-card grid (`grid grid-cols-3`, local/A1/k8s hand-included 3×) | N registry-derived lanes, `{% for %}`-looped, rank-ascending | This phase | Handles N same-kind backends (e.g. 2 Kueue clusters) the old grid couldn't |
| `cloud_lane_kind = resolved_non_local_kind(settings)` single legacy-shaped kind ("local"/"compute"/"kueue") | Per-lane snapshot list (D-01) | This phase | `resolved_non_local_kind` CANNOT represent 2 Kueue clusters (`backends.py:468`, `# TRANSITIONAL — Phase 68/71` marker at `pipeline.py:574`). This phase RETIRES it from the Analyze render. |
| No runtime cloud on/off (only `cloud_enabled` config-derived, restart to change) | Live force-local control row (D-08) | This phase | Incident revert with no redeploy |
| Global cloud-state cards only | Per-lane admission attribution (D-03) + global roll-up (D-07) | This phase | Enabled by Phase 70's per-`backend_id` reconcile |

**Deprecated/outdated:**
- `resolved_non_local_kind(settings)` — retire from `analyze_workspace.html` + `build_dashboard_context` (`pipeline.py:576`). It stays used by other single-kind callers (`agent_s3.report_uploaded`, backfill) — do NOT delete it wholesale; only remove the `cloud_lane_kind` context key the N-lane grid replaces. Verify with `test_pipeline.py:1026 test_dashboard_context_binds_cloud_lane_kind` (that test will need updating to assert the new `lanes` key).
- `cloud_target` — already removed from code (Phase 67); this phase marks it deprecated in DOCS only (§Assumptions A1).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | **The `cloud_target` back-compat shim + deprecation site assumed by CONTEXT D-12/D-13 does NOT exist in `config.py`.** `configuration.md:123` says `cloud_target` was "removed with no shim"; zero `cloud_target` refs in `src/phaze/`; `extra="ignore"` silently drops `PHAZE_CLOUD_TARGET`. **The design doc's proposed shim (§111) was not implemented.** `[VERIFIED: config.py:87 + grep + configuration.md:123]` | Pitfall 1, Docs, Runtime State | HIGH — a plan task "edit the cloud_target shim" has nothing to edit. D-13's "startup log when cloud_target is set" needs either (a) collapse to docs-only, or (b) a NEW explicit legacy-env-var check. Must reconcile with operator before executing. |
| A2 | The D-02 probe timeout `~1.5s` is a placeholder; the exact value is Claude's-discretion (D-02) and must be confirmed to sit well under the 5s poll while tolerating a slow-but-healthy kr8s round-trip. `[ASSUMED]` | Pattern 2 | LOW — tune during plan; too-low starves a healthy-but-slow cluster (renders offline), too-high risks poll latency. |
| A3 | Per-lane `offline` is best derived from the live D-02 probe, NOT the global `phaze:k8s:localqueue_unreachable` Redis flag (which is not per-`backend_id`). `[ASSUMED — recommendation]` | Pattern 3 | LOW — if wrong, per-lane offline could mis-attribute with N Kueue clusters; recommendation avoids that. |
| A4 | Gating BOTH the drain AND the duration router (not just the drain) best matches "revert all routing to local" (D-08). `[ASSUMED — recommendation]` | Pattern 5 | MEDIUM — drain-only leaves new long files HELD in AWAITING_CLOUD (not analyzed) while forced; planner should decide + document held-file behavior in the runbook. |
| A5 | A single-row `route_control` table (mirroring the 3-row `pipeline_stage_control`) is the cleanest D-09 shape. `[ASSUMED — D-09 explicitly leaves shape to planner]` | Pattern 5 | LOW — shape is discretionary; any row-persisted flag satisfies D-09. |

**Non-empty table:** the planner + discuss-phase should confirm A1 (blocking for D-13) and A4 (affects runbook copy) before execution.

## Open Questions

1. **D-13 deprecation log placement (blocking).**
   - What we know: no `cloud_target` shim exists; `PHAZE_CLOUD_TARGET` is silently ignored (A1).
   - What's unclear: does the operator want a genuinely-new legacy-env-var detector (log a warning if `PHAZE_CLOUD_TARGET` is set), or is docs-only sufficient?
   - Recommendation: surface in plan-check/discuss; default to docs-only + an OPTIONAL 3-line startup check reading the raw env var if the operator wants the nudge.
2. **Held-file behavior under force-local (A4).**
   - What we know: drain-gate stops new dispatch; router-gate stops new HOLDS.
   - What's unclear: what happens to files ALREADY in `AWAITING_CLOUD` when force-local engages — stay held (drain no-op) or spill to local?
   - Recommendation: gate both; document that already-held files remain held until unforced (or add a note that the staleness spill won't fire while the drain is no-op'd). Runbook must state this so an operator isn't surprised.
3. **Fate of the 6 global cards (D-07 discretion).** Recommendation: keep as roll-up (lowest risk, unchanged OOB ids). Planner/UI-phase may fold.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python `asyncio.timeout`/`wait_for` | D-02 bounded probes | ✓ | 3.14 (3.11+ feature) | — |
| FastAPI / Jinja2 / HTMX / Alpine / Tailwind | all surfaces | ✓ | pinned in `uv.lock` | — |
| PostgreSQL + Alembic | `route_control` migration + `cloud_job` reads | ✓ | in-repo (migrations 001–030) | — |
| kr8s (Kueue `is_available` probe) | D-02 Kueue lane probe | ✓ | `kr8s 0.20.15` (`kube_staging.py:95`) | probe try/except → `offline` (built-in) |
| Live Kueue cluster | actual per-lane availability at runtime | N/A at build time | — | D-02 timeout → `offline` per lane; UAT needs a `backends.toml` with ≥1 Kueue backend to see a non-local lane |

**Missing dependencies with no fallback:** none — all code/config/test dependencies are present. Live-cluster availability is a UAT concern, bounded by the D-02 timeout by design.

## Validation Architecture

> nyquist_validation is enabled (`config.json workflow.nyquist_validation: true`, absent-treated-as-enabled anyway).

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio `>=1.4.0` `[VERIFIED: pyproject.toml:233]` |
| Config file | `pyproject.toml` (`[tool.coverage.*]`); test fixtures in `tests/conftest.py` |
| Quick run command | `uv run pytest tests/shared/routers/test_pipeline.py -x` |
| Full suite command | `uv run pytest` (or `just test` / per-bucket `just test-bucket <bucket>`) |
| Client fixture | `client` (httpx `AsyncClient` + `ASGITransport`, `conftest.py:213`); `session` (fresh migrated DB, `conftest.py:205`) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| BEUI-01 | `get_backend_lane_snapshot` returns one rank-ascending dict per registry backend with `{id,kind,rank,cap,in_flight,available,+admission}` | unit (service) | `uv run pytest tests/shared/services/test_lane_snapshot.py -x` | ❌ Wave 0 |
| BEUI-01 | Snapshot degrades to `[]` on DB error (never raises) | unit | `uv run pytest -k lane_snapshot_degrades -x` | ❌ Wave 0 |
| BEUI-01 | Per-`backend_id` admission attribution (GROUP BY): 2 Kueue backends get distinct quota-wait/inadmissible counts | unit | `uv run pytest -k lane_admission_per_backend -x` | ❌ Wave 0 |
| BEUI-01 | Live probe timeout isolates one hung backend → that lane `offline`, others unaffected, request still fast/200 | unit (fake slow probe) | `uv run pytest -k lane_probe_timeout_isolation -x` | ❌ Wave 0 |
| BEUI-01 | Both context builders seed `lanes` identically (D-04) | unit | `uv run pytest -k lanes_seeded_in_both_contexts -x` (extend `test_dashboard_context_binds_cloud_lane_kind` at `test_pipeline.py:1026`) | ⚠️ extend |
| BEUI-01 | `/pipeline/stats` renders N lane cards with `hx-swap-oob` on `#analyze-lanes`; rank order; word-labels present (WCAG) | integration (template render) | `uv run pytest -k analyze_lanes_render -x` (mirror `test_enrich_analyze_workspaces.py:396`) | ❌ Wave 0 |
| BEUI-02 | Force-local write round-trip: POST engages → row true → GET shell shows engaged pill (`aria-checked=false`, `FORCED LOCAL`) | integration | `uv run pytest -k force_local_toggle_roundtrip -x` | ❌ Wave 0 |
| BEUI-02 | Drain `stage_cloud_window` is a clean no-op when forced (mirror `test_backfill_disabled_when_cloud_local` at `test_pipeline.py:882`) | unit | `uv run pytest tests/analyze/core/test_staging_cron.py -k forced_local -x` | ⚠️ extend |
| BEUI-02 | Duration router routes long files LOCAL (not held) when forced (A4) | unit | `uv run pytest -k route_forced_local_no_hold -x` | ❌ Wave 0 |
| BEUI-02 | Force-local read degrades to False on DB error (poll never 500s) | unit | `uv run pytest -k route_control_degrades -x` | ❌ Wave 0 |
| BEUI-03 | Docs coverage: `docs/runbook.md` exists + covers toggle/lanes/spillover/`_FILE`; `configuration.md` has `backends:` schema | docs-drift guard | `just docs-drift` (`justfile:97`) — extend if it tracks new docs | ⚠️ verify guard scope |
| BEUI-03 (D-13) | Startup deprecation log when legacy `PHAZE_CLOUD_TARGET` set — ONLY IF A1 resolved to code-touch | unit | `uv run pytest -k cloud_target_deprecation_warns -x` | ❌ Wave 0 (conditional on A1) |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/shared/routers/test_pipeline.py tests/shared/services/test_lane_snapshot.py -x` + `uv run ruff check . && uv run mypy .`
- **Per wave merge:** `uv run pytest` (full suite) + `pre-commit run --all-files`
- **Phase gate:** full suite green + `scripts/coverage_floor.py` per-module ≥90 (`FLOOR = 90.0`) AND project `fail_under = 95` (`pyproject.toml:73`) before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/shared/services/test_lane_snapshot.py` — snapshot shape, rank order, `[]` degrade, per-backend admission, probe-timeout isolation (BEUI-01)
- [ ] `tests/shared/routers/test_routing.py` (or extend `test_pipeline.py`) — force-local write round-trip, degrade, drain/router gate (BEUI-02)
- [ ] Extend `tests/shared/routers/test_pipeline.py:1026` — assert new `lanes` context key seeded in BOTH builders; update/replace the `cloud_lane_kind` assertions the N-lane grid retires
- [ ] Extend `tests/analyze/core/test_staging_cron.py` — forced-local drain no-op (mirror the `cloud_target == "local"` no-op test at `test_staging_cron.py:335`)
- [ ] Template-render test for `#analyze-lanes` OOB + WCAG word-labels (mirror `test_enrich_analyze_workspaces.py:396` which already asserts the three partials render with no `cloud_target` string)
- [ ] Verify `just docs-drift` guard scope covers `runbook.md` (BEUI-03); extend the traceability guard if it only tracks REQUIREMENTS/ROADMAP
- [ ] Migration test for `031_add_route_control` (mirror the migration-test convention; `conftest.py:147` auto-marks `test_migrations` paths as DB tests)

## Security Domain

> `security_enforcement` not explicitly false in config → included. This is a low-surface presentation phase; the relevant ASVS categories are input validation on the ONE new write endpoint and injection-safety on the new read.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | Same reverse-proxy internal-realm auth as all `/pipeline/*` + `/saq` (no app-layer auth added — matches T-37-04 precedent, `pipeline_stages.py:26`) |
| V3 Session Management | no | Stateless; no new session surface |
| V4 Access Control | no | No new privilege boundary; the toggle sits behind the same internal realm |
| V5 Input Validation | **yes** | The force-local POST takes at most a boolean form field — validate/coerce to bool; no free-text. The snapshot renders ints + registry-declared `id`/`kind` (operator-declared, not user free-text). Autoescape on. |
| V6 Cryptography | no | No secrets handled in code paths here. Docs (D-11) describe `_FILE` secrets but never render them; T-68-04 secret-hygiene (log only `{id,kind,rank,cap}`, `backends.py:36`) — the lane snapshot must log/render ONLY `{id,kind,rank,cap,counts}`, NEVER a `SecretStr`/kube token/S3 key. |

### Known Threat Patterns for FastAPI + Jinja + control-row
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Template-path injection | Tampering | N/A — no `stage`-like param spliced into a template path here (the lanes/pill templates are static includes; `shell.py:69` STAGE_PARTIALS precedent) |
| Secret leakage into the lane card/log | Information disclosure | Render/log only `{id,kind,rank,cap,in_flight,available,admission-counts}`; never `config.*_file`/token/SecretStr (T-68-04, `backends.py:36`) |
| XSS via lane `id`/`kind` | Tampering/XSS | Jinja autoescape (registry ids are operator-declared; still autoescaped). If any value reaches an Alpine JS context, use `|tojson` not `|e` (the Phase-60 `_diff_row.html` XSS lesson from MEMORY) |
| Poll DoS via a hung Kueue probe | Denial of Service | D-02 bounded `asyncio.wait_for` per probe (Pattern 2) — a hung cluster can't stall the shared poll |
| Force-local as a stall lever | DoS (mitigated) | The toggle only reverts to LOCAL (the safe direction — no work lost); reversible; behind internal auth. No new attack surface beyond the existing pause control. |
| Priority/flag tamper | Tampering | Boolean coercion on the write; DB default `false`; mirror the `pipeline_stage_control` CHECK-constraint discipline if a bounded value is added |

## Sources

### Primary (HIGH confidence — read directly from the repo this session)
- `src/phaze/services/backends.py` — `Backend` protocol, `_BaseBackend.in_flight_count` (:164), `is_available` per impl (Local :188 / Compute :244 / Kueue :325), `resolve_backends` (:442), `resolved_non_local_kind` (:468, transitional)
- `src/phaze/services/backend_selection.py` — pure `select_backend` (:80), `BackendSlot` (:58)
- `src/phaze/routers/pipeline.py` — `build_dashboard_context` (:455), `pipeline_stats_partial` (:598), `_route_discovered_by_duration` (:278, `is_long` gate :335), `cloud_lane_kind` seed (:576)
- `src/phaze/services/pipeline.py` — `_safe_count` (:275), `_DEFAULT_CONTROLS`/`get_stage_controls` (:413/:416), `get_cloud_phase_counts` (:1165), `get_inadmissible_count` (:1122), `get_localqueue_unreachable` (:1145), `count_active_agents` (:705)
- `src/phaze/models/pipeline_stage_control.py` — the control-model template (D-09)
- `src/phaze/routers/pipeline_stages.py` + `src/phaze/services/stage_control.py` — the thin write-endpoint pattern (D-10)
- `src/phaze/tasks/release_awaiting_cloud.py` — drain `stage_cloud_window` `cloud_enabled` early no-op (:110-113), snapshot loop (:129-155), per-backend isolation (:143-148)
- `src/phaze/templates/pipeline/partials/{analyze_workspace,_lane_card,admission_state_card,stats_bar,_workspace_scaffold,_workspace_poll_seeds}.html` — the grid, card, OOB pattern
- `src/phaze/templates/shell/partials/header.html` + `src/phaze/routers/shell.py` — header pill placement + global shell context
- `src/phaze/config.py` (:87 `extra="ignore"`, :360-470 registry loading) + `src/phaze/config_backends.py` — no `cloud_target`, `_FILE` secrets, discriminated union
- `src/phaze/services/kube_staging.py` (:274 `get_local_queue`, :121 `_api`) — the D-02 kr8s latency source
- `tests/conftest.py` (:32 settings isolation, :164 `backends_toml_env`, :205 `session`, :213 `client`), `tests/shared/routers/test_pipeline.py:1026` — test patterns
- `pyproject.toml` (deps, `fail_under=95`) + `scripts/coverage_floor.py` (`FLOOR=90.0`) + `justfile:97` (`docs-drift`)
- `docs/configuration.md:123` — "`cloud_target` … removed with no shim" (A1 evidence)

### Secondary (MEDIUM)
- MEMORY entries — CI per-bucket isolation gotcha, `_diff_row.html` XSS lesson, coverage-floor 90/95 history

### Tertiary (LOW)
- None — every claim is repo-cited.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new deps; all versions read from `pyproject.toml`/`uv.lock`
- Architecture (snapshot/OOB/control-row patterns): HIGH — each mechanic cited to an existing, tested in-repo precedent
- Pitfalls: HIGH — Pitfall 1 (the `cloud_target` shim gap) is directly verified by grep + `configuration.md:123`; the rest by code
- Security: HIGH — low surface, controls mirror shipped T-37/T-68 precedents

**Research date:** 2026-07-04
**Valid until:** 2026-08-03 (30 days — stable internal codebase; only risk is another phase touching `pipeline.py`/`backends.py` before execution)
