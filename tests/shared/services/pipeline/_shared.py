"""Shared prelude for the split `tests/shared/services/pipeline/test_*.py` suite (phaze-7l8jh).

`tests/shared/services/test_pipeline.py` (2,351 lines, 99 tests) was the fourth-highest
weighted_deficit test file in the repo -- and its production counterpart,
`src/phaze/services/pipeline.py`, was already split into the `services/pipeline/` package
(phaze-vsqpr: agents.py, analyze.py, buckets.py, cloud.py, common.py, failures.py, files.py,
jobs.py, orphans.py, pending.py, proposals.py, reconciliation.py, stages.py, tracklists.py). The
test file never followed. This package mirrors that split, one test file per production
submodule this suite exercises (`common.py` and `orphans.py` have no direct unit test in this
suite today -- `_safe_count` and the orphan-count helpers are exercised indirectly through the
callers this suite DOES cover, and through `tests/shared/services/test_orphan_cache.py` /
`tests/integration/test_orphan_count.py` for `orphans.py` specifically -- so no
`test_common.py` / `test_orphans.py` exists here; adding direct coverage is a separate decision,
out of scope for a pure restructuring).

This module holds everything that ISN'T a `def test_*`: the shared imports and every helper
function/class/constant the split files import from. Nothing here was rewritten -- every symbol
is a verbatim carry-over from the original file, so no test's behavior changed as a side effect
of the split.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import text

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.tracklist import Tracklist
from phaze.services import pipeline as pipeline_mod
from phaze.services.pipeline import (
    analyze as pipeline_analyze_mod,
    analyze_lanes_content_hash,
    cloud as pipeline_cloud_mod,
    count_active_agents,
    count_backfill_candidates,
    count_inflight_jobs,
    count_proposal_pending_files,
    deduped_count,
    get_agent_lane_depths,
    get_agent_recent_scans,
    get_agent_reconciliations,
    get_analysis_failed_count,
    get_analysis_failed_files,
    get_analysis_stalled_count,
    get_analyze_files_page,
    get_analyze_working_set,
    get_awaiting_cloud_count,
    get_backfill_candidates,
    get_discovered_files_with_duration,
    get_global_reconciliation,
    get_match_busy_count,
    get_match_pending_tracklists,
    get_metadata_activity_summary,
    get_metadata_pending_files,
    get_proposal_busy_count,
    get_proposal_pending_batches,
    get_pushed_count,
    get_pushing_count,
    get_queue_activity,
    get_scanned_total,
    get_stage_activity_counts,
    get_stage_busy_counts,
    get_stage_progress,
    get_untracked_files,
    stages as pipeline_stages_mod,
)
from tests._queue_fakes import FakeQueue, FakeTaskRouter, seed_active_agent


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# ANALYSIS_FAILED bucket (Phase 44, D-02; Phase 90 PR-A: derived from failed_clause) — count/list
# ---------------------------------------------------------------------------


def _failed_file(i: int) -> FileRecord:
    """Build a FileRecord seed in the given state (default ANALYSIS_FAILED)."""
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=f"f{i:063d}"[:64],
        original_path=f"/music/failed{i}.mp3",
        original_filename=f"failed{i}.mp3",
        current_path=f"/music/failed{i}.mp3",
        file_type="mp3",
        file_size=1000,
    )


def _failed_analysis_for(file_id: uuid.UUID, error_message: str = "boom") -> AnalysisResult:
    """Build an analyze-FAILURE marker (analysis row, ``failed_at`` set) for ``file_id`` (Phase 90 D-09).

    This is the DERIVED source the cutover reads via ``failed_clause(Stage.ANALYZE)`` -- an
    ``analysis`` row whose ``failed_at`` is non-NULL. Distinct from a completed row (``failed_at``
    NULL, ``analysis_completed_at`` set); the XOR CHECK forbids both being set.

    ``error_message`` defaults to a generic detail (crashed/error shape). Pass the real
    ``"timeout: <detail>"`` prefix (``routers/agent_analysis.py::report_analysis_failed``'s
    composed format) to seed a heartbeat-STALLED marker for :func:`get_analysis_stalled_count`.
    """
    return AnalysisResult(id=uuid.uuid4(), file_id=file_id, failed_at=datetime.now(UTC), error_message=error_message)


def _completed_analysis_for(file_id: uuid.UUID, fine_done: int | None = None, fine_total: int | None = None) -> AnalysisResult:
    """Build a completed analyze marker (``analysis_completed_at`` set, ``failed_at`` NULL) for ``file_id``."""
    return AnalysisResult(
        id=uuid.uuid4(),
        file_id=file_id,
        analysis_completed_at=datetime.now(UTC),
        fine_windows_analyzed=fine_done,
        fine_windows_total=fine_total,
    )


# ---------------------------------------------------------------------------
# get_stage_busy_counts (t7k FIX2) — per-stage in-flight gate, degrade-safe
# ---------------------------------------------------------------------------


class _NullSavepoint:
    """Async-context-manager stand-in for ``session.begin_nested()`` in the fake-session tests.

    ``__aexit__`` returns ``False`` so an exception raised inside the ``async with`` block (the
    saq_jobs read) propagates out to ``get_stage_busy_counts``'s degrade ``except`` — exactly as a
    real SAVEPOINT does after ``ROLLBACK TO SAVEPOINT``.
    """

    async def __aenter__(self) -> _NullSavepoint:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# get_match_busy_count (Phase 41, REQ-41-3) — the controller-task in-flight gate over the
# saq_jobs table, degrade-safe.
# ---------------------------------------------------------------------------


class _BusyResult:
    """Minimal ``.all()`` result double over a fixed list of ``(fn_prefix, count)`` rows."""

    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, int]]:
        return self._rows


class _BusySession:
    """Fake session whose ``execute`` returns the seeded grouped-prefix rows inside a SAVEPOINT."""

    def __init__(self, rows: list[tuple[str, int]]) -> None:
        self._rows = rows

    def begin_nested(self) -> _NullSavepoint:
        return _NullSavepoint()

    async def execute(self, *_args: object, **_kwargs: object) -> _BusyResult:
        return _BusyResult(self._rows)


# ---------------------------------------------------------------------------
# get_match_pending_tracklists (Phase 41, REQ-41-2) — the exact complement of
# get_stage_progress match.done. (phaze-2akf removed its get_scrape_pending_tracklists sibling
# along with the SCRAPE ALL trigger and the ``scrape`` stage node it complemented.)
# ---------------------------------------------------------------------------


def _make_tracklist(n: int) -> Tracklist:
    """Build a bare Tracklist row (no version, no discogs chain)."""
    uid = uuid.uuid4()
    return Tracklist(id=uid, external_id=f"tl-{n}-{uid.hex}", source_url=f"http://x/{n}")


# ---------------------------------------------------------------------------
# Phase 42 (D-03 anti-drift): shared pending-set helpers + queue-loss detector.
# These four helpers are the ONE source of truth the manual DAG triggers AND the
# recovery producer read, so the two paths cannot drift apart.
# ---------------------------------------------------------------------------


def _make_pipeline_file(*, file_type: str = "mp3") -> FileRecord:
    """Build a fully-populated FileRecord row for the pending-set helper tests."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.{file_type}",
        original_filename=f"{uid.hex}.{file_type}",
        current_path=f"/music/{uid.hex}.{file_type}",
        file_type=file_type,
        file_size=1000,
    )


def _backend(backend_id: str, kind: str) -> SimpleNamespace:
    """A minimal registry-entry stand-in — non_local_backend_kinds reads only ``.id`` / ``.kind``."""
    return SimpleNamespace(id=backend_id, kind=kind)


def _backend_settings(*backends: SimpleNamespace) -> SimpleNamespace:
    """A minimal settings stand-in carrying only ``.backends`` (the sole attribute the derivation reads)."""
    return SimpleNamespace(backends=list(backends))


def _inflight_analysis_for(file_id: uuid.UUID, fine_done: int = 5, fine_total: int = 10) -> AnalysisResult:
    """Build a mid-flight analyze marker: an ``analysis`` row with NO completed/failed timestamp (57.1 N/M)."""
    return AnalysisResult(id=uuid.uuid4(), file_id=file_id, fine_windows_analyzed=fine_done, fine_windows_total=fine_total)


# ---------------------------------------------------------------------------
# get_straggler_count / _job_started_ms (Phase 44, D-01) — REMOVED by phaze-g84sk. The
# straggler bucket's running-age proxy for "stuck" was superseded by phaze-w55w1's
# progress-heartbeat stall watchdog: a genuinely wedged job now dies on its own and lands in
# ANALYSIS_FAILED (reason="timeout"), and a healthy multi-hour analysis is no longer
# distinguishable from a stuck one by elapsed time. See services/pipeline.py and the
# phaze-g84sk bead comment for the full writeup.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scanned / deduped / unique reconciliation (quick 260622-i0w) — turns the
# Discovery-count vs agent-scan-total gap into a self-explaining reconciliation.
#   scanned   = SUM over agents of (each agent's LATEST completed batch).total_files
#   deduped   = max(0, scanned - discovery_done)  [global: discovery_done = COUNT(all files)]
#   per-agent = max(0, agent_latest_total_files - agent file-row count)
# A None scanned (no completed batches / DB error) hides the whole line.
# ---------------------------------------------------------------------------


def _completed_batch(agent_id: str, total_files: int, *, status: str = ScanStatus.COMPLETED.value, created_at: object = None) -> ScanBatch:
    """Build a ScanBatch seed; set ``created_at`` explicitly when latest-per-agent ordering matters."""
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id=agent_id,
        scan_path="/music",
        status=status,
        total_files=total_files,
        processed_files=total_files,
    )
    if created_at is not None:
        batch.created_at = created_at  # type: ignore[assignment]
    return batch


def _recon_file(agent_id: str, i: int) -> FileRecord:
    """Build a unique FileRecord owned by ``agent_id`` (the reconciliation groups by agent_id)."""
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{agent_id}/{i}-{uid.hex}.mp3",
        original_filename=f"{i}.mp3",
        current_path=f"/music/{agent_id}/{i}-{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        agent_id=agent_id,
    )


# ---------------------------------------------------------------------------
# Phase 49 duration-routing helpers (D-05, D-09/D-10): duration join,
# awaiting-cloud count, and backfill candidates (ANALYSIS_FAILED + duration>=N)
# ---------------------------------------------------------------------------


def _file(i: int) -> FileRecord:
    """Build a FileRecord seed in the given state (unique hash/path per ``i``)."""
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=f"d{i:063d}"[:64],
        original_path=f"/music/dur{i}.mp3",
        original_filename=f"dur{i}.mp3",
        current_path=f"/music/dur{i}.mp3",
        file_type="mp3",
        file_size=1000,
    )


def _metadata_for(file_id: uuid.UUID, duration: float | None) -> FileMetadata:
    """Build a FileMetadata row for ``file_id`` carrying ``duration`` (or None)."""
    return FileMetadata(id=uuid.uuid4(), file_id=file_id, duration=duration)


async def _seed_process_file_ledger(session: AsyncSession, *files: FileRecord) -> None:
    """Seed a ``process_file:<id>`` scheduling-ledger row per file.

    Phase 55 (L4) scopes the backfill candidate query to *previously-scheduled* work: a file
    is a candidate only if such a ledger row exists (a SAQ timeout abandons the job without
    clearing the row, so it persists into ANALYSIS_FAILED). These tests assert the state +
    duration filter, so every failed file is ledgered — exclusions come from state/duration,
    not a missing ledger row.
    """
    from phaze.services.scheduling_ledger import insert_ledger_if_absent

    for f in files:
        await insert_ledger_if_absent(
            session,
            key=f"process_file:{f.id}",
            function="process_file",
            kwargs={},
            timeout=7200,
            retries=2,
        )


async def _seed_cloud_job(session: AsyncSession, file_index: int, status: CloudJobStatus, *, backend_id: str | None = None) -> None:
    """Seed a ``(FileRecord, cloud_job)`` pair; the cloud_job carries ``status`` (Phase 90 D-12) + an
    optional ``backend_id`` (phaze-zyoag: the per-backend-kind seam)."""
    f = _file(file_index)
    session.add(f)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=f.id, status=status.value, backend_id=backend_id))


# phaze-zyoag: a kueue-only registry (mirrors the bug report's "vox" lane) and a kueue+compute
# registry (acceptance 3: one row of each kind must land correctly SIMULTANEOUSLY). Both reuse the
# exact shape `tests/analyze/routers/test_lane_detail.py` already establishes for a kueue backend.
_KUEUE_ONLY_TOML = """
[[backends]]
kind = "kueue"
id = "vox"
rank = 10
cap = 3
buckets = ["burst-vox"]

  [backends.kube]
  api_url = "https://kube.example:6443"
  namespace = "phaze"
  local_queue = "phaze-burst"

[[backends]]
kind = "local"
id = "local"
rank = 99
cap = 1

[[buckets]]
id = "burst-vox"
scope = "cluster-specific"
bucket = "phaze-burst"
endpoint_url = "https://s3.example"
"""

_KUEUE_AND_COMPUTE_TOML = """
[[backends]]
kind = "kueue"
id = "vox"
rank = 10
cap = 3
buckets = ["burst-vox"]

  [backends.kube]
  api_url = "https://kube.example:6443"
  namespace = "phaze"
  local_queue = "phaze-burst"

[[backends]]
kind = "compute"
id = "a1"
rank = 20
cap = 2
agent_ref = "a1-node"
scratch_dir = "/scratch/a1"
push_host = "a1.push"

[[backends]]
kind = "local"
id = "local"
rank = 99
cap = 1

[[buckets]]
id = "burst-vox"
scope = "cluster-specific"
bucket = "phaze-burst"
endpoint_url = "https://s3.example"
"""


# ---------------------------------------------------------------------------
# get_agent_recent_scans (phaze-c6j5): the LIMIT boundary must be deterministic
# on a created_at tie, not arbitrary heap order.
# ---------------------------------------------------------------------------


def _scan_batch(agent_id: str, *, batch_id: uuid.UUID, created_at: object = None) -> ScanBatch:
    """Build a ScanBatch with an INJECTABLE id, for tiebreaker tests that need a fixed pk.

    ``scan_path`` is per-batch-id (not a shared literal): phaze-1a71's
    ``uq_scan_batches_agent_id_scan_path_running`` allows at most one RUNNING batch per
    (agent_id, scan_path), and these tests seed many RUNNING rows for the SAME agent to
    exercise the id tiebreaker -- a shared path would collide with that guard.
    """
    batch = ScanBatch(
        id=batch_id,
        agent_id=agent_id,
        scan_path=f"/music/{batch_id}",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    if created_at is not None:
        batch.created_at = created_at  # type: ignore[assignment]
    return batch


__all__ = [
    "TYPE_CHECKING",
    "UTC",
    "_KUEUE_AND_COMPUTE_TOML",
    "_KUEUE_ONLY_TOML",
    "Agent",
    "AnalysisResult",
    "CloudJob",
    "CloudJobStatus",
    "FakeQueue",
    "FakeTaskRouter",
    "FileMetadata",
    "FileRecord",
    "ProposalStatus",
    "RenameProposal",
    "ScanBatch",
    "ScanStatus",
    "SimpleNamespace",
    "Tracklist",
    "_BusyResult",
    "_BusySession",
    "_NullSavepoint",
    "_backend",
    "_backend_settings",
    "_completed_analysis_for",
    "_completed_batch",
    "_failed_analysis_for",
    "_failed_file",
    "_file",
    "_inflight_analysis_for",
    "_make_pipeline_file",
    "_make_tracklist",
    "_metadata_for",
    "_recon_file",
    "_scan_batch",
    "_seed_cloud_job",
    "_seed_process_file_ledger",
    "analyze_lanes_content_hash",
    "asyncio",
    "count_active_agents",
    "count_backfill_candidates",
    "count_inflight_jobs",
    "count_proposal_pending_files",
    "datetime",
    "deduped_count",
    "get_agent_lane_depths",
    "get_agent_recent_scans",
    "get_agent_reconciliations",
    "get_analysis_failed_count",
    "get_analysis_failed_files",
    "get_analysis_stalled_count",
    "get_analyze_files_page",
    "get_analyze_working_set",
    "get_awaiting_cloud_count",
    "get_backfill_candidates",
    "get_discovered_files_with_duration",
    "get_global_reconciliation",
    "get_match_busy_count",
    "get_match_pending_tracklists",
    "get_metadata_activity_summary",
    "get_metadata_pending_files",
    "get_proposal_busy_count",
    "get_proposal_pending_batches",
    "get_pushed_count",
    "get_pushing_count",
    "get_queue_activity",
    "get_scanned_total",
    "get_stage_activity_counts",
    "get_stage_busy_counts",
    "get_stage_progress",
    "get_untracked_files",
    "pipeline_analyze_mod",
    "pipeline_cloud_mod",
    "pipeline_mod",
    "pipeline_stages_mod",
    "pytest",
    "seed_active_agent",
    "text",
    "timedelta",
    "uuid",
]
