"""phaze-mvq8z.9: the heartbeat loop's runtime-config poll + effective-config reporting.

Every store here is a REAL ``RuntimeConfigStore`` (mirrors ``tests/shared/config/test_runtime_config.py``'s
own construction discipline: a real ``ControlSettings()`` layer, an injected ``physical_cores`` so
sizing verdicts do not depend on the machine running the suite, and no on-disk ``runtime.toml``).
Only the remote HTTP half -- ``PhazeAgentClient.get_config``/``.heartbeat`` -- is a hand-written
stub, because that is the injected seam this bead adds.

``ctx["runtime_config_store"]`` is populated by ``phaze.tasks.agent_worker.startup`` in
production; every test here builds ctx by hand, so absence/presence of that key is exactly what
distinguishes an upgraded agent from one running an older build (the blast-radius case this
module's tests are most concerned with -- see ``test_ctx_without_a_store_skips_the_poll_*``).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
import respx
from structlog.testing import capture_logs

from phaze.config import ControlSettings
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore
from phaze.schemas.agent_config import AgentConfigResponse, compute_overrides_digest
from phaze.schemas.agent_heartbeat import LAST_RELOAD_ERROR_MAX_LENGTH
from phaze.schemas.agent_identity import AgentIdentity
from phaze.services.agent_client import PhazeAgentClient
from phaze.tasks import heartbeat
from phaze.tasks.heartbeat import send_heartbeat
from tests._async_settle import wait_until


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from phaze.schemas.agent_heartbeat import HeartbeatRequest


_IDENTITY = AgentIdentity(agent_id="test-agent", name="Test Agent", scan_roots=["/data"], created_at=datetime(2026, 1, 1, tzinfo=UTC))


_BASE_URL = "http://app.test"
_TOKEN = "phaze_agent_test-token-1234567890abcdef"


async def _no_retry_sleep(_delay: float) -> None:
    return None


def _store(tmp_path: Path, *, cores: int = 64) -> RuntimeConfigStore:
    return RuntimeConfigStore(ControlSettings(), runtime_toml=tmp_path / RUNTIME_TOML_NAME, physical_cores=lambda: cores)


class _StubClient:
    """A hand-written double for PhazeAgentClient's two methods this module drives.

    Deliberately NOT an ``AsyncMock`` for the "happy path" tests below -- an unconfigured
    ``AsyncMock.get_config()`` returns a further Mock whose ``.overrides``/``.digest`` are
    themselves Mocks, which is exactly the shape ``test_bare_asyncmock_client_does_not_crash``
    exists to prove is handled, not a shape to build the positive assertions against.
    """

    def __init__(self, *, overrides: dict[str, Any] | None = None, get_config_error: Exception | None = None) -> None:
        self._overrides = overrides or {}
        self._get_config_error = get_config_error
        self.get_config_calls = 0
        self.heartbeat_calls: list[HeartbeatRequest] = []

    async def get_config(self) -> AgentConfigResponse:
        self.get_config_calls += 1
        if self._get_config_error is not None:
            raise self._get_config_error
        return AgentConfigResponse(overrides=dict(self._overrides), digest=compute_overrides_digest(self._overrides))

    async def heartbeat(self, payload: HeartbeatRequest) -> None:
        self.heartbeat_calls.append(payload)


def _ctx(client: Any, *, store: RuntimeConfigStore | None = None) -> dict[str, Any]:
    ctx: dict[str, Any] = {"api_client": client, "agent_identity": _IDENTITY}
    if store is not None:
        ctx["runtime_config_store"] = store
    return ctx


async def test_an_override_reached_via_the_poll_is_applied_and_reported(tmp_path: Path) -> None:
    """THE acceptance scenario: an override set via the admin API reaches the agent via the poll
    and is applied -- here simulated by the GET /api/internal/agent/config response the real
    endpoint (tests/agents/routers/test_agent_config.py) would hand back once the admin API wrote
    a row, since this module is about the AGENT side of that round trip.
    """
    store = _store(tmp_path)
    await store.reload("startup")
    assert store.current().worker_max_jobs != 9  # precondition: the override actually changes something

    client = _StubClient(overrides={"worker_max_jobs": 9})
    await send_heartbeat(_ctx(client, store=store))

    # Applied locally.
    assert store.current().worker_max_jobs == 9
    assert store.snapshot().sources["worker_max_jobs"] == "override"

    # Reported on the SAME beat.
    assert len(client.heartbeat_calls) == 1
    payload = client.heartbeat_calls[0]
    assert payload.effective_config is not None
    assert payload.effective_config.values["worker_max_jobs"] == 9
    assert payload.effective_config.sources["worker_max_jobs"] == "override"
    assert payload.effective_config.last_reload is not None
    assert payload.effective_config.last_reload.source == "poll"
    assert payload.effective_config.last_reload.outcome == "applied"


async def test_an_unchanged_digest_does_not_trigger_a_second_reload(tmp_path: Path) -> None:
    """`reload("poll")` runs only on a REAL change -- a second tick with the same digest must not
    re-reload (cheap, but the bead's acceptance is explicit: "calls reload('poll') only on
    change"). `RuntimeConfigStore.reload()` always assigns a FRESH `ReloadResult` on every call
    (even an `outcome="unchanged"` one), so this spies on `store.reload` itself rather than
    comparing `store.last_result` by identity -- the latter would pass even if the poll called
    `reload("poll")` on every tick.
    """
    store = _store(tmp_path)
    await store.reload("startup")
    client = _StubClient(overrides={})

    reload_sources: list[str] = []
    original_reload = store.reload

    async def _spy_reload(source: str) -> Any:
        reload_sources.append(source)
        return await original_reload(source)

    store.reload = _spy_reload  # type: ignore[method-assign]

    # ONE ctx reused across both ticks -- exactly like production, where `_heartbeat_loop` calls
    # `send_heartbeat(ctx)` on the SAME dict every iteration. The digest cache this test is
    # proving lives in `ctx["_runtime_config_digest"]`; a fresh ctx per call would trivially
    # "pass" by never having anything to compare against.
    ctx = _ctx(client, store=store)
    await send_heartbeat(ctx)
    await send_heartbeat(ctx)

    assert client.get_config_calls == 2  # polled both ticks...
    assert reload_sources == ["poll"]  # ...but reload() only ran once (the second tick's identical digest skipped it)


async def test_ctx_without_a_store_skips_the_poll_and_omits_effective_config() -> None:
    """An older `agent_worker` build (or a hand-built test ctx) never populates
    `ctx["runtime_config_store"]` -- the poll must be a complete no-op, not merely a degraded one,
    and `effective_config` stays `None` (Optional per HeartbeatRequest's own rolling-deploy
    contract).
    """
    client = _StubClient(overrides={"worker_max_jobs": 9})
    await send_heartbeat(_ctx(client))

    assert client.get_config_calls == 0
    assert len(client.heartbeat_calls) == 1
    assert client.heartbeat_calls[0].effective_config is None


async def test_a_poll_failure_is_swallowed_and_never_blocks_the_beat(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A network blip / auth error / malformed response degrades to "local config unchanged this
    tick" -- logged at WARNING -- and never prevents the heartbeat POST. The store still reports
    its LAST good local state (from `reload("startup")`), not nothing.
    """
    store = _store(tmp_path)
    await store.reload("startup")
    client = _StubClient(get_config_error=RuntimeError("network blip"))

    with caplog.at_level("WARNING", logger="phaze.tasks.heartbeat"):
        await send_heartbeat(_ctx(client, store=store))

    assert len(client.heartbeat_calls) == 1
    payload = client.heartbeat_calls[0]
    assert payload.effective_config is not None
    assert payload.effective_config.last_reload is not None
    assert payload.effective_config.last_reload.source == "startup"  # the poll never landed
    assert any("runtime-config poll failed" in r.message for r in caplog.records)


async def test_bare_asyncmock_client_does_not_crash_the_poll(tmp_path: Path) -> None:
    """Regression guard for this bead's own blast radius: every EXISTING heartbeat test
    (tests/shared/tasks/test_heartbeat_loop.py and siblings) builds `ctx["api_client"]` as a bare
    `unittest.mock.AsyncMock()` with no `get_config` configured. `AsyncMock().get_config()` returns
    a further Mock whose `.overrides`/`.digest` are themselves Mocks -- `dict(mock.overrides)`
    raises `TypeError` -- and this must degrade exactly like any other poll failure, never crash
    `send_heartbeat` or block the heartbeat POST those tests assert on.
    """
    store = _store(tmp_path)
    await store.reload("startup")
    client = AsyncMock()

    await send_heartbeat(_ctx(client, store=store))  # must not raise

    assert client.heartbeat.await_count == 1


async def test_a_long_rejected_reload_error_still_produces_a_heartbeat(tmp_path: Path) -> None:
    """phaze-mvq8z.19 finding 2: a reload error longer than the wire bound must never stop a beat.

    Reload errors are unbounded (every unknown key is listed by name), while
    ``EffectiveConfigLastReload.error`` keeps its server-side ``max_length``. Built from the raw
    error, the payload fails validation BEFORE ``client.heartbeat`` is called, so a config typo would
    turn into a liveness outage. The agent truncates to the bound instead.
    """
    store = _store(tmp_path)
    (tmp_path / RUNTIME_TOML_NAME).write_text("".join(f"not_a_real_key_{index:03d} = 1\n" for index in range(60)), encoding="utf-8")
    result = await store.reload("file")
    assert result.outcome == "rejected"
    assert len(result.error or "") > LAST_RELOAD_ERROR_MAX_LENGTH, "the fixture must actually exceed the wire bound"
    client = _StubClient()

    await send_heartbeat(_ctx(client, store=store))

    assert len(client.heartbeat_calls) == 1, "the beat was never sent"
    last_reload = client.heartbeat_calls[0].effective_config.last_reload  # type: ignore[union-attr]
    assert last_reload is not None
    assert last_reload.outcome == "rejected"
    assert last_reload.error is not None
    assert len(last_reload.error) <= LAST_RELOAD_ERROR_MAX_LENGTH
    assert last_reload.error.startswith("unknown key(s): not_a_real_key_000"), "truncation must keep the head of the error"


class _PreEffectiveConfigHeartbeatRequest(BaseModel):
    """The heartbeat schema an OLD control plane validates against -- ``HeartbeatRequest`` exactly as
    it stood before phaze-mvq8z.9 added ``effective_config`` (``extra="forbid"`` included)."""

    model_config = ConfigDict(extra="forbid")

    agent_version: str
    worker_pid: int
    queue_depth: int
    lane: str | None = None


def _old_control_plane(accepted: list[dict[str, Any]]) -> Any:
    """A respx side effect standing in for an old control plane's heartbeat route: 422 on any body
    its schema rejects (FastAPI's behaviour), 204 -- recording the body -- on one it accepts."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        try:
            _PreEffectiveConfigHeartbeatRequest.model_validate(body)
        except ValidationError as exc:
            return httpx.Response(422, json={"detail": exc.errors(include_url=False, include_context=False)})
        accepted.append(body)
        return httpx.Response(204)

    return _handler


@respx.mock
async def test_a_new_agent_still_beats_against_a_control_plane_that_rejects_effective_config(tmp_path: Path) -> None:
    """phaze-mvq8z.20 finding 1, agent half: agents may upgrade before the control plane. An old
    control plane 422s every beat carrying ``effective_config``; the agent must fall back to the
    core beat so ``last_seen_at`` keeps moving -- the optional snapshot can never cost liveness.

    The real ``PhazeAgentClient`` is driven over HTTP (respx) so the 422 travels the real
    ``_request`` funnel into the real exception class ``send_heartbeat`` has to handle.
    """
    store = _store(tmp_path)
    await store.reload("startup")
    accepted: list[dict[str, Any]] = []
    respx.get(f"{_BASE_URL}/api/internal/agent/config").mock(return_value=httpx.Response(404))
    route = respx.post(f"{_BASE_URL}/api/internal/agent/heartbeat").mock(side_effect=_old_control_plane(accepted))

    client = PhazeAgentClient(base_url=_BASE_URL, token=_TOKEN, timeout=5.0, _retry_sleep=_no_retry_sleep)
    try:
        await send_heartbeat(_ctx(client, store=store))
    finally:
        await client.close()

    assert len(accepted) == 1, "the old control plane never accepted a beat -- the agent would read stale, then dead"
    assert "effective_config" not in accepted[0]
    assert route.call_count == 2  # the full beat (422), then the core beat


@respx.mock
async def test_a_beat_without_effective_config_omits_the_key_for_an_old_control_plane() -> None:
    """Found while writing the test above: ``PhazeAgentClient.heartbeat`` dumped an absent snapshot
    as ``"effective_config": null``, which an old control plane's ``extra="forbid"`` rejects exactly
    like a populated one -- so even a beat with NO snapshot (and the fallback beat) 422'd. An absent
    snapshot must be absent from the body, and the old control plane must accept it first time."""
    accepted: list[dict[str, Any]] = []
    route = respx.post(f"{_BASE_URL}/api/internal/agent/heartbeat").mock(side_effect=_old_control_plane(accepted))

    client = PhazeAgentClient(base_url=_BASE_URL, token=_TOKEN, timeout=5.0, _retry_sleep=_no_retry_sleep)
    try:
        await send_heartbeat(_ctx(client))
    finally:
        await client.close()

    assert route.call_count == 1
    assert len(accepted) == 1
    assert "effective_config" not in accepted[0]


@respx.mock
async def test_a_rejected_beat_without_effective_config_is_not_re_sent(caplog: pytest.LogCaptureFixture) -> None:
    """The fallback exists only to shed the optional snapshot: a 4xx on a beat that carries none has
    nothing to shed, so it is reported once and NOT re-sent (it would fail identically)."""
    route = respx.post(f"{_BASE_URL}/api/internal/agent/heartbeat").mock(return_value=httpx.Response(422, json={"detail": []}))

    client = PhazeAgentClient(base_url=_BASE_URL, token=_TOKEN, timeout=5.0, _retry_sleep=_no_retry_sleep)
    try:
        with caplog.at_level("WARNING", logger="phaze.tasks.heartbeat"):
            await send_heartbeat(_ctx(client))
    finally:
        await client.close()

    assert route.call_count == 1
    assert any("heartbeat failed:" in r.message for r in caplog.records)
    assert not any("re-sending the beat without it" in r.message for r in caplog.records)


async def test_a_hanging_config_poll_does_not_prevent_the_heartbeat_post(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-mvq8z.20 finding 4: the poll runs inside ``send_heartbeat`` under the loop's per-beat
    bound (``BEAT_TIMEOUT_SECONDS``), and the client allowed it 30 s x 3 attempts -- so a GET that
    hangs had the beat cancelled BEFORE the heartbeat POST. The poll now has its own short bound.

    Deterministic: the GET never returns at all (an Event nobody sets), so no ratio of timeouts
    decides the outcome -- only whether the poll is bounded separately from the beat. The outer
    ``wait_for`` is the loop's own production bound, unpatched.
    """
    monkeypatch.setattr(heartbeat, "CONFIG_POLL_TIMEOUT_SECONDS", 0.01, raising=False)
    store = _store(tmp_path)
    await store.reload("startup")

    class _HangingPollClient(_StubClient):
        async def get_config(self) -> AgentConfigResponse:
            self.get_config_calls += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

    client = _HangingPollClient()
    await asyncio.wait_for(send_heartbeat(_ctx(client, store=store)), timeout=heartbeat.BEAT_TIMEOUT_SECONDS)

    assert client.get_config_calls == 1
    assert len(client.heartbeat_calls) == 1, "a hanging config poll cost the heartbeat POST"
    assert client.heartbeat_calls[0].effective_config is not None  # still reports the last good local state


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_the_config_poll_loop_applies_an_override_without_sending_a_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-mvq8z.21: a heartbeat-disabled worker's own poll loop delivers the override -- and POSTs nothing."""
    monkeypatch.setattr(heartbeat, "AGENT_HEARTBEAT_INTERVAL_SECONDS", 0)
    store = _store(tmp_path)
    await store.reload("startup")
    assert store.current().worker_max_jobs != 9  # precondition: the override actually changes something
    client = _StubClient(overrides={"worker_max_jobs": 9})
    task = asyncio.create_task(heartbeat._config_poll_loop(_ctx(client, store=store)))
    try:
        await wait_until(lambda: store.current().worker_max_jobs == 9, description="the polled override being applied")
        await wait_until(lambda: client.get_config_calls >= 2, description="a second poll tick")
    finally:
        await _cancel(task)

    assert store.snapshot().sources["worker_max_jobs"] == "override"
    assert store.last_result is not None
    assert store.last_result.source == "poll"
    assert client.heartbeat_calls == []


async def test_a_failed_config_poll_iteration_does_not_kill_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-mvq8z.21: like the heartbeat loop, one raising iteration is logged and the next tick still runs."""
    monkeypatch.setattr(heartbeat, "AGENT_HEARTBEAT_INTERVAL_SECONDS", 0)
    calls = 0

    async def _flaky_poll(_ctx: dict[str, Any], _client: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("one bad tick")

    monkeypatch.setattr(heartbeat, "_poll_runtime_config", _flaky_poll)
    task = asyncio.create_task(heartbeat._config_poll_loop({}))
    try:
        await wait_until(lambda: calls >= 2, description="the tick after a raising one")
    finally:
        await _cancel(task)
    assert task.cancelled()


# phaze-mvq8z.22 finding 2: version skew between a NEWER control plane and an OLDER agent. The
# endpoint filters by the SERVER's reloadable keys, so an agent can be handed a key its own build
# does not know. That key must be ignored and reported -- not reject the whole reload -- and a
# reload that IS rejected must not be recorded as applied, or the agent never retries it.

_KEY_FROM_A_NEWER_CONTROL_PLANE = "a_key_from_a_newer_control_plane"


def _veto_once(store: RuntimeConfigStore) -> list[int]:
    """Register a validator that rejects the FIRST candidate it sees and accepts every later one -- a
    transient veto (phaze-mvq8z.8's in-flight ``cloud_job`` check is the production shape)."""
    seen: list[int] = []

    def _validator(config: Any) -> None:
        seen.append(config.worker_max_jobs)
        if len(seen) == 1:
            raise RuntimeError("transiently vetoed")

    store.register_validator("veto_once", _validator)
    return seen


async def test_an_override_key_this_agent_does_not_know_is_ignored_and_the_rest_apply(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.reload("startup")
    client = _StubClient(overrides={"worker_max_jobs": 9, "log_level": "DEBUG", _KEY_FROM_A_NEWER_CONTROL_PLANE: 3})
    ctx = _ctx(client, store=store)

    with capture_logs() as logs:
        await send_heartbeat(ctx)

    assert store.last_result is not None
    assert store.last_result.outcome == "applied", store.last_result.error
    assert (store.current().worker_max_jobs, store.current().log_level) == (9, "DEBUG")
    ignored = [entry for entry in logs if entry.get("event") == "heartbeat: ignoring runtime-config override keys this agent does not know"]
    assert [entry["ignored"] for entry in ignored] == [[_KEY_FROM_A_NEWER_CONTROL_PLANE]]
    reported = client.heartbeat_calls[0].effective_config
    assert reported is not None
    assert reported.values["worker_max_jobs"] == 9


async def test_a_rejected_reload_is_retried_on_the_next_poll_with_the_same_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.reload("startup")
    seen = _veto_once(store)
    client = _StubClient(overrides={"worker_max_jobs": 9})
    ctx = _ctx(client, store=store)

    await send_heartbeat(ctx)
    assert store.last_result is not None
    assert store.last_result.outcome == "rejected"
    assert store.current().worker_max_jobs != 9

    await send_heartbeat(ctx)  # the SAME digest: a rejected set was never applied, so it is retried

    assert seen == [9, 9]
    assert store.last_result.outcome == "applied"
    assert store.current().worker_max_jobs == 9
    assert client.heartbeat_calls[-1].effective_config.values["worker_max_jobs"] == 9  # type: ignore[union-attr]


async def test_the_config_poll_loop_ignores_an_unknown_key_and_retries_a_rejected_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The heartbeat-disabled worker's loop (phaze-mvq8z.21) shares the poll, and so both fixes."""
    monkeypatch.setattr(heartbeat, "AGENT_HEARTBEAT_INTERVAL_SECONDS", 0)
    store = _store(tmp_path)
    await store.reload("startup")
    seen = _veto_once(store)
    client = _StubClient(overrides={"worker_max_jobs": 9, _KEY_FROM_A_NEWER_CONTROL_PLANE: 3})
    task = asyncio.create_task(heartbeat._config_poll_loop(_ctx(client, store=store)))
    try:
        await wait_until(lambda: store.current().worker_max_jobs == 9, description="the vetoed override applied on a later tick")
    finally:
        await _cancel(task)

    assert seen[:2] == [9, 9]
    assert store.snapshot().sources["worker_max_jobs"] == "override"
    assert client.heartbeat_calls == []


async def test_withdrawing_a_rejected_set_back_to_the_one_in_force_is_still_reloaded(tmp_path: Path) -> None:
    """The withdrawn set's digest is the one already applied -- but the provider still holds the rejected
    set, so skipping the reload on that digest would leave the next SIGHUP re-rejecting a withdrawn value."""
    store = _store(tmp_path)
    await store.reload("startup")

    def _refuse_ten(config: Any) -> None:
        if config.worker_max_jobs == 10:
            raise RuntimeError("ten is refused")

    store.register_validator("refuse_ten", _refuse_ten)
    ctx = _ctx(_StubClient(overrides={"worker_max_jobs": 9}), store=store)
    await send_heartbeat(ctx)
    assert store.current().worker_max_jobs == 9
    ctx["api_client"] = _StubClient(overrides={"worker_max_jobs": 10})
    await send_heartbeat(ctx)
    assert store.last_result is not None
    assert store.last_result.outcome == "rejected"

    ctx["api_client"] = _StubClient(overrides={"worker_max_jobs": 9})  # withdrawn: the digest already applied
    await send_heartbeat(ctx)

    assert (store.last_result.source, store.last_result.outcome) == ("poll", "unchanged")
    assert (await store.reload("sighup")).outcome == "unchanged", "the provider still held the withdrawn, rejected set"
    assert store.current().worker_max_jobs == 9


# phaze-dhycx: a PERMANENTLY rejected set is retried with backoff, not every tick forever.


async def test_a_permanently_rejected_set_is_retried_with_bounded_attempts_and_keeps_last_reload_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.reload("startup")
    attempts: list[int] = []

    def _always_refuse(config: Any) -> None:
        attempts.append(config.worker_max_jobs)
        raise RuntimeError("permanently refused")

    store.register_validator("always_refuse", _always_refuse)
    client = _StubClient(overrides={"worker_max_jobs": 9, _KEY_FROM_A_NEWER_CONTROL_PLANE: 3})
    ctx = _ctx(client, store=store)

    with capture_logs() as logs:
        await send_heartbeat(ctx)
        assert store.last_result is not None
        first_at = store.last_result.at
        ticks = 200
        for _ in range(ticks - 1):
            await send_heartbeat(ctx)

    # Attempt 1, then attempt 2 on the next tick, then gaps of 1, 2, 4, ... up to the cap.
    assert 2 < len(attempts) < ticks // 5
    ignored = [entry for entry in logs if entry.get("event") == "heartbeat: ignoring runtime-config override keys this agent does not know"]
    assert len(ignored) == len(attempts), "the ignored-keys warning is repeated per real attempt, not per tick"
    assert heartbeat._rejection_backoff_ticks(1) == 0
    assert [heartbeat._rejection_backoff_ticks(n) for n in (2, 3, 4, 5)] == [1, 2, 4, 8]
    assert heartbeat._rejection_backoff_ticks(50) == heartbeat.RUNTIME_CONFIG_REJECT_BACKOFF_MAX_TICKS
    # Every beat still went out, and the rejection's timestamp is from the attempt, not the last tick.
    assert len(client.heartbeat_calls) == ticks
    last_reload = client.heartbeat_calls[-1].effective_config.last_reload  # type: ignore[union-attr]
    assert last_reload is not None
    assert last_reload.outcome == "rejected"
    assert first_at <= last_reload.at
    assert store.last_result.at == last_reload.at


async def test_a_changed_set_is_applied_promptly_while_another_is_backing_off(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.reload("startup")

    def _refuse_nine(config: Any) -> None:
        if config.worker_max_jobs == 9:
            raise RuntimeError("nine is refused")

    store.register_validator("refuse_nine", _refuse_nine)
    ctx = _ctx(_StubClient(overrides={"worker_max_jobs": 9}), store=store)
    for _ in range(10):  # deep into the backoff
        await send_heartbeat(ctx)
    assert store.last_result is not None
    assert store.last_result.outcome == "rejected"

    ctx["api_client"] = _StubClient(overrides={"worker_max_jobs": 11})
    await send_heartbeat(ctx)  # the very next tick: a different digest never waits out another's backoff

    assert store.last_result.outcome == "applied"
    assert store.current().worker_max_jobs == 11
    assert "_runtime_config_rejected" not in ctx
