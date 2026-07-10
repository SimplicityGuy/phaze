"""Phase 57.1 SPIKE -- de-risk the two load-bearing safety properties before the
production write path is locked (PLAN 57.1-01; consumed by Plan 04).

This module is a SPIKE harness. It touches NO production module under
``src/phaze/`` -- it exercises the pebble pickling boundary with a synthetic
picklable worker and the EXISTING ``put_analysis`` replace path to prove
crash-mid-run idempotency.

Two concerns, two task groups:

* ``transport`` -- prototype the per-window ``progress_cb`` transport across the
  pebble child boundary (RESEARCH Q1 / Assumption A2). Option A (recommended):
  a picklable ``multiprocessing.Manager().Queue()`` proxy drained parent-side; a
  future-done sentinel tears the drainer down so a SIGKILLed child can never hang
  it. Option B (compared): the child builds a sync ``httpx.Client`` from picklable
  primitives and POSTs directly. The Queue-drainer is chosen iff its SIGKILL
  teardown is clean (it is -- proven below); see ``57.1-01-SPIKE-FINDINGS.md``.
* ``idempotent`` -- prove a file killed mid-analysis re-runs cleanly via the
  existing ``put_analysis`` ``file_id``-UQ replace path (PROG-02 / RESEARCH Q3):
  the final ``analysis`` row + ``analysis_window`` set is identical to an
  uninterrupted control run, ``FileState`` reaches ``ANALYZED`` exactly once, and
  no duplicate/orphaned window rows remain. The Phase 32 re-enqueue done-predicate
  is state-only, so the partial row cannot mis-drive recovery.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import queue as queue_mod
import threading
import time
from typing import TYPE_CHECKING
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pebble import ProcessPool
import pytest
from sqlalchemy import func, select

from phaze.database import get_session
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.file import FileRecord, FileState
from phaze.routers.agent_analysis import router as agent_analysis_router
from phaze.tasks.reenqueue import _select_done_analyze_ids


if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


# ===========================================================================
# Task 1 -- pebble callback transport + SIGKILL kill-safety (group: transport)
# ===========================================================================

# Sentinel pushed onto the drainer queue when the child future completes/raises.
# A real (analyzed, total) tuple is two non-negative ints, so a string-tagged
# sentinel can never collide with a genuine progress emission.
_SENTINEL: tuple[str, int] = ("__CHILD_DONE__", -1)


def _worker_emit_then_hang(progress_q: queue_mod.Queue, n: int, per_sleep: float, tail_sleep: float) -> int:
    """SPIKE worker (module-level => picklable): emit ``n`` synthetic counts, then hang.

    Runs inside the pebble child. ``progress_q`` is a picklable
    ``multiprocessing.Manager().Queue()`` proxy (the only kind that survives a
    SIGKILL of THIS process -- a raw ``mp.Queue`` can wedge the feeder if the
    writer dies holding the internal lock). After emitting the counts the worker
    sleeps ``tail_sleep`` so a tiny pebble per-task timeout SIGKILLs it mid-hang,
    standing in for a real ``kill -9`` mid-fine-pass.
    """
    for analyzed in range(1, n + 1):
        progress_q.put_nowait((analyzed, n))
        time.sleep(per_sleep)
    time.sleep(tail_sleep)
    return n


def _worker_emit_clean(progress_q: queue_mod.Queue, n: int, per_sleep: float) -> int:
    """SPIKE worker: emit ``n`` counts and return cleanly (no hang, no kill)."""
    for analyzed in range(1, n + 1):
        progress_q.put_nowait((analyzed, n))
        time.sleep(per_sleep)
    return n


async def _drain_until_sentinel(
    progress_q: queue_mod.Queue,
    deadline_sec: float,
    post_stub: Callable[[int, int], object] | None = None,
) -> list[tuple[int, int]]:
    """Parent-side drainer: read ``(analyzed, total)`` tuples until the sentinel.

    Mirrors the Plan-04 bridge: a blocking ``q.get`` is offloaded via
    ``asyncio.to_thread`` so the event loop is never blocked; each tuple is where
    the real bridge would ``await ctx['api_client'].post_analysis_progress(...)``
    (``post_stub`` stands in). The loop terminates the moment the future-done
    callback pushes ``_SENTINEL`` -- so a SIGKILLed child tears the drainer down
    instead of hanging it (Assumption A2). ``deadline_sec`` is a hard backstop so
    a never-arriving sentinel still fails fast rather than hanging the test.
    """
    import asyncio

    drained: list[tuple[int, int]] = []
    end = time.monotonic() + deadline_sec
    while time.monotonic() < end:
        try:
            item = await asyncio.to_thread(progress_q.get, True, 0.2)
        except queue_mod.Empty:
            continue
        if item == _SENTINEL:
            break
        drained.append(item)
        if post_stub is not None:
            post_stub(item[0], item[1])
    return drained


def _should_post(last_post_monotonic: float | None, now: float, interval_sec: float) -> bool:
    """D-04 throttle decision (lives in the bridge, not in ``analyze_file``).

    Returns True if a POST is due (no prior post, or >= ``interval_sec`` since the
    last). The final flush is unconditional and handled separately by the caller.
    """
    return last_post_monotonic is None or (now - last_post_monotonic) >= interval_sec


async def test_transport_queue_drainer_observes_progress_and_is_kill_safe() -> None:
    """Option A: ≥2 counts drained parent-side, then SIGKILL → drainer tears down promptly.

    The single load-bearing proof of the spike (T-57.1-30 / Assumption A2): a
    Manager().Queue() proxy carries synthetic per-window counts out of a real
    pebble child; the child is SIGKILLed by a tiny pebble timeout
    (``builtins.TimeoutError``); the future-done callback pushes a sentinel so the
    parent drainer joins within a bounded deadline and NEVER hangs a worker slot.
    """
    import asyncio

    manager = multiprocessing.Manager()
    progress_q: queue_mod.Queue = manager.Queue()
    pool = ProcessPool(max_workers=1, max_tasks=1)
    try:
        # Emit 4 counts over ~1.2s, then hang 30s. A 3.0s pebble timeout kills the
        # child mid-hang -- AFTER all 4 counts are emitted, so ≥2 are observable.
        future = pool.schedule(_worker_emit_then_hang, args=[progress_q, 4, 0.3, 30.0], timeout=3.0)
        afut = asyncio.wrap_future(future)
        # KILL-SAFE TEARDOWN: when the child future completes/raises (SIGKILL ->
        # TimeoutError), push the sentinel so the drainer's next get returns it.
        afut.add_done_callback(lambda _f: progress_q.put(_SENTINEL))

        drainer = asyncio.create_task(_drain_until_sentinel(progress_q, deadline_sec=15.0))

        # The child is SIGKILLed for exceeding its 3.0s timeout.
        with pytest.raises(TimeoutError):
            await afut

        # The drainer must terminate within a bounded wait (no hang) once the
        # sentinel lands -- this is the kill-safety property under test.
        drained = await asyncio.wait_for(drainer, timeout=10.0)
    finally:
        pool.stop()
        pool.join()
        manager.shutdown()

    assert len(drained) >= 2, f"expected >=2 mid-flight counts drained before the kill, got {drained}"
    # Counts are monotonic (analyzed, total) with a stable denominator.
    assert all(total == 4 for _analyzed, total in drained)
    assert [a for a, _t in drained] == sorted(a for a, _t in drained)


async def test_transport_queue_drainer_clean_completion_teardown() -> None:
    """Sentinel teardown also fires on NORMAL child completion (no kill).

    Confirms the same future-done-callback mechanism that makes the drainer
    kill-safe also retires it on the happy path -- so Plan 04 can use ONE teardown
    path for both crash and clean-finish.
    """
    import asyncio

    manager = multiprocessing.Manager()
    progress_q: queue_mod.Queue = manager.Queue()
    pool = ProcessPool(max_workers=1, max_tasks=1)
    try:
        future = pool.schedule(_worker_emit_clean, args=[progress_q, 3, 0.1], timeout=30.0)
        afut = asyncio.wrap_future(future)
        afut.add_done_callback(lambda _f: progress_q.put(_SENTINEL))
        drainer = asyncio.create_task(_drain_until_sentinel(progress_q, deadline_sec=15.0))
        result = await asyncio.wait_for(afut, timeout=20.0)
        drained = await asyncio.wait_for(drainer, timeout=10.0)
    finally:
        pool.stop()
        pool.join()
        manager.shutdown()

    assert result == 3
    assert drained == [(1, 3), (2, 3), (3, 3)]


def test_transport_throttle_skips_within_interval() -> None:
    """D-04 ~5s throttle: the bridge skips a POST < interval since the last, fires otherwise."""
    interval = 5.0
    assert _should_post(None, now=100.0, interval_sec=interval) is True  # first post always fires
    assert _should_post(100.0, now=101.0, interval_sec=interval) is False  # 1s later -> skip
    assert _should_post(100.0, now=105.0, interval_sec=interval) is True  # exactly interval -> fire
    assert _should_post(100.0, now=107.5, interval_sec=interval) is True  # > interval -> fire


# --- Option B (compared, not chosen): child-side sync httpx POST -------------


class _ProgressCollector(BaseHTTPRequestHandler):
    """Minimal POST sink: record each progress body the child posts. Spike-only."""

    posts: list[dict] = []  # noqa: RUF012 -- shared collector across handler instances
    lock = threading.Lock()

    def do_POST(self) -> None:  # BaseHTTPRequestHandler API name
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", "replace")}
        with _ProgressCollector.lock:
            _ProgressCollector.posts.append(body)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args: object) -> None:
        """Silence the default stderr access log during the spike run."""


def _worker_httpx_post(base_url: str, file_id: str, token: str, n: int, per_sleep: float) -> int:
    """SPIKE Option B worker (picklable): build a sync httpx.Client from primitives and POST.

    Demonstrates the child-side transport the planner compared against the
    Queue-drainer: the child reaches the control plane DIRECTLY, so there is no
    parent drainer to hang -- but it couples the child to the bearer token and
    duplicates the POST surface (no tenacity reuse). Best-effort: a failed POST is
    swallowed so a transport blip never fails the (synthetic) analysis.
    """
    import contextlib

    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=5.0) as client:
        for analyzed in range(1, n + 1):
            with contextlib.suppress(httpx.HTTPError):
                client.post(
                    f"/api/internal/agent/analysis/{file_id}/progress",
                    json={"fine_windows_analyzed": analyzed, "fine_windows_total": n},
                )
            time.sleep(per_sleep)
    return n


def test_transport_child_side_httpx_option_b_posts_progress() -> None:
    """Option B prototyped far enough to compare: child posts ≥2 counts to a real local server.

    Proves the child can construct a sync ``httpx.Client`` from picklable
    primitives (base_url/token/file_id) and reach a control endpoint -- the
    alternative the spike weighed against the Queue-drainer. Recorded as the
    rejected option in ``57.1-01-SPIKE-FINDINGS.md`` (extra HTTP surface; child
    coupled to auth).
    """
    _ProgressCollector.posts = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProgressCollector)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    pool = ProcessPool(max_workers=1, max_tasks=1)
    file_id = str(uuid.uuid4())
    try:
        future = pool.schedule(
            _worker_httpx_post,
            args=[f"http://127.0.0.1:{port}", file_id, "phaze_agent_spike-token", 3, 0.1],
            timeout=30.0,
        )
        result = future.result(timeout=20.0)
    finally:
        pool.stop()
        pool.join()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)

    assert result == 3
    with _ProgressCollector.lock:
        posts = list(_ProgressCollector.posts)
    assert len(posts) >= 2, f"expected >=2 child-side progress posts, got {posts}"
    assert all(p["fine_windows_total"] == 3 for p in posts)
    assert {p["fine_windows_analyzed"] for p in posts} >= {1, 2}


# ===========================================================================
# Task 2 -- crash-mid-run idempotency on the put_analysis replace path
#           (group: idempotent; real Postgres via the `session` fixture)
# ===========================================================================


# Canonical "uninterrupted control" payload: the known-good final analysis a
# clean run produces. Five fine windows fully analyzed (sampled=False), plus the
# representative aggregates. The re-run after a crash must land byte-identical.
_CONTROL_PAYLOAD: dict = {
    "bpm": 128.0,
    "musical_key": "C minor",
    "mood": {"energetic": 0.9, "happy": 0.4},
    "style": {"electronic": 0.8, "house": 0.5},
    "danceability": 0.77,
    "fine_windows_analyzed": 5,
    "fine_windows_total": 5,
    "coarse_windows_analyzed": 2,
    "coarse_windows_total": 2,
    "sampled": False,
    "windows": [
        {"tier": "fine", "window_index": 0, "start_sec": 0.0, "end_sec": 30.0, "bpm": 128.0, "musical_key": "C minor"},
        {"tier": "fine", "window_index": 1, "start_sec": 30.0, "end_sec": 60.0, "bpm": 127.0, "musical_key": "C minor"},
        {"tier": "fine", "window_index": 2, "start_sec": 60.0, "end_sec": 90.0, "bpm": 129.0, "musical_key": "G major"},
        {"tier": "fine", "window_index": 3, "start_sec": 90.0, "end_sec": 120.0, "bpm": 128.0, "musical_key": "C minor"},
        {"tier": "fine", "window_index": 4, "start_sec": 120.0, "end_sec": 150.0, "bpm": 130.0, "musical_key": "A minor"},
        {"tier": "coarse", "window_index": 0, "start_sec": 0.0, "end_sec": 180.0, "mood": "energetic", "style": "electronic", "danceability": 0.77},
        {"tier": "coarse", "window_index": 1, "start_sec": 180.0, "end_sec": 360.0, "mood": "happy", "style": "house", "danceability": 0.71},
    ],
}


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Smoke app wiring ONLY the agent_analysis router (mirrors test_agent_analysis.py)."""
    app = FastAPI(title="spike-smoke", version="test")
    app.include_router(agent_analysis_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _make_client(session: AsyncSession, token: str) -> AsyncClient:
    app = _make_smoke_app(session)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"})


async def _seed_file(session: AsyncSession, agent_id: str, state: FileState) -> uuid.UUID:
    """Seed a FileRecord so the AnalysisResult.file_id FK is satisfiable."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash="0" * 64,
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=4096,
            state=state,
        )
    )
    await session.commit()
    return file_id


async def _window_keys(session: AsyncSession, file_id: uuid.UUID) -> list[tuple[str, int]]:
    """Return the (tier, window_index) keys for a file's analysis_window rows, sorted."""
    session.expire_all()
    rows = (await session.execute(select(AnalysisWindow.tier, AnalysisWindow.window_index).where(AnalysisWindow.file_id == file_id))).all()
    return sorted((tier, idx) for tier, idx in rows)


async def _analysis_aggregates(session: AsyncSession, file_id: uuid.UUID) -> tuple:
    """Return the comparable aggregate columns of a file's analysis row."""
    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    return (
        row.bpm,
        row.musical_key,
        row.mood,
        row.style,
        row.fine_windows_analyzed,
        row.fine_windows_total,
        row.coarse_windows_analyzed,
        row.coarse_windows_total,
        row.sampled,
    )


async def _analyze_is_done(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """True iff the recovery done-predicate classifies the file as analyze-domain-complete.

    Phase 80 (READ-03) cut ``_select_done_analyze_ids`` over from a ``FileRecord.state`` read to the
    ledger-scoped ``domain_completed_clause(ANALYZE)`` derivation, so it now takes the fids to bind (a
    single-element list is enough for this single-file probe). A crash-partial analysis row has
    ``analysis_completed_at`` NULL (not done, not failed) -> not domain-complete; a completed rerun
    stamps ``analysis_completed_at`` -> done.
    """
    session.expire_all()
    done_ids = set((await session.scalars(_select_done_analyze_ids([file_id]))).all())
    return file_id in done_ids


@pytest.mark.asyncio
async def test_idempotent_crash_midrun_rerun_matches_control(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A file killed mid-analysis re-runs to a state byte-identical to an uninterrupted control.

    Asserts the four must_have truths:
      (a) final analysis aggregates == the uninterrupted-control values (file_id-UQ
          on_conflict_do_update overwrote the partial row);
      (b) the analysis_window set == the control set, matched by (tier, window_index),
          with NO duplicate and NO orphaned rows (delete-then-insert replace ran);
      (c) FileState reaches ANALYZED exactly once (one analysis row, terminal state);
      (d) the state-only re-enqueue done-predicate classifies the file as done ONLY
          after the terminal re-run.
    """
    agent, raw_token = seed_test_agent

    # --- The CONTROL: a fresh file, single clean put_analysis (uninterrupted). ---
    control_id = await _seed_file(session, agent.id, FileState.DISCOVERED)
    async with _make_client(session, raw_token) as ac:
        resp = await ac.put(f"/api/internal/agent/analysis/{control_id}", json=_CONTROL_PAYLOAD)
    assert resp.status_code == 200, resp.text

    # --- The INTERRUPTED file: a non-terminal file carrying LEFTOVER partial state. ---
    crashed_id = await _seed_file(session, agent.id, FileState.DISCOVERED)
    # Mimic "previous completed-then-killed": a partial aggregate row (NULL bpm,
    # analyzed < total) + 3 stale fine windows, written WITHOUT flipping state.
    session.add(
        AnalysisResult(
            id=uuid.uuid4(),
            file_id=crashed_id,
            bpm=None,
            fine_windows_analyzed=3,
            fine_windows_total=10,
            sampled=True,
        )
    )
    for idx in range(3):
        session.add(
            AnalysisWindow(
                id=uuid.uuid4(), file_id=crashed_id, tier="fine", window_index=idx, start_sec=idx * 30.0, end_sec=(idx + 1) * 30.0, bpm=99.0
            )
        )
    await session.commit()

    # (d) BEFORE the re-run the file is non-terminal -> NOT analyze-done.
    assert await _analyze_is_done(session, crashed_id) is False
    # Leftover partial state is present pre-re-run (the thing the replace must overwrite).
    assert await _window_keys(session, crashed_id) == [("fine", 0), ("fine", 1), ("fine", 2)]

    # --- The clean RE-RUN: Phase 32 re-enqueues -> put_analysis with the good payload. ---
    async with _make_client(session, raw_token) as ac:
        resp = await ac.put(f"/api/internal/agent/analysis/{crashed_id}", json=_CONTROL_PAYLOAD)
    assert resp.status_code == 200, resp.text

    # (a) aggregates identical to the uninterrupted control run.
    assert await _analysis_aggregates(session, crashed_id) == await _analysis_aggregates(session, control_id)
    # The partial-row markers were overwritten (NULL bpm -> control bpm; sampled True -> False).
    crashed_aggs = await _analysis_aggregates(session, crashed_id)
    assert crashed_aggs[0] == 128.0  # bpm
    assert crashed_aggs[8] is False  # sampled

    # (b) window set identical to control, by (tier, window_index); no dup, no orphan.
    control_keys = await _window_keys(session, control_id)
    crashed_keys = await _window_keys(session, crashed_id)
    assert crashed_keys == control_keys
    assert len(crashed_keys) == len(_CONTROL_PAYLOAD["windows"])  # exactly the control count, no orphans
    assert len(crashed_keys) == len(set(crashed_keys))  # no duplicate (tier, window_index)
    # The three stale windows (bpm=99.0) are gone -- replace, not append.
    session.expire_all()
    stale = (
        await session.execute(
            select(func.count()).select_from(AnalysisWindow).where(AnalysisWindow.file_id == crashed_id, AnalysisWindow.bpm == 99.0)
        )
    ).scalar_one()
    assert stale == 0

    # (c) FileState ANALYZED, and exactly one analysis row (file_id UQ -> never duplicated).
    session.expire_all()
    file_row = (await session.execute(select(FileRecord).where(FileRecord.id == crashed_id))).scalar_one()
    assert file_row.state == FileState.ANALYZED
    analysis_count = (
        await session.execute(select(func.count()).select_from(AnalysisResult).where(AnalysisResult.file_id == crashed_id))
    ).scalar_one()
    assert analysis_count == 1

    # (d) AFTER the terminal re-run the file is analyze-done.
    assert await _analyze_is_done(session, crashed_id) is True


@pytest.mark.asyncio
async def test_idempotent_repeat_rerun_no_duplicate_windows(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """Replaying the identical put_analysis twice leaves the SAME single row + window set.

    Locks the counter/idempotency claim from a second angle: a double re-run (e.g.
    a retried job) does not duplicate analysis_window rows nor create a second
    analysis row -- the file_id-UQ upsert + delete-then-insert replace are
    idempotent under replay.
    """
    agent, raw_token = seed_test_agent
    file_id = await _seed_file(session, agent.id, FileState.DISCOVERED)

    async with _make_client(session, raw_token) as ac:
        first = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_CONTROL_PAYLOAD)
        second = await ac.put(f"/api/internal/agent/analysis/{file_id}", json=_CONTROL_PAYLOAD)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    keys = await _window_keys(session, file_id)
    assert len(keys) == len(_CONTROL_PAYLOAD["windows"])
    assert len(keys) == len(set(keys))  # no duplicates after replay

    session.expire_all()
    analysis_count = (await session.execute(select(func.count()).select_from(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert analysis_count == 1
