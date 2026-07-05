# Phase 71: Deployment, Config, Docs & N-Lane UI - Pattern Map

**Mapped:** 2026-07-04
**Files analyzed:** 20 (create/modify) + 5 test targets
**Analogs found:** 20 / 20 (this is a presentation/ops phase over EXISTING code — every analog is in-repo and already cited by file:line in 71-RESEARCH.md)

> **Reading guide for the planner:** every "new" capability here is a thin recombination of a shipped,
> tested primitive. The risk is drifting from an existing contract, not building something novel.
> Mirror the analog VERBATIM; the excerpts below are the exact code to copy from.

---

## File Classification

| New/Modified File | New? | Role | Data Flow | Closest Analog | Match |
|-------------------|------|------|-----------|----------------|-------|
| `src/phaze/services/backends.py` (add `get_backend_lane_snapshot` + `_probe_availability` + `_admission_by_backend_id`) | mod | service | read / batch-fan-out | `services/pipeline.py` `get_cloud_phase_counts` (:1165) + `_safe_count` (:275) + `_BaseBackend.in_flight_count` (backends.py:164) | exact |
| `src/phaze/models/route_control.py` | new | model | — | `models/pipeline_stage_control.py` | exact |
| `src/phaze/services/route_control.py` (or fold into an existing service) | new | service | read (degrade-safe) | `services/pipeline.py` `get_stage_controls` (:416) | exact |
| `src/phaze/routers/routing.py` (or add to `routers/pipeline_stages.py`) | new | router/controller | request-response (thin write) | `routers/pipeline_stages.py` `pause`/`resume` (:103-128) | exact |
| `src/phaze/routers/pipeline.py` (seed `lanes`+`force_local`; duration-router gate) | mod | router | request-response | its own `build_dashboard_context` (:455) / `pipeline_stats_partial` (:599) / `_route_discovered_by_duration` (:278) | exact |
| `src/phaze/routers/shell.py` (seed `force_local` into base shell ctx) | mod | router | request-response | `shell.py` `_render_stage` (:144, `oob_counts=False` at :164) | exact |
| `src/phaze/tasks/release_awaiting_cloud.py` (drain force-local gate) | mod | task/cron | event-driven | its own `cloud_enabled` early no-op (:110-113) | exact |
| `alembic/versions/031_add_route_control.py` | new | migration | — | `alembic/versions/020_add_pipeline_stage_control.py` | exact |
| `templates/pipeline/partials/_lane_card.html` (extend: rank + `{in_flight}/{cap}` + admission caption) | mod | component | SSR | itself (Phase-58 card) | exact |
| `templates/pipeline/partials/_analyze_lanes.html` | new | component | SSR + OOB swap | `templates/pipeline/partials/admission_state_card.html` (`{% if oob %}hx-swap-oob`) | exact |
| `templates/pipeline/partials/analyze_workspace.html` (replace 3× include with loop) | mod | component | SSR | itself (:41-82) | exact |
| `templates/pipeline/partials/stats_bar.html` (OOB include of `_analyze_lanes`) | mod | component | OOB swap | itself (:73-103, the 6 cloud-card OOB includes) | exact |
| `templates/shell/partials/header.html` (add force-local pill) | mod | component | SSR | itself (the Agents pill, :46-49) | exact |
| `templates/shell/partials/_force_local_pill.html` | new | component | SSR (write-response swap) | header Agents pill + `admission_state_card` oob idiom | role-match |
| `docs/configuration.md` (update: `backends:` schema + `_FILE` + `cloud_target` REMOVED note) | mod | docs | — | itself (:89 backend registry, :121-131 the stale cloud-burst section to fix) | exact |
| `docs/runbook.md` | new | docs | — | `docs/cloud-burst.md` / `docs/k8s-burst.md` (prose/structure) | role-match |
| `docs/cloud-burst.md`, `docs/k8s-burst.md` (pointers) | mod | docs | — | themselves | exact |
| `tests/shared/services/test_lane_snapshot.py` | new | test | — | `tests/shared/routers/test_pipeline.py:1026` + degrade tests | role-match |
| `tests/shared/routers/test_routing.py` (or extend test_pipeline) | new | test | — | `test_pipeline.py:882` (`test_backfill_disabled_when_cloud_local`) | role-match |
| `tests/analyze/core/test_staging_cron.py` (extend: forced-local no-op) | mod | test | — | itself (:335 cloud-local no-op test) | exact |

---

## Shared Patterns

### SP-1 — Never-500 degrade idiom (`_safe_count` / SAVEPOINT)
**Source:** `src/phaze/services/pipeline.py:275-292` (`_safe_count`), `:416-446` (`get_stage_controls`)
**Apply to:** `get_backend_lane_snapshot` (`[]` on error), `_admission_by_backend_id`, `get_route_control` (`False` on error). The whole `/pipeline/stats` 5s poll depends on this contract — the new reads MUST NOT raise into it.
```python
# _safe_count — the canonical never-500 shape (pipeline.py:275)
async def _safe_count(session: AsyncSession, stmt: Select[Any], *, node: str) -> int:
    try:
        return int((await session.execute(stmt)).scalar() or 0)
    except Exception:
        logger.warning("stage_progress_degraded", node=node, exc_info=True)
        try:
            await session.rollback()          # clear the aborted txn so later COUNTs aren't poisoned
        except Exception:
            logger.warning("stage_progress_rollback_failed", node=node, exc_info=True)
        return 0
```
The guarded double-rollback (`try/except` around `session.rollback()`) is load-bearing — a failed rollback must not mask the original error. Copy it verbatim into every new degrade path.

### SP-2 — Seeded-identically dual-context discipline (WORK-05/R-2, D-04)
**Source:** `routers/pipeline.py` `build_dashboard_context` (:455, returns at :550) and `pipeline_stats_partial` (:599, context at :652). Every cloud count (`inadmissible_count`, `cloud_phase_counts`, `localqueue_unreachable`) is seeded in BOTH with identical keys.
**Apply to:** add `"lanes": await get_backend_lane_snapshot(session)` to BOTH builders — never one, never a second endpoint.

### SP-3 — Whole-partial OOB swap on the existing poll
**Source:** `templates/pipeline/partials/admission_state_card.html:25-27` (the `{% if oob %}hx-swap-oob="true"{% endif %}` on the `<section id>`), included initial (no oob) by `analyze_workspace.html:88` and re-pushed with `oob=True` by `stats_bar.html:103`.
**Apply to:** the new `_analyze_lanes.html` grid on `#analyze-lanes`.

### SP-4 — Control-table plane (model + read helper + thin endpoint)
**Source:** `models/pipeline_stage_control.py` + `services/stage_control.py` + `routers/pipeline_stages.py` + migration `020`. The whole D-08/D-09/D-10 stack mirrors this quartet.

### SP-5 — Secret hygiene (T-68-04)
**Source:** the drain snapshot logs `backend_id` ONLY, never a `KubeConfig`/`SecretStr`/token (`release_awaiting_cloud.py:141-147`).
**Apply to:** the lane snapshot + probe error logs and the rendered card — emit ONLY `{id, kind, rank, cap, in_flight, available, admission-counts}`. If any registry value reaches an Alpine JS context, use `|tojson` not `|e` (the Phase-60 `_diff_row.html` XSS lesson).

---

## Pattern Assignments

### `src/phaze/services/backends.py` — `get_backend_lane_snapshot` (service, read/batch-fan-out) — BEUI-01 / D-01,D-02,D-03,D-06

**Substrate 1 — per-backend in-flight COUNT** (`backends.py:164-176`, copy the semantics exactly so UI and scheduler never drift):
```python
async def in_flight_count(self, session: AsyncSession) -> int:
    """COUNT(cloud_job WHERE backend_id == self.id AND status IN {UPLOADING,UPLOADED,SUBMITTED,RUNNING})."""
    return int(
        (await session.execute(
            select(func.count(CloudJob.id)).where(
                CloudJob.backend_id == self.id,
                CloudJob.status.in_([status.value for status in IN_FLIGHT]),
            )
        )).scalar() or 0
    )
```
`LocalBackend.in_flight_count` is hard-0 (`backends.py:192`) — a local lane card always shows `0/{cap}` for in-flight.

**Substrate 2 — registry resolution** (`backends.py:442-465`): `resolve_backends(settings)` returns `list[Backend]`, one impl per registry entry, N non-local supported. Each carries `id`/`rank`/`cap` (`Backend` protocol `backends.py:120-122`).

**D-06 ordering — server-side, template loops verbatim:** `lanes.sort(key=lambda l: (l["rank"], l["id"]))` (lowest rank first, tie-broken by id). Do NOT sort in Jinja.

**Degrade contract:** wrap the whole snapshot per SP-1; return `[]` on any top-level failure. Structure (from RESEARCH §Pattern 1):
```python
async def get_backend_lane_snapshot(session: AsyncSession) -> list[dict[str, Any]]:
    try:
        backends = resolve_backends(cast("ControlSettings", get_settings()))
        admission = await _admission_by_backend_id(session)       # D-03, GROUP BY backend_id
        availability = await _probe_availability(session, backends)  # D-02, bounded concurrent
        lanes = []
        for be in backends:
            lanes.append({
                "id": be.id, "kind": _kind_of(be), "rank": be.rank, "cap": be.cap,
                "in_flight": await be.in_flight_count(session),
                "available": availability.get(be.id, False),
                **admission.get(be.id, _ZERO_ADMISSION),
            })
        lanes.sort(key=lambda l: (l["rank"], l["id"]))
        return lanes
    except Exception:
        logger.warning("backend_lane_snapshot_degraded", exc_info=True)
        try: await session.rollback()
        except Exception: logger.warning("lane_snapshot_rollback_failed", exc_info=True)
        return []
```
`_kind_of(be)`: derive from `isinstance` (`LocalBackend`→`"local"`, `ComputeAgentBackend`→`"compute"`, `KueueBackend`→`"kueue"`) — mirrors `resolve_backends`'s own dispatch at `backends.py:458-463`. This RETIRES the `cloud_lane_kind` context key (see "Retirement" below).

---

### `src/phaze/services/backends.py` — `_probe_availability` (D-02 bounded concurrent probes)

**Per-impl probe cost (verified — drives the D-02 short-circuit + session-safety reasoning):**
- `LocalBackend.is_available` (`backends.py:188-190`): unconditionally `True`, no I/O → **short-circuit, never penalize**.
- `ComputeAgentBackend.is_available` (`backends.py:244-254`): a single `select_active_agent(session, kind="compute")` DB query → the ONLY probe that touches the session; there is ≤1 compute backend (D-05 invariant) → no concurrent-session contention.
- `KueueBackend.is_available` (`backends.py:325-339`): a LIVE kr8s API call (`kube_staging.get_local_queue(self._kube())`) to a remote cluster; **ignores its `session` arg** (`# noqa: ARG002`). This is the latency source that MUST be bounded.

**Isolation idiom — mirror the drain's per-backend try/except** (`release_awaiting_cloud.py:143-149`, which treats a raising/timing-out backend as unavailable=0-slots and continues) **+ the `asyncio.wait_for` precedent** (`tasks/s3_upload.py:126`, `tasks/push.py:184`) **+ the `asyncio.gather` fan-out** (`tasks/discogs.py:66`):
```python
_PROBE_TIMEOUT_SEC = 1.5   # D-02/A2: planner confirms; well under the 5s poll, tolerant of a slow-healthy kr8s RTT
async def _probe_one(session, be) -> tuple[str, bool]:
    if isinstance(be, LocalBackend):        # D-02 short-circuit: no network dep
        return be.id, True
    try:
        return be.id, await asyncio.wait_for(be.is_available(session), _PROBE_TIMEOUT_SEC)
    except Exception:                        # TimeoutError OR any raise -> offline THIS poll only
        logger.info("lane probe failed/timed out -> offline", backend_id=be.id)
        return be.id, False
async def _probe_availability(session, backends) -> dict[str, bool]:
    return dict(await asyncio.gather(*(_probe_one(session, be) for be in backends)))
```
**Session-safety (Pitfall 2):** only the lone compute probe uses the session; Kueue probes use their own kr8s client; local is short-circuited → no concurrent-AsyncSession use. Confirm the ≤1-compute invariant holds in the plan.

---

### `src/phaze/services/backends.py` — `_admission_by_backend_id` (D-03 per-lane attribution)

**Analog to generalize:** `get_cloud_phase_counts` (`pipeline.py:1165-1199`) does four global `_safe_count` COUNTs of `cloud_job.cloud_phase`; `get_inadmissible_count` (`pipeline.py:1122-1142`) counts `cloud_job.inadmissible.is_(True)` scoped to in-flight status. **Generalize both to GROUP BY `backend_id`** (now possible because Phase 70 made reconcile per-`backend_id`; `cloud_job.backend_id` = migration 029).
```python
# global inadmissible predicate to make per-backend (pipeline.py:1137-1140):
select(func.count(CloudJob.id)).where(
    CloudJob.inadmissible.is_(True),
    CloudJob.status.in_([CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value]),
)
```
**Attribution semantics:** `cloud_phase` is NULL for local/compute rows (`pipeline.py:1175`), so a GROUP BY naturally attributes counts only to Kueue lanes. `quota_wait` = `cloud_phase == QUEUED_BEHIND_QUOTA`; per-lane `inadmissible` = the above predicate filtered by `backend_id`.
**A3 (recommendation):** derive per-lane `offline` from the D-02 live probe, NOT the global `phaze:k8s:localqueue_unreachable` Redis flag (`get_localqueue_unreachable`, `pipeline.py:1145` — a single cross-process boolean, NOT per-`backend_id`). Keep that Redis flag ONLY in the global roll-up (D-07).

---

### `src/phaze/models/route_control.py` (model) — BEUI-02 / D-09

**Analog:** `models/pipeline_stage_control.py` (copy verbatim, single-row shape). A1 shape recommendation: `route_control(id text PK default 'global', force_local bool not null default false)` + `TimestampMixin`.
```python
# from models/pipeline_stage_control.py — the exact model template
class PipelineStageControl(TimestampMixin, Base):
    __tablename__ = "pipeline_stage_control"
    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("50"))
```
New model: PK `id: Mapped[str] = mapped_column(String(...), primary_key=True)` (default `'global'`), `force_local: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))`, inherit `TimestampMixin` (do NOT redeclare `created_at`/`updated_at`). Register it in `models/__init__.py` (the router imports `from phaze.models import PipelineStageControl`).

---

### `alembic/versions/031_add_route_control.py` (migration) — D-09

**Analog:** `alembic/versions/020_add_pipeline_stage_control.py` (verbatim structure). `down_revision = "030"` (last migration is `030_add_cloud_job_staging_bucket.py`). Additive-only: `create_table` + seed ONE default-`false` `'global'` row with bound params (no interpolation — T-37-01).
```python
# from 020 — create_table + bound-param seed shape to copy
def upgrade() -> None:
    op.create_table(
        "route_control",
        sa.Column("id", sa.String(...), nullable=False),
        sa.Column("force_local", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_control")),
    )
    bind = op.get_bind()
    bind.execute(sa.text("INSERT INTO route_control (id, force_local, created_at, updated_at) VALUES (:id, false, NOW(), NOW())"), {"id": "global"})
def downgrade() -> None:
    op.drop_table("route_control")
```
Migration test: mirror the migration-test convention (`conftest.py:147` auto-marks `test_migrations` paths as DB tests).

---

### `src/phaze/services/route_control.py` — `get_route_control` (service, degrade-safe read) — D-08,D-09

**Analog:** `get_stage_controls` (`pipeline.py:416-446`, SP-1). Degrade to `False` (cloud-enabled) on any error/absent row so the hot poll + the routing gate never 500.
```python
# mirror get_stage_controls degrade (pipeline.py:433-446)
async def get_route_control(session: AsyncSession) -> bool:
    """True iff routing is forced-local. Degrades to False on any error."""
    try:
        row = await session.get(RouteControl, "global")
        return bool(row.force_local) if row is not None else False
    except Exception:
        logger.warning("route_control_degraded", exc_info=True)
        try: await session.rollback()
        except Exception: logger.warning("route_control_rollback_failed", exc_info=True)
        return False
```

---

### `src/phaze/routers/routing.py` (or `pipeline_stages.py`) — thin force-local write — BEUI-02 / D-10

**Analog:** `routers/pipeline_stages.py` `pause`/`resume` (:103-128) + `_load_control_row` (:59-74).
```python
# pause/resume — the thin-endpoint shape to mirror (pipeline_stages.py:103-114)
@router.post("/pipeline/stages/{stage}/pause")
async def pause(stage: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    _validate_stage(stage)
    row = await _load_control_row(session, stage)
    row.paused = True
    await pause_stage(session, stage)
    await session.commit()
    return _response(row)
```
```python
# _load_control_row — the get-or-defensively-create idiom (pipeline_stages.py:70-74)
row = await session.get(PipelineStageControl, stage)
if row is None:
    row = PipelineStageControl(stage=stage, paused=False, priority=50)
    session.add(row)
```
For force-local: `POST /pipeline/routing/force-local`, body `engage: Annotated[bool, Form()]` (form-encoded to match HTMX — precedent `pipeline_stages.py:85`; V5 boolean coercion, no free-text). Set `row.force_local = engage`, `await session.commit()`, return the pill partial (in-place swap) + an OOB polite-`aria-live` toast (UI-SPEC copy: "Routing forced to LOCAL — cloud & Kueue backends bypassed." / "Cloud routing restored — backends dispatch by rank."). Same T-37-04 note: no app-layer auth, sits behind the internal realm.

---

### `src/phaze/tasks/release_awaiting_cloud.py` — drain force-local gate — BEUI-02 / D-08 (primary gate)

**Analog:** the `cloud_enabled` early no-op at `:110-113` (BEFORE the session/advisory-lock). Add the force-local read JUST AFTER the session opens (`:123`), before the advisory lock — same `{"staged":0,"skipped":0}` no-op:
```python
# insert after: async with ctx["async_session"]() as session:   (release_awaiting_cloud.py:123)
if await get_route_control(session):        # BEUI-02 force-local -> behave exactly like cloud_enabled=False
    return {"staged": 0, "skipped": 0}
await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})
```
This stops ALL new cloud/Kueue dispatch instantly. Held `AWAITING_CLOUD` files stay held while forced (drain no-op) — **document this in the runbook (A4/Open-Q2).**

---

### `src/phaze/routers/pipeline.py` — duration-router force-local gate — BEUI-02 / D-08 (secondary gate, A4)

**Analog:** `_route_discovered_by_duration` `is_long` gate (`:335`); the 3 callers pass `settings.cloud_enabled` (`:391`, `:698`, `:791`).
```python
# the effective-flag change (pipeline.py:335):
is_long = cloud_enabled and duration is not None and duration >= threshold_sec
# make the callers pass:  cloud_enabled AND NOT await get_route_control(session)
```
So NEW long files route local immediately (not HELD in `AWAITING_CLOUD`) while forced. Thread the `get_route_control` read into the caller (`trigger_analysis` / `trigger_analysis_ui` — they already have a session). **Do NOT** inject the flag into pure `select_backend` (`backend_selection.py:80-119`) — gate only at the two callers that already read `cloud_enabled`. **A4 recommendation: gate both** (drain + router).

---

### `src/phaze/routers/pipeline.py` + `shell.py` — seed `lanes` + `force_local` — D-04, D-10

- **`lanes`** (D-04): add `"lanes": await get_backend_lane_snapshot(session)` to BOTH `build_dashboard_context` (return dict, `pipeline.py:550`) AND `pipeline_stats_partial` (context dict, `pipeline.py:652`). Follow the exact seeded-identically comment discipline the cloud counts use (`pipeline.py:534` vs `:644`).
- **`force_local` pill seed** (D-10): the header renders on EVERY `/s/{stage}` but `build_dashboard_context` runs ONLY for Analyze (`shell.py:166`). So read `get_route_control(session)` UNCONDITIONALLY in `shell.py` `_render_stage` (:144) and add `force_local` to the base shell context, next to the `oob_counts=False` seed (:164). Do not rely on the Analyze-only dashboard context.

---

### `templates/pipeline/partials/_lane_card.html` (component, extend) — BEUI-01 / D-06, D-03

**Current card contract** (`_lane_card.html:31-52`): a bordered card (`rounded-xl border {{lane_border}} p-4`, `opacity-60` when down), title row (glyph + title + capacity numeral `font-mono text-sm font-medium {{lane_color}}`), an `mt-3 h-1.5` capacity bar, and an `mt-2 text-xs` sub-label that swaps to the offline/not-configured word. **Box model FROZEN** (D-05 / UI-SPEC §Spacing: `p-4`/`mt-3`/`mt-2`/`h-1.5`/`rounded-xl` unchanged — the only non-standard `mt-3=12px` is inherited, not authored).
**Three NEW data points to add** (UI-SPEC §BEUI-01 "New per-card data"):
1. `RANK {n}` micro-label — Jura 500 `text-xs uppercase tracking-wider text-gray-400 dark:text-gray-500`, inline after the title (muted, must not compete with title/numeral).
2. capacity numeral becomes `{{ in_flight }}/{{ cap }}` (Inter 500 mono, lane color); bar fill % = `in_flight/cap` clamped 0–100.
3. per-lane admission caption (kueue only): `{{ quota_wait }} waiting · {{ inadmissible }} inadmissible`; the `inadmissible` segment goes amber + `role="alert"` when `> 0` (word-labelled, never hue-only).
**States:** keep the word+glyph contract (`available` / `offline`); **retire `not configured`** (every rendered lane is registry-derived). Lane identity color derives from `kind` (UI-SPEC §Color: local=emerald/🖥️, compute=blue/☁️, kueue=amber/⎈; kueue keeps `border-amber-500/30`). Stays pure presentation — no `hx-trigger`/`setInterval`.

---

### `templates/pipeline/partials/_analyze_lanes.html` (new, OOB grid) — BEUI-01 / D-04, D-05

**Analog:** `admission_state_card.html:25-27` (the `{% if oob %}hx-swap-oob="true"{% endif %}` gate on the `<section id>`).
```html
<!-- admission_state_card.html:25-27 — the OOB gate to mirror on #analyze-lanes -->
<section id="admission-state-card"
         aria-labelledby="admission-state-heading"
         {% if oob %}hx-swap-oob="true"{% endif %}
         class="...">
```
Extract `#analyze-lanes` into this partial: the wrapping grid container + `{% for lane in lanes %}{% include "pipeline/partials/_lane_card.html" %}{% endfor %}`. Set the card's context vars from `lane.*` inside the loop (or refactor the card to read a `lane` dict directly — planner discretion). **Grid class (D-05, UI-SPEC locked recommendation):** replace `grid grid-cols-3` with `grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4 p-6`. **Empty/degrade (`lanes == []`):** render a single full-width muted panel (`rounded-xl border phaze-border p-4`) with heading "Lane status unavailable" + body copy — never a 500, never collapse layout (self-heals next poll).

---

### `templates/pipeline/partials/analyze_workspace.html` (component, modify) — BEUI-01 / D-05, D-07

Replace the hand-written 3× `{% include _lane_card.html %}` block (`:41-82`) with `{% include "pipeline/partials/_analyze_lanes.html" %}` (no `oob` → initial render, matching the `oob_counts=False` initial-render discipline). **Keep the 6 global cloud-state cards VERBATIM below** (`:87-94`, D-07 lean = roll-up; their ids + OOB swaps stay byte-stable). Keep the per-file table (`:101-148`) unchanged.

---

### `templates/pipeline/partials/stats_bar.html` (component, modify) — BEUI-01 / D-04

**Analog:** the 6 cloud-card OOB includes at `:73-103`:
```html
{% with oob = True %}{% include "pipeline/partials/admission_state_card.html" %}{% endwith %}
```
Add (inside the `{% if oob_counts %}` block): `{% with oob = True %}{% include "pipeline/partials/_analyze_lanes.html" %}{% endwith %}`. `#analyze-lanes` exists on the Analyze render so the swap lands; absent on other stages → harmless no-op (same as the cloud cards).

---

### `templates/shell/partials/header.html` (component, modify) + `_force_local_pill.html` (new) — BEUI-02 / D-10

**Analog:** the Agents pill anatomy (`header.html:46-49`): `<a>`/`<button>` `font-jura tracking-wider px-3 h-8 flex items-center rounded-lg bg-white dark:bg-phaze-panel border ... focus-visible:ring-2 focus-visible:ring-blue-500`. Place the force-local pill in the `ml-auto` cluster (`:41`), IMMEDIATELY LEFT of the Agents pill.
**Control:** a `<button role="switch" :aria-checked=...>` (UI-SPEC §BEUI-02): Normal → `CLOUD ROUTING` (neutral, `aria-checked=true`); Engaged → `FORCED LOCAL` (loud amber `bg-amber-500/15 border-amber-500/40 text-amber-700 dark:text-amber-300` + warning `<svg>` triangle, `aria-checked=false`). `hx-post="/pipeline/routing/force-local"`, no optimistic mutation (authoritative state from the write response — mirrors the Phase-38 `@htmx:after-request` pattern). Instant-on both directions, no confirm (reversible, R-4). `aria-label="Force all analysis routing to local (incident revert)"`. Initial `aria-checked` seeded from the `force_local` shell-context key (above). Factor the pill body into `_force_local_pill.html` so the write endpoint can return it for the in-place swap.

---

### Docs — BEUI-03 / D-11, D-12, D-13 (DOCS-ONLY — no runtime code)

- **`docs/configuration.md` (update):** the `## Backend registry` section already exists (`:89`). The `## Cloud-burst settings` block (`:121-131`) and the `### Cloud target` section (`:200-204`) are **internally contradictory** — the `:123` banner says `cloud_target` was "removed with no shim" but the tables/section below still describe `PHAZE_CLOUD_TARGET` as a live selector. **Reconcile per the A1 correction (D-12/D-13):** state `cloud_target` was **removed in Phase 67 — use `backends:`** (NOT "deprecated but still works"), show the trivial 1:1 `cloud_target`→`backends` equivalence, add `backends:` schema + per-backend `_FILE` secrets coverage. `extra="ignore"` silently drops a stale `PHAZE_CLOUD_TARGET` env var.
- **`docs/runbook.md` (new):** operator ops — the master toggle / incident revert procedure, how to read the N lanes (rank order = dispatch preference), spillover + **held-file behavior under force-local** (A4: already-held `AWAITING_CLOUD` files stay held while forced), per-backend `_FILE` secrets. Structure/tone from `docs/cloud-burst.md` / `docs/k8s-burst.md`.
- **`docs/cloud-burst.md` / `docs/k8s-burst.md` (pointers):** add a pointer to the unified `backends` model. **Do NOT reintroduce the "one shared bucket" framing** (superseded by REG-05).
- **D-13 is DROPPED (docs-only):** NO startup deprecation-log code. There is no `cloud_target` shim to warn about; the env var is already silently ignored (A1). Any plan task that says "edit the `cloud_target` shim in config.py" is WRONG — there is nothing to edit.
- **Guard:** verify `just docs-drift` (`justfile:97`) scope covers `runbook.md`; extend the traceability guard if it only tracks REQUIREMENTS/ROADMAP.

---

### Tests — Test-Map (from RESEARCH §Validation Architecture)

**Fixtures:** `client` (httpx `AsyncClient` + `ASGITransport`, `conftest.py:213`), `session` (fresh migrated DB, `conftest.py:205`). **Isolation gotcha (Pitfall 5):** `get_settings()` is `@lru_cache`; the autouse fixture clears it (`conftest.py:69`), and `backends_toml_env` clears before+after (`conftest.py:174`). New tests that set backends must `monkeypatch.setattr(settings, "backends", [...])` (`test_pipeline.py:1038`) or use `backends_toml_env` — **run new tests in isolation (`just test-bucket <bucket>`), not just the full suite.**

| File | New/Mod | Mirrors | Asserts |
|------|---------|---------|---------|
| `tests/shared/services/test_lane_snapshot.py` | new | degrade tests + `test_pipeline.py:1026` | snapshot shape `{id,kind,rank,cap,in_flight,available,+admission}`, rank-asc order, `[]` on DB error, per-`backend_id` admission GROUP BY (2 Kueue → distinct counts), probe-timeout isolation (one hung backend → that lane offline, others fine, request fast/200) |
| `tests/shared/routers/test_routing.py` | new | `pipeline_stages` tests + `test_pipeline.py:882` (`test_backfill_disabled_when_cloud_local`) | force-local write round-trip (POST engage → row true → shell shows `FORCED LOCAL`/`aria-checked=false`), `get_route_control` degrades to False on DB error, duration router routes long files LOCAL not held when forced (A4) |
| `tests/shared/routers/test_pipeline.py` (extend `:1026` `test_dashboard_context_binds_cloud_lane_kind`) | mod | itself | new `lanes` key seeded in BOTH builders; update/replace the retired `cloud_lane_kind` assertions |
| `tests/analyze/core/test_staging_cron.py` (extend) | mod | its cloud-local no-op test (`:335`) | forced-local drain is a clean `{"staged":0,"skipped":0}` no-op |
| template-render test for `#analyze-lanes` | new | `test_enrich_analyze_workspaces.py:396` | N lane cards render with `hx-swap-oob` on `#analyze-lanes`, rank order, WCAG word-labels present, no `cloud_target` string |

**Phase gate:** full suite green + `scripts/coverage_floor.py` per-module ≥90 AND project `fail_under=95` (`pyproject.toml:73`) before `/gsd:verify-work`.

---

## Retirement (State of the Art)

**`resolved_non_local_kind` / the `cloud_lane_kind` context key** (`backends.py:468`, seeded `pipeline.py:576`, marked `# TRANSITIONAL — Phase 68/71`): RETIRE the `cloud_lane_kind` key from `build_dashboard_context` + `analyze_workspace.html` — the D-01 `lanes` list replaces it. **Do NOT delete `resolved_non_local_kind` wholesale** — other single-kind callers (`agent_s3.report_uploaded`, backfill) still use it. Only remove the one context key. Update `test_pipeline.py:1026` (`test_dashboard_context_binds_cloud_lane_kind`) to assert the new `lanes` key.

---

## No Analog Found

None. Every file in this phase copies from an in-repo, tested precedent (cited above). The only NEW-code judgment calls are all reconciliations of an existing idiom to N-backend shape (GROUP BY `backend_id`, `list.sort`, the bounded `gather`), not novel mechanics.

---

## Anti-Patterns (do NOT do)

- Second poll loop / new read endpoint for lanes — ride `/pipeline/stats` (WORK-05/R-2/D-04).
- Per-lane `$store.pipeline` Alpine keys — N is dynamic; use whole-grid OOB (SP-3).
- Coupling the lane read to the drain's advisory-locked `BackendSlot` — build the independent read (D-01).
- Emitting `hx-swap-oob` on the initial full-page render — gate behind `oob`/`oob_counts` (Pitfall 4, `shell.py:164`).
- Injecting force-local into pure `select_backend` — gate at the callers (D-08).
- A third UI type weight / new spacing token — C3 two-weight contract (only inherited `mt-3` is non-standard, frozen).
- "Edit the `cloud_target` shim in config.py" — there is NO shim (A1); D-13 is docs-only.
- Reintroducing "one shared bucket" framing in docs — superseded by REG-05.
- Logging/rendering a `SecretStr`/kube token/S3 key — emit only `{id,kind,rank,cap,counts}` (SP-5, T-68-04).

---

## Metadata

**Analog search scope:** `src/phaze/{services,routers,models,tasks,templates}`, `alembic/versions`, `docs`, `tests` — all cited by RESEARCH file:line and read directly this session.
**Files read for excerpts:** `models/pipeline_stage_control.py`, `routers/pipeline_stages.py`, `services/stage_control.py`, `alembic/versions/020_add_pipeline_stage_control.py`, `services/backends.py` (:110-465), `services/pipeline.py` (:275-446, :1122-1199), `routers/pipeline.py` (:278-345, :455-668), `tasks/release_awaiting_cloud.py` (:104-158), `templates/pipeline/partials/{_lane_card,analyze_workspace,admission_state_card,stats_bar}.html`, `templates/shell/partials/header.html`, `docs/configuration.md` (structure).
**Pattern extraction date:** 2026-07-04
</content>
</invoke>
