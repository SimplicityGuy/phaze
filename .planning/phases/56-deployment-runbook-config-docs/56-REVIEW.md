---
phase: 56-deployment-runbook-config-docs
reviewed: 2026-06-28T00:00:00Z
depth: standard
files_reviewed: 21
files_reviewed_list:
  - docs/cloud-burst.md
  - docs/configuration.md
  - docs/deployment.md
  - docs/k8s-burst.md
  - docs/README.md
  - src/phaze/routers/pipeline.py
  - src/phaze/services/kube_staging.py
  - src/phaze/services/pipeline.py
  - src/phaze/tasks/controller.py
  - src/phaze/templates/admin/agents.html
  - src/phaze/templates/pipeline/dashboard.html
  - src/phaze/templates/pipeline/partials/localqueue_card.html
  - src/phaze/templates/pipeline/partials/stats_bar.html
  - tests/kube_fakes.py
  - tests/test_deployment/test_k8s_runbook.py
  - tests/test_routers/test_pipeline_localqueue.py
  - tests/test_services/test_agent_liveness.py
  - tests/test_services/test_kube_staging.py
  - tests/test_task_split.py
  - tests/test_tasks/test_controller_startup_localqueue.py
findings:
  critical: 1
  warning: 2
  info: 1
  total: 4
status: issues_found
---

# Phase 56: Code Review Report

**Reviewed:** 2026-06-28
**Depth:** standard
**Files Reviewed:** 21
**Status:** issues_found

## Summary

Phase 56 adds the Kubernetes-burst deployment runbook, config docs, and a runtime
LocalQueue-reachability probe with a dashboard alert. The bulk of the change is documentation
and is internally consistent (the RBAC verb floor in `docs/k8s-burst.md` matches
`tests/test_deployment/test_k8s_runbook.py`; the manifest spec matches
`tests/test_services/test_kube_staging.py`; the never-not-dead invariant for the non-heartbeating
k8s lane is well-proven). The new probe wiring in `controller.startup`, however, has a boot-resilience
defect that directly contradicts the design's load-bearing "control plane boots regardless" invariant,
plus a flag-lifecycle gap that produces a stuck false alert. The new probe failure paths are not
exercised against a raising Redis handle, so the tests give false confidence on exactly the path that
breaks.

## Critical Issues

### CR-01: Unguarded `ctx["redis"].set(...)` in the probe's `except` aborts controller boot when Redis is unreachable

**File:** `src/phaze/tasks/controller.py:167-176`
**Issue:**
The LocalQueue probe is wrapped in a broad `try/except` whose stated purpose is boot resilience
("a transient kube/mesh blip MUST NEVER abort controller boot (D-05 -- the control plane still boots
Postgres/Redis/UI/local-analysis)"). But the `except` handler itself performs an unguarded
`await ctx["redis"].set("phaze:k8s:localqueue_unreachable", "1")`:

```python
if cfg.cloud_target == "k8s":
    try:
        await kube_staging.get_local_queue()
        await ctx["redis"].delete("phaze:k8s:localqueue_unreachable")
    except Exception:
        logger.warning(...)
        await ctx["redis"].set("phaze:k8s:localqueue_unreachable", "1")  # <-- can raise
```

`ctx["redis"]` is built via `redis_async.Redis.from_url(...)` (line 105), which connects lazily — this
probe is the **first** Redis operation in `startup` (the recovery/backfill blocks above use Postgres,
not Redis). So if Redis is down at boot AND `cloud_target=k8s`, `get_local_queue()` may fail, the handler
runs `.set(...)`, that `.set` raises `ConnectionError`, and the exception propagates **out of `startup`**,
crashing the SAQ control worker boot — the exact outcome the comment forbids. The same crash occurs on the
success path if `.delete(...)` raises (a Redis hiccup), since that lands in the same handler that re-calls
`.set`. The whole codebase is otherwise built to degrade when Redis is unavailable; this path is the
exception that takes the control plane down.

The startup tests (`tests/test_tasks/test_controller_startup_localqueue.py`) only use a non-raising
`AsyncMock()` for `fake_redis`, so this crash path is never covered.

**Fix:** Guard the flag writes so a Redis failure cannot escape the probe (mirror the `logger.exception` +
swallow discipline of the recovery/backfill blocks above):

```python
if cfg.cloud_target == "k8s":
    try:
        await kube_staging.get_local_queue()
        reachable = True
    except Exception:
        logger.warning("phaze.controller startup: Kueue LocalQueue unreachable ...")
        reachable = False
    try:
        if reachable:
            await ctx["redis"].delete("phaze:k8s:localqueue_unreachable")
        else:
            await ctx["redis"].set("phaze:k8s:localqueue_unreachable", "1")
    except Exception:
        logger.warning("phaze.controller startup: could not update localqueue flag (Redis)")
```

Add a startup test where `fake_redis.set`/`get_local_queue` both raise and assert `startup` returns
without propagating.

## Warnings

### WR-01: `phaze:k8s:localqueue_unreachable` flag is startup-only — leaks a stuck false alert across a target switch and never self-heals at runtime

**File:** `src/phaze/tasks/controller.py:167` (writer gate) and `src/phaze/services/pipeline.py:844-861` (reader)
**Issue:**
The flag is written/cleared **only** inside the `if cfg.cloud_target == "k8s":` block at controller startup.
Two consequences:

1. **Stale across target switch.** If the last `k8s` boot set the flag (LocalQueue unreachable) and the
   operator then switches to `PHAZE_CLOUD_TARGET=local` (or `a1`) and restarts, the probe block is skipped
   entirely, so the key is never deleted. Redis (a separate, long-lived container that does not restart with
   the controller) keeps the key, and `get_localqueue_unreachable` returns `True` — the dashboard shows a
   perpetual amber "K8s LocalQueue unreachable" alert on a deployment that no longer uses k8s. The
   documented one-flip revert (`deployment.md` §Revert) does not clear this alert.
2. **No runtime self-heal.** The flag is re-evaluated only at controller restart, never by the `*/5`
   `reconcile_cloud_jobs` cron. A LocalQueue that recovers mid-run keeps the alert until the next restart.
   This contradicts the in-code claim in `src/phaze/templates/pipeline/partials/stats_bar.html:94-95`
   ("so a previously-shown banner clears once the LocalQueue becomes reachable again") — the OOB re-push only
   reflects the flag; the flag itself does not change at runtime. (Contrast the Inadmissible alert, which
   `configuration.md:154` correctly describes as cron-cleared.)

**Fix:** Clear the flag on every non-k8s startup (unconditionally delete before the `if`, or `else: await
ctx["redis"].delete(...)`), and either set a TTL on the key or re-run the probe from the `*/5` reconcile cron
so a recovered LocalQueue clears the alert without a restart. Correct or soften the stats_bar comment to
reflect "clears on the next controller restart," not "once reachable again."

### WR-02: `kube_staging._api` depends on private kr8s internals pinned to one version

**File:** `src/phaze/services/kube_staging.py:97-106`
**Issue:**
Applying the SA bearer token mutates `api.auth.token` and then calls the private
`await api._create_session()` to force the header onto the wire. The comment pins this to
"kr8s 0.20.15 `_api._create_session`". A kr8s minor upgrade that renames/removes `_create_session` or
changes `auth.token` handling would break control-plane→kube authentication — and would fail as live-cluster
401s, the hardest failure to diagnose, since there is no CI cluster (`docs/k8s-burst.md` §Smoke test). The
seam test (`test_sa_token_applied_as_bearer`) pins the *current* behavior but cannot catch a silent
upstream API change because it stubs the kr8s REST surface, not kr8s's session-construction internals.

**Fix:** Prefer a public kr8s construction path that accepts the token at `api()` creation time (so the
session is built once with the header), or add a guard that fails loud if `_create_session` is absent on the
installed kr8s. At minimum, pin `kr8s` to a compatible range in `pyproject.toml` and document the coupling so
an upgrade is gated on re-verifying this line.

## Info

### IN-01: Internal decision/threat IDs leak into operator-facing documentation

**File:** `docs/deployment.md:367`, `docs/configuration.md` (e.g. lines 64, 154), `docs/cloud-burst.md`, `docs/k8s-burst.md` (throughout)
**Issue:**
Operator-facing docs are peppered with internal traceability tokens — `CR-01`, `D-06`, `DIST-04`,
`KSUBMIT-04`, `T-54-07`, `CLOUDPIPE-01`, `KDEPLOY-06`, etc. (e.g. `deployment.md:367` "refuses non-`https://`
`agent_api_url` (CR-01) and passwordless `redis_url` (D-06)"). These mean nothing to a deploying operator and
add noise to runbooks that are otherwise copy-paste-ready. They are harmless but reduce doc quality.
**Fix:** Strip or relocate phase/threat IDs to planning artifacts; keep operator docs in operator vocabulary
(or move IDs into HTML comments so they stay traceable without rendering to readers).

---

_Reviewed: 2026-06-28_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
