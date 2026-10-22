"""The Coarse cell reads ``step <a>/<T> · <X>% · <N> windows`` from the real heartbeat counters (phaze-s03fz).

``coarse_windows_analyzed`` moves once per chunk (at assembly) while ``coarse_work_percent`` counts the
13 steps of every chunk, so ``a/N`` sat at ``0/21`` while the percentage climbed. The cell now shows the
steps the percentage is counting. These tests drive a REAL multi-chunk ``analyze_file`` run (essentia is
mocked, exactly as ``test_analysis_signals`` does -- the claim is about the plumbing, not the audio) through
EACH lane's real progress callbacks (``job_runner._make_progress_cb`` and ``tasks.functions
._run_analysis_with_progress``), the real ``AnalysisProgressPayload``, the real router upsert, the stored row,
the real ``_kueue_running`` reader and the real template. Nothing between ``signals.beat("coarse_work", ...)``
and the rendered text is reconstructed from the percentage.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
import uuid

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
import pytest
from sqlalchemy import select

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.schemas.agent_analysis import AnalysisProgressPayload
from phaze.services.analysis import MODEL_SETS, analyze_file
from phaze.services.backends.lane_detail import _file_facts, _kueue_running
from phaze.utils.humanize import relative_time
from phaze.web.template_globals import register_format_filters
from tests.agents.routers.test_agent_analysis import _make_client, _seed_file
from tests.analyze.services.pipeline.test_analysis_signals import _build_mock_essentia, _mock_labels_file


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


TEMPLATES = Path(__file__).resolve().parents[3] / "src/phaze/templates"
_BACKEND = "kueue-a"
# 6000 s mocked -> 34 coarse windows across 2 chunks (30 + 4); work_per_chunk = every model + genre + assembly.
_COARSE_WINDOWS = 34
_CHUNKS = 2
_WORK_PER_CHUNK = sum(len(s.models) for s in MODEL_SETS) + 1 + 1
_WORK_TOTAL = _CHUNKS * _WORK_PER_CHUNK


class _Recorder:
    """Stands in for ``PhazeAgentClient``: records every progress POST in arrival order."""

    def __init__(self) -> None:
        self.progress: list[AnalysisProgressPayload] = []

    async def post_analysis_progress(self, _file_id: uuid.UUID, payload: AnalysisProgressPayload) -> None:
        self.progress.append(payload)


def _run_real_analysis(progress_cb: Any, heartbeat_cb: Any) -> list[tuple[int, int]]:
    """Run the real ``analyze_file`` over a mocked essentia; return the raw ``coarse_work`` (done, total) beats."""
    raw: list[tuple[int, int]] = []

    def _beat(stage: str, done: int, total: int) -> None:
        if stage == "coarse_work":
            raw.append((done, total))
        heartbeat_cb(stage, done, total)

    with (
        patch("phaze.services.analysis._probe_duration_sec", return_value=6000.0),
        patch("phaze.services.analysis._get_labels", side_effect=_mock_labels_file),
        patch("phaze.services.analysis.es", new_callable=_build_mock_essentia),
    ):
        analyze_file("/fake/audio.mp3", "/fake/models", progress_cb=progress_cb, heartbeat_cb=_beat)
    return raw


async def _job_runner_lane(monkeypatch: pytest.MonkeyPatch) -> tuple[list[AnalysisProgressPayload], list[tuple[int, int]]]:
    import phaze.job_runner as jr

    monkeypatch.setattr(jr, "_HEARTBEAT_SURFACE_INTERVAL_SEC", 0.0)
    recorder = _Recorder()
    progress_cb, heartbeat_cb, pending = jr._make_progress_cb(recorder, uuid.uuid4(), 0.0)  # type: ignore[arg-type]
    raw = _run_real_analysis(progress_cb, heartbeat_cb)
    await asyncio.gather(*list(pending))
    return recorder.progress, raw


async def _saq_lane() -> tuple[list[AnalysisProgressPayload], list[tuple[int, int]]]:
    from phaze.tasks import functions as fn

    recorder = _Recorder()
    raw: list[tuple[int, int]] = []

    async def _child(_path: str, _models: str, *, progress_cb: Any, heartbeat_cb: Any, **_kw: Any) -> dict[str, Any]:
        raw.extend(_run_real_analysis(progress_cb, heartbeat_cb))
        return {}

    runtime_cfg = MagicMock(analysis_job_heartbeat_sec=600, analysis_stall_timeout_sec=1800)
    cfg = MagicMock(analysis_progress_interval_sec=0.0)
    with patch.object(fn, "run_analysis_subprocess", _child):
        await fn._run_analysis_with_progress(recorder, cfg, uuid.uuid4(), "/fake/audio.mp3", "/fake/models", None, runtime_cfg=runtime_cfg)  # type: ignore[arg-type]
    return recorder.progress, raw


@pytest.mark.parametrize("lane", ["job_runner", "saq"])
async def test_zero_steps_publish_before_coarse_work_even_inside_both_throttles(lane: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The initialization POST must happen before decode, rather than only during the final flush."""
    import phaze.job_runner as jr
    from phaze.tasks import functions as fn

    recorder = _Recorder()

    async def _before_decode(progress_cb: Any, heartbeat_cb: Any) -> None:
        progress_cb(0, 200, 0, _COARSE_WINDOWS)
        heartbeat_cb("fine", 0, 200)  # consume the heartbeat throttle, too
        await asyncio.sleep(0)
        progress_cb(200, 200, 0, _COARSE_WINDOWS)
        heartbeat_cb("coarse_work", 0, _WORK_TOTAL)
        await asyncio.sleep(0)
        assert recorder.progress[-1].coarse_work_done == 0
        assert recorder.progress[-1].coarse_work_total == _WORK_TOTAL
        assert recorder.progress[-1].fine_windows_analyzed == 200
        assert recorder.progress[-1].coarse_work_percent == 0
        count = len(recorder.progress)
        heartbeat_cb("coarse_work", 1, _WORK_TOTAL)
        await asyncio.sleep(0)
        assert len(recorder.progress) == count, "subsequent work updates must still obey the throttle"

    if lane == "job_runner":
        monkeypatch.setattr(jr, "_HEARTBEAT_SURFACE_INTERVAL_SEC", 3600.0)
        progress_cb, heartbeat_cb, pending = jr._make_progress_cb(recorder, uuid.uuid4(), 3600.0)  # type: ignore[arg-type]
        await _before_decode(progress_cb, heartbeat_cb)
        await asyncio.gather(*list(pending))
    else:

        async def _child(_path: str, _models: str, *, progress_cb: Any, heartbeat_cb: Any, **_kw: Any) -> dict[str, Any]:
            await _before_decode(progress_cb, heartbeat_cb)
            return {}

        cfg = MagicMock(analysis_progress_interval_sec=3600.0)
        runtime_cfg = MagicMock(analysis_job_heartbeat_sec=600, analysis_stall_timeout_sec=1800)
        with patch.object(fn, "run_analysis_subprocess", _child):
            await fn._run_analysis_with_progress(recorder, cfg, uuid.uuid4(), "/fake/audio.mp3", "/fake/models", None, runtime_cfg=runtime_cfg)  # type: ignore[arg-type]


def _coarse_cell_text(run: Any) -> str:
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    environment.globals["humanize_relative_time"] = relative_time
    register_format_filters(environment)
    html = environment.get_template("pipeline/partials/_analyze_queue.html").render(
        running_analyses=[run], analyze_running_total=1, total_queued_analyze=0, queue_now=now, oob=False
    )
    bar = BeautifulSoup(html, "html.parser").select('[role="progressbar"]')[1]
    caption = bar.find_next_sibling("div")  # the text line under the Coarse bar
    assert caption is not None
    return caption.get_text(" ", strip=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["job_runner", "saq"])
async def test_coarse_cell_steps_follow_the_real_heartbeat_on_both_lanes(
    lane: str, seed_test_agent: tuple[Agent, str], session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads, raw = await (_job_runner_lane(monkeypatch) if lane == "job_runner" else _saq_lane())
    # The analysis itself reports the denominator we expect: 2 chunks x every model + genre + assembly.
    assert raw[-1] == (_WORK_TOTAL, _WORK_TOTAL)
    assert raw[0] == (0, _WORK_TOTAL)
    # The first Fine START already carries the natural Coarse window total, without decoding it.
    assert payloads[0].fine_windows_analyzed == 0
    assert payloads[0].coarse_windows_total == _COARSE_WINDOWS
    assert {total for _done, total in raw} == {_WORK_TOTAL}
    raw_done = {done for done, _total in raw}

    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file_id,
            s3_key="k",
            status=CloudJobStatus.RUNNING.value,
            backend_id=_BACKEND,
            started_at=datetime(2026, 10, 20, 11, tzinfo=UTC),
        )
    )
    await session.commit()

    rendered: list[tuple[int, int]] = []
    async with _make_client(session, token) as ac:
        for payload in payloads:
            response = await ac.post(f"/api/internal/agent/analysis/{file_id}/progress", json=payload.model_dump(mode="json"))
            assert response.status_code == 200, response.text
            session.expire_all()
            (run,) = await _kueue_running(session, [_BACKEND], datetime(2026, 10, 20, 12, tzinfo=UTC))
            if run.coarse_work_done is None:
                continue
            text = _coarse_cell_text(run)
            a, total = run.coarse_work_done, run.coarse_work_total
            assert total == _WORK_TOTAL
            # X is the existing percent and agrees with the steps; 100 only at completion.
            assert text == f"step {a}/{total} · {min(99, a * 100 // total)}% · {_COARSE_WINDOWS} windows", text
            rendered.append((a, total))

    steps = [a for a, _t in rendered]
    assert len(steps) > 3, "the run must surface several distinct step readings, not one"
    assert steps == sorted(steps), "a must never run backwards"
    assert set(steps) <= raw_done, "a must never show a step the analysis did not complete"
    assert steps[-1] == _WORK_TOTAL, "a == T when the coarse tier ends"
    assert steps[0] == 0, "zero steps must be published before the first coarse decode/model operation"

    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert (row.coarse_work_done, row.coarse_work_total) == (_WORK_TOTAL, _WORK_TOTAL)
    # The local lane reads the same columns through the shared fact query.
    fact = (await _file_facts(session, [file_id]))[file_id]
    assert (fact.coarse_work_done, fact.coarse_work_total) == (_WORK_TOTAL, _WORK_TOTAL)


@pytest.mark.asyncio
async def test_progress_without_step_counts_leaves_them_alone_and_renders_unknown_steps(
    seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """An older agent sends no step fields: nothing is clobbered, and missing steps stay explicit."""
    agent, token = seed_test_agent
    file_id = await _seed_file(session, agent.id)
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file_id,
            s3_key="k",
            status=CloudJobStatus.RUNNING.value,
            backend_id=_BACKEND,
            started_at=datetime(2026, 10, 20, 11, tzinfo=UTC),
        )
    )
    await session.commit()
    base = {"fine_windows_analyzed": 40, "fine_windows_total": 40, "coarse_windows_analyzed": 0, "coarse_windows_total": 21}

    async with _make_client(session, token) as ac:
        for extra in ({"coarse_work_percent": 0}, {"coarse_work_percent": 30}):  # an old agent: percent, no steps
            assert (await ac.post(f"/api/internal/agent/analysis/{file_id}/progress", json={**base, **extra})).status_code == 200
        session.expire_all()
        (run,) = await _kueue_running(session, [_BACKEND], datetime(2026, 10, 20, 12, tzinfo=UTC))
        assert (run.coarse_work_done, run.coarse_work_total) == (None, None)
        assert _coarse_cell_text(run) == "step unavailable · 30% work · 21 windows"

        steps = {**base, "coarse_work_percent": 30, "coarse_work_done": 4, "coarse_work_total": 13}
        assert (await ac.post(f"/api/internal/agent/analysis/{file_id}/progress", json=steps)).status_code == 200
        # A later post without steps (e.g. a mixed-version rollout) must not erase them, nor lower them.
        assert (await ac.post(f"/api/internal/agent/analysis/{file_id}/progress", json={**base, "coarse_work_percent": 40})).status_code == 200
        assert (await ac.post(f"/api/internal/agent/analysis/{file_id}/progress", json={**steps, "coarse_work_done": 3})).status_code == 200

    session.expire_all()
    row = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one()
    assert (row.coarse_work_done, row.coarse_work_total) == (4, 13)


def test_schema_accepts_an_older_payload_without_step_fields() -> None:
    payload = AnalysisProgressPayload(fine_windows_analyzed=1, fine_windows_total=2)
    assert (payload.coarse_work_done, payload.coarse_work_total) == (None, None)
