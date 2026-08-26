"""LocalQueue startup-probe tests for phaze.tasks.controller (Phase 56, KDEPLOY-04 / D-05 / D-06).

REVISED phaze-6r39: the probe is a non-fatal, per-kueue-backend reachability check (Phase 70 rewired
it off a single ``active_cloud_kind == "kueue"``-gated global probe onto iterating EVERY configured
kueue backend) that GETs each backend's own Kueue LocalQueue and logs a WARNING on failure, wrapped in
a broad try/except that NEVER re-raises (boot resilience: a transient kube/mesh blip must not take
down Postgres/Redis/UI/local-analysis).

phaze-6r39 retired the probe's Redis side effect entirely: it used to ALSO persist a cross-process
flag via ``ctx["redis"]`` (``.set(...)`` on failure, ``.delete(...)`` on success) for the dashboard to
read. That flag was a boot-time snapshot with no TTL and no other writer -- it never cleared once
connectivity was restored (the reported bug) and never appeared at all for an outage that began after
boot. The dashboard now derives the same alert live from the per-lane probe every 5s ``/pipeline/stats``
poll already runs (``derive_localqueue_unreachable``, see ``tests/shared/routers/test_pipeline_localqueue.py``
and ``tests/shared/services/test_lane_snapshot.py``), so this suite only covers what remains: the probe
itself still runs (or is skipped when no kueue backend is configured), still logs on failure, and STILL
never aborts boot on a kube blip -- and it asserts the retired Redis write is really gone (``fake_redis.set``
/ ``fake_redis.delete`` are never awaited by this probe any more).

The monkeypatch recipe clones ``test_controller_startup_banner.py``: stub the heavyweight
constructors + ``get_settings`` so ``startup`` opens no Postgres/HTTP connection, and replace
``redis_async.Redis.from_url`` so ``ctx["redis"]`` is an ``AsyncMock`` we can assert was left alone.
The probe seam ``phaze.services.kube_staging.get_local_queue`` is patched with ``raising=False``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.shared.tasks._shared import make_stub_session_factory


def _stub_collaborators(monkeypatch: pytest.MonkeyPatch, fake_redis: AsyncMock) -> None:
    """Patch controller.startup's heavyweight collaborators (no ``get_settings`` stub).

    Mirrors ``test_controller_startup_banner`` exactly and points ``redis_async.Redis.from_url`` at
    ``fake_redis`` so ``ctx["redis"]`` (built inside startup) is our assertable AsyncMock. Kept
    separate from the ``get_settings`` stub so a test may instead supply a REAL ``ControlSettings``
    (the registry-log test drives the actual ``log_effective_registry`` projection through it).
    """
    monkeypatch.setattr("phaze.database.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: make_stub_session_factory())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.redis_async.Redis.from_url", lambda *_a, **_kw: fake_redis)


def _stub_controller(monkeypatch: pytest.MonkeyPatch, fake_redis: AsyncMock, *, active_cloud_kind: str | None) -> MagicMock:
    """Patch collaborators + a MagicMock ``get_settings``; return the fake_cfg.

    Phase 70 (MKUE-01/03): the probe iterates EVERY configured kueue backend, threading each backend's
    own ``KubeConfig`` into ``kube_staging.get_local_queue(kube)``. The stub sets the registry shape --
    ``cloud_enabled`` + a ``backends`` list whose kueue entry duck-types the Phase-67 submodel
    (kind/id/rank/cap + ``kube``). Pass ``active_cloud_kind="kueue"`` to seed a one-kueue registry (probe
    runs) or ``None`` (all-local, probe skipped). ``log_effective_registry`` is a MagicMock no-op here
    (the real projection is asserted in ``test_startup_logs_effective_registry_secret_free``).
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
    # Registry shape the rewired per-cluster probe reads: an all-local registry (cloud disabled) skips
    # the probe; a kueue backend runs it (threaded its own KubeConfig). Entries duck-type the submodel.
    if active_cloud_kind is None:
        fake_cfg.cloud_enabled = False
        fake_cfg.backends = [SimpleNamespace(kind="local", id="local", rank=0, cap=0)]
    else:
        fake_cfg.cloud_enabled = True
        entry_kwargs: dict[str, Any] = {"kind": active_cloud_kind, "id": f"{active_cloud_kind}-1", "rank": 10, "cap": 2}
        if active_cloud_kind == "kueue":
            entry_kwargs["kube"] = SimpleNamespace(api_url="https://kube.test", namespace="phaze", local_queue="phaze-lq")
        fake_cfg.backends = [SimpleNamespace(**entry_kwargs)]
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)
    return fake_cfg


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
    # phaze-6r39: the probe no longer touches Redis at all -- not on the skip path either.
    fake_redis.set.assert_not_awaited()
    fake_redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_localqueue_probe_logs_warning_on_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """D-05/D-06: an unreachable LocalQueue logs a WARNING AND startup returns without raising (boot-resilient).

    phaze-6r39: the probe no longer persists a cross-process Redis flag on failure -- that mechanism
    was retired (see module docstring). Only the boot-time WARNING log and boot resilience remain.
    Captures stdout instead of caplog, mirroring ``test_controller_startup_banner.py`` (structlog's
    own processors write to stdout; the root handler ``configure_logging`` installs is not what caplog
    listens on).
    """
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind="kueue")

    probe = AsyncMock(side_effect=RuntimeError("kube unreachable"))
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- a probe failure can never abort controller boot.
    await controller.startup(ctx)

    probe.assert_awaited()
    assert "LocalQueue is unreachable" in capsys.readouterr().out
    # phaze-6r39: the retired Redis flag write -- neither .set nor .delete is called by the probe.
    fake_redis.set.assert_not_awaited()
    fake_redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_localqueue_probe_reachable_does_not_touch_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable LocalQueue probes clean and touches NO Redis key (phaze-6r39: the clearing ``.delete`` is gone)."""
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind="kueue")

    probe = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    probe.assert_awaited()
    fake_redis.set.assert_not_awaited()
    fake_redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_kube_blip_does_not_abort_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-05: a kube probe failure must never abort controller boot, independent of Redis health."""
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind="kueue")

    probe = AsyncMock(side_effect=RuntimeError("kube unreachable"))
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- a kube blip can never abort controller boot (D-05).
    await controller.startup(ctx)

    probe.assert_awaited()


@pytest.mark.asyncio
async def test_switching_off_k8s_probes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-01 (revised phaze-6r39): switching the control plane away from k8s simply skips the probe.

    There is no stale flag to clear any more -- the dashboard's alert is derived live from the current
    lane snapshot on every poll, so an all-local boot naturally shows no kueue lanes and no banner,
    with nothing to reset in Redis (see ``tests/shared/routers/test_pipeline_localqueue.py``).
    """
    fake_redis = AsyncMock()
    _stub_controller(monkeypatch, fake_redis, active_cloud_kind=None)

    probe = AsyncMock()
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    probe.assert_not_called()
    fake_redis.set.assert_not_awaited()
    fake_redis.delete.assert_not_awaited()


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


# Two kueue backends sharing one shared bucket: the literal MKUE-01 multi-cluster schema (D-09 allows a
# shared-scope bucket to be referenced by many kueue backends). Phase 70 probes EACH cluster.
_MULTI_KUEUE_REGISTRY = """
    [[backends]]
    kind = "kueue"
    id = "cluster-a"
    rank = 10
    cap = 4
    buckets = ["shared-bucket"]

    [backends.kube]
    api_url = "https://kube-a.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq-a"

    [[backends]]
    kind = "kueue"
    id = "cluster-b"
    rank = 20
    cap = 4
    buckets = ["shared-bucket"]

    [backends.kube]
    api_url = "https://kube-b.example.com"
    namespace = "phaze"
    local_queue = "phaze-lq-b"

    [[buckets]]
    id = "shared-bucket"
    scope = "shared"
    endpoint_url = "https://s3.example.com"
    bucket = "phaze-staging"
"""


@pytest.mark.asyncio
async def test_multi_kueue_registry_probes_every_cluster(
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Any,
) -> None:
    """MKUE-01/03: a valid N-Kueue registry probes EVERY cluster's LocalQueue -- no >1-non-local skip/abort.

    The registry is the literal multi-cluster scenario (two kueue clusters sharing a shared-scope bucket,
    D-09), so ControlSettings() constructs and cloud_enabled is True. Phase 70 iterates every kueue
    backend (was a single ≤1-non-local-gated global probe), threading each backend's own KubeConfig, so
    BOTH clusters are probed; boot never aborts (D-05). phaze-6r39: neither outcome touches Redis any
    more -- the assertion below replaces the old "stale flag cleared" check with "no Redis write at all".
    """
    from phaze.config import ControlSettings

    fake_redis = AsyncMock()
    _stub_collaborators(monkeypatch, fake_redis)

    backends_toml_env(_MULTI_KUEUE_REGISTRY)
    settings = ControlSettings()
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: settings)

    probe = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("phaze.services.kube_staging.get_local_queue", probe, raising=False)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- N Kueue clusters are the milestone target, each probed independently.
    await controller.startup(ctx)

    # Both clusters were probed (one get_local_queue call per configured kueue backend).
    assert probe.await_count == 2
    # phaze-6r39: reachable or not, the probe never writes the retired Redis flag any more.
    fake_redis.set.assert_not_awaited()
    fake_redis.delete.assert_not_awaited()
