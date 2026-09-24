"""Seam E5 (phaze-6zgtm): the companion-read RESULT crosses a real SAQ ``apply`` hop.

``tests/integration/test_companion_ingestion_e2e.py`` already runs the real agent task
(``tasks.companion_read.read_companion_files``) and hands its real return value to the real
``services.proposal_context.fetch_companion_contents`` -- but its ``_InlineAgentQueue.apply``
(that module's lines 44-56) calls the task function DIRECTLY, in-process. The result never
crosses a real ``saq.Queue.apply``: no job-result serialization/deserialization through the
Postgres ``saq_jobs`` ``BYTEA`` column, no ``_COMPANION_READ_TIMEOUT_S`` wait, and no
job-failure-to-exception mapping. This module closes exactly that gap.

THE CONSUMER is named explicitly and is the real one: ``proposal_context.fetch_companion_contents``
(via ``_read_companion_chunk``, ``proposal_context.py:~153-174``), called with the REAL
``services.agent_task_router.AgentTaskRouter`` -- the same class ``app.state.task_router`` is in
production (``phaze.main``) -- so ``task_router.queue_for(agent_id, lane)`` returns a real
``PostgresQueue`` (built through the production ``build_pipeline_queue`` seam, with the real
before_enqueue hook chain) rather than a test double. A real ``saq.Worker`` runs the real
``read_companion_files`` task function against that same queue while the consumer's
``queue.apply(...)`` call is in flight -- see :func:`_drive_worker`.

Only ``phaze.tasks.companion_read.get_settings`` is patched (matching
``tests/review/tasks/test_companion_read.py``'s own pattern) so the WORKER SIDE of the hop --
which normally runs on an agent host with ``PHAZE_ROLE=agent`` -- resolves this test's
``scan_roots`` instead. That patch never touches the broker, the serializer, or the consumer.

Runs against the ephemeral integration-test Postgres broker (``just test-db``, host port 5433);
the DSN is derived via ``tests.db_guard.integration_dsns`` (phaze-9nz1g's pattern), and the module
skips cleanly when the broker is down.

FAILURE-PATH ACCEPTANCE (bead AC 2): :func:`test_a_failing_worker_attempt_degrades_to_empty_context_with_only_a_warning`
drives a REAL job failure (not a mock) through the REAL broker and worker, exhausting the role's
retry budget, and shows the current, accepted behavior: ``fetch_companion_contents`` returns ``[]``
and the only trace is a single ``companion_read_unavailable`` warning log. See that test's
docstring for why this bead ACCEPTS the current degradation rather than adding new observability.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
import uuid

import pytest
import pytest_asyncio
from saq import Worker

from phaze.config import AgentSettings
from phaze.schemas.agent_tasks import CompanionReadItem
from phaze.services.agent_task_router import AgentTaskRouter
from phaze.services.enqueue_router import lane_for_task
from phaze.services.proposal_context import MAX_COMPANION_CHARS, fetch_companion_contents
from phaze.tasks.companion_read import read_companion_files
from tests.db_guard import integration_dsns


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Coroutine
    from pathlib import Path

    from saq.queue.postgres import PostgresQueue


pytestmark = pytest.mark.integration

BROKER_DSN, _ = integration_dsns()
_TASK_NAME = "read_companion_files"
_LANE = lane_for_task(_TASK_NAME)
# Hard wall-clock bound on the worker-driving section of each test, independent of any single
# await's own timeout (queue.apply's 30s, the Worker's 0.5s dequeue_timeout, SAQ's retry_delay).
# Measured real runs complete in 1-3s; this is a generous multiple, not a tuned floor, so a
# regression fails LOUD (a real pytest failure) instead of hanging the suite the way the
# cancellation bug documented in _drive_worker's docstring did before it was found and fixed.
_WALL_CLOCK_BOUND_S = 20.0


def _agent_settings(scan_roots: list[str]) -> Any:
    """A spec'd ``AgentSettings`` stand-in -- mirrors ``tests/review/tasks/test_companion_read.py``.

    ``isinstance(cfg, AgentSettings)`` inside ``read_companion_files`` gates the scan_roots read
    (a non-agent role gets every companion skipped), so the mock must carry the real spec.
    """
    cfg = MagicMock(spec=AgentSettings)
    cfg.scan_roots = scan_roots
    return cfg


async def _drive_worker(worker: Worker, coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a real dequeue+dispatch loop concurrently with ``coro``, then stop it.

    ``queue.apply()`` only enqueues and polls (``saq.Queue.map``) -- it dequeues nothing itself.
    Something has to actually run ``Worker.process()`` for the enqueued job to ever complete, and
    it has to do so WHILE the consumer is waiting, exactly as a real agent worker process would be
    running concurrently with the controller's ``queue.apply()`` call in production. Mirrors the
    real ``saq.Worker`` usage already established in ``tests/integration/test_stage_pause_retry_bounce.py``.

    Stopped via a plain ``asyncio.Event``, NOT ``task.cancel()``: ``PostgresQueue.dequeue()``
    catches ``asyncio.CancelledError`` internally (treats it exactly like its own dequeue-timeout
    expiring, ``saq/queue/postgres.py``) and returns ``None`` instead of propagating it -- so a
    cancelled loop task simply keeps looping, uncancelled, forever. Measured directly: with
    ``task.cancel()`` + ``await task``, the loop survived a 5s wait_for and then, after the queue's
    pool was closed out from under it, spun into a true CPU-bound busy loop. The event-based stop
    is bounded by ``worker.dequeue_timeout`` (the in-flight dequeue call's own timeout) and needs
    no cancellation at all.
    """
    stop_event = asyncio.Event()

    async def _loop() -> None:
        while not stop_event.is_set():
            await worker.process()

    task = asyncio.create_task(_loop())
    try:
        return await coro
    finally:
        stop_event.set()
        await task


@pytest_asyncio.fixture
async def companion_router() -> AsyncGenerator[tuple[AgentTaskRouter, str]]:
    """A real ``AgentTaskRouter`` against the 5433 harness, plus a per-test-unique agent id.

    Skips cleanly (mirroring every other ``tests/integration/test_pg_*`` module) when the broker
    is unreachable. Teardown closes the router (disconnects its cached queues) and deletes this
    test's ``saq_jobs`` rows so repeated runs never accumulate rows in the shared harness.
    """
    import os

    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    redis_url = os.environ.get("PHAZE_REDIS_URL", "redis://localhost:6380/0")
    router = AgentTaskRouter(queue_url=BROKER_DSN, cache_redis_url=redis_url)
    agent_id = f"itest-companion-{uuid.uuid4().hex[:8]}"
    try:
        yield router, agent_id
    finally:
        queue = router.queue_for(agent_id, _LANE)
        with contextlib.suppress(Exception):
            async with queue.pool.connection() as conn:
                # Parameterized -- queue.name is bound, never interpolated.
                await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await router.close()


async def test_read_companion_files_result_reaches_the_real_consumer_through_the_real_broker_and_worker(
    tmp_path: Path,
    companion_router: tuple[AgentTaskRouter, str],
) -> None:
    """The full chain: real task -> real ``PostgresQueue.apply`` -> real ``fetch_companion_contents``.

    Three companions, each pinning a distinct edge case the bead names:

    * a non-ASCII sidecar -- proves the broker's JSON round trip (``json.dumps``/``json.loads``
      through the ``saq_jobs`` ``BYTEA`` column) preserves the text exactly, not just ASCII;
    * an oversized sidecar (well over ``MAX_COMPANION_CHARS``) -- proves a large payload survives
      the real broker whole (unlike the consumer's own post-hoc truncation, which is asserted
      separately below) before the consumer's ``clean_companion_content`` truncates it;
    * a MISSING sidecar (path never created) -- the real worker's containment/read step skips it
      with a warning and returns fewer entries than requested; the consumer must receive exactly
      the entries that succeeded, never a placeholder for the missing one.

    Asserts on TYPES (a ``list[dict[str, str]]``, not e.g. a ``list[CompanionReadItem]`` or bytes
    left over from a half-deserialized job result) as well as content, per the bead's ask.
    """
    router, agent_id = companion_router

    non_ascii = tmp_path / "notas.txt"
    non_ascii.write_text("Sábado en el Club — arranca a las 12:00am ⚡ line-up completo", encoding="utf-8")

    oversized_body = "La cumbia nunca termina. " * 200  # well over MAX_COMPANION_CHARS (3000)
    assert len(oversized_body) > MAX_COMPANION_CHARS
    oversized = tmp_path / "huge.nfo"
    oversized.write_text(oversized_body, encoding="utf-8")

    missing = tmp_path / "gone.nfo"  # deliberately never created

    by_agent = {
        agent_id: [
            CompanionReadItem(filename=non_ascii.name, path=str(non_ascii)),
            CompanionReadItem(filename=oversized.name, path=str(oversized)),
            CompanionReadItem(filename=missing.name, path=str(missing)),
        ]
    }

    queue: PostgresQueue = router.queue_for(agent_id, _LANE)
    await queue.connect()
    worker = Worker(queue=queue, functions=[(_TASK_NAME, read_companion_files)], concurrency=1, dequeue_timeout=0.5)

    with patch("phaze.tasks.companion_read.get_settings", return_value=_agent_settings([str(tmp_path)])):
        contents = await asyncio.wait_for(
            _drive_worker(
                worker,
                fetch_companion_contents(by_agent, MAX_COMPANION_CHARS, router, media_file_id=uuid.uuid4()),
            ),
            timeout=_WALL_CLOCK_BOUND_S,
        )

    # TYPES: the real job-result deserialization must reconstruct plain JSON-native types, not
    # leave anything wire-shaped (bytes, a CompanionReadItem, a job/Result wrapper) behind.
    assert isinstance(contents, list)
    for entry in contents:
        assert isinstance(entry, dict)
        assert isinstance(entry["filename"], str)
        assert isinstance(entry["content"], str)

    by_filename = {entry["filename"]: entry["content"] for entry in contents}

    # The missing companion is silently absent -- never a placeholder, never an empty string
    # standing in for "could not read".
    assert set(by_filename) == {non_ascii.name, oversized.name}

    assert by_filename[non_ascii.name] == "Sábado en el Club — arranca a las 12:00am ⚡ line-up completo"

    # The oversized one crossed the broker WHOLE (its own body is unicode-repeated well past
    # MAX_COMPANION_CHARS) and was then truncated by the CONSUMER's clean_companion_content, not
    # clipped by the broker itself.
    assert by_filename[oversized.name] == oversized_body[:MAX_COMPANION_CHARS] + "\n[...truncated]"


async def test_a_failing_worker_attempt_degrades_to_empty_context_with_only_a_warning(
    tmp_path: Path,
    companion_router: tuple[AgentTaskRouter, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A REAL job failure on the REAL broker still only ever logs a warning (bead AC 2).

    ``get_settings`` is patched to raise inside the real worker's ``read_companion_files``
    invocation -- a real exception, a real ``saq.Job`` FAILED status (after the role's retry
    budget, ``worker_max_retries=4``, is genuinely exhausted by real re-dequeues -- not
    simulated), and a real ``saq.queue.base.JobError`` raised out of the real
    ``PostgresQueue.apply()``. ``_read_companion_chunk``'s ``except Exception:`` catches it and
    returns ``[]``.

    The retry budget stays REAL but fast: SAQ's own ``retry_delay``/``retry_backoff`` defaults
    (``0.0``/``False``) are untouched for this task (``queue_defaults.py``'s per-function policy
    table only pins ``process_file``), so each retry re-queues at ~now() rather than backing off --
    measured at well under a second for all 4 attempts. ``_WALL_CLOCK_BOUND_S`` (a hard
    ``asyncio.wait_for`` around the whole worker-driving section, independent of
    ``_COMPANION_READ_TIMEOUT_S``'s own 30s) still guards against a future change to that policy,
    or a genuine hang, turning either into a loud test FAILURE instead of a stuck suite.

    ACCEPTED, not fixed, on this bead -- as the IMPLEMENTER'S decision (dev/companion, 2026-09-24,
    bead phaze-6zgtm; CLAUDE.md rule 2), NOT an operator decision: the bead's own AC wording offers
    this exact branch ("The failure path ...
    is shown to be observable as more than a per-call warning, OR the current degradation is
    explicitly accepted on this bead with reasoning") and its body states the mechanism is
    "NOT decided". This codebase already has an identical-shaped precedent --
    ``services/agent_liveness.py``'s ``compute_lane_identity_registry_unavailable`` degrades a
    different control-plane read to a bare warning with no counter or alert either. Adding new
    alerting/metrics infrastructure for ONE lane here, ahead of a decision on the general shape (a
    metric? an alert rule? a per-agent health flag? which of several similarly-shaped warnings in
    this codebase?), would be scope invented by the implementer -- so the acceptance itself, not a
    new mechanism, is what this bead delivers against that AC branch. Recorded as a comment on
    phaze-6zgtm with this same reasoning and the same explicit implementer-not-operator label.
    """
    router, agent_id = companion_router
    companion = tmp_path / "notes.txt"
    companion.write_text("never read -- the worker fails before opening it", encoding="utf-8")
    by_agent = {agent_id: [CompanionReadItem(filename=companion.name, path=str(companion))]}

    queue: PostgresQueue = router.queue_for(agent_id, _LANE)
    await queue.connect()
    worker = Worker(queue=queue, functions=[(_TASK_NAME, read_companion_files)], concurrency=1, dequeue_timeout=0.5)

    with (
        patch("phaze.tasks.companion_read.get_settings", side_effect=RuntimeError("simulated agent-side failure")),
        caplog.at_level("WARNING"),
    ):
        contents = await asyncio.wait_for(
            _drive_worker(
                worker,
                fetch_companion_contents(by_agent, MAX_COMPANION_CHARS, router, media_file_id=uuid.uuid4()),
            ),
            timeout=_WALL_CLOCK_BOUND_S,
        )

    assert contents == []
    assert "companion_read_unavailable" in caplog.text
    # The degradation is total and silent beyond that one line: no exception escapes the
    # consumer, and no result-shape hint of the failure (e.g. a partial/None entry) leaks through.
