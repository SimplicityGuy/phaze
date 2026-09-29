"""phaze-mvq8z.11: retune a RUNNING agent worker through every trigger, with jobs in flight, end to end.

The epic's acceptance (``phaze-mvq8z``, carried from ``phaze-y6af8``), which this module discharges:

1. an allowed knob changed on a running agent takes effect for NEW work, without restarting the
   process and without dropping in-flight jobs;
2. ``worker_process_pool_size`` can be raised and lowered live, a shrink draining by attrition;
3. an invalid config is rejected with the previous config retained and a clear error;
4. the effective config is visible (logs + heartbeat), and a non-reloadable key is reported as
   requiring restart.

Nothing on the path under test is a stand-in (CLAUDE.md, verification-fidelity rule 3):

* the agent is a separate OS process started by SAQ's own CLI on the production settings module
  (``_retune_agent_harness.py`` appends ONE gated job function to it and shortens the heartbeat
  cadence -- see that module);
* SAQ is the installed ``saq.Worker`` on a real ``PostgresQueue`` on the seat's database, adopted as
  a ``LiveConcurrencyWorker`` by the production startup hook;
* the control plane is the real app (``create_app()``) served by uvicorn on a real TCP port, against
  committed rows in the same database: ``/whoami``, the heartbeat, ``GET /api/internal/agent/config``
  and ``/admin/runtime-config`` are the production routes;
* the three triggers are the production ones: a PollingObserver directory watch on a real
  ``runtime.toml`` edit, a real ``SIGHUP`` to the worker's pid, and an admin-API override written to
  the DB and carried to the agent by the heartbeat-cadence config poll.

The job is test code, and deliberately so: it holds ``ctx["analysis_semaphore"]`` -- the limiter
``process_file`` holds around every analysis child -- and parks until the test drops a release file.
Each knob is observed separately through it: a job that has STARTED occupies one of the lane's SAQ
job loops (``lane_analyze_concurrency``); a job that HOLDS is inside the analysis pool
(``worker_process_pool_size``).

Determinism. Every wait is on a condition, bounded only by the shared child-process hang guard. The
"never over-admits after a shrink" claim is not a wait-and-see: a job cannot finish until the test
releases it, and the test releases only jobs it has seen start, so an over-admitted job is ALWAYS
in the events log overlapping another -- :func:`_assert_admission_bounded` reads that off the log
after the drain, whatever the timing was.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import certifi
import httpx
import pytest
import pytest_asyncio
from saq.queue.postgres import PostgresQueue
from sqlalchemy import select, text
import uvicorn

from phaze.database import get_session
from phaze.main import create_app
from phaze.models.agent import Agent
from phaze.models.runtime_config_override import RuntimeConfigOverride
from phaze.runtime_config import get_runtime_config_store
from phaze.scripts.download_models import MANIFEST
from phaze.services.runtime_config_overrides import get_runtime_config_overrides
from tests._async_settle import wait_until
from tests._child_process_budget import CHILD_PROCESS_HANG_GUARD_SEC
from tests._uvicorn_leaks import contained_uvicorn_logging
from tests.db_guard import integration_dsns


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


pytestmark = pytest.mark.integration

BROKER_DSN, _ = integration_dsns()
_REPO_ROOT = Path(__file__).resolve().parents[2]

AGENT_ID = "retune-agent"
LANE = "analyze"
QUEUE_NAME = f"phaze-agent-{AGENT_ID}-{LANE}"
JOBS = tuple(f"job-{index}" for index in range(1, 7))

#: Start-time (env layer) sizing of the agent: two analyze loops, a one-slot analysis pool.
START = {"lane_analyze_concurrency": 2, "worker_process_pool_size": 1}
RAISED = {"lane_analyze_concurrency": 4, "worker_process_pool_size": 3}
LOWERED = {"lane_analyze_concurrency": 1, "worker_process_pool_size": 1}
#: The agent is a small host and the control plane a big one, through the production core-count
#: override (``PHAZE_ANALYSIS_PHYSICAL_CORES``) rather than whatever this machine has, so the
#: sizing verdicts below are the same everywhere. RAISED needs 6 cores on the agent (3 children x
#: 2 threads): it fits. The threads are the agent's env pin, which the reloadable layers can lower
#: but never raise (phaze-mvq8z.22), so every oversubscription below comes from more CHILDREN.
AGENT_CORES = 6
AGENT_THREADS = 2
CONTROL_CORES = 16

#: Every wait below ends the moment its condition holds; only a wedged worker ever reaches this.
HANG_GUARD = CHILD_PROCESS_HANG_GUARD_SEC
#: How often the test re-reads what the worker process has written. A cadence, not a margin.
POLL = 0.05
#: The agent's heartbeat (and so admin-override poll) cadence, via the harness. Production: 30 s.
HEARTBEAT_INTERVAL = 0.25

#: A restart-only key offered through a reloadable layer, with a value that must never be echoed:
#: several restart-only keys carry credentials, so the rejection names the key and nothing else.
RESTART_ONLY_KEY = "queue_url"
RESTART_ONLY_VALUE = "postgresql://phaze:not-a-real-secret@elsewhere.invalid/phaze"


# The control plane: the real app on a real port


@dataclass
class ControlPlane:
    base_url: str
    agent_token: str
    session_factory: async_sessionmaker[AsyncSession]

    async def overrides(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            rows = (await session.execute(select(RuntimeConfigOverride))).scalars().all()
            return {row.key: row.value for row in rows}


class _NoSignalServer(uvicorn.Server):
    """uvicorn inside the pytest process: leave the process's SIGINT/SIGTERM handlers alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Any:
        yield


@pytest_asyncio.fixture
async def control_plane(
    committed_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ControlPlane]:
    """Seed the agent, wire this process's runtime-config store to the DB, serve ``create_app()``."""
    _engine, session_factory = committed_db
    raw_token = "phaze_agent_" + secrets.token_urlsafe(32)
    async with session_factory() as session:
        session.add(
            Agent(id=AGENT_ID, name=AGENT_ID, kind="fileserver", token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), scan_roots=["/var/empty"])
        )
        await session.commit()

    # The admin API validates a candidate override against THIS process's store before storing it,
    # the way the api process does. Sized explicitly, like the agent, so its verdicts are host-free.
    monkeypatch.setenv("PHAZE_ANALYSIS_PHYSICAL_CORES", str(CONTROL_CORES))
    monkeypatch.setenv("TF_NUM_INTRAOP_THREADS", "1")
    monkeypatch.setenv("OMP_NUM_THREADS", "1")
    get_runtime_config_store.cache_clear()

    async def _db_overrides() -> dict[str, Any]:
        async with session_factory() as session:
            return await get_runtime_config_overrides(session)

    get_runtime_config_store().set_override_provider(_db_overrides)

    async def _session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _session

    # uvicorn.Config leaves process-global logging state behind (logger levels, a TRACE level
    # name); contained so no later test in this process sees it (tests/_uvicorn_leaks.py).
    with contained_uvicorn_logging():
        server = _NoSignalServer(uvicorn.Config(app, log_level="warning", lifespan="off"))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await wait_until(lambda: server.started or serving.done(), timeout=HANG_GUARD, description="uvicorn serving", interval=POLL)
            assert not serving.done(), serving
            yield ControlPlane(base_url=f"http://127.0.0.1:{port}", agent_token=raw_token, session_factory=session_factory)
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, HANG_GUARD)
            with contextlib.suppress(OSError):
                sock.close()
            get_runtime_config_store.cache_clear()


# The agent: the production worker, as its own process


@dataclass
class Observed:
    """What the running agent has made visible: its job events, its heartbeat, its queue rows."""

    events: list[tuple[str, str]]
    lane_status: dict[str, Any] | None
    statuses: dict[str, int]

    def names(self, kind: str) -> set[str]:
        return {name for event, name in self.events if event == kind}

    @property
    def started(self) -> set[str]:
        return self.names("start")

    @property
    def holding(self) -> set[str]:
        return self.names("hold") - self.names("done")

    @property
    def done(self) -> set[str]:
        return self.names("done")

    @property
    def effective(self) -> dict[str, Any] | None:
        return None if self.lane_status is None else self.lane_status.get("effective_config")


class AgentProcess:
    """One real agent worker process, and the test's view of it."""

    def __init__(self, tmp_path: Path, control: ControlPlane, *, debounce_seconds: float) -> None:
        self.control = control
        self.runtime_dir = tmp_path / "runtime"
        self.stage_dir = tmp_path / "stage"
        self.probe_dir = tmp_path / "probe"
        self.log_path = tmp_path / "agent.log"
        models_dir = tmp_path / "models"
        for directory in (self.runtime_dir, self.stage_dir, self.probe_dir, models_dir):
            directory.mkdir()
        # The production model check stats every manifest file for its pinned size; sparse files
        # satisfy it without writing a byte of weights. Nothing here ever loads a model.
        for name, size in MANIFEST.items():
            with (models_dir / name).open("wb") as handle:
                handle.truncate(size)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "PYTHONUNBUFFERED": "1",
            "PHAZE_ROLE": "agent",
            "PHAZE_AGENT_API_URL": control.base_url,
            "PHAZE_AGENT_TOKEN": control.agent_token,
            "PHAZE_AGENT_QUEUE": f"phaze-agent-{AGENT_ID}",
            "PHAZE_AGENT_LANE": LANE,
            "PHAZE_AGENT_SCAN_ROOTS": str(tmp_path),
            "PHAZE_AGENT_CA_FILE": certifi.where(),
            "PHAZE_QUEUE_URL": BROKER_DSN,
            "PHAZE_REDIS_URL": os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0"),
            "MODELS_PATH": str(models_dir),
            "PHAZE_LOG_JSON": "true",
            "PHAZE_LOG_LEVEL": "INFO",
            "PHAZE_RUNTIME_CONFIG_DIR": str(self.runtime_dir),
            "PHAZE_RUNTIME_CONFIG_WATCH_POLLING": "true",
            "PHAZE_RUNTIME_CONFIG_WATCH_POLL_INTERVAL_SECONDS": "0.1",
            "PHAZE_RUNTIME_CONFIG_WATCH_DEBOUNCE_SECONDS": str(debounce_seconds),
            "PHAZE_LANE_ANALYZE_CONCURRENCY": str(START["lane_analyze_concurrency"]),
            "WORKER_PROCESS_POOL_SIZE": str(START["worker_process_pool_size"]),
            "WORKER_MAX_JOBS": "8",
            "PHAZE_ANALYSIS_PHYSICAL_CORES": str(AGENT_CORES),
            "TF_NUM_INTRAOP_THREADS": str(AGENT_THREADS),
            "OMP_NUM_THREADS": str(AGENT_THREADS),
            # Read by _retune_agent_harness (not imported here: importing it builds the agent's queue).
            "PHAZE_RETUNE_PROBE_DIR": str(self.probe_dir),
            "PHAZE_RETUNE_HEARTBEAT_INTERVAL_SEC": str(HEARTBEAT_INTERVAL),
        }
        self.proc: subprocess.Popen[bytes] | None = None
        self.queue = PostgresQueue.from_url(BROKER_DSN, name=QUEUE_NAME)
        self.observed = Observed(events=[], lane_status=None, statuses={})
        self._refresher: asyncio.Task[None] | None = None

    # lifecycle

    async def start(self) -> None:
        await self.queue.connect()
        await self._delete_rows()
        with self.log_path.open("wb") as log:
            self.proc = subprocess.Popen(  # trusted input: literal sys.executable, SAQ's CLI, our own module
                [sys.executable, "-m", "saq", "tests.integration._retune_agent_harness.settings"],
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=_REPO_ROOT,
                env=self.env,
            )
        self._refresher = asyncio.create_task(self._refresh_forever())
        await self.until(lambda o: o.effective is not None, "the agent's first heartbeat carries its effective config")

    async def stop(self) -> None:
        for name in JOBS:
            (self.probe_dir / f"{name}.release").touch()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(self.proc.wait, CHILD_PROCESS_HANG_GUARD_SEC)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                await asyncio.to_thread(self.proc.wait, CHILD_PROCESS_HANG_GUARD_SEC)
        if self._refresher is not None:
            self._refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresher
        with contextlib.suppress(Exception):
            await self._delete_rows()
        await self.queue.disconnect()

    @property
    def pid(self) -> int:
        assert self.proc is not None
        return self.proc.pid

    # driving it

    async def enqueue(self, *names: str) -> None:
        for name in names:
            # timeout=0: SAQ's default 10 s job timeout would cancel a job the test is holding.
            await self.queue.enqueue("retune_probe", key=f"{QUEUE_NAME}:{name}", name=name, timeout=0)

    def release(self, names: set[str]) -> None:
        for name in names:
            (self.probe_dir / f"{name}.release").touch()

    def write_runtime_toml(self, values: dict[str, Any]) -> None:
        """The safe-write convention: stage the file OUTSIDE the watched directory, rename it in."""
        body = "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items())
        staged = self.stage_dir / "runtime.toml"
        staged.write_text(body, encoding="utf-8")
        staged.replace(self.runtime_dir / "runtime.toml")

    # observing it

    async def until(self, predicate: Callable[[Observed], bool], description: str) -> None:
        def _check() -> bool:
            if self._refresher is not None and self._refresher.done():
                self._refresher.result()  # re-raise whatever stopped the refresher
                raise AssertionError(f"the observation refresher stopped waiting for: {description}")
            if self.proc is not None and self.proc.poll() is not None:
                raise AssertionError(f"the agent worker exited (code {self.proc.returncode}) waiting for: {description}\n{self.log_tail()}")
            return predicate(self.observed)

        try:
            await wait_until(_check, timeout=HANG_GUARD, description=description, interval=POLL)
        except AssertionError as exc:
            raise AssertionError(f"{exc}\nobserved: {self.observed}\n{self.log_tail()}") from exc

    def log_tail(self, lines: int = 60) -> str:
        text_ = self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""
        return "agent log tail:\n" + "\n".join(text_.splitlines()[-lines:])

    def reload_log_lines(self) -> list[dict[str, Any]]:
        """The agent's own ``phaze.runtime_config reload`` audit lines (ADR §6), parsed."""
        records = []
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                if isinstance(record, dict) and record.get("event") == "phaze.runtime_config reload":
                    records.append(record)
        return records

    async def _refresh_forever(self) -> None:
        while True:
            events_file = self.probe_dir / "events.log"
            events = []
            if events_file.exists():
                events = [tuple(line.split(" ", 1)) for line in events_file.read_text(encoding="utf-8").splitlines() if " " in line]
            async with self.control.session_factory() as session:
                last_status = (await session.execute(select(Agent.last_status).where(Agent.id == AGENT_ID))).scalar_one_or_none()
                rows = await session.execute(
                    text("SELECT status, count(*) FROM saq_jobs WHERE queue = :queue GROUP BY status"), {"queue": QUEUE_NAME}
                )
                statuses = {str(status): int(count) for status, count in rows.all()}
            lane_status = (last_status or {}).get("lanes", {}).get(LANE)
            self.observed = Observed(events=events, lane_status=lane_status, statuses=statuses)
            await asyncio.sleep(POLL)

    async def _delete_rows(self) -> None:
        async with self.queue.pool.connection() as conn:
            await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (QUEUE_NAME,))


@pytest_asyncio.fixture
async def launch_agent(control_plane: ControlPlane, tmp_path: Path) -> AsyncIterator[Callable[..., Awaitable[AgentProcess]]]:
    agents: list[AgentProcess] = []

    async def _launch(*, debounce_seconds: float = 0.1) -> AgentProcess:
        agent = AgentProcess(tmp_path, control_plane, debounce_seconds=debounce_seconds)
        agents.append(agent)
        await agent.start()
        return agent

    try:
        yield _launch
    finally:
        for agent in agents:
            await agent.stop()


# Triggers


@dataclass
class Trigger:
    """One way of changing the agent's reloadable config, and how the agent should report it."""

    #: The layer the heartbeat must name as the source of a value this trigger set.
    layer: str
    #: The ``last_reload.source`` the heartbeat must name for a reload this trigger caused.
    reload_source: str
    apply: Callable[[dict[str, Any]], Awaitable[None]]


def file_trigger(agent: AgentProcess) -> Trigger:
    async def _apply(values: dict[str, Any]) -> None:
        agent.write_runtime_toml(values)

    return Trigger(layer="file", reload_source="file", apply=_apply)


def sighup_trigger(agent: AgentProcess) -> Trigger:
    async def _apply(values: dict[str, Any]) -> None:
        agent.write_runtime_toml(values)
        os.kill(agent.pid, signal.SIGHUP)

    return Trigger(layer="file", reload_source="sighup", apply=_apply)


def api_trigger(control: ControlPlane) -> Trigger:
    async def _apply(values: dict[str, Any]) -> None:
        async with httpx.AsyncClient(base_url=control.base_url) as client:
            for key, value in values.items():
                response = await client.post(f"/admin/runtime-config/{key}", data={"value": str(value)})
                assert response.status_code == 200, (key, value, response.status_code, response.text)

    return Trigger(layer="override", reload_source="poll", apply=_apply)


# The scenario every trigger runs


def _in_force(observed: Observed, values: dict[str, Any], *, layer: str, reload_source: str) -> bool:
    """Does the heartbeat report ``values`` in force, from ``layer``, after a ``reload_source`` reload?"""
    effective = observed.effective
    if effective is None or effective["last_reload"] is None:
        return False
    return (
        all(effective["values"][key] == value for key, value in values.items())
        and all(effective["sources"][key] == layer for key in values)
        and effective["last_reload"]["source"] == reload_source
        and effective["last_reload"]["outcome"] == "applied"
    )


def _rejected_with(observed: Observed, fragment: str, *, reload_source: str) -> bool:
    effective = observed.effective
    if effective is None or effective["last_reload"] is None:
        return False
    last = effective["last_reload"]
    return last["source"] == reload_source and last["outcome"] == "rejected" and fragment in (last["error"] or "")


def _assert_last_good_retained(agent: AgentProcess, trigger: Trigger, frozen: Observed) -> None:
    """After a rejection: the RAISED values still in force from the trigger's layer, and no job disturbed."""
    effective = agent.observed.effective
    assert effective is not None
    assert {key: effective["values"][key] for key in RAISED} == RAISED
    assert {key: effective["sources"][key] for key in RAISED} == dict.fromkeys(RAISED, trigger.layer)
    assert agent.observed.started == frozen.started
    assert agent.observed.holding == frozen.holding
    assert agent.observed.done == set()


def _assert_admission_bounded(events: list[tuple[str, str]], marker: int, target: dict[str, int]) -> None:
    """After the shrink took effect (``events[marker:]``), nothing was admitted over the new target.

    Replays the log: ``running`` is started-and-not-done (a SAQ job loop is occupied), ``holding``
    is held-and-not-done (an analysis-pool slot is occupied). Every START after the marker must see
    at most ``lane_analyze_concurrency`` running including itself, every HOLD at most
    ``worker_process_pool_size`` holding including itself. The jobs already in flight when the
    shrink landed are over both targets and are ALLOWED to be -- that is attrition; what may not
    happen is admitting a new one while they are.
    """
    running: set[str] = set()
    holding: set[str] = set()
    for index, (kind, name) in enumerate(events):
        if kind == "start":
            running.add(name)
        elif kind == "hold":
            holding.add(name)
        elif kind == "done":
            running.discard(name)
            holding.discard(name)
        if index < marker:
            continue
        if kind == "start":
            assert len(running) <= target["lane_analyze_concurrency"], (f"{name} started over the lowered lane target", sorted(running), events)
        if kind == "hold":
            assert len(holding) <= target["worker_process_pool_size"], (f"{name} entered the pool over the lowered size", sorted(holding), events)


async def _retune(agent: AgentProcess, trigger: Trigger, reject_invalid: Callable[[], Awaitable[None]]) -> None:
    """Raise both knobs, refuse an invalid change, lower both knobs -- all with jobs in flight."""
    # Start: the env layer is in force, and the agent runs two jobs, one of them inside the pool.
    effective = agent.observed.effective
    assert effective is not None
    assert {key: effective["values"][key] for key in START} == START
    assert {key: effective["sources"][key] for key in START} == dict.fromkeys(START, "env")
    await agent.enqueue(*JOBS)
    await agent.until(lambda o: len(o.started) == 2 and len(o.holding) == 1, "two jobs on the two lane loops, one inside the one-slot pool")

    # Raise. Nothing is released, so the extra starts and holds can only come from the new sizing.
    await trigger.apply(RAISED)
    await agent.until(lambda o: _in_force(o, RAISED, layer=trigger.layer, reload_source=trigger.reload_source), f"the heartbeat reports {RAISED}")
    await agent.until(lambda o: len(o.started) == 4 and len(o.holding) == 3, "four jobs on four lane loops, three inside the pool, none released")
    in_flight = agent.observed
    assert in_flight.done == set(), "nothing was released, so nothing may have finished"
    assert in_flight.statuses.get("failed", 0) == in_flight.statuses.get("aborted", 0) == 0, in_flight.statuses

    # Refuse. Each trigger offers its own invalid change; every one must leave the RAISED config
    # in force and every in-flight job exactly where it was.
    await reject_invalid()
    _assert_last_good_retained(agent, trigger, in_flight)

    # Lower, with four jobs in flight and three in the pool: none may be cancelled.
    await trigger.apply(LOWERED)
    await agent.until(lambda o: _in_force(o, LOWERED, layer=trigger.layer, reload_source=trigger.reload_source), f"the heartbeat reports {LOWERED}")
    marker = len(agent.observed.events)
    assert agent.observed.started == in_flight.started, "the shrink admitted or lost a job before anything was released"
    assert agent.observed.holding == in_flight.holding
    assert agent.observed.done == set()

    # Drain by attrition, ONE job at a time. Releasing the whole pool at once would let every
    # holder leave in the same instant, and a limiter that ignored the shrink would then admit the
    # next job with nobody left to overlap -- measured: that version passed with the shrink
    # mutated away. One at a time, each departure leaves the others still holding, which is
    # exactly when an ignored shrink admits a job it must not.
    released: set[str] = set()
    while agent.observed.done != set(JOBS):
        await agent.until(lambda o: bool(o.holding - released) or o.done == set(JOBS), "the next job enters the pool")
        if agent.observed.done == set(JOBS):
            break
        name = min(agent.observed.holding - released)
        agent.release({name})
        released.add(name)
        await agent.until(lambda o, name=name: name in o.done, f"{name} finishes")
    await agent.until(lambda o: o.statuses == {"complete": len(JOBS)}, "every job's row reaches complete")

    # Every job ran exactly once and completed: none dropped, cancelled, retried or duplicated.
    final = agent.observed
    assert Counter(name for kind, name in final.events if kind == "start") == Counter(JOBS)
    assert Counter(name for kind, name in final.events if kind == "done") == Counter(JOBS)
    assert final.statuses == {"complete": len(JOBS)}
    _assert_admission_bounded(final.events, marker, LOWERED)

    # The same process did all of it: never restarted, still alive, still reporting the lowered config.
    assert agent.proc is not None
    assert agent.proc.poll() is None
    assert final.lane_status is not None
    assert final.lane_status["worker_pid"] == agent.pid
    assert _in_force(final, LOWERED, layer=trigger.layer, reload_source=trigger.reload_source)

    # The agent's own audit log names the trigger and every key it changed, old -> new.
    audit = [line for line in agent.reload_log_lines() if line["source"] == trigger.reload_source and line["outcome"] == "applied"]
    changes = [{key: (change["old"], change["new"]) for key, change in line["changes"].items() if key in START} for line in audit]
    raised_changes = {key: (START[key], RAISED[key]) for key in START}
    lowered_changes = {key: (RAISED[key], LOWERED[key]) for key in START}
    if trigger.reload_source == "poll":
        # An override is one POST per key, so a poll may land between the two and apply them apart.
        assert raised_changes.items() <= {item for change in changes for item in change.items()}, changes
        assert lowered_changes.items() <= {item for change in changes for item in change.items()}, changes
    else:
        assert raised_changes in changes, changes
        assert lowered_changes in changes, changes


def _assert_restart_only_rejection(agent: AgentProcess, *, reload_source: str) -> None:
    """AC4: named as requiring restart -- in the heartbeat and the audit log -- and the value never echoed."""
    effective = agent.observed.effective
    assert effective is not None
    assert RESTART_ONLY_KEY in effective["restart_only_keys"]
    assert RESTART_ONLY_VALUE not in json.dumps(effective)
    rejected = [line for line in agent.reload_log_lines() if line["source"] == reload_source and line["outcome"] == "rejected"]
    assert any(line.get("restart_required") == [RESTART_ONLY_KEY] for line in rejected), rejected
    assert RESTART_ONLY_VALUE not in agent.log_path.read_text(encoding="utf-8", errors="replace")


# One test per trigger


async def test_retune_through_a_runtime_toml_edit(launch_agent: Callable[..., Awaitable[AgentProcess]]) -> None:
    agent = await launch_agent()
    trigger = file_trigger(agent)

    async def reject_invalid() -> None:
        # A value the schema refuses (the pool must be at least 1).
        agent.write_runtime_toml({**RAISED, "worker_process_pool_size": 0})
        await agent.until(lambda o: _rejected_with(o, "worker_process_pool_size", reload_source="file"), "a zero pool size is rejected")
        # A restart-only key offered through the file layer.
        agent.write_runtime_toml({**RAISED, RESTART_ONLY_KEY: RESTART_ONLY_VALUE})
        await agent.until(
            lambda o: _rejected_with(o, f"requires restart: {RESTART_ONLY_KEY}", reload_source="file"), "a restart-only key is rejected"
        )
        _assert_restart_only_rejection(agent, reload_source="file")

    await _retune(agent, trigger, reject_invalid)


async def test_retune_through_sighup(launch_agent: Callable[..., Awaitable[AgentProcess]]) -> None:
    # The directory watch still runs, as in production, but its debounce never elapses inside this
    # test, so every reload here is the SIGHUP's -- which the heartbeat's last_reload.source proves.
    agent = await launch_agent(debounce_seconds=3600)
    trigger = sighup_trigger(agent)

    async def reject_invalid() -> None:
        # A sizing that oversubscribes the agent's cores: 4 children x 2 threads on 6 cores.
        agent.write_runtime_toml({**RAISED, "worker_process_pool_size": 4})
        os.kill(agent.pid, signal.SIGHUP)
        await agent.until(
            lambda o: _rejected_with(o, f"oversubscribes {AGENT_CORES} physical cores", reload_source="sighup"), "oversubscription is rejected"
        )
        agent.write_runtime_toml({**RAISED, RESTART_ONLY_KEY: RESTART_ONLY_VALUE})
        os.kill(agent.pid, signal.SIGHUP)
        await agent.until(
            lambda o: _rejected_with(o, f"requires restart: {RESTART_ONLY_KEY}", reload_source="sighup"), "a restart-only key is rejected"
        )
        _assert_restart_only_rejection(agent, reload_source="sighup")

    await _retune(agent, trigger, reject_invalid)


async def test_retune_through_an_admin_api_override(launch_agent: Callable[..., Awaitable[AgentProcess]], control_plane: ControlPlane) -> None:
    agent = await launch_agent()
    trigger = api_trigger(control_plane)

    async def reject_invalid() -> None:
        stored = await control_plane.overrides()
        assert stored == RAISED
        async with httpx.AsyncClient(base_url=control_plane.base_url) as client:
            # Refused by the control plane itself, before anything is stored for the agent to poll.
            zero_pool = await client.post("/admin/runtime-config/worker_process_pool_size", data={"value": "0"})
            assert zero_pool.status_code == 400, zero_pool.text
            assert "worker_process_pool_size" in zero_pool.json()["detail"]
            restart_only = await client.post(f"/admin/runtime-config/{RESTART_ONLY_KEY}", data={"value": RESTART_ONLY_VALUE})
            assert restart_only.status_code == 400, restart_only.text
            assert restart_only.json()["detail"] == "requires restart"
            assert await control_plane.overrides() == stored
            # Valid on the 16-core control plane, so it IS stored -- but 4 children x 2 threads
            # oversubscribes the 4-core agent, which must refuse it when the poll delivers it.
            pool = await client.post("/admin/runtime-config/worker_process_pool_size", data={"value": "4"})
            assert pool.status_code == 200, pool.text
            await agent.until(
                lambda o: _rejected_with(o, f"oversubscribes {AGENT_CORES} physical cores", reload_source="poll"), "the agent refuses the override"
            )
            effective = agent.observed.effective
            assert effective is not None
            assert effective["values"]["worker_process_pool_size"] == RAISED["worker_process_pool_size"]
            assert effective["sources"]["worker_process_pool_size"] == "override"
            # Withdrawn (back to RAISED's value); the agent's next poll takes the unchanged set cleanly.
            cleared = await client.post("/admin/runtime-config/worker_process_pool_size", data={"value": str(RAISED["worker_process_pool_size"])})
            assert cleared.status_code == 200, cleared.text
        await agent.until(
            lambda o: (
                o.effective is not None and o.effective["last_reload"]["source"] == "poll" and o.effective["last_reload"]["outcome"] == "unchanged"
            ),
            "the agent takes the withdrawn override",
        )

    await _retune(agent, trigger, reject_invalid)
