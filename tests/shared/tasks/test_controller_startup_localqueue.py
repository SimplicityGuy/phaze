"""LocalQueue startup-probe tests for phaze.tasks.controller (Phase 56, KDEPLOY-04 / D-05 / D-06).

The probe is a non-fatal, ``active_cloud_kind == "kueue"``-gated reachability check (Phase 67 rewired
it off the flat ``cloud_target`` onto the registry accessor) that GETs the configured Kueue LocalQueue and
writes a cross-process flag via ``ctx["redis"]`` -- ``.set("phaze:k8s:localqueue_unreachable", ...)``
on failure, ``.delete(...)`` on success -- wrapped in a broad try/except that NEVER re-raises
(boot resilience: a transient kube/mesh blip must not take down Postgres/Redis/UI/local-analysis).

The monkeypatch recipe clones ``test_controller_startup_banner.py``: stub the heavyweight
constructors + ``get_settings`` so ``startup`` opens no Postgres/HTTP connection, and replace
``redis_async.Redis.from_url`` so ``ctx["redis"]`` is an ``AsyncMock`` whose ``set``/``delete`` we
assert on. The probe seam ``phaze.services.kube_staging.get_local_queue`` is patched with
``raising=False`` so the tests collect/run before 56-01 adds that function.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _stub_collaborators(monkeypatch: pytest.MonkeyPatch, fake_redis: AsyncMock) -> None:
    """Patch controller.startup's heavyweight collaborators (no ``get_settings`` stub).

    Mirrors ``test_controller_startup_banner`` exactly and points ``redis_async.Redis.from_url`` at
    ``fake_redis`` so ``ctx["redis"]`` (built inside startup) is our assertable AsyncMock. Kept
    separate from the ``get_settings`` stub so a test may instead supply a REAL ``ControlSettings``
    (the registry-log test drives the actual ``log_effective_registry`` projection through it).
    """
    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.redis_async.Redis.from_url", lambda *_a, **_kw: fake_redis)


def _stub_controller(monkeypatch: pytest.MonkeyPatch, fake_redis: AsyncMock, *, active_cloud_kind: str | None) -> MagicMock:
    """Patch collaborators + a MagicMock ``get_settings``; return the fake_cfg.

    Phase 67 (REG-04): the probe now gates on ``cfg.active_cloud_kind == "kueue"`` (the registry-derived
    transitional accessor), NOT the flat ``cloud_target``. Pass ``active_cloud_kind="kueue"`` to run the
    probe, or ``None`` (all-local) to skip it. ``log_effective_registry`` is a MagicMock no-op here (the
    real projection is asserted in ``test_startup_logs_effective_registry_secret_free``).
    """
    _stub_collaborators(monkeypatch, fake_redis)

    fake_cfg = MagicMock()
    fake_cfg.redis_url = "redis://localhost:6379/0"
    fake_cfg.database_url = "postgresql+asyncpg://test"
    fake_cfg.queue_url = "postgresql+asyncpg://test"
    fake_cfg.debug = False
    fake_cfg.discogsography_url = "http://test"
    fake_cfg.llm_model = "stub-model"
    fake_cfg.llm_max_rpm = 60
    fake_cfg.log_level = "INFO"
    fake_cfg.log_json = True
    fake_cfg.anthropic_api_key = None
    fake_cfg.openai_api_key = None
    fake_cfg.active_cloud_kind = active_cloud_kind
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)
    return fake_cfg


_FLAG_KEY = "phaze:k8s:localqueue_unreachable"


@pytest.mark.asyncio
async def test_localqueue_probe_skipped_when_not_k8s(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-05: with ``active_cloud_kind != "kueue"`` (all-local) the probe never runs -- get_local_queue is not called."""
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind=None)

    probe = AsyncMock()
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    probe.assert_not_called()


@pytest.mark.asyncio
async def test_localqueue_probe_sets_flag_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-05/D-06: an unreachable LocalQueue raises a flag AND startup returns without raising (boot-resilient)."""
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind="kueue")

    probe = AsyncMock(side_effect=RuntimeError("kube unreachable"))
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- a probe failure can never abort controller boot.
    await controller.startup(ctx)

    probe.assert_awaited()
    # The cross-process unreachable flag is written so the dashboard can surface the alert.
    set_keys = [call.args[0] for call in fake_redis.set.await_args_list if call.args]
    assert _FLAG_KEY in set_keys
    fake_redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_localqueue_probe_clears_flag_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable LocalQueue clears the flag -- ``ctx["redis"].delete(<flag key>)`` is called."""
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind="kueue")

    probe = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    probe.assert_awaited()
    delete_keys = [call.args[0] for call in fake_redis.delete.await_args_list if call.args]
    assert _FLAG_KEY in delete_keys


@pytest.mark.asyncio
async def test_redis_down_during_unreachable_probe_does_not_abort_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: with kube unreachable AND Redis down, the flag ``.set`` raises -- startup must still NOT abort.

    The probe is the first Redis call in ``startup`` (backfill/recovery use Postgres). If a Redis-down
    boot lets the flag write propagate, the control worker crashes -- the exact opposite of the D-05
    "control plane boots regardless" invariant. Persisting the flag must therefore be guarded too.
    """
    fake_redis = AsyncMock()
    fake_redis.set.side_effect = ConnectionError("redis down")
    fake_redis.delete.side_effect = ConnectionError("redis down")
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind="kueue")

    probe = AsyncMock(side_effect=RuntimeError("kube unreachable"))
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- neither a kube blip nor a Redis blip can abort controller boot (D-05).
    await controller.startup(ctx)

    probe.assert_awaited()


@pytest.mark.asyncio
async def test_redis_down_during_reachable_probe_does_not_abort_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01 (success path): kube reachable but Redis down -- the clearing ``.delete`` must not abort boot."""
    fake_redis = AsyncMock()
    fake_redis.delete.side_effect = ConnectionError("redis down")
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind="kueue")

    probe = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    probe.assert_awaited()


@pytest.mark.asyncio
async def test_stale_flag_cleared_when_not_k8s(monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-01: switching the control plane away from k8s clears any stale unreachable flag.

    The flag lives in long-lived Redis. Without an explicit clear on a non-k8s boot, a previously-set
    flag persists forever and the dashboard shows a perpetual false alert -- the documented one-flip
    revert (``PHAZE_CLOUD_TARGET=k8s`` -> ``local``) would not silence it.
    """
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind=None)

    probe = AsyncMock()
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    # The probe never runs off-k8s, but the stale flag is cleared so the alert cannot persist.
    probe.assert_not_called()
    delete_keys = [call.args[0] for call in fake_redis.delete.await_args_list if call.args]
    assert _FLAG_KEY in delete_keys


@pytest.mark.asyncio
async def test_stale_flag_clear_redis_down_does_not_abort_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-01 + D-05: the off-k8s stale-flag clear is best-effort -- a Redis blip must not abort boot."""
    fake_redis = AsyncMock()
    fake_redis.delete.side_effect = ConnectionError("redis down")
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind=None)

    probe = AsyncMock()
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    probe.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 67 (REG-04): boot-time effective-registry log
# ---------------------------------------------------------------------------


# One kueue backend carrying an SA token; the startup projection (id/kind/rank/cap only) must never
# leak the token (Pitfall 5 / T-67-05-02). A shared bucket satisfies the D-08 bucket-ref invariant.
_KUEUE_REGISTRY_WITH_SECRET = """
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[backends]]
    kind = "kueue"
    id = "kueue-cluster"
    rank = 10
    cap = 4
    buckets = ["shared-bucket"]

    [backends.kube]
    api_url = "https://kube.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq"
    sa_token = "SUPERSECRETTOKEN"

    [[buckets]]
    id = "shared-bucket"
    scope = "shared"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-staging"
"""


@pytest.mark.asyncio
async def test_startup_logs_effective_registry_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """REG-04: controller.startup logs the resolved registry (id/kind/rank/cap) with NO secret material.

    Drives a REAL ``ControlSettings`` off a one-kueue registry (via the shared ``backends_toml_env``
    fixture) so the actual ``log_effective_registry`` projection is emitted through the boot pipeline.
    The projection is secret-free by construction (Plan 02): the backend id/kind/rank/cap appear, the
    SA token never does (T-67-05-02). The kueue-gated LocalQueue probe also runs off the same registry.
    """
    from phaze.config import ControlSettings

    fake_redis = AsyncMock()
    _stub_collaborators(monkeypatch, fake_redis)

    backends_toml_env(_KUEUE_REGISTRY_WITH_SECRET)
    settings = ControlSettings()
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: settings)

    # The registry resolves active_cloud_kind == "kueue", so the LocalQueue probe fires; stub it clean.
    probe = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    out = capsys.readouterr().out
    # The boot-time registry projection line is emitted with the backend id/kind/rank/cap.
    assert "effective backend registry" in out
    assert "kueue-cluster" in out
    assert "kueue" in out
    # Pitfall 5 / T-67-05-02: the SA token (and any secret material) never reaches the log.
    assert "SUPERSECRETTOKEN" not in out
    # The registry-gated probe ran (active_cloud_kind == "kueue" derived from the real registry).
    probe.assert_awaited()
