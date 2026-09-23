"""Tests for `services/pipeline/proposals.py` (split from test_pipeline.py, phaze-7l8jh).

count_proposal_pending_files, get_proposal_pending_batches -- `services/pipeline/proposals.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.services.pipeline._shared import (
    UTC,
    AnalysisResult,
    FileMetadata,
    ProposalStatus,
    RenameProposal,
    _make_pipeline_file,
    count_proposal_pending_files,
    datetime,
    get_proposal_pending_batches,
    pytest,
    seed_active_agent,
    uuid,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


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


# phaze-rhs6m: the propose convergence gate's METADATA conjunct.
#
# Operator decision 2026-08-24, answer as given verbatim and in full: "Close the gate asymmetry:
# require metadata failed_at IS NULL". Durable record: the operator-decision comment on phaze-rhs6m.
#
# WHY THESE TWO CELLS AND WHY AT THIS SEAM. Until this fix the metadata conjunct was a BARE
# `exists(FileMetadata)` while the analysis conjunct had carried a completion discriminator since
# Phase 57.1. A metadata FAILURE is stored as a `metadata` row with `failed_at` set and payload NULL
# (`routers/agent_metadata.py::report_metadata_failed`), so it satisfied bare existence -- a file
# whose metadata NEVER LANDED was proposable, and therefore approvable and EXECUTABLE. Once executed
# the file has moved on disk, but `done(metadata)` stays False until real metadata lands, so it sat
# in the metadata pending set FOREVER, where all four `ExtractMetadataPayload` producers re-drive it
# at `original_path` -- the ingest location it was just moved away from (D-24, schemas/agent_tasks.py).
#
# The first cell is the RED: it drives the REAL gate (`get_proposal_pending_batches`, the exact query
# POST /pipeline/proposals enqueues) against real Postgres and asserts REFUSAL. Against the pre-fix
# gate it FAILS -- the pre-fix behaviour was measured directly, as an acceptance, by the phaze-rhs6m
# step-1 reachability probe. The second cell is the ordinary case: metadata that genuinely SUCCEEDED
# is untouched, which is what keeps this a closed asymmetry rather than a narrowed pipeline.


@pytest.mark.asyncio
async def test_propose_gate_refuses_a_file_whose_metadata_failed(session: AsyncSession) -> None:
    """A metadata FAILURE row + a completed analysis must NOT be proposable (phaze-rhs6m).

    RED against the pre-fix gate, whose metadata conjunct was a bare `exists(FileMetadata)` and
    admitted this file. The analysis side is deliberately fully converged so the ONLY thing keeping
    the file out is the metadata conjunct -- if this cell ever goes green for the wrong reason, the
    positive control below is what catches it.
    """
    failed = _make_pipeline_file()
    session.add(failed)
    await session.flush()
    # The 81-03 failure shape: failed_at set, every payload column NULL.
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="tag read failed"))
    session.add(AnalysisResult(file_id=failed.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    await session.flush()

    batched = {fid for batch in await get_proposal_pending_batches(session, 10) for fid in batch}

    assert str(failed.id) not in batched, "a file whose metadata extraction FAILED must not be proposable"
    # The counter shares `_proposal_pending_clauses` with the batcher; assert it agrees, so the two
    # cannot drift into a dashboard that advertises a set GENERATE ALL will not batch (phaze-37i1.2).
    assert await count_proposal_pending_files(session) == 0


@pytest.mark.asyncio
async def test_propose_gate_still_accepts_a_file_whose_metadata_succeeded(session: AsyncSession) -> None:
    """The ordinary case is UNCHANGED: real metadata + a completed analysis is still proposable.

    The positive control for the cell above. `failed_at IS NULL` is the whole of what the new
    conjunct adds, so the never-failed path -- which is every one of the 11,428 files in the archive
    at the time of the change -- must behave exactly as before.
    """
    ok = _make_pipeline_file()
    session.add(ok)
    await session.flush()
    session.add(FileMetadata(file_id=ok.id, artist="A", title="T", failed_at=None))
    session.add(AnalysisResult(file_id=ok.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))
    await session.flush()

    batched = {fid for batch in await get_proposal_pending_batches(session, 10) for fid in batch}

    assert str(ok.id) in batched, "a metadata-SUCCEEDED, analysis-complete file must stay proposable"
    assert await count_proposal_pending_files(session) == 1


# phaze-hk7b8: sibling grouping by parent directory (FileRecord.original_path), replacing the old
# flat sorted-UUID chunking that scattered one release's files across unrelated batches.


async def _converge(session: AsyncSession, f: FileRecord) -> None:
    """Make one already-inserted FileRecord clear the propose convergence gate (metadata + analysis)."""
    session.add(FileMetadata(file_id=f.id, artist="A", title="T"))
    session.add(AnalysisResult(file_id=f.id, bpm=120.0, analysis_completed_at=datetime.now(UTC)))


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_groups_siblings_that_fit_in_one_batch(session: AsyncSession) -> None:
    """Two releases (folders) of 3 files each, batch_size == folder size: each folder is its OWN batch.

    Combined (6) exceeds ``batch_size`` (3), so the two folders cannot share one batch -- but the
    greedy pack still keeps each folder WHOLE rather than falling back to the old flat 3/3 slice,
    which (given arbitrary UUID sort order) could have split either folder across both batches.
    """
    album_a = [_make_pipeline_file(original_path=f"/music/Album A/{i:02d}.mp3") for i in range(3)]
    album_b = [_make_pipeline_file(original_path=f"/music/Album B/{i:02d}.mp3") for i in range(3)]
    session.add_all([*album_a, *album_b])
    await session.flush()
    for f in [*album_a, *album_b]:
        await _converge(session, f)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 3)

    assert len(batches) == 2
    ids_a = sorted(str(f.id) for f in album_a)
    ids_b = sorted(str(f.id) for f in album_b)
    assert sorted(batches[0]) == ids_a, "Album A's 3 siblings must land together in one batch"
    assert sorted(batches[1]) == ids_b, "Album B's 3 siblings must land together in one batch"
    # Determinism: identical pending set, second call, byte-identical batches (not just membership).
    assert await get_proposal_pending_batches(session, 3) == batches


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_packs_small_folders_together(session: AsyncSession) -> None:
    """Two small releases that TOGETHER fit in one batch are packed into the SAME batch."""
    album_a = [_make_pipeline_file(original_path=f"/music/Album A/{i:02d}.mp3") for i in range(2)]
    album_b = [_make_pipeline_file(original_path=f"/music/Album B/{i:02d}.mp3") for i in range(2)]
    session.add_all([*album_a, *album_b])
    await session.flush()
    for f in [*album_a, *album_b]:
        await _converge(session, f)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 10)

    assert len(batches) == 1, "both small folders fit inside one batch_size=10 batch"
    expected = sorted(str(f.id) for f in [*album_a, *album_b])
    assert sorted(batches[0]) == expected


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_splits_an_oversized_folder_deterministically(session: AsyncSession) -> None:
    """A folder LARGER than batch_size must still be split, deterministically, and NEVER blended
    with an unrelated sibling folder's files (each resulting batch's provenance stays simple: it is
    either whole small folders, or a slice of exactly one oversized folder)."""
    big = [_make_pipeline_file(original_path=f"/music/Big Release/{i:02d}.mp3") for i in range(5)]
    small = [_make_pipeline_file(original_path="/music/Small Release/00.mp3")]
    session.add_all([*big, *small])
    await session.flush()
    for f in [*big, *small]:
        await _converge(session, f)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 2)

    big_ids = sorted(str(f.id) for f in big)
    small_ids = sorted(str(f.id) for f in small)
    # The 5-file "Big Release" folder splits into deterministic contiguous slices of <= 2.
    big_batches = [b for b in batches if set(b) & set(big_ids)]
    assert [len(b) for b in big_batches] == [2, 2, 1]
    assert [fid for b in big_batches for fid in b] == big_ids, "slices are sorted-id contiguous chunks"
    assert all(set(b).issubset(big_ids) for b in big_batches), "no oversized-folder batch mixes in another folder's files"
    # The small folder is untouched -- its own, separate, whole batch.
    small_batches = [b for b in batches if set(b) & set(small_ids)]
    assert small_batches == [small_ids]
    # Determinism across repeated calls against the identical pending set.
    assert await get_proposal_pending_batches(session, 2) == batches


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_closes_a_pending_small_batch_before_an_oversized_folder(
    session: AsyncSession,
) -> None:
    """A small folder already packed into the in-progress batch is CLOSED OUT, as its own batch,
    the moment an oversized folder is reached -- rather than being silently dropped or merged into
    the oversized folder's slices.

    ``test_..._splits_an_oversized_folder_deterministically`` only ever reaches the oversized-folder
    branch with an EMPTY in-progress batch (alphabetically, "Big Release" sorts before "Small
    Release", so nothing has been packed into ``current`` yet). This test sorts a small folder
    BEFORE the oversized one ("AAA Warmup" < "ZZZ Big Release") so the in-progress-batch-close path
    is the one actually exercised.
    """
    warmup = [_make_pipeline_file(original_path="/music/AAA Warmup/00.mp3")]
    big = [_make_pipeline_file(original_path=f"/music/ZZZ Big Release/{i:02d}.mp3") for i in range(5)]
    session.add_all([*warmup, *big])
    await session.flush()
    for f in [*warmup, *big]:
        await _converge(session, f)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 2)

    warmup_ids = sorted(str(f.id) for f in warmup)
    big_ids = sorted(str(f.id) for f in big)
    # The warmup folder was packed into `current`, then closed out as its OWN batch (not dropped,
    # not merged into the oversized folder's slices) the moment the oversized folder was reached.
    warmup_batches = [b for b in batches if set(b) & set(warmup_ids)]
    assert warmup_batches == [warmup_ids]
    big_batches = [b for b in batches if set(b) & set(big_ids)]
    assert [len(b) for b in big_batches] == [2, 2, 1]
    assert [fid for b in big_batches for fid in b] == big_ids
    assert len(batches) == 4, "warmup's own batch, plus the big folder's 3 deterministic slices"


@pytest.mark.asyncio
async def test_get_proposal_pending_batches_does_not_cross_agent_boundaries(session: AsyncSession) -> None:
    """Two DIFFERENT agents whose files happen to share a directory STRING are not treated as
    siblings -- ``original_path`` is unique only per agent, so a path collision across agents must
    not merge unrelated files into one batch (mirrors ``services/companion.py``'s ``(agent_id,
    parent)`` grouping key, and the same reasoning).

    Two files per agent, same directory string, batch_size=3: if grouping ignored ``agent_id`` the
    4 files would collapse into ONE 4-file "folder" -- oversized against batch_size=3 -- and split
    into slices that MIX the two agents' files together. Grouping on ``(agent_id, parent)`` instead
    keeps each agent's 2 files as its own (non-oversized) group, so the greedy pack keeps every
    batch pure to a single agent.
    """
    await seed_active_agent(session, "agent-one")
    await seed_active_agent(session, "agent-two")
    agent_one = [_make_pipeline_file(agent_id="agent-one", original_path=f"/music/Shared Folder Name/{i:02d}.mp3") for i in range(2)]
    agent_two = [_make_pipeline_file(agent_id="agent-two", original_path=f"/music/Shared Folder Name/{i:02d}.mp3") for i in range(2)]
    session.add_all([*agent_one, *agent_two])
    await session.flush()
    for f in [*agent_one, *agent_two]:
        await _converge(session, f)
    await session.flush()

    batches = await get_proposal_pending_batches(session, 3)

    ids_one = sorted(str(f.id) for f in agent_one)
    ids_two = sorted(str(f.id) for f in agent_two)
    assert len(batches) == 2, "each agent's 2-file group is its own batch, never merged across agents"
    matched = {frozenset(b) for b in batches}
    assert matched == {frozenset(ids_one), frozenset(ids_two)}, "no batch may mix files from two different agents"
