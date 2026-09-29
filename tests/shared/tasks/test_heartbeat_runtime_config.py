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

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from phaze.config import ControlSettings
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore
from phaze.schemas.agent_config import AgentConfigResponse, compute_overrides_digest
from phaze.schemas.agent_heartbeat import LAST_RELOAD_ERROR_MAX_LENGTH
from phaze.schemas.agent_identity import AgentIdentity
from phaze.tasks.heartbeat import send_heartbeat


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from phaze.schemas.agent_heartbeat import HeartbeatRequest


_IDENTITY = AgentIdentity(agent_id="test-agent", name="Test Agent", scan_roots=["/data"], created_at=datetime(2026, 1, 1, tzinfo=UTC))


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
    assert payload.effective_config.values.worker_max_jobs == 9
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
