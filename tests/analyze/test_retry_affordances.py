"""87-07 (UI-02 / D-04): per-file + bulk analyze retry affordances, manual-only terminal guard.

The console surfaces a per-row "Retry" on a failed analyze cell (``POST
/pipeline/files/{file_id}/analysis-failed/retry``) and a bulk "Retry all failed · Analyze" on the
failed-filter view (the pre-existing ``POST /pipeline/analysis-failed/retry``). Both re-drive files
through the SAME Phase-30-hardened guarded funnel (per-agent routing -> ``NoActiveAgentError`` guard
-> ``enqueue_process_file`` with the COMPLETE ``ProcessFilePayload`` on the per-agent queue, never
the consumer-less default). This suite pins:

- behavior 8 (the 44.5K over-enqueue guard): a FAILED analyze is terminal and NEVER auto-eligible
  (``ELIGIBLE_AFTER_FAILURE[ANALYZE]=False``); the retry is the deliberate manual-only counterpart —
  it flips ANALYSIS_FAILED -> FINGERPRINTED and CLEARS ``analysis.failed_at`` (Phase-81 CR-01) so the
  file leaves the failed disjunct and derives a fresh re-analysis, never an auto-retry loop;
- the per-file variant is scoped to ONE file (a non-failed / unknown id is a safe no-op);
- the Phase-30 no-agent guard survives on BOTH the per-file and bulk paths (amber ack, no mutation,
  no default-queue fallthrough);
- the render half of Task 2 (a per-row Retry appears only on a failed cell; the bulk button on the
  failed-filter view; no manual fingerprint retry control) — see ``-k render``.

Uses the operator ``client`` fixture (tests/conftest.py) + the fake named-queue capture harness
(tests/_queue_fakes.py), mirroring the bulk retry tests. Independent-session reads verify commits
(the ``client`` fixture's shared session sees uncommitted rows).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from phaze.enums.stage import ELIGIBLE_AFTER_FAILURE, Stage, Status, eligible
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.schemas.agent_tasks import ProcessFilePayload
from tests._queue_fakes import install_fake_queues, seed_active_agent, wire_fakes


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


pytestmark = pytest.mark.integration


def _make_file(*, state: str = FileState.ANALYSIS_FAILED) -> FileRecord:
    """A FileRecord parked in the terminal analyze-failed bucket (unique id/path)."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


def _make_failed_analysis(file_id: uuid.UUID) -> AnalysisResult:
    """An analyze failure row exactly as the 81-05 writer persists it (failed_at set, completed NULL)."""
    return AnalysisResult(id=uuid.uuid4(), file_id=file_id, failed_at=datetime.now(UTC), error_message="boom: bad frame", analysis_completed_at=None)


async def _seed_failed_file(session: AsyncSession) -> FileRecord:
    """Seed ONE ANALYSIS_FAILED file carrying the durable analyze failure marker; return it."""
    file = _make_file()
    session.add(file)
    await session.commit()
    session.add(_make_failed_analysis(file.id))
    await session.commit()
    return file


# --------------------------------------------------------------------------------------------------
# behavior 8: terminal-analyze retry is MANUAL-ONLY (no auto-loop). Pure predicate — DB-free.
# --------------------------------------------------------------------------------------------------
def test_analyze_failure_is_never_auto_eligible() -> None:
    """behavior 8 / T-87-24: a FAILED analyze is terminal — the derived scheduler NEVER auto-retries it.

    ``ELIGIBLE_AFTER_FAILURE[ANALYZE]`` is False, so ``eligible()`` excludes a FAILED analyze from the
    pending set — the ONLY enrich carve-out. This is the invariant the manual retry endpoint is the
    deliberate counterpart to; without it, re-driving the failed set would recreate the 44.5K
    over-enqueue class. Contrast: metadata/fingerprint failures DO stay auto-eligible.
    """
    assert ELIGIBLE_AFTER_FAILURE[Stage.ANALYZE] is False
    assert eligible({Stage.ANALYZE: Status.FAILED}, Stage.ANALYZE) is False
    # The manual retry re-drives from FINGERPRINTED/not_started, which IS eligible.
    assert eligible({Stage.ANALYZE: Status.NOT_STARTED}, Stage.ANALYZE) is True
    # Contrast — the other two enrich stages auto-retry a failure (ELIGIBLE_AFTER_FAILURE True).
    assert eligible({Stage.METADATA: Status.FAILED}, Stage.METADATA) is True
    assert eligible({Stage.FINGERPRINT: Status.FAILED}, Stage.FINGERPRINT) is True


# --------------------------------------------------------------------------------------------------
# Per-file scoped analyze retry.
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_per_file_retry_reenqueues_one_file_through_guarded_funnel(
    client: AsyncClient,
    session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    """The per-file retry routes ONE process_file through the per-agent queue (never the default).

    The COMPLETE ProcessFilePayload lands (v4.0.8 guard); the ack reports 1 and is analyze-worded.
    The commit is verified from an INDEPENDENT session (the client fixture's shared session sees
    uncommitted rows, so a same-session read cannot prove the manual flip committed).
    """
    file = await _seed_failed_file(session)
    # A second failed file that MUST be untouched (proves scoping to one file_id).
    other = await _seed_failed_file(session)
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post(f"/pipeline/files/{file.id}/analysis-failed/retry")
    assert response.status_code == 200
    assert "re-queued 1 failed file(s) for analysis" in response.text.lower()

    queue = task_router.queues["nox-analyze"]
    assert queue.name == "phaze-agent-nox-analyze"
    assert queue.name != "default"
    assert len(queue.captured) == 1
    task_name, payload = queue.captured[0]
    assert task_name == "process_file"
    ProcessFilePayload.model_validate(payload)
    assert payload["file_id"] == str(file.id)

    # Independent-session read: the marker clear COMMITTED for the target file only.
    independent = async_sessionmaker(async_engine, expire_on_commit=False)
    async with independent() as s:
        target = (await s.execute(select(FileRecord).where(FileRecord.id == file.id))).scalar_one()
        # Phase 90 (D-09): retry no longer flips files.state to FINGERPRINTED (that write was removed);
        # the file leaves the failed bucket purely by clearing analysis.failed_at below.
        assert target.state == FileState.ANALYSIS_FAILED
        arow = (await s.execute(select(AnalysisResult).where(AnalysisResult.file_id == file.id))).scalar_one()
        assert arow.failed_at is None
        assert arow.error_message is None
        # The XOR CHECK holds and the row now derives not_started — a fresh re-analysis.
        assert arow.analysis_completed_at is None
        # The OTHER failed file is untouched (scoped to one file_id).
        untouched = (await s.execute(select(FileRecord).where(FileRecord.id == other.id))).scalar_one()
        assert untouched.state == FileState.ANALYSIS_FAILED


@pytest.mark.asyncio
async def test_per_file_retry_no_active_agent_mutates_nothing(client: AsyncClient, session: AsyncSession) -> None:
    """Phase-30 guard / T-87-25: no agent -> amber ack, zero enqueues, no state/marker mutation."""
    file = await _seed_failed_file(session)
    fid = file.id  # capture before expiry (an expired ORM attr would lazy-reload outside greenlet)
    capture = wire_fakes(client)  # no seed_active_agent -> NoActiveAgentError path

    response = await client.post(f"/pipeline/files/{fid}/analysis-failed/retry")
    assert response.status_code == 200
    assert "no active agent" in response.text.lower()
    assert capture == []  # nothing enqueued anywhere, never the default queue

    session.expire_all()
    reread = (await session.execute(select(FileRecord).where(FileRecord.id == fid))).scalar_one()
    assert reread.state == FileState.ANALYSIS_FAILED
    arow = (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == fid))).scalar_one()
    assert arow.failed_at is not None  # marker intact (no premature clear)


@pytest.mark.asyncio
async def test_per_file_retry_non_failed_file_is_noop(client: AsyncClient, session: AsyncSession) -> None:
    """T-87-27: a non-ANALYSIS_FAILED (or unknown) file id is a safe no-op — no enqueue, "none" ack.

    The endpoint is scoped ``id == file_id AND state == ANALYSIS_FAILED``, so an already-analyzed
    file (or a random UUID) never re-drives an auto-retry — the manual path acts only on the failed
    bucket (behavior 8: no unscoped re-enqueue).
    """
    healthy = _make_file()
    session.add(healthy)
    await session.commit()
    await seed_active_agent(session)
    capture = wire_fakes(client)

    # A file that exists but is not failed.
    r1 = await client.post(f"/pipeline/files/{healthy.id}/analysis-failed/retry")
    assert r1.status_code == 200
    assert "no failed files to retry" in r1.text.lower()

    # A completely unknown id.
    r2 = await client.post(f"/pipeline/files/{uuid.uuid4()}/analysis-failed/retry")
    assert r2.status_code == 200
    assert "no failed files to retry" in r2.text.lower()

    assert capture == []


# --------------------------------------------------------------------------------------------------
# Bulk "Retry all failed · Analyze" still routes through the guarded funnel (regression backstop).
# --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bulk_retry_reenqueues_all_failed_through_guarded_funnel(client: AsyncClient, session: AsyncSession) -> None:
    """The bulk endpoint re-drives EVERY failed analyze file on the per-agent queue (never default)."""
    files = [await _seed_failed_file(session) for _ in range(3)]
    await seed_active_agent(session)
    _, task_router = install_fake_queues(client)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200
    assert "re-queued 3 failed file(s) for analysis" in response.text.lower()

    queue = task_router.queues["nox-analyze"]
    assert queue.name != "default"
    assert {p["file_id"] for _t, p in queue.captured} == {str(f.id) for f in files}


@pytest.mark.asyncio
async def test_bulk_retry_no_active_agent_is_amber_no_enqueue(client: AsyncClient, session: AsyncSession) -> None:
    """Phase-30 guard / T-87-25 on the bulk path: no agent -> amber ack, nothing enqueued or flipped."""
    files = [await _seed_failed_file(session) for _ in range(2)]
    fids = [f.id for f in files]  # capture before expiry
    capture = wire_fakes(client)

    response = await client.post("/pipeline/analysis-failed/retry")
    assert response.status_code == 200
    assert "no active agent" in response.text.lower()
    assert capture == []

    session.expire_all()
    rows = (await session.execute(select(FileRecord).where(FileRecord.id.in_(fids)))).scalars().all()
    assert {r.state for r in rows} == {FileState.ANALYSIS_FAILED}


# --------------------------------------------------------------------------------------------------
# Task 2 render assertions (files_table_view.html + _stage_matrix.html).  `-k render`
# --------------------------------------------------------------------------------------------------
def _render_files_table(*, bucket: str, active_stage: str | None = None, active_bucket: str | None = None) -> str:
    """Render files_table_view.html with a single row whose stage cells carry ``bucket``.

    Rendered through FastAPI's ``Jinja2Templates`` (the same safe wrapper prod uses, so autoescape +
    include resolution match production exactly), mirroring ``tests/shared/test_stage_pill_render.py``.
    """
    from pathlib import Path
    from types import SimpleNamespace

    from fastapi.templating import Jinja2Templates
    from starlette.requests import Request

    templates_dir = Path(__file__).resolve().parent.parent.parent / "src" / "phaze" / "templates"
    _templates = Jinja2Templates(directory=str(templates_dir))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/pipeline/files",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": None,
    }
    request = Request(scope=scope)  # type: ignore[arg-type]

    file = _make_file()
    buckets = {
        "metadata": bucket,
        "fingerprint": bucket,
        "analyze": bucket,
        "propose": "not_started",
        "review": "not_started",
        "apply": "not_started",
    }
    row = SimpleNamespace(file=file, buckets=buckets)
    files_page = SimpleNamespace(rows=[row], page=1, page_size=25, has_next=False)
    response = _templates.TemplateResponse(
        request=request,
        name="pipeline/partials/files_table_view.html",
        context={"files_page": files_page, "active_stage": active_stage, "active_bucket": active_bucket},
    )
    return response.body.decode()


def test_render_per_row_retry_only_on_failed_enrich_cell() -> None:
    """A per-row Retry renders ONLY on a failed metadata/analyze cell — never on a healthy row (UI-02/D-04)."""
    failed = _render_files_table(bucket="failed", active_stage="analyze", active_bucket="failed")
    assert "analysis-failed/retry" in failed
    assert "metadata-failed/retry" in failed
    assert 'aria-label="Retry Analyze for this file"' in failed
    assert 'aria-label="Retry Metadata for this file"' in failed

    healthy = _render_files_table(bucket="done")
    assert "analysis-failed/retry" not in healthy
    assert "metadata-failed/retry" not in healthy


def test_render_no_manual_fingerprint_retry_control() -> None:
    """Fingerprint failures self-retry via the pending set — NO manual fingerprint retry control (RESEARCH)."""
    failed = _render_files_table(bucket="failed", active_stage="fingerprint", active_bucket="failed")
    assert "fingerprint-failed/retry" not in failed
    assert "fingerprint/retry" not in failed


def test_render_bulk_retry_all_button_on_failed_filter_view() -> None:
    """The bulk "Retry all failed · {stage}" renders on the failed-filter view for an enrich stage (UI-02)."""
    analyze = _render_files_table(bucket="failed", active_stage="analyze", active_bucket="failed")
    assert "Retry all failed" in analyze
    assert "/pipeline/analysis-failed/retry" in analyze

    metadata = _render_files_table(bucket="failed", active_stage="metadata", active_bucket="failed")
    assert "/pipeline/metadata-failed/retry" in metadata

    # No bulk retry button on an unfiltered / non-failed view.
    unfiltered = _render_files_table(bucket="done")
    assert "Retry all failed" not in unfiltered


def test_render_no_bulk_fingerprint_retry_button() -> None:
    """Even on the fingerprint failed-filter view there is NO bulk fingerprint retry (self-retrying stage)."""
    fp = _render_files_table(bucket="failed", active_stage="fingerprint", active_bucket="failed")
    assert "fingerprint-failed/retry" not in fp
    assert "Retry all failed" not in fp
