# Phase 56: Deployment, runbook, config & docs - Pattern Map

**Mapped:** 2026-06-28
**Files analyzed:** 16 (4 code, 5 test, 7 docs/prose)
**Analogs found:** 16 / 16 (every file has a concrete in-repo analog)

> Ops-only phase. The net-new **code** surface is tiny: one kr8s GET, one cross-process
> Redis flag, one Jinja alert partial, one static note. Everything else is docs that mirror
> the v5.0 Phase 51 precedent. Nearly every file is a near-verbatim clone of an existing one —
> copy the analog, swap the content.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/services/kube_staging.py` (+`get_local_queue`) | service | request-response (kr8s GET) | `kube_staging.get_job` (same file, `:197-203`) | exact |
| `src/phaze/tasks/controller.py` (+probe wiring in `startup`) | task / startup hook | event-driven (boot) | ledger-backfill + recovery try/except in same `startup` (`:145-157`) | exact |
| `src/phaze/services/pipeline.py` (+`get_localqueue_unreachable`) | service | request-response (degrade-safe read) | `get_inadmissible_count` (same file, `:821-841`) + `_read_pipeline_counters` (`routers/pipeline.py:93-111`) | role+flow match |
| `src/phaze/routers/pipeline.py` (wire flag into 2 render paths) | router | request-response (SSR + 5s OOB) | `inadmissible_count` seeding at `:499` (first-load) + `:575` (OOB re-push) | exact |
| `src/phaze/templates/pipeline/partials/localqueue_card.html` (NEW) | component (Jinja partial) | event-driven (OOB swap) | `inadmissible_card.html` (whole file) | exact |
| `src/phaze/templates/admin/agents.html` (+static note) | component (Jinja) | static render | intro `<p>` at `agents.html:9-11` + neutral panel idiom | exact |
| (no code) DEAD-suppression invariant | n/a — structural | n/a | `agent_liveness.classify` (`:77-86`), `agent_worker._heartbeat_loop` import (`:67`) | structural proof |
| `tests/test_deployment/test_k8s_runbook.py` (NEW) | test | batch (YAML parse) | `tests/test_deployment/test_job_image.py` | role+flow match |
| `tests/kube_fakes.py` (+`fake_local_queue`) | test helper | n/a | `fake_job` / `fake_workload` (same file) | exact |
| `tests/test_services/test_kube_staging.py` (+`get_local_queue` cases) | test | request-response | `test_get_job_returns_status` (`:228`) + `kube_respx` fixture | exact |
| `tests/test_tasks/test_controller_startup_*.py` (NEW or extend) | test | event-driven | `test_controller_startup_banner.py` (whole file) | exact |
| `tests/test_routers/test_pipeline_localqueue.py` (NEW) | test | request-response | `test_routers/test_pipeline_inadmissible.py` (whole file) | exact |
| `tests/test_services/test_agent_liveness.py` (extend) | test | n/a | `test_classify_never_when_last_seen_at_is_none` (`:72`) | exact |
| `tests/test_task_split.py` (extend) | test | n/a (import boundary) | `test_job_runner_does_not_import_phaze_database` (`:496`) | exact |
| `docs/k8s-burst.md` (NEW) | docs (prose) | n/a | `docs/cloud-burst.md` (structure/tone) | structural |
| `.planning/phases/56-.../56-HOMELAB-CHANGE-PROMPT.md` (NEW) | docs (prose) | n/a | `51-HOMELAB-CHANGE-PROMPT.md` (headings) | structural |
| `docs/configuration.md`, `docs/deployment.md`, `docs/README.md` (edits) | docs (prose) | n/a | existing sections in each file | exact |

---

## Pattern Assignments

### `src/phaze/services/kube_staging.py` — add `get_local_queue()` (service, request-response)

**Analog:** `kube_staging.get_job` (`src/phaze/services/kube_staging.py:197-203`) — a near-exact clone.
Use the `get_workload_for` line for the `new_class(...)` Kueue-group idiom.

**The GET idiom to clone** (`get_job`, `:197-203`):
```python
async def get_job(name: str) -> Any:
    """Fetch the Job by name (its ``status`` carries succeeded/failed -- the terminal signals)."""
    cfg = _kube_config()
    api = await _api(cfg)
    job = Job({"metadata": {"name": name, "namespace": cfg.kube_namespace}}, api=api)
    await job.refresh()
    return job
```

**The Kueue-group `new_class` idiom to reuse** (`get_workload_for`, `:229`):
```python
workload_cls = new_class(kind="Workload", version=cfg.kube_workload_api_version, namespaced=True)
```

**New function (compose the two — RESEARCH §Net-New Code Design):**
```python
async def get_local_queue() -> Any:
    """GET the configured Kueue LocalQueue by name; refresh() raises NotFoundError on 404."""
    cfg = _kube_config()                 # reuses the kube_api_url/namespace/local_queue gate (:72-84)
    api = await _api(cfg)
    lq_cls = new_class(kind="LocalQueue", version=cfg.kube_workload_api_version, namespaced=True)
    lq = lq_cls({"metadata": {"name": cfg.kube_local_queue, "namespace": cfg.kube_namespace}}, api=api)
    await lq.refresh()                   # 404 -> kr8s.NotFoundError; transient -> other exc; caller treats BOTH as unreachable
    return lq
```

**Imports:** already present in this module — `import kr8s`, `from kr8s.asyncio.objects import Job, new_class` (`:25-27`). `LocalQueue` uses `new_class` (no new import).
**Error handling:** this service raises (`KubeStagingError` / `kr8s.NotFoundError`); the *caller* (controller startup) owns the non-fatal catch. Do NOT add a validator/fail-fast here (anti-pattern, RESEARCH).

---

### `src/phaze/tasks/controller.py` — wire the LocalQueue probe into `startup` (task, event-driven)

**Analog:** the ledger-backfill + recovery boot-resilience blocks in the SAME `startup(ctx)` (`:145-157`).
Each is gated work wrapped in its OWN broad `try/except` that logs and NEVER aborts boot.

**Boot-resilience pattern to clone** (`:153-157`):
```python
    try:
        result = await recover_orphaned_work(ctx)
        logger.info("phaze.controller startup recovery", detected_loss=result["detected_loss"], stages=result["stages"])
    except Exception:
        logger.exception("recover_orphaned_work on startup failed")
```

**New probe block (gate on `cloud_target == "k8s"`; D-05/D-06):**
```python
    # Phase 56 (KDEPLOY-04, D-05/D-06): live LocalQueue-reachability probe. Non-fatal — a transient
    # kube/mesh blip must NEVER take down Postgres/Redis/UI/local-analysis (distinct from the three
    # fail-fast config validators). Cross-process flag via ctx["redis"] (the dashboard reads it).
    if cfg.cloud_target == "k8s":
        try:
            await kube_staging.get_local_queue()
            await ctx["redis"].delete("phaze:k8s:localqueue_unreachable")   # reachable -> clear
        except Exception:
            logger.warning("phaze.controller startup: K8s LocalQueue unreachable; check PHAZE_KUBE_LOCAL_QUEUE / cluster connectivity")
            await ctx["redis"].set("phaze:k8s:localqueue_unreachable", "1")  # unreachable -> raise the flag
```

**Where:** after the recovery block (`:157`), inside `startup`. `ctx["redis"]` is already built at `:104`.
**Import:** add `from phaze.services import kube_staging` (module currently imports `kube_staging`-adjacent tasks but not the service — verify). `cfg = get_settings()` already at `:57`.
**Anti-pattern to avoid (RESEARCH):** an in-memory boolean — invisible across the controller↔api process boundary. Must be Redis.

---

### `src/phaze/services/pipeline.py` — add `get_localqueue_unreachable(redis)` (service, degrade-safe read)

**Analog:** `get_inadmissible_count` (`src/phaze/services/pipeline.py:821-841`) for the degrade-safe
discipline; `_read_pipeline_counters` (`routers/pipeline.py:93-111`) for the Redis-handle / missing-handle
degrade. NOTE this is a **Redis** read (boot flag), not a Postgres `_safe_count` — so model it on
`_read_pipeline_counters`, returning `False` (not 0) on any error/missing handle.

**Degrade-safe Redis read pattern to clone** (`_read_pipeline_counters`, `:106-111`):
```python
    try:
        redis = getattr(app_state, "redis", None)
        return await read_counters(redis)
    except Exception:
        logger.warning("pipeline_counters_degraded", exc_info=True)
        return {}
```

**Degrade-safe docstring discipline to mirror** (`get_inadmissible_count`, `:821-830`): name the dashboard
surface, state the silent-degrade contract, and the "never 500s the hot 5s poll" (T-54-10) rationale.

**New function:**
```python
async def get_localqueue_unreachable(redis: Any) -> bool:
    """Return True iff the controller flagged the K8s LocalQueue unreachable; degrade to False on any error.

    Drives the Phase 56 LocalQueue-unreachable alert (D-05). A missing app.state.redis (test client
    skips the lifespan) or any Redis hiccup degrades to False (silent/reachable) so the 5s
    /pipeline/stats poll NEVER 500s (T-54-10). The probe in controller.startup writes the key.
    """
    try:
        if redis is None:
            return False
        return bool(await redis.exists("phaze:k8s:localqueue_unreachable"))
    except Exception:
        logger.warning("localqueue_unreachable_read_degraded", exc_info=True)
        return False
```

---

### `src/phaze/routers/pipeline.py` — seed the flag into both render paths (router, SSR + OOB)

**Analog:** the `inadmissible_count` seeding — wire the new flag in the SAME two places, identically.

- **First-load render** (`:499`): `inadmissible_count = await get_inadmissible_count(session)` then added to context dict at `:526` (`"inadmissible_count": inadmissible_count`).
- **5s OOB re-push** (`:575`): `inadmissible_count = await get_inadmissible_count(session)` then context at `:598`.

**Pattern to mirror at both sites:**
```python
    # Phase 56 (KDEPLOY-04, D-05): degrade-safe LocalQueue-unreachable flag, seeded IDENTICALLY in
    # both the first-load render and the 5s OOB re-push (mirrors inadmissible_count). Reads the shared
    # app.state.redis cache handle; missing/error -> False (silent).
    localqueue_unreachable = await get_localqueue_unreachable(getattr(request.app.state, "redis", None))
    # ... add to the template context dict alongside "inadmissible_count":
    "localqueue_unreachable": localqueue_unreachable,
```
(Read the `app.state.redis` handle the same way `_read_pipeline_counters` does — `getattr(..., "redis", None)`. Confirm the request/app-state accessor available at each site.)

**Import:** add `get_localqueue_unreachable` to the existing `from phaze.services.pipeline import (...)` block (where `get_inadmissible_count` is imported, `:35`).
**OOB contract:** the new `localqueue_card.html` section must live OUTSIDE `#pipeline-stats` and be re-pushed `hx-swap-oob` on the 5s poll — exactly like `#inadmissible-card` (56-UI-SPEC Interaction Contract).

---

### `src/phaze/templates/pipeline/partials/localqueue_card.html` (NEW) — alert partial (component, OOB)

**Analog:** `src/phaze/templates/pipeline/partials/inadmissible_card.html` (whole file) — copy verbatim,
swap the flag name and the locked copy. Same amber box, same `role="alert"`, same empty-when-healthy
`<section>` carrier, same `{% if oob %}hx-swap-oob{% endif %}`.

**Structure to clone** (`inadmissible_card.html:19-33`):
```jinja
<section id="inadmissible-card"
         {% if oob %}hx-swap-oob="true"{% endif %}>
    {% if inadmissible_count %}
    <div role="alert"
         aria-labelledby="inadmissible-heading"
         class="border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950 rounded-lg p-4 space-y-1">
        <h2 id="inadmissible-heading" class="text-lg font-semibold text-amber-800 dark:text-amber-300">
            ⚠ K8s Jobs not admitting — check LocalQueue config
        </h2>
        <p class="text-sm text-amber-700 dark:text-amber-400">
            {{ inadmissible_count }} cloud job(s) Inadmissible — the Kueue LocalQueue/ClusterQueue may be misconfigured.
        </p>
    </div>
    {% endif %}
</section>
```

**New partial (locked copy from 56-UI-SPEC §Copywriting):** stable `id` (e.g. `localqueue-card`),
`{% if localqueue_unreachable %}`, heading `⚠ K8s LocalQueue unreachable`, body
`K8s LocalQueue unreachable — verify PHAZE_KUBE_LOCAL_QUEUE / cluster connectivity.`
Same amber classes — DO NOT use red (D-05 is warn+surface, app stays up).
Include it inline in `dashboard.html` on first load (next to `inadmissible_card.html`) and re-push as an OOB fragment from the same `stats_bar.html` / stats partial that re-pushes `inadmissible_card.html` (pass `oob=True`).

---

### `src/phaze/templates/admin/agents.html` (+static note) — ephemeral k8s-lane note (component, static)

**Analog:** the intro `<p>` at `agents.html:9-11` (tone) + the neutral-panel idiom (`border … rounded-lg p-4`,
`text-sm text-gray-*`) from `agents_table.html` (56-UI-SPEC Component Reuse Map).

**Tone/markup to match** (`agents.html:9-11`):
```jinja
    <p class="text-sm text-gray-500 dark:text-gray-400">
        Live status of every registered file-server agent. Refreshes every 5 seconds.
    </p>
```

**New static note (locked copy from 56-UI-SPEC; place after the intro `<p>`, before `agents_table.html`):**
neutral panel `border border-gray-200 dark:border-phaze-border rounded-lg p-4`, body
`text-sm text-gray-600 dark:text-gray-400`, optional `ℹ` glyph `text-blue-600 dark:text-blue-400`
(`aria-hidden="true"`). Heading (optional inline) `K8s burst lane`. Body:
`The Kubernetes burst lane runs as ephemeral, per-file Jobs — it does not register as a heartbeating agent here. Its live activity is visible as in-flight Kueue workloads on the pipeline dashboard.`
Fully static — no `hx-trigger`, no Alpine, no poll (56-UI-SPEC Interaction Contract).

---

### DEAD-pill suppression — STRUCTURAL, ZERO code (RESEARCH §Net-New Code Design)

**No code to write.** The invariant holds by construction (this is a *verification target*, not a build):
- `classify()` returns `never` (not `dead`) when `last_seen_at IS NULL` — `agent_liveness.py:79`:
  ```python
      if agent.last_seen_at is None:
          return "never"
  ```
  `dead` requires `last_seen_at` set AND `delta >= 300s` (`:81-86`).
- `last_seen_at` is set ONLY by the heartbeat loop, which lives in the long-lived SAQ **agent worker**
  (`agent_worker.py:67` imports `_heartbeat_loop`; `:181` runs it as a background task). The k8s one-shot
  pod runs `job_runner.py` — which never imports/calls `_heartbeat_loop`.

**Anti-pattern to avoid (RESEARCH Pitfall 4):** a `kind=compute` filter to hide the k8s row — it would
also hide the v5.0 A1 agent (also `kind=compute`, which DOES heartbeat and SHOULD show). Accept the
`never` pill + the static note (the recommended option (a)).

---

### `tests/kube_fakes.py` — add `fake_local_queue(...)` (test helper)

**Analog:** `fake_job` / `fake_workload` in the SAME file (`:21-50`) — `SimpleNamespace` factories mirroring
the kr8s `.status`(dict)/`.metadata`(attr) shape.

**Pattern to clone** (`fake_job`, `:39-50`):
```python
def fake_job(succeeded: int = 0, failed: int = 0, suspend: bool = False, uid: str = "uid-1", name: str = "phaze-analyze-fake") -> SimpleNamespace:
    return SimpleNamespace(
        status={"succeeded": succeeded, "failed": failed},
        spec={"suspend": suspend},
        metadata=SimpleNamespace(uid=uid, name=name),
    )
```
**New helper:** `fake_local_queue(name="phaze-lq", namespace="phaze")` returning a `SimpleNamespace` with
`metadata=SimpleNamespace(name=..., namespace=...)`; plus a 404/transient seam (monkeypatch
`kube_staging.get_local_queue` to raise `kr8s.NotFoundError` / a generic `Exception`).

---

### `tests/test_services/test_kube_staging.py` — `get_local_queue` cases (test, request-response)

**Analog:** `test_get_job_returns_status` (`:228`) using the shared `kube_respx` fixture
(`conftest.py:307`, `KUBE_TEST_API_URL = "https://kube.test"`, `:303`) + the `_StubCfg` / `stub_cfg`
fixture (`:42-64`). The Workload path uses `/apis/kueue.x-k8s.io/v1beta1/namespaces/{ns}/...` (`_WL_PATH`,
`:40`) — the LocalQueue path is the same group: `/apis/kueue.x-k8s.io/v1beta1/namespaces/{ns}/localqueues/{name}`.

**Stub-cfg fixture to reuse** (`:42-64`): already carries `kube_workload_api_version="kueue.x-k8s.io/v1beta1"`.

**New cases (RESEARCH Test Map):** `test_get_local_queue_success` (200 → returns obj),
`test_get_local_queue_not_found` (404 → `kr8s.NotFoundError`), `test_get_local_queue_transient` (500 → raises).
Mock the `localqueues/{name}` GET via `kube_respx`.

---

### `tests/test_tasks/test_controller_startup_*.py` — probe gating + boot-resilience (test, event-driven)

**Analog:** `tests/test_tasks/test_controller_startup_banner.py` (whole file) — the canonical recipe for
testing `controller.startup` without real Postgres/HTTP: monkeypatch the heavy constructors + `get_settings`,
then `await controller.startup(ctx)`.

**Monkeypatch recipe to clone** (`:19-44`):
```python
    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: MagicMock())
    # ... DiscogsographyClient, load_prompt_template, ProposalService ...
    fake_cfg = MagicMock(); fake_cfg.redis_url = "redis://localhost:6379/0"  # etc
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)
```

**New cases (RESEARCH Test Map):**
- `test_localqueue_probe_skipped_when_not_k8s` — `fake_cfg.cloud_target != "k8s"` ⇒ `get_local_queue` never called.
- `test_localqueue_probe_sets_flag_on_failure` — patch `kube_staging.get_local_queue` to raise ⇒ `ctx["redis"].set(...)` called, **startup still returns** (no raise).
- `test_localqueue_probe_clears_flag_on_success` — success ⇒ `ctx["redis"].delete(...)` called.
- Provide a fake `ctx["redis"]` (`AsyncMock`) since the test patches the engine away.

---

### `tests/test_routers/test_pipeline_localqueue.py` (NEW) — alert OOB + degrade-safe read (test, request-response)

**Analog:** `tests/test_routers/test_pipeline_inadmissible.py` (whole file) — the dashboard alert test
recipe (empty when healthy, banner when flagged, OOB on `/pipeline/stats`). Uses `AsyncClient` + seeded DB.

**Cases (RESEARCH Test Map):** alert renders empty when the Redis key is absent / `app.state.redis` missing
(degrade-safe → reachable/silent); alert renders the locked amber copy when the key is set; stable
`<section id>` on both first-load and the OOB re-push. Drive the flag by setting the Redis key (or
monkeypatching `get_localqueue_unreachable`).

---

### `tests/test_services/test_agent_liveness.py` (extend) — the `never`-not-`dead` invariant (test)

**Analog (already exists, extend):** `test_classify_never_when_last_seen_at_is_none` (`:72-75`):
```python
def test_classify_never_when_last_seen_at_is_none() -> None:
    agent = _make_agent("never-agent")
    assert classify(agent, NOW) == "never"
```
**Add (KDEPLOY-04 invariant):** assert `classify(agent, now) != "dead"` for any `now` when
`last_seen_at is None` (e.g. parametrize `now` far in the future) — the structural DEAD-suppression proof.

---

### `tests/test_task_split.py` (extend) — k8s pod never heartbeats (test, import boundary)

**Analog (already exists, extend):** the import-boundary tests, e.g. `test_job_runner_does_not_import_phaze_database`
(`:496`) and `test_reconcile_cloud_jobs_is_control_only_not_in_agent_worker` (`:237`). These spawn a
subprocess / introspect imports to assert a module never pulls a forbidden dependency.

**Add:** assert `phaze.job_runner` never imports/calls `_heartbeat_loop` (the ephemeral pod doesn't
heartbeat) — mirror the import-boundary subprocess/introspection style.

---

### `docs/k8s-burst.md` (NEW) — feature runbook (docs; no code analog, structural)

**Structural analog:** `docs/cloud-burst.md` (23KB, A1-specific). Mirror its shape/tone; do NOT fold K8s
into it (D-03 — leave a short pointer in `cloud-burst.md`, the transitional k8s section is at `:297-389`).

**Sections (D-01/D-02/D-03):** cluster-admin runbook (the verified v1beta1 Kueue manifests — ResourceFlavor /
ClusterQueue / LocalQueue / namespaced RBAC Role+SA+RoleBinding / bearer-token Secret, all in
RESEARCH §Runbook Content), homelab change-prompt pointer, deploy ordering, transport-agnostic endpoint
notes (Tailscale **or** WireGuard), the v1beta1↔v1beta2 "keep all three in sync" caveat (RESEARCH Pitfall 1),
and a smoke test (doc checklist — discretion).

---

### `.planning/phases/56-.../56-HOMELAB-CHANGE-PROMPT.md` (NEW) — homelab change-prompt (docs, structural)

**Structural analog:** `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md`.
**Headings to mirror** (from that file): `# Homelab Change Prompt — …` → `## Context for the homelab agent`
→ numbered `## 1. Provision …` / `## 2. Apply …` / `## 3. Create …` steps → `## Deploy ordering`
→ `## Done-when checklist`. Swap OCI/Tailscale/PG-role content for: apply the Kueue objects + RBAC + Secret
(`kubectl apply`), provision the compute-agent token Secret, deploy ordering via `datum@nox` / `datum@lux`.
Workspace boundary (D-02): phaze = spec, homelab = live infra; NO live `kubectl` authored in the phaze repo.

---

### `docs/configuration.md`, `docs/deployment.md`, `docs/README.md` (edits) — docs (exact in-file analogs)

- **`docs/configuration.md`** (D-04, KDEPLOY-02): add the K8s/S3 knob table sourced verbatim from the
  `Field(...)` descriptions in `config.py` — `cloud_target` (`:406`), `kube_api_url`/`kube_namespace`/
  `kube_local_queue` (`:534`/`:539`/`:544`), `kube_workload_api_version` (`:564`), `s3_*` (`:466`–`:496`),
  presign/lifecycle/part-size/max-attempts. Flag the `_FILE`-secret fields (`SECRET_FILE_FIELDS`, `:348`).
  RESEARCH notes a partial table already exists at `configuration.md` §Kube submit/reconcile settings — extend it.
- **`docs/deployment.md`** (D-04, KDEPLOY-05): add the single-`cloud_target` revert section (KDEPLOY-05 names
  deployment.md *literally* — satisfy the criterion) + a pointer to `docs/k8s-burst.md`. RESEARCH points at
  the existing §Cloud-burst compute agent section as the insertion neighborhood.
- **`docs/README.md`** (D-04): add a `k8s-burst.md` index row under `## 🚀 Operations` — mirror the existing
  rows (`docs/README.md:28-29`, the Deployment Guide / Cloud Burst entries).

---

## Shared Patterns

### Cross-process flag (controller writes, dashboard reads)
**Source:** `ctx["redis"]` build (`controller.py:104`) + `_read_pipeline_counters` degrade
(`routers/pipeline.py:106-111`) + `get_inadmissible_count` (`services/pipeline.py:821-841`).
**Apply to:** the LocalQueue probe (write side, controller) + `get_localqueue_unreachable` (read side, dashboard).
Key: `phaze:k8s:localqueue_unreachable`. The probe writes via `ctx["redis"]`; the dashboard reads via
`getattr(request.app.state, "redis", None)`, degrading to `False`.

### Boot-resilience try/except (never abort startup)
**Source:** `controller.py:145-157` (ledger backfill + recovery).
**Apply to:** the LocalQueue probe block — own broad `try/except`, `logger.warning(...)`, never re-raise.

### Degrade-safe dashboard read (hot 5s poll never 500s — T-54-10)
**Source:** `_safe_count` (`services/pipeline.py:274-291`), `get_inadmissible_count` (`:821-841`),
`_read_pipeline_counters` (`routers/pipeline.py:93-111`).
**Apply to:** `get_localqueue_unreachable` — any error/missing handle → `False` (silent).

### Amber OOB alert card (loud only on failure, empty otherwise)
**Source:** `inadmissible_card.html` (whole file). Stable `<section id>` outside `#pipeline-stats`,
`{% if oob %}hx-swap-oob{% endif %}`, `role="alert"` + `aria-labelledby`, amber classes.
**Apply to:** `localqueue_card.html`.

### kr8s GET via the single kube-staging home
**Source:** `kube_staging.get_job` (`:197-203`) + `_kube_config()` gate (`:72-84`) + `_api()` (`:87-106`) +
`new_class(...)` (`:229`). **Apply to:** `get_local_queue()`. Every kr8s call stays in `kube_staging.py`.

### Static-string Jinja autoescape (no operator free-text → no injection — T-54-11)
**Source:** `inadmissible_card.html` (static copy), `agents.html` intro.
**Apply to:** both the LocalQueue alert and the ephemeral note — all copy is static (locked in 56-UI-SPEC).

### Docs-as-spec, homelab-as-infra (workspace boundary)
**Source:** `51-HOMELAB-CHANGE-PROMPT.md` + `docs/cloud-burst.md`.
**Apply to:** `docs/k8s-burst.md` + `56-HOMELAB-CHANGE-PROMPT.md` — phaze ships the authoritative manifest
spec; the operator/homelab applies it. No live `kubectl` in the phaze repo.

---

## No Analog Found

None. Every file maps to a concrete in-repo analog (most are near-verbatim clones).

---

## Metadata

**Analog search scope:** `src/phaze/services/`, `src/phaze/tasks/`, `src/phaze/routers/`,
`src/phaze/templates/{pipeline/partials,admin}/`, `tests/{test_services,test_tasks,test_routers,test_deployment}/`,
`tests/kube_fakes.py`, `docs/`, `.planning/milestones/v5.0-phases/51-*`.
**Files scanned:** ~24 (read first-hand: kube_staging.py, controller.py, reconcile_cloud_jobs.py,
inadmissible_card.html, agent_liveness.py, agents.html, kube_fakes.py, pipeline.py [router+service targeted
sections], plus 5 test analogs and the v5.0 precedent headings).
**Pattern extraction date:** 2026-06-28
</content>
</invoke>
