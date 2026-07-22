"""Tests for get_stage_progress -- the authoritative per-stage output-table reconcile (D-03).

The discriminating test is :func:`test_analyzed_but_no_metadata_counts_independently`:
a file with an ``analysis`` row but NO ``metadata`` row must yield ``analyze.done == 1``
and ``metadata.done == 0``. The linear ``FileRecord.state`` enum (a single value per file)
structurally cannot express that -- proving get_stage_progress counts the OUTPUT TABLES,
not the state machine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services import pipeline as pipeline_mod
from phaze.services.pipeline import _safe_count, get_stage_progress


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _make_file(i: int, *, file_type: str = "mp3") -> FileRecord:
    """Build a FileRecord with a unique path/hash (agent_id defaults to the seeded legacy agent)."""
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=f"{i:064d}"[:64],
        original_path=f"/music/f{i}.{file_type}",
        original_filename=f"f{i}.{file_type}",
        current_path=f"/music/f{i}.{file_type}",
        file_type=file_type,
        file_size=1000,
    )


@pytest.mark.asyncio
async def test_empty_db_returns_zeros_and_scan_search_has_no_denominator(session: AsyncSession):
    """An empty DB yields done=0 everywhere and scan_search.total stays None (em-dash sentinel)."""
    progress = await get_stage_progress(session)

    for node in ("discovery", "metadata", "fingerprint", "analyze", "scan_search", "scrape", "match", "proposals", "execute"):
        assert progress[node]["done"] == 0, node

    # The tracklist head node never fabricates a denominator.
    assert progress["scan_search"]["total"] is None


@pytest.mark.asyncio
async def test_analyzed_but_no_metadata_counts_independently(session: AsyncSession):
    """THE KEY TEST: a file with an analysis row but no metadata row -> analyze.done==1, metadata.done==0.

    A single ``FileRecord.state`` enum could never report this: the parallel stages must
    read their OWN output tables, not the linear state machine.
    """
    f = _make_file(1)
    session.add(f)
    await session.flush()
    # done(analyze) is the canonical derivation-layer clause: analysis_completed_at IS NOT NULL
    # (DERIV-03 / Phase 82 cutover). A bare partial-analysis row is in_flight, not done.
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=f.id, bpm=128.0, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    progress = await get_stage_progress(session)

    assert progress["analyze"]["done"] == 1
    assert progress["metadata"]["done"] == 0


@pytest.mark.asyncio
async def test_fingerprint_done_counts_success_status(session: AsyncSession):
    """fingerprint.done counts DISTINCT file_id whose fingerprint_results row is in a done state.

    Regression for WR-02: the engine adapters persist ``status="success"`` via ``put_fingerprint``
    (``fingerprint.py``); ``"completed"`` is NEVER written on that path. Counting only
    ``status == "completed"`` therefore made ``fingerprint.done`` permanently 0 in production.
    done now counts the real ``"success"`` value (and tolerates the defensive ``"completed"``,
    matching ``_trackid_engine_badge``); ``"failed"`` and ``"pending"`` must NOT count.
    """
    success_file = _make_file(1)  # the value production actually writes
    completed_file = _make_file(2)  # defensively tolerated
    failed_file = _make_file(3)
    pending_file = _make_file(4)
    session.add_all([success_file, completed_file, failed_file, pending_file])
    await session.flush()
    session.add(FingerprintResult(id=uuid.uuid4(), file_id=success_file.id, engine="chromaprint", status="success"))
    session.add(FingerprintResult(id=uuid.uuid4(), file_id=completed_file.id, engine="chromaprint", status="completed"))
    session.add(FingerprintResult(id=uuid.uuid4(), file_id=failed_file.id, engine="chromaprint", status="failed"))
    session.add(FingerprintResult(id=uuid.uuid4(), file_id=pending_file.id, engine="chromaprint", status="pending"))
    await session.commit()

    progress = await get_stage_progress(session)

    # success + completed both count; failed + pending are excluded.
    assert progress["fingerprint"]["done"] == 2


@pytest.mark.asyncio
async def test_metadata_denominator_is_music_video_count(session: AsyncSession):
    """metadata/fingerprint/analyze share the music+video file count as their denominator."""
    music = _make_file(1, file_type="mp3")
    video = _make_file(2, file_type="mp4")
    companion = _make_file(3, file_type="cue")  # not music/video -> excluded from the denominator
    session.add_all([music, video, companion])
    await session.commit()

    progress = await get_stage_progress(session)

    assert progress["metadata"]["total"] == 2
    assert progress["fingerprint"]["total"] == 2
    assert progress["analyze"]["total"] == 2


@pytest.mark.asyncio
async def test_scan_search_done_counts_tracklists_without_total(session: AsyncSession):
    """scan_search.done = DISTINCT file_id in tracklists; total stays None (no fabricated denominator)."""
    f1 = _make_file(1)
    f2 = _make_file(2)
    session.add_all([f1, f2])
    await session.flush()
    session.add(Tracklist(id=uuid.uuid4(), external_id="tl-1", source_url="http://x/1", file_id=f1.id))
    session.add(Tracklist(id=uuid.uuid4(), external_id="tl-2", source_url="http://x/2", file_id=f2.id))
    # A tracklist with no file_id must not inflate the distinct-file done-count.
    session.add(Tracklist(id=uuid.uuid4(), external_id="tl-3", source_url="http://x/3", file_id=None))
    await session.commit()

    progress = await get_stage_progress(session)

    assert progress["scan_search"]["done"] == 2
    assert progress["scan_search"]["total"] is None


@pytest.mark.asyncio
async def test_proposals_total_is_convergence_set(session: AsyncSession):
    """proposals.total = files with BOTH metadata AND analysis (the convergence gate)."""
    both = _make_file(1)
    meta_only = _make_file(2)
    analysis_only = _make_file(3)
    session.add_all([both, meta_only, analysis_only])
    await session.flush()
    session.add(FileMetadata(id=uuid.uuid4(), file_id=both.id))
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=both.id, bpm=120.0))
    session.add(FileMetadata(id=uuid.uuid4(), file_id=meta_only.id))
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=analysis_only.id, bpm=110.0))
    session.add(RenameProposal(id=uuid.uuid4(), file_id=both.id, proposed_filename="x.mp3", status=ProposalStatus.PENDING))
    await session.commit()

    progress = await get_stage_progress(session)

    assert progress["proposals"]["total"] == 1  # only the file with BOTH metadata and analysis
    assert progress["proposals"]["done"] == 1


@pytest.mark.asyncio
async def test_scrape_and_match_count_distinct_tracklist_id(session: AsyncSession):
    """scrape counts DISTINCT tracklist_id in tracklist_versions; match walks discogs_links -> tracklist."""
    f = _make_file(1)
    session.add(f)
    await session.flush()
    tl = Tracklist(id=uuid.uuid4(), external_id="tl-1", source_url="http://x/1", file_id=f.id)
    session.add(tl)
    await session.flush()
    version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tl.id, version_number=1)
    session.add(version)
    await session.flush()
    track = TracklistTrack(id=uuid.uuid4(), version_id=version.id, position=1)
    session.add(track)
    await session.flush()
    session.add(DiscogsLink(id=uuid.uuid4(), track_id=track.id, discogs_release_id="r1", confidence=0.9))
    await session.commit()

    progress = await get_stage_progress(session)

    assert progress["scrape"]["done"] == 1
    assert progress["scrape"]["total"] == 1
    assert progress["match"]["done"] == 1
    assert progress["match"]["total"] == 1


@pytest.mark.asyncio
async def test_execute_counts_completed_execution_log(session: AsyncSession):
    """execute.done = DISTINCT file_id with a completed execution_log row; total = approved-proposal count."""
    f = _make_file(1)
    session.add(f)
    await session.flush()
    proposal = RenameProposal(id=uuid.uuid4(), file_id=f.id, proposed_filename="x.mp3", status=ProposalStatus.APPROVED)
    session.add(proposal)
    await session.flush()
    session.add(
        ExecutionLog(
            id=uuid.uuid4(),
            proposal_id=proposal.id,
            operation="move",
            source_path="/a",
            destination_path="/b",
            sha256_verified=True,
            status="completed",
        )
    )
    await session.commit()

    progress = await get_stage_progress(session)

    assert progress["execute"]["done"] == 1
    assert progress["execute"]["total"] == 1


@pytest.mark.asyncio
async def test_single_source_db_error_degrades_to_zero(session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """A forced error on ONE stage's read degrades that stage to its safe default; siblings intact, no raise.

    Post-CLEAN-01, ``get_stage_progress`` no longer reads the passed ``session`` -- every independent
    read runs in its OWN ``async_session`` via ``_read_in_own_session``. So the degrade is exercised at
    the fan-out seam: force the FINGERPRINT enrich-bucket read to RAISE (simulating a mid-read/
    acquisition failure that escapes ``_safe_bucket_counts``) and assert ``_read_in_own_session`` catches
    it -> fingerprint degrades to the all-zero bucket (``done == 0``) while the independently-sessioned
    metadata/analyze reads stay correct (no cross-node poisoning; the poll never raises).
    """
    f = _make_file(1)
    session.add(f)
    await session.flush()
    session.add(FileMetadata(id=uuid.uuid4(), file_id=f.id))
    # analysis_completed_at required for done(analyze) under the canonical derivation layer (DERIV-03 / Phase 82).
    session.add(AnalysisResult(id=uuid.uuid4(), file_id=f.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    session.add(FingerprintResult(id=uuid.uuid4(), file_id=f.id, engine="chromaprint", status="completed"))
    await session.commit()

    orig_buckets = pipeline_mod._safe_bucket_counts

    async def failing_buckets(read_session, stage):  # type: ignore[no-untyped-def]
        # Force ONLY the fingerprint enrich-bucket read to fail. The raise escapes _safe_bucket_counts
        # and must be caught by _read_in_own_session's acquisition-degrade belt (RESEARCH Pitfall 2).
        if stage is Stage.FINGERPRINT:
            raise RuntimeError("forced fingerprint source error")
        return await orig_buckets(read_session, stage)

    monkeypatch.setattr(pipeline_mod, "_safe_bucket_counts", failing_buckets)

    progress = await get_stage_progress(session)

    # The poisoned source degrades to its all-zero bucket (done=0) without raising...
    assert progress["fingerprint"]["done"] == 0
    # ...while sibling stages in their OWN sessions stay correct.
    assert progress["metadata"]["done"] == 1
    assert progress["analyze"]["done"] == 1


async def test_safe_count_swallows_begin_nested_failure() -> None:
    """_safe_count isolation must hold even when opening the SAVEPOINT itself fails: it logs and
    still returns 0 rather than letting the exception escape into the 5s poll."""

    class _BoomSession:
        def begin_nested(self) -> object:
            raise RuntimeError("forced begin_nested error")

    result = await _safe_count(_BoomSession(), select(FileRecord), node="metadata")  # type: ignore[arg-type]
    assert result == 0
