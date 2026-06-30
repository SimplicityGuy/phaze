"""Behavioral tests for the v7.0 Enrich + Analyze workspaces (Phase 58, WORK-01..05 + R-5).

This is the single Phase-58 test file (Wave 0, task 58-01-00 / 58-VALIDATION.md). It defines
the full Phase-58 test surface up front:

* The two **foundation** tests are FILLED here:
    - ``test_stage_fragment_is_bare``   -> R-5    (fragment correctness; mirrors
      ``test_shell_routes.py::test_stage_fragment_is_bare``)
    - ``test_single_poll_discipline``   -> WORK-05 / R-2 / R-3 (one chrome poll +
      ``visibilitychange`` shed; no second loop in any workspace fragment). This test FAILS
      until Plan 58-01 Task 1 wires the chrome poll into ``shell/shell.html`` -- that is the
      expected RED state at task 58-01-00.

* The four **workspace** tests are ``xfail`` stubs that COLLECT cleanly now and are converted
  to real assertions by their owning task (see 58-VALIDATION.md Per-Task Verification Map):
    - ``test_discover_workspace``                 -> WORK-01 (Plan 58-02, task 58-02-03)
    - ``test_metadata_trigger_all_wired``         -> WORK-02 (Plan 58-03, task 58-03-02)
    - ``test_lane_cards_states``                  -> WORK-03 (Plan 58-04, task 58-04-02)
    - ``test_analyze_file_table_lane_and_windows``-> WORK-04 (Plan 58-04, task 58-04-03)

The module-level ``_seed_*`` helpers below are test fixtures (ORM inserts only -- never a
backend change). Plans 58-02..04 use them to seed the analyze-stage rows (incl. partial-window
in-flight rows for the WORK-04 mid-flight assertion) and the cloud_job lane/admission states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord, FileState


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# The four redesigned workspace stages whose HX fragments must ride the ONE chrome poll
# (no per-fragment ``hx-trigger="every"`` / ``setInterval``).
_WORKSPACE_STAGES = ["discover", "metadata", "fingerprint", "analyze"]


# ---------------------------------------------------------------------------
# Module-level async seed helpers (test fixtures -- ORM inserts only, no backend change).
# Plans 58-02..04 build their workspace assertions on these. They live here (not conftest)
# because they are Phase-58-specific shapes; ``conftest.py`` already seeds the legacy agent
# so a bare FileRecord satisfies its NOT NULL + FK ``agent_id`` default.
# ---------------------------------------------------------------------------


async def _seed_file(
    session: AsyncSession,
    *,
    state: str = FileState.ANALYZED,
    original_filename: str = "set.mp3",
    file_type: str = "mp3",
    file_size: int = 1024,
) -> FileRecord:
    """Insert one FileRecord (legacy-agent default) and return it.

    The parent row every ``_seed_analysis`` / ``_seed_cloud_job`` FK points at.
    """
    file_id = uuid.uuid4()
    record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,  # 64 hex chars
        original_path=f"/test/music/{original_filename}",
        original_filename=original_filename,
        current_path=f"/test/music/{original_filename}",
        file_type=file_type,
        file_size=file_size,
        state=state,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def _seed_analysis(
    session: AsyncSession,
    file_id: uuid.UUID,
    fine_done: int | None,
    fine_total: int | None,
) -> AnalysisResult:
    """Insert the 1:1 ``analysis`` aggregate row for ``file_id``.

    ``fine_done < fine_total`` models an in-flight file (the 57.1 PR #184 mid-flight
    ``fine_windows_analyzed/total`` signal); ``fine_done == fine_total`` models a completed
    file's full window coverage (WORK-04).
    """
    result = AnalysisResult(
        file_id=file_id,
        fine_windows_analyzed=fine_done,
        fine_windows_total=fine_total,
    )
    session.add(result)
    await session.commit()
    await session.refresh(result)
    return result


async def _seed_cloud_job(
    session: AsyncSession,
    file_id: uuid.UUID,
    cloud_phase: str | None,
    *,
    status: str = CloudJobStatus.RUNNING,
    inadmissible: bool = False,
) -> CloudJob:
    """Insert the per-file ``cloud_job`` sidecar for ``file_id``.

    ``cloud_phase`` drives the k8s admission-state surfaces (WORK-03); NULL models an
    a1/local row. ``inadmissible`` flags the Kueue fault path (the ``role="alert"`` banner).
    """
    job = CloudJob(
        file_id=file_id,
        s3_key=f"staging/{file_id}",
        status=status,
        cloud_phase=cloud_phase,
        inadmissible=inadmissible,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# Foundation tests (FILLED in task 58-01-00).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_fragment_is_bare(client: AsyncClient) -> None:
    """R-5 -- an HX ``/s/<stage>`` response is a bare fragment (no document wrapper/head).

    Mirrors ``test_shell_routes.py::test_stage_fragment_is_bare``: a swapped workspace
    fragment NEVER carries ``<html>``/``<head>`` (no duplicate landmarks/skip-links). The
    chrome -- including the single poll element -- persists across swaps, so the dead-template
    AST guard stays green and the fragment never re-injects the poll.
    """
    hx = await client.get("/s/discover", headers={"HX-Request": "true"})
    assert hx.status_code == 200
    assert "<html" not in hx.text
    assert "<head" not in hx.text


@pytest.mark.asyncio
async def test_single_poll_discipline(client: AsyncClient) -> None:
    """WORK-05 / R-2 / R-3 -- exactly one chrome poll + ``visibilitychange`` shed; no 2nd loop.

    The full shell (``GET /``) fires the live refresh from persistent chrome: EXACTLY ONE
    ``hx-get="/pipeline/stats"`` element, and a ``visibilitychange`` listener that sheds
    polling while the tab is backgrounded. No swappable workspace fragment may carry its own
    ``hx-trigger="every"`` poll or a ``setInterval`` loop -- every workspace's live values ride
    the one chrome poll via ``hx-swap-oob`` against the existing ``stats_bar.html`` seeds.
    """
    shell = await client.get("/")
    assert shell.status_code == 200
    body = shell.text
    # Exactly one persistent poll element in chrome (R-2).
    assert body.count('hx-get="/pipeline/stats"') == 1, "shell must fire exactly one /pipeline/stats poll"
    # The foreground/background shed (R-3).
    assert "visibilitychange" in body, "shell must shed polling on visibilitychange"

    # No workspace fragment starts a second poll loop (R-2).
    for stage in _WORKSPACE_STAGES:
        frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert frag.status_code == 200
        assert 'hx-trigger="every' not in frag.text, f"{stage} fragment must not start a second poll loop"
        assert "setInterval" not in frag.text, f"{stage} fragment must not use setInterval"


# ---------------------------------------------------------------------------
# Workspace tests -- xfail stubs converted to real assertions by their owning plan/task.
# (names + reasons per 58-VALIDATION.md Per-Task Verification Map)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(reason="lands in Plan 58-02 (task 58-02-03)", strict=False)
async def test_discover_workspace(client: AsyncClient) -> None:
    """WORK-01 -- Discover workspace: recent-scans table + not-yet-enriched sub-count + SCAN/RECOVER."""
    raise NotImplementedError


@pytest.mark.asyncio
@pytest.mark.xfail(reason="lands in Plan 58-03 (task 58-03-02)", strict=False)
async def test_metadata_trigger_all_wired(client: AsyncClient) -> None:
    """WORK-02 -- Metadata/Fingerprint ALL buttons post to the existing endpoints; no EXTRACT SELECTED (D-02)."""
    raise NotImplementedError


@pytest.mark.asyncio
@pytest.mark.xfail(reason="lands in Plan 58-04 (task 58-04-02)", strict=False)
async def test_lane_cards_states(client: AsyncClient) -> None:
    """WORK-03 -- all 3 lane cards always render; not-configured vs offline labels; Inadmissible role=alert."""
    raise NotImplementedError


@pytest.mark.asyncio
@pytest.mark.xfail(reason="lands in Plan 58-04 (task 58-04-03)", strict=False)
async def test_analyze_file_table_lane_and_windows(client: AsyncClient) -> None:
    """WORK-04 -- per-file lane badge + mid-flight N/M windows (in-flight) / full coverage (completed)."""
    raise NotImplementedError
