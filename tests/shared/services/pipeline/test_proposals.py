"""Tests for `services/pipeline/proposals.py` (split from test_pipeline.py, phaze-7l8jh).

count_proposal_pending_files, get_proposal_pending_batches -- `services/pipeline/proposals.py`.
"""

from __future__ import annotations

from tests.shared.services.pipeline._shared import *


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_sorts_then_chunks(session: AsyncSession) -> None:
    """Convergence files (metadata+analysis) are SORTED by id then chunked -- deterministic batches.

    Sorting before chunking is what aligns the generate_proposals:<sha256(sorted ids)> set-hash key
    between the manual trigger and recovery (42-RESEARCH Pitfall 2). Insert in arbitrary order and
    assert the batches are globally sorted and chunked by batch_size.
    """
    files = [_make_pipeline_file() for _ in range(3)]
    session.add_all(files)
    await session.flush()
    related: list[object] = []
    for f in files:
        related.append(FileMetadata(file_id=f.id, artist="A", title="T"))
        # Phase 57.1: a COMPLETED analysis row carries analysis_completed_at -- the convergence
        # gate now requires it IS NOT NULL, so the positive control must stamp it.
        related.append(AnalysisResult(file_id=f.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    session.add_all(related)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 2)

    flat = [fid for batch in batches for fid in batch]
    expected = sorted(str(f.id) for f in files)
    assert flat == expected  # globally sorted, deterministic membership
    assert [len(b) for b in batches] == [2, 1]  # 3 ids / batch_size 2


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_zero_batch_size_clamps_to_one(session: AsyncSession) -> None:
    """phaze-ceuvd: batch_size=0 used to raise ValueError (range() arg 3 must not be zero) --
    an unhandled 500 on GENERATE ALL. It must now degrade to one file per batch instead of
    crashing, and the full pending set must still be returned (no files dropped)."""
    files = [_make_pipeline_file() for _ in range(3)]
    session.add_all(files)
    await session.flush()
    related: list[object] = []
    for f in files:
        related.append(FileMetadata(file_id=f.id, artist="A", title="T"))
        related.append(AnalysisResult(file_id=f.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    session.add_all(related)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 0)

    flat = [fid for batch in batches for fid in batch]
    expected = sorted(str(f.id) for f in files)
    assert flat == expected  # full set still returned, nothing silently dropped
    assert [len(b) for b in batches] == [1, 1, 1]  # clamped to 1 -> one file per batch


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_negative_batch_size_clamps_to_one(session: AsyncSession) -> None:
    """phaze-ceuvd: batch_size<0 used to make range(0, N, -k) empty -- a silent no-op that
    returned success having enqueued ZERO batches while leaving the pending backlog untouched.
    It must now degrade to one file per batch instead of silently dropping the whole set."""
    files = [_make_pipeline_file() for _ in range(3)]
    session.add_all(files)
    await session.flush()
    related: list[object] = []
    for f in files:
        related.append(FileMetadata(file_id=f.id, artist="A", title="T"))
        related.append(AnalysisResult(file_id=f.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    session.add_all(related)
    await session.flush()

    batches = await get_proposal_pending_batches(session, -5)

    flat = [fid for batch in batches for fid in batch]
    expected = sorted(str(f.id) for f in files)
    assert flat == expected  # full set still returned, nothing silently dropped
    assert batches != []  # must NOT silently collapse to zero batches
    assert [len(b) for b in batches] == [1, 1, 1]  # clamped to 1 -> one file per batch


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_excludes_files_missing_metadata_or_analysis(session: AsyncSession) -> None:
    """Convergence gate: a file with ONLY metadata (no analysis) is NOT batched."""
    only_metadata = _make_pipeline_file()
    session.add(only_metadata)
    await session.flush()
    session.add(FileMetadata(file_id=only_metadata.id, artist="A", title="T"))
    await session.flush()

    batches = await get_proposal_pending_batches(session, 10)
    flat = [fid for batch in batches for fid in batch]
    assert str(only_metadata.id) not in flat


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_excludes_partial_analysis_row(session: AsyncSession) -> None:
    """Phase 57.1 KEY RISK: a METADATA_EXTRACTED file with a PARTIAL analysis row is NOT batched.

    Under D-03 an `analysis` row is upserted at analysis START (NULL aggregates, fine_windows_analyzed
    < total, analysis_completed_at NULL) while the file is still METADATA_EXTRACTED. That row would
    satisfy the old bare `exists(AnalysisResult)` gate and leak into generate_proposals with NULL
    bpm/key/mood. The tightened gate (analysis_completed_at IS NOT NULL) must return it in ZERO batches.
    Positive control: once analysis_completed_at is stamped, the SAME file appears -- proving the
    tighten did not over-exclude legitimate completed files.
    """
    pending = _make_pipeline_file()
    session.add(pending)
    await session.flush()
    session.add(FileMetadata(file_id=pending.id, artist="A", title="T"))
    # Partial in-flight row: NULL bpm, analyzed < total, NO completion stamp.
    partial = AnalysisResult(file_id=pending.id, bpm=None, fine_windows_analyzed=3, fine_windows_total=40, analysis_completed_at=None)
    session.add(partial)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 10)
    flat = [fid for batch in batches for fid in batch]
    assert str(pending.id) not in flat, "a partial (in-flight) analysis row must NOT leak into proposal batches"

    # Positive control: stamping completion makes the same file eligible.
    partial.analysis_completed_at = datetime.now(UTC)
    await session.flush()
    batches_after = await get_proposal_pending_batches(session, 10)
    flat_after = [fid for batch in batches_after for fid in batch]
    assert str(pending.id) in flat_after, "a completed analysis row MUST appear (tighten did not over-exclude)"


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_excludes_already_proposed_file(session: AsyncSession) -> None:
    """Phase 90 (PR-A, Pitfall 4): a file with an EXISTING proposal is NOT re-batched.

    The retired ``files.state IN (ANALYZED, METADATA_EXTRACTED)`` gate is replaced by
    ``~done_clause(Stage.PROPOSE)`` -- ``done_clause(PROPOSE)`` is "a RenameProposal row exists", so a
    file that already has a proposal is a DONE propose and MUST be excluded (no re-propose), even though
    it still carries its converging metadata + completed-analysis rows. A twin file with no proposal is
    the positive control that appears.
    """
    proposed = _make_pipeline_file()
    unproposed = _make_pipeline_file()
    session.add_all([proposed, unproposed])
    await session.flush()
    for f in (proposed, unproposed):
        session.add(FileMetadata(file_id=f.id, artist="A", title="T"))
        session.add(AnalysisResult(file_id=f.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    # Only ``proposed`` already carries a RenameProposal -> done_clause(PROPOSE) True -> excluded.
    session.add(RenameProposal(id=uuid.uuid4(), file_id=proposed.id, proposed_filename="x.mp3", status=ProposalStatus.PENDING.value))
    await session.flush()

    batches = await get_proposal_pending_batches(session, 10)
    flat = [fid for batch in batches for fid in batch]
    assert str(proposed.id) not in flat, "an already-proposed file must NOT be re-batched (Pitfall 4)"
    assert str(unproposed.id) in flat, "a not-yet-proposed converged file MUST still be batched"


@pytest.mark.asyncio
async def test_count_proposal_pending_files_agrees_with_the_batched_set(session: AsyncSession) -> None:
    """phaze-37i1.2: the counter and the batcher answer the SAME question, over every exclusion.

    ``count_proposal_pending_files`` feeds the Audit Log's "N files ready for proposal generation"
    affordance, which links straight to the GENERATE ALL trigger built from
    ``get_proposal_pending_batches``. If the two predicates ever diverged the page would quote a
    number the button does not honour -- a dishonest count is worse than no count, and is precisely
    the confusion this bead exists to remove. Seeds one eligible file plus one of EACH exclusion
    (metadata-only, in-flight analysis, already proposed) and asserts count == batched membership.
    """
    eligible = _make_pipeline_file()
    metadata_only = _make_pipeline_file()
    inflight_analysis = _make_pipeline_file()
    already_proposed = _make_pipeline_file()
    session.add_all([eligible, metadata_only, inflight_analysis, already_proposed])
    await session.flush()

    for f in (eligible, metadata_only, inflight_analysis, already_proposed):
        session.add(FileMetadata(file_id=f.id, artist="A", title="T"))
    session.add(AnalysisResult(file_id=eligible.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    session.add(AnalysisResult(file_id=inflight_analysis.id, bpm=None, analysis_completed_at=None))
    session.add(AnalysisResult(file_id=already_proposed.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    session.add(RenameProposal(id=uuid.uuid4(), file_id=already_proposed.id, proposed_filename="x.mp3", status=ProposalStatus.PENDING.value))
    await session.flush()

    count = await count_proposal_pending_files(session)
    flat = [fid for batch in await get_proposal_pending_batches(session, 10) for fid in batch]

    assert flat == [str(eligible.id)], "only the converged, unproposed file is batched"
    assert count == len(flat), "the counter must return exactly the size of the batched set"
