"""LocalQueue startup-probe tests for phaze.tasks.controller (Phase 56, KDEPLOY-04 / D-05 / D-06).

RED until 56-01 wires the probe into ``controller.startup``. The probe is a non-fatal,
``cloud_target == "k8s"``-gated reachability check that GETs the configured Kueue LocalQueue and
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


def _stub_controller(monkeypatch: pytest.MonkeyPatch, fake_redis: AsyncMock, *, cloud_target: str) -> MagicMock:
    """Patch controller.startup's heavy collaborators; return the fake_cfg (cloud_target set).

    Mirrors ``test_controller_startup_banner`` exactly, plus: (1) ``cloud_target`` is a concrete
    value so the ``if cfg.cloud_target == "k8s"`` gate evaluates, and (2) ``redis_async.Redis.from_url``
    yields ``fake_redis`` so ``ctx["redis"]`` (built at controller.py:104) is our assertable AsyncMock.
    """
    monkeypatch.setattr("phaze.tasks.controller.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: MagicMock())

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
    fake_cfg.cloud_target = cloud_target
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)

    # ctx["redis"] is built inside startup via redis_async.Redis.from_url -- override it so the
    # probe's .set/.delete land on our AsyncMock (we cannot pre-seed ctx; startup overwrites it).
    monkeypatch.setattr("phaze.tasks.controller.redis_async.Redis.from_url", lambda *_a, **_kw: fake_redis)
    return fake_cfg


_FLAG_KEY = "phaze:k8s:localqueue_unreachable"


@pytest.mark.asyncio
async def test_localqueue_probe_skipped_when_not_k8s(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-05: with ``cloud_target != "k8s"`` the probe never runs -- get_local_queue is not called."""
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, cloud_target="local")

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
    _stub_controller(monkeypatch, fake_redis, cloud_target="k8s")

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
    _stub_controller(monkeypatch, fake_redis, cloud_target="k8s")

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
    _stub_controller(monkeypatch, fake_redis, cloud_target="k8s")

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
    _stub_controller(monkeypatch, fake_redis, cloud_target="k8s")

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
    _stub_controller(monkeypatch, fake_redis, cloud_target="local")

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
    _stub_controller(monkeypatch, fake_redis, cloud_target="local")

    probe = AsyncMock()
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    probe.assert_not_called()
