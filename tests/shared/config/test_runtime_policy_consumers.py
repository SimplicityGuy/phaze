"""Live policy is observed by real consumers without resetting work or accounting."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError
import pytest
import redis.asyncio as redis_async
from saq import Job

from phaze import runtime_config
from phaze.config import AgentSettings, ControlSettings
from phaze.config_backends import BucketConfig
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers.admin_runtime_config import _coerce
from phaze.routers.pipeline_scans import is_scan_stalled
from phaze.runtime_config import RuntimeConfigStore, effective_settings, partition_overrides
from phaze.schemas.agent_config import AgentConfigResponse, compute_overrides_digest
from phaze.services import s3_staging
from phaze.services.cloud_budget import HOLD_CLOUD_BUDGET_CHAINS, CloudBudgetState, cloud_budget_hold_reason
from phaze.services.proposal_provider import check_rate_limit
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults
from phaze.tasks.heartbeat import _poll_runtime_config
from phaze.tasks.scan import _resolve_chunk_size


@pytest.fixture
def policy_store(monkeypatch: pytest.MonkeyPatch) -> tuple[RuntimeConfigStore, dict[str, Any]]:
    overrides: dict[str, Any] = {}

    async def provider() -> dict[str, Any]:
        return dict(overrides)

    store = RuntimeConfigStore(ControlSettings(), runtime_toml=None, override_provider=provider, check_sizing=False)
    monkeypatch.setattr(runtime_config, "get_runtime_config_store", lambda: store)
    return store, overrides


@pytest.mark.asyncio
async def test_new_job_defaults_apply_without_rewriting_old_jobs_or_analysis_policy(policy_store: tuple[RuntimeConfigStore, dict[str, Any]]) -> None:
    store, overrides = policy_store
    old = Job(function="extract_metadata")
    await apply_project_job_defaults(old)
    overrides.update(worker_job_timeout=42, worker_max_retries=7, worker_keep_result=99)
    assert (await store.reload("api")).successful
    new = Job(function="extract_metadata")
    await apply_project_job_defaults(new)
    analysis = Job(function="process_file")
    await apply_project_job_defaults(analysis)
    assert (old.timeout, old.retries, old.ttl) == (600, 4, 3600)
    assert (new.timeout, new.retries, new.ttl) == (42, 7, 99)
    assert (analysis.timeout, analysis.retries) == (0, 2)
    assert analysis.heartbeat == store.current().analysis_job_heartbeat_sec


@pytest.mark.asyncio
async def test_idle_watcher_polls_policy_and_shutdown_does_not_report_liveness(
    policy_store: tuple[RuntimeConfigStore, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from phaze.agent_watcher.__main__ import _policy_poll_loop

    store, _overrides = policy_store
    stopped = asyncio.Event()
    real_wait_for = asyncio.wait_for

    async def short_poll_wait(awaitable: Any, timeout: float) -> Any:
        return await real_wait_for(awaitable, timeout=0.001 if timeout == 30.0 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_poll_wait)
    values = {"watcher_settle_seconds": 1, "watcher_max_pending_seconds": 90}

    async def fetch() -> AgentConfigResponse:
        stopped.set()
        return AgentConfigResponse(overrides=values, digest=compute_overrides_digest(values))

    client = MagicMock(get_config=AsyncMock(side_effect=fetch), heartbeat=AsyncMock())
    await real_wait_for(_policy_poll_loop({"runtime_config_store": store}, client, stopped), timeout=1)
    assert store.current().watcher_settle_seconds == 1
    assert store.current().watcher_max_pending_seconds == 90
    client.get_config.assert_awaited_once()
    client.heartbeat.assert_not_awaited()

    # Shutdown wakes an idle poll immediately without fetching or posting another request.
    stopped.clear()
    task = asyncio.create_task(_policy_poll_loop({"runtime_config_store": store}, client, stopped))
    await asyncio.sleep(0)
    stopped.set()
    await real_wait_for(task, timeout=1)
    client.get_config.assert_awaited_once()
    client.heartbeat.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_warning_rechecks_live_threshold_without_rewriting_progress(policy_store: tuple[RuntimeConfigStore, dict[str, Any]]) -> None:
    store, overrides = policy_store
    progress_at = datetime.now(UTC) - timedelta(seconds=400)
    batch = ScanBatch(status=ScanStatus.RUNNING, last_progress_at=progress_at)
    assert not is_scan_stalled(batch)
    overrides["scan_stall_seconds"] = 600
    assert (await store.reload("api")).successful
    assert is_scan_stalled(batch)
    assert batch.last_progress_at == progress_at
    overrides.clear()
    assert (await store.reload("api")).successful
    assert not is_scan_stalled(batch)


@pytest.mark.asyncio
async def test_s3_client_replacement_is_per_operation_and_rejected_change_keeps_last_good(
    policy_store: tuple[RuntimeConfigStore, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, overrides = policy_store
    session = MagicMock()
    monkeypatch.setattr(s3_staging, "get_session", lambda: session)
    bucket = BucketConfig(id="test", scope="shared", endpoint_url="https://storage.invalid", bucket="test")
    s3_staging._client(bucket)
    old_config = session.create_client.call_args.kwargs["config"]
    overrides["s3_client_timeout_sec"] = 8
    assert (await store.reload("api")).successful
    s3_staging._client(bucket)
    new_config = session.create_client.call_args.kwargs["config"]
    assert (old_config.connect_timeout, old_config.read_timeout) == (30, 30)
    assert (new_config.connect_timeout, new_config.read_timeout) == (8, 8)
    overrides["s3_client_timeout_sec"] = 0
    assert (await store.reload("api")).outcome == "rejected"
    s3_staging._client(bucket)
    assert session.create_client.call_args.kwargs["config"].read_timeout == 8
    assert old_config.read_timeout == 30


@pytest.mark.asyncio
async def test_cloud_budget_threshold_changes_preserve_charges_and_inflight_snapshot(policy_store: tuple[RuntimeConfigStore, dict[str, Any]]) -> None:
    store, overrides = policy_store
    cfg = ControlSettings()
    now = datetime.now(UTC)
    budget = CloudBudgetState(3, 0, 0, now - timedelta(days=20))
    old = effective_settings(cfg)
    assert cloud_budget_hold_reason(budget, now, old) == HOLD_CLOUD_BUDGET_CHAINS
    overrides["cloud_budget_max_chains"] = 4
    assert (await store.reload("api")).successful
    assert cloud_budget_hold_reason(budget, now, effective_settings(cfg)) == ""
    assert cloud_budget_hold_reason(budget, now, old) == HOLD_CLOUD_BUDGET_CHAINS
    assert budget.chains_spent == 3
    overrides["cloud_budget_max_chains"] = 2
    assert (await store.reload("api")).successful
    assert cloud_budget_hold_reason(budget, now, effective_settings(cfg)) == HOLD_CLOUD_BUDGET_CHAINS
    assert budget.chains_spent == 3


@pytest.mark.asyncio
async def test_scan_chunks_read_live_policy_but_always_obey_server_schema_cap(
    policy_store: tuple[RuntimeConfigStore, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, overrides = policy_store
    from phaze.tasks import scan

    monkeypatch.setattr(scan, "get_settings", lambda: ControlSettings(agent_file_chunk_max=100))
    assert _resolve_chunk_size() == 100
    overrides["scan_chunk_size"] = 25
    assert (await store.reload("api")).successful
    assert _resolve_chunk_size() == 25
    overrides["scan_chunk_size"] = 2000
    assert (await store.reload("api")).successful
    assert _resolve_chunk_size() == 100


@pytest.mark.asyncio
async def test_agent_poll_recovers_missed_notification_and_preserves_wire_types(tmp_path: Path) -> None:
    values = {"convention_date_fallback_enabled": False, "analysis_progress_interval_sec": 0.25, "cloud_redrive_backoff_sec": [5, 15]}
    response = AgentConfigResponse(overrides=values, digest=compute_overrides_digest(values))
    wire = AgentConfigResponse.model_validate_json(response.model_dump_json())
    assert wire.overrides["convention_date_fallback_enabled"] is False
    assert wire.overrides["analysis_progress_interval_sec"] == 0.25
    agent = AgentSettings(agent_api_url="http://test.invalid", agent_token="test", kind="compute")  # noqa: S106 -- synthetic test credential
    store = RuntimeConfigStore(agent, runtime_toml=tmp_path / "runtime.toml", check_sizing=False)
    client = MagicMock(get_config=AsyncMock(return_value=wire))
    ctx = {"runtime_config_store": store}
    await _poll_runtime_config(ctx, client)
    assert store.current().analysis_progress_interval_sec == 0.25
    assert store.current().cloud_redrive_backoff_sec == (5, 15)
    # Failure retains the same snapshot; a later successful poll withdraws the overrides.
    client.get_config.side_effect = RuntimeError("temporary transport outage")
    await _poll_runtime_config(ctx, client)
    assert store.current().analysis_progress_interval_sec == 0.25
    client.get_config.side_effect = None
    client.get_config.return_value = AgentConfigResponse(overrides={}, digest=compute_overrides_digest({}))
    await _poll_runtime_config(ctx, client)
    assert store.current().analysis_progress_interval_sec == 5.0


@pytest.mark.asyncio
async def test_live_rpm_wakeup_keeps_real_redis_window(policy_store: tuple[RuntimeConfigStore, dict[str, Any]]) -> None:
    store, overrides = policy_store
    client = redis_async.Redis.from_url(os.environ["PHAZE_REDIS_URL"])
    key = "phaze:llm:rpm"
    try:
        await client.set(key, 2, ex=60)
        overrides["llm_max_rpm"] = 2
        assert (await store.reload("api")).successful
        waited = asyncio.Event()
        release = asyncio.Event()

        async def sleep(_seconds: float) -> None:
            waited.set()
            await release.wait()

        task = asyncio.create_task(check_rate_limit(client, lambda: store.current().llm_max_rpm, sleep=sleep))
        await asyncio.wait_for(waited.wait(), timeout=3)
        assert int(await client.get(key)) == 2
        ttl = await client.ttl(key)
        overrides["llm_max_rpm"] = 3
        assert (await store.reload("api")).successful
        release.set()
        await asyncio.wait_for(task, timeout=3)
        assert int(await client.get(key)) == 3
        assert 0 < await client.ttl(key) <= ttl
    finally:
        await client.delete(key)
        await client.aclose()


def test_exact_provider_policy_keys_are_never_stale_or_general_runtime_values() -> None:
    effective, stale = partition_overrides({"provider_trackid_policy": {}, "provider_trackid_api_key": "never-display", "worker_max_jobs": 3})
    assert effective == {"worker_max_jobs": 3}
    assert stale == ("provider_trackid_api_key",)


@pytest.mark.parametrize(
    ("key", "raw", "value"),
    [
        ("llm_model", "model-name", "model-name"),
        ("convention_date_fallback_enabled", "false", False),
        ("analysis_progress_interval_sec", "0.25", 0.25),
        ("cloud_redrive_backoff_sec", "[5, 15]", [5, 15]),
    ],
)
def test_admin_forms_coerce_real_types(key: str, raw: str, value: Any) -> None:
    assert _coerce(key, raw) == value


@pytest.mark.asyncio
async def test_real_analysis_children_keep_their_captured_batch_while_later_children_use_reload(
    policy_store: tuple[RuntimeConfigStore, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from phaze.services.analysis_exec import run_analysis_subprocess
    from tests.analyze._child_stubs import _GATE_MAX_WAIT_SEC

    store, overrides = policy_store
    monkeypatch.chdir(Path(__file__).resolve().parents[3])
    monkeypatch.setenv("PHAZE_ANALYSIS_CHILD_TARGET", "tests.analyze._child_stubs:thread_env_analyze")
    gate = tmp_path / "batch-gate"
    monkeypatch.setenv("PHAZE_STUB_GATE_FILE", str(gate))
    started = asyncio.Event()
    old = asyncio.create_task(
        run_analysis_subprocess(
            "/fake/old.mp3",
            "/fake/models",
            tf_batch_size=store.current().analysis_tf_batch_size,
            heartbeat_cb=lambda *_args: started.set(),
            stall_timeout=30,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=_GATE_MAX_WAIT_SEC)
        overrides["analysis_tf_batch_size"] = 8
        assert (await store.reload("api")).successful
        monkeypatch.delenv("PHAZE_STUB_GATE_FILE")
        new = await run_analysis_subprocess("/fake/new.mp3", "/fake/models", tf_batch_size=store.current().analysis_tf_batch_size)
        assert new["echo"]["tf_batch_size_env"] == "8"
        assert new["echo"]["tunable_batch"] == 8
        assert new["echo"]["fixed_batch"] == 64
        gate.touch()
        old_result = await asyncio.wait_for(old, timeout=_GATE_MAX_WAIT_SEC)
        assert old_result["echo"]["tf_batch_size_env"] == "32"
        assert old_result["echo"]["tunable_batch"] == 32
        assert old_result["echo"]["fixed_batch"] == 64
    finally:
        if not old.done():
            old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_reject_unsafe_transfer_timeout_and_batch_raise_before_swap(policy_store: tuple[RuntimeConfigStore, dict[str, Any]]) -> None:
    store, overrides = policy_store
    previous = store.snapshot()
    for values in ({"push_timeout_sec": 629}, {"push_timeout_sec": 630}, {"cloud_submit_max_attempts": 4}):
        overrides.clear()
        overrides.update(values)
        assert (await store.reload("api")).outcome == "rejected"
        assert store.snapshot() is previous
    agent_store = RuntimeConfigStore(ControlSettings(), runtime_toml=None, override_provider=store._override_provider, physical_cores=lambda: 64)
    overrides.clear()
    overrides["analysis_tf_batch_size"] = 64
    previous = agent_store.snapshot()
    assert (await agent_store.reload("api")).outcome == "rejected"
    assert agent_store.snapshot() is previous
    # A non-analysis API host must not judge a larger agent's native memory ceiling.
    assert (await store.reload("api")).successful


@pytest.mark.asyncio
async def test_live_proposal_models_do_not_mutate_shared_service_or_inflight_parser(
    policy_store: tuple[RuntimeConfigStore, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from phaze.services import proposal

    store, overrides = policy_store
    service = proposal.ProposalService("old-model", "{files_json}", 30)
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: list[str] = []

    async def complete(**kwargs: Any) -> Any:
        observed.append(kwargs["model"])
        if kwargs["model"] == "old-model":
            entered.set()
            await release.wait()
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = '{"proposals": []}'
        response.choices[0].finish_reason = "stop"
        return response

    monkeypatch.setattr(proposal, "acompletion", complete)
    old = asyncio.create_task(service.generate_batch([], model="old-model"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        overrides["llm_model"] = "new-model"
        assert (await store.reload("api")).successful
        await service.generate_batch([], model=effective_settings(ControlSettings()).llm_model)
        assert observed == ["old-model", "new-model"]
        assert service.model == "old-model"
        release.set()
        await asyncio.wait_for(old, timeout=3)
    finally:
        if not old.done():
            old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_watcher_live_timing_preserves_pending_files_and_grace_state(
    policy_store: tuple[RuntimeConfigStore, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from phaze.agent_watcher import debouncer as debouncer_module

    store, overrides = policy_store
    clock = [0.0]
    monkeypatch.setattr(debouncer_module.time, "monotonic", lambda: clock[0])

    def policy() -> tuple[float, float]:
        snapshot = store.current()
        return float(snapshot.watcher_settle_seconds), float(snapshot.watcher_max_pending_seconds)

    debouncer = debouncer_module.Debouncer(policy_provider=policy)
    debouncer.touch("old.mp3")
    overrides.update(watcher_settle_seconds=2, watcher_max_pending_seconds=5)
    assert (await store.reload("api")).successful
    debouncer.touch("new.mp3")
    clock[0] = 3
    assert debouncer.sweep(settle_period=999, max_pending=999) == (["new.mp3"], [])
    assert debouncer.pending_count() == 1
    clock[0] = 6
    debouncer.touch("old.mp3")
    debouncer.touch("grace.mp3")
    clock[0] = 12
    debouncer.touch("grace.mp3")
    assert debouncer.sweep(settle_period=999, max_pending=999) == ([], [])
    assert debouncer._pending["grace.mp3"].cap_grace_used
    overrides.update(watcher_settle_seconds=0, watcher_max_pending_seconds=1)
    assert (await store.reload("api")).successful
    clock[0] = 13.5
    debouncer.touch("grace.mp3")
    # Shrinking the live cap cannot retroactively spend this entry's captured five-second grace.
    assert debouncer.sweep(settle_period=999, max_pending=999) == ([], [])
    clock[0] = 18
    debouncer.touch("grace.mp3")
    ready, evicted = debouncer.sweep(settle_period=999, max_pending=999)
    assert ready == ["old.mp3"]
    assert evicted == ["grace.mp3"]


def test_agent_wire_cannot_coerce_boolean_retry_delay_into_an_integer() -> None:
    with pytest.raises(ValidationError):
        AgentConfigResponse.model_validate_json('{"overrides":{"cloud_redrive_backoff_sec":[true]},"digest":"test"}')


def test_runtime_form_rejects_ambiguous_boolean_without_changing_policy() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="invalid value"):
        _coerce("convention_date_fallback_enabled", "yes")


def test_nonpositive_native_batch_environment_keeps_safe_default() -> None:
    store = RuntimeConfigStore(ControlSettings(), runtime_toml=None, env={"PHAZE_ANALYSIS_TF_BATCH_SIZE": "0"}, check_sizing=False)
    assert store.current().analysis_tf_batch_size == 32
    assert store.snapshot().sources["analysis_tf_batch_size"] == "default"


def test_review_selection_bound_does_not_expand_to_all_url_identifiers() -> None:
    import uuid

    from phaze.routers.shell.stage_context import _parse_selected_ids

    identifiers = [str(uuid.uuid4()) for _ in range(3)]
    assert _parse_selected_ids(",".join(identifiers), limit=2) == identifiers[:2]


@pytest.mark.asyncio
async def test_analysis_admission_refuses_missing_parent_and_drains_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.tasks.functions import _analysis_capacity

    real_current_task = asyncio.current_task
    calls = 0

    def missing_parent_once() -> Any:
        nonlocal calls
        calls += 1
        return None if calls == 1 else real_current_task()

    monkeypatch.setattr(asyncio, "current_task", missing_parent_once)
    limiter = asyncio.Semaphore(1)
    with pytest.raises(RuntimeError, match="requires an asyncio task"):
        async with _analysis_capacity(limiter, MagicMock(), 30):
            pytest.fail("unsafe admission yielded without a parent task")
    assert limiter._value == 1


@pytest.mark.parametrize("startup_timeout", [450, 629])
@pytest.mark.asyncio
async def test_live_transfer_ceiling_uses_serialized_s3_policy_without_revalidating_startup(startup_timeout: int) -> None:
    changes: dict[str, Any] = {}

    async def provider() -> dict[str, Any]:
        return dict(changes)

    settings = AgentSettings(agent_api_url="http://test.invalid", agent_token="test", kind="compute", push_timeout_sec=startup_timeout)  # noqa: S106 -- synthetic credential
    store = RuntimeConfigStore(
        settings, runtime_toml=None, env={"PHAZE_PUSH_TIMEOUT_SEC": str(startup_timeout)}, override_provider=provider, check_sizing=False
    )
    original = store.snapshot()
    assert original.config.push_timeout_sec == startup_timeout
    changes["push_timeout_sec"] = 629
    assert (await store.reload("api")).outcome == "rejected"
    assert store.snapshot() is original
    # The producer uses 600 regardless of this agent's original setting. A 450-second
    # startup baseline may safely increase to 600; a 629-second SSH baseline may lower to it.
    changes["push_timeout_sec"] = 600
    assert (await store.reload("api")).successful
    assert store.current().push_timeout_sec == 600
    assert settings.push_timeout_sec == startup_timeout
    changes.clear()
    assert (await store.reload("api")).successful
    assert store.current().push_timeout_sec == startup_timeout
