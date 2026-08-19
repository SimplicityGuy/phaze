"""Behavior-asserting degrade + formatter tests for phaze.services.review (COV-01, D-07).

Raises the ``services/review.py`` combined coverage above the 90% per-module floor (it was
the ONLY sub-floor module at 83.16%). Every test asserts an OBSERVABLE outcome (D-07): each
degrade branch returns ``[]`` AND emits its named ``*_degraded`` warning; each formatter
returns the documented string. No ``src/phaze`` edit — the degrade tests inject a raising
stub session (no D-08 seam needed).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING
from unittest.mock import patch
import uuid

import pytest

from phaze.models.discogs_link import DiscogsLink
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.cue_generator import generate_cue_content as _real_generate_cue_content
from phaze.services.review import (
    _format_quality,
    _format_size,
    get_cue_review_cards,
    get_dedupe_groups,
    get_pending_proposal_rows,
    get_tagwrite_review_page,
    get_tagwrite_review_rows,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _RaisingSession:
    """Minimal stub whose ``begin_nested`` raises the moment control enters the ``try``.

    Each ``review.py`` read helper opens ``async with session.begin_nested():`` as the first
    statement inside its ``try``. Raising synchronously from ``begin_nested`` drives control
    straight into the ``except Exception`` degrade branch (observable via the return value +
    the emitted warning key).
    """

    def begin_nested(self) -> object:
        raise RuntimeError("db down")


# ---------------------------------------------------------------------------
# Degrade branches — assert BOTH the [] return AND the named warning (D-07)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pending_proposal_rows_degrades_to_empty_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = await get_pending_proposal_rows(_RaisingSession())  # type: ignore[arg-type]
    # phaze-rw14: the degrade branch returns an all-empty/zero PendingProposalRows bundle, not a
    # bare list -- rows AND both real-total counts degrade together.
    assert result.rows == []
    assert result.total_pending == 0
    assert result.high_confidence_pending == 0
    assert any("pending_proposal_rows_degraded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_tagwrite_review_rows_degrades_to_empty_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = await get_tagwrite_review_rows(_RaisingSession())  # type: ignore[arg-type]
    assert result == []
    assert any("tagwrite_review_rows_degraded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_dedupe_groups_degrades_to_empty_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = await get_dedupe_groups(_RaisingSession())  # type: ignore[arg-type]
    assert result == []
    assert any("dedupe_groups_degraded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_cue_review_cards_degrades_to_empty_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = await get_cue_review_cards(_RaisingSession())  # type: ignore[arg-type]
    assert result == []
    assert any("cue_review_cards_degraded" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Pure formatters — exact / endswith / startswith return-value assertions
# ---------------------------------------------------------------------------


def test_format_size_edges() -> None:
    assert _format_size(None) == "unknown size"
    assert _format_size(0) == "unknown size"  # covers the falsy guard
    assert _format_size(22_400_000).endswith(" MB")
    assert _format_size(2**60).endswith(" PB")  # covers the loop-exhaustion branch


def test_format_quality_with_and_without_bitrate() -> None:
    # phaze-iw2k: bitrate is stored in BITS per second; _format_quality divides by 1000 for display.
    assert _format_quality({"file_size": 22_400_000, "bitrate": 320_000}).startswith("320 kbps · ")
    assert "kbps" not in _format_quality({"file_size": 22_400_000})  # covers the no-bitrate branch


# ---------------------------------------------------------------------------
# READ-05 / Plan 85-04 — applied() cutover + D-03 bound on get_tagwrite_review_rows
#
# These exercise the REAL predicate against the DB session fixture:
#   * D-03: the builder never returns more than ``_MAX_REVIEW_ROWS`` (the render can't
#     blow up on the now-populating applied backlog at 200K scale).
#   * D-01 admit: a file is offered iff an ``executed`` RenameProposal exists — the file's
#     own ``state`` is deliberately ``moved`` (NOT ``executed``), so the row only appears
#     because ``applied_clause()`` reads ``proposals.status``, not ``files.state``.
#   * D-02 idempotency: an applied file with a COMPLETED ``TagWriteLog`` is excluded
#     (the ``completed_subq`` anti-join is preserved).
# ---------------------------------------------------------------------------


async def _seed_applied_tagwrite_file(session: AsyncSession, *, completed_log: bool = False) -> uuid.UUID:
    """Insert an APPLIED (state='moved' + executed proposal) file with a >=1-change tag comparison.

    ``FileMetadata.title`` is left NULL while the filename carries a parseable title, so the proposed
    tags differ from the current metadata (``changed_count >= 1``) and the file qualifies for the
    tag-write queue. Applied-ness comes ENTIRELY from the ``executed`` ``RenameProposal`` — the file's
    ``state`` is ``moved`` (the real apply-path outcome), never ``executed``. Pass ``completed_log=True``
    to also attach a COMPLETED ``TagWriteLog`` (the D-02 idempotency exclusion case).
    """
    file_id = uuid.uuid4()
    filename = "Some Artist - Some Title.mp3"
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/dest/{uuid.uuid4().hex}/{filename}",
            original_filename=filename,
            current_path=f"/dest/{filename}",
            file_type="mp3",
            file_size=5_000_000,
            # NOT 'executed' — applied-ness is carried by the proposal, not files.state
        )
    )
    await session.flush()
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file_id, artist="Some Artist", title=None))
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=filename,
            status=ProposalStatus.EXECUTED.value,
        )
    )
    if completed_log:
        session.add(
            TagWriteLog(
                id=uuid.uuid4(),
                file_id=file_id,
                before_tags={},
                after_tags={"title": "Some Title"},
                source="review",
                status=TagWriteStatus.COMPLETED.value,
            )
        )
    await session.commit()
    return file_id


@pytest.mark.asyncio
async def test_get_tagwrite_review_rows_bounded_by_cap(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-03: the builder returns at most ``_MAX_REVIEW_ROWS`` even when more applied files qualify."""
    monkeypatch.setattr("phaze.services.review._MAX_REVIEW_ROWS", 3)
    for _ in range(5):  # 5 qualifying applied files > the patched cap of 3
        await _seed_applied_tagwrite_file(session)

    rows = await get_tagwrite_review_rows(session)

    assert len(rows) == 3, "the .limit(_MAX_REVIEW_ROWS) cap bounds the builder (D-03)"


@pytest.mark.asyncio
async def test_get_tagwrite_review_rows_admits_applied_excludes_completed(session: AsyncSession) -> None:
    """D-01 admit + D-02 idempotency: an applied file appears; one with a COMPLETED log does not."""
    admitted_id = await _seed_applied_tagwrite_file(session, completed_log=False)
    completed_id = await _seed_applied_tagwrite_file(session, completed_log=True)

    offered_ids = {row["file_id"] for row in await get_tagwrite_review_rows(session)}

    # D-01: admitted purely because an executed proposal exists (its files.state is 'moved').
    assert admitted_id in offered_ids
    # D-02: the completed_subq anti-join excludes the already-written file (idempotency preserved).
    assert completed_id not in offered_ids


@pytest.mark.asyncio
async def test_get_tagwrite_review_rows_readmits_file_after_a_completed_undo(session: AsyncSession) -> None:
    """phaze-vwyco: the shared anti-join must re-admit a file whose latest terminal write is a
    completed UNDO, not evict it forever.

    D-02 correctly excludes a file with an un-reverted COMPLETED write. But the undo itself is ALSO
    a COMPLETED ``TagWriteLog`` row (``source="undo"``) -- the anti-join this builder shares with
    ``bulk_write_no_discrepancies`` (:func:`phaze.routers.tags._terminal_tagwrite_subq`) used to
    match it identically to a genuine forward completion, permanently dropping every reverted file
    out of review coverage even though its disk tags are (again) changed.
    """
    file_id = await _seed_applied_tagwrite_file(session)
    base = datetime(2026, 8, 1, 12, 0, 0)
    session.add(
        TagWriteLog(
            id=uuid.uuid4(),
            file_id=file_id,
            before_tags={},
            after_tags={"title": "Some Title"},
            source="review",
            status=TagWriteStatus.COMPLETED.value,
            written_at=base,
        )
    )
    await session.commit()

    assert file_id not in {row["file_id"] for row in await get_tagwrite_review_rows(session)}, (
        "an un-reverted COMPLETED write must still be excluded (D-02 unchanged)"
    )

    session.add(
        TagWriteLog(
            id=uuid.uuid4(),
            file_id=file_id,
            before_tags={"title": "Some Title"},
            after_tags={},
            source="undo",
            status=TagWriteStatus.COMPLETED.value,
            written_at=base + timedelta(seconds=30),
        )
    )
    await session.commit()

    offered_ids = {row["file_id"] for row in await get_tagwrite_review_rows(session)}
    assert file_id in offered_ids, "a reverted file must re-enter the candidate window, not stay evicted forever"


@pytest.mark.asyncio
async def test_get_tagwrite_review_rows_has_prior_write_flag(session: AsyncSession) -> None:
    """phaze-o5rf: ``has_prior_write`` is False for a fresh row (no log at all -- undo_tag_write would
    404 on it) and True for a row that already carries a non-terminal (DISCREPANCY) TagWriteLog,
    where undo_tag_write can actually revert something. Both rows stay IN the queue (only
    COMPLETED/NO_OP are terminal/excluded), so the flag is the only signal distinguishing them.
    """
    fresh_id = await _seed_applied_tagwrite_file(session)

    discrepancy_id = await _seed_applied_tagwrite_file(session)
    session.add(
        TagWriteLog(
            id=uuid.uuid4(),
            file_id=discrepancy_id,
            before_tags={"title": None},
            after_tags={"title": "Some Title"},
            source="review",
            status=TagWriteStatus.DISCREPANCY.value,
        )
    )
    await session.commit()

    rows_by_id = {row["file_id"]: row for row in await get_tagwrite_review_rows(session)}

    assert rows_by_id[fresh_id]["has_prior_write"] is False
    assert rows_by_id[discrepancy_id]["has_prior_write"] is True


# ---------------------------------------------------------------------------
# WR-01 (85-REVIEW): the SQL cap must bound QUALIFYING rows, not raw candidates.
#
# The old builder applied ``.limit(_MAX_REVIEW_ROWS)`` to a filename-ordered candidate set
# BEFORE the Python ">= 1 change" filter. A wall of zero-change applied files that sort
# alphabetically first fully consumed the capped window, so a qualifying file behind them was
# never surfaced (silent false-empty). These assert the qualifying file IS surfaced even when
# it sorts behind >_MAX zero-change applied files.
# ---------------------------------------------------------------------------


async def _seed_zero_change_applied_file(session: AsyncSession, *, filename: str) -> uuid.UUID:
    """Insert an APPLIED file whose server-computed proposal has ZERO changes (nothing to write).

    The filename has no parseable ``artist - title`` structure, so ``compute_proposed_tags`` draws
    solely from ``FileMetadata`` -- the proposed tags exactly equal the current tags and
    ``changed_count == 0``. Such a file never qualifies, so (pre-WR-01) it permanently re-occupied the
    alphabetically-first ``.limit()`` slots without ever earning a terminal log.
    """
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/dest/{uuid.uuid4().hex}/{filename}",
            original_filename=filename,
            current_path=f"/dest/{filename}",
            file_type="mp3",
            file_size=5_000_000,
        )
    )
    await session.flush()
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file_id, artist="Static Artist", title="Static Title"))
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=filename,
            status=ProposalStatus.EXECUTED.value,
        )
    )
    await session.commit()
    return file_id


async def _seed_qualifying_applied_file(session: AsyncSession, *, filename: str) -> uuid.UUID:
    """Insert an APPLIED file with a ``>= 1`` change (title parsed from the filename, NULL in metadata)."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/dest/{uuid.uuid4().hex}/{filename}",
            original_filename=filename,
            current_path=f"/dest/{filename}",
            file_type="mp3",
            file_size=5_000_000,
        )
    )
    await session.flush()
    # artist matches the filename-parsed artist; title is NULL so the filename title is a real change.
    session.add(FileMetadata(id=uuid.uuid4(), file_id=file_id, artist=filename.split(" - ", 1)[0], title=None))
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=filename,
            status=ProposalStatus.EXECUTED.value,
        )
    )
    await session.commit()
    return file_id


@pytest.mark.asyncio
async def test_get_tagwrite_review_rows_surfaces_qualifying_behind_zero_change_wall(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-01: a qualifying file behind >_MAX zero-change applied files is still surfaced.

    Three zero-change applied files (``aaa_noop_*``) sort alphabetically before the qualifying
    ``aaa_qual - Title.mp3``. With the old cap-before-filter, ``.limit(_MAX_REVIEW_ROWS)`` (patched
    to 2) selected only the first two zero-change files, which then filtered to nothing -- the
    qualifying file was never returned. The fix accumulates QUALIFYING rows up to the cap, so it
    surfaces regardless of the alphabetical wall.
    """
    monkeypatch.setattr("phaze.services.review._MAX_REVIEW_ROWS", 2)
    for i in range(3):  # > the patched cap of 2, all sorting before the qualifying file
        await _seed_zero_change_applied_file(session, filename=f"aaa_noop_{i}.mp3")
    qual_id = await _seed_qualifying_applied_file(session, filename="aaa_qual - Title.mp3")

    offered = {row["file_id"] for row in await get_tagwrite_review_rows(session)}

    assert qual_id in offered, "the qualifying file must not be starved behind a wall of zero-change files"


@pytest.mark.asyncio
async def test_get_tagwrite_review_rows_pages_across_scan_batches(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-01: keyset paging finds a qualifying file that sits beyond a single scan batch.

    With the scan batch patched to 2, the three zero-change files span two full batches before the
    qualifying file in a third -- proving the builder keyset-pages the whole candidate set (bounded
    memory per batch) rather than materializing it all or stopping at the first batch.
    """
    monkeypatch.setattr("phaze.services.review._MAX_REVIEW_ROWS", 2)
    monkeypatch.setattr("phaze.services.review._REVIEW_SCAN_BATCH", 2)
    for i in range(3):
        await _seed_zero_change_applied_file(session, filename=f"aaa_noop_{i}.mp3")
    qual_id = await _seed_qualifying_applied_file(session, filename="aaa_qual - Title.mp3")

    offered = {row["file_id"] for row in await get_tagwrite_review_rows(session)}

    assert qual_id in offered


# ---------------------------------------------------------------------------
# phaze-bto9 — the tag-write review scan's cost shape
# ---------------------------------------------------------------------------


class _CountingSession:
    """Wrap a real session and count SELECTs, so a per-row query fan-out is directly observable.

    The defect phaze-bto9 fixes is invisible to a result-shape assertion -- the rows returned were
    always correct, it was the ROUND-TRIP COUNT that was linear (and quadratic with the missing
    index) in the applied backlog. Counting is therefore the only honest test of it.
    """

    def __init__(self, inner: AsyncSession) -> None:
        self._inner = inner
        self.executes = 0

    async def execute(self, *args: object, **kwargs: object) -> object:
        self.executes += 1
        return await self._inner.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_scan_issues_a_constant_number_of_queries_per_batch(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-bto9: query count is per SCAN BATCH, not per CANDIDATE.

    Pre-fix the loop called ``_get_tracklist_for_file`` (1 SELECT) and ``_get_accepted_discogs_link``
    (1-2 SELECTs) for EVERY candidate, so a 200K applied backlog of already-correct files issued
    ~500K round-trips to render a page that qualifies almost nothing. Both are now batched by
    ``file_id IN (...)`` alongside the ``logged_ids`` batch that was already there.

    Asserted as a bound rather than an exact number so the test does not ossify the batch internals:
    doubling the candidate count within ONE batch must not change the query count at all.
    """
    monkeypatch.setattr("phaze.services.review._REVIEW_SCAN_BATCH", 50)

    for i in range(4):
        await _seed_qualifying_applied_file(session, filename=f"aaa_{i} - Title.mp3")
    counting_four = _CountingSession(session)
    await get_tagwrite_review_page(counting_four)  # type: ignore[arg-type]

    for i in range(4, 12):
        await _seed_qualifying_applied_file(session, filename=f"aaa_{i} - Title.mp3")
    counting_twelve = _CountingSession(session)
    page = await get_tagwrite_review_page(counting_twelve)  # type: ignore[arg-type]

    assert len(page.rows) == 12, "all twelve candidates fit in one scan batch"
    assert counting_twelve.executes == counting_four.executes, (
        f"tripling the candidates in one batch changed the query count "
        f"({counting_four.executes} -> {counting_twelve.executes}) -- a per-row query survived the batching"
    )


@pytest.mark.asyncio
async def test_batched_lookups_pick_the_same_rows_as_the_per_file_helpers(session: AsyncSession) -> None:
    """The batch helpers must resolve EXACTLY what the per-file helpers would, ties included.

    Both are ``DISTINCT ON`` rewrites of an ``ORDER BY ... LIMIT 1``, so a mismatched ORDER BY would
    silently change which tracklist (and therefore which proposed tags) a file is reviewed against.
    Seeded with a deliberate confidence TIE so the ``id`` tiebreak is what decides.
    """
    # phaze-b4u3p: the per-file helpers stay importable from routers.tags (still used by its own
    # routes); the batch forms moved to services.tag_comparison, their only remaining caller being
    # services.review -- see that module's docstring for the layering rationale.
    from phaze.routers.tags import _get_accepted_discogs_link, _get_tracklist_for_file
    from phaze.services.tag_comparison import _get_accepted_discogs_links_for_files, _get_tracklists_for_files

    file_id = await _seed_qualifying_applied_file(session, filename="Tie Artist - Tie Title.mp3")
    version_ids = []
    for confidence in (0.5, 0.5, 0.9):
        version_id = uuid.uuid4()
        tracklist_id = uuid.uuid4()
        session.add(
            Tracklist(
                id=tracklist_id,
                external_id=f"ext-{uuid.uuid4().hex[:8]}",
                source_url="https://example.com",
                file_id=file_id,
                artist="Tie Artist",
                latest_version_id=version_id,
                source="1001tracklists",
                status="approved",
                match_confidence=confidence,
            )
        )
        session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
        version_ids.append(version_id)
    await session.commit()

    per_file = await _get_tracklist_for_file(session, file_id)
    batched = (await _get_tracklists_for_files(session, [file_id])).get(file_id)
    assert per_file is not None
    assert batched is not None
    assert batched.id == per_file.id, "the batch helper must pick the same tracklist as the per-file one"

    # Two accepted links on the winning version's tracks, tied on confidence -> id DESC decides.
    track_ids = [uuid.uuid4(), uuid.uuid4()]
    for position, track_id in enumerate(track_ids, start=1):
        session.add(TracklistTrack(id=track_id, version_id=per_file.latest_version_id, position=position, title=f"T{position}", timestamp="0:00"))
        session.add(
            DiscogsLink(
                id=uuid.uuid4(),
                track_id=track_id,
                discogs_release_id=f"rel-{position}",
                status="accepted",
                confidence=0.7,
                discogs_label="L",
                discogs_year=2024,
            )
        )
    await session.commit()

    per_file_link = await _get_accepted_discogs_link(session, file_id)
    batched_link = (await _get_accepted_discogs_links_for_files(session, {file_id: per_file})).get(file_id)
    assert per_file_link is not None
    assert batched_link is not None
    assert batched_link.id == per_file_link.id, "the batch helper must pick the same accepted link as the per-file one"


@pytest.mark.asyncio
async def test_scan_stops_at_the_batch_cap_and_reports_partial(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-bto9: the walk is bounded by CANDIDATES SCANNED, not just by rows accumulated.

    The accumulate-only-qualifying-rows design (WR-01) means the ``len(rows) < _MAX_REVIEW_ROWS``
    guard NEVER fires when candidates do not qualify -- zero-change files ``continue`` without
    contributing -- so pre-fix the loop terminated only on candidate exhaustion, i.e. after paging
    every applied file. Here five zero-change files span more batches than the cap allows: the scan
    must stop early and SAY so, rather than walking the whole backlog.
    """
    monkeypatch.setattr("phaze.services.review._REVIEW_SCAN_BATCH", 1)
    monkeypatch.setattr("phaze.services.review._MAX_REVIEW_SCAN_BATCHES", 2)
    for i in range(5):
        await _seed_zero_change_applied_file(session, filename=f"aaa_noop_{i}.mp3")
    # A qualifying file sorting AFTER the wall -- unreachable within the cap, by design.
    await _seed_qualifying_applied_file(session, filename="zzz_qual - Title.mp3")

    page = await get_tagwrite_review_page(session)

    assert page.partial is True, "a truncated scan must report itself partial, not silently understate the queue"
    assert page.rows == [], "nothing qualified within the scanned prefix"


@pytest.mark.asyncio
async def test_complete_scan_is_not_reported_partial(session: AsyncSession) -> None:
    """The complement: an exhausted candidate set is a COMPLETE answer, so the subcount is exact.

    Without this, "always partial" would satisfy the assertion above while making every count in the
    workspace read as an underestimate.
    """
    qual_id = await _seed_qualifying_applied_file(session, filename="aaa_qual - Title.mp3")

    page = await get_tagwrite_review_page(session)

    assert page.partial is False
    assert {row["file_id"] for row in page.rows} == {qual_id}


# ---------------------------------------------------------------------------
# phaze-a2ytu — the row-cap exit must also report ``partial``, not just the
# batch-cap exit. Three shapes at the ``_MAX_REVIEW_ROWS`` boundary, matching
# the adversarial finder's case split:
#   (a) row cap hit mid-batch, with unexamined siblings still in that batch;
#   (b) row cap hit exactly on a FULL batch's last member (a later keyset page
#       may still hold candidates, so this must still be reported partial);
#   (c) row cap hit exactly as the candidate set itself is exhausted (a SHORT
#       batch, fully consumed) -- the one true "not partial" case.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_cap_hit_mid_batch_reports_partial(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) The row cap lands before the end of a batch -- unexamined siblings remain in it."""
    monkeypatch.setattr("phaze.services.review._MAX_REVIEW_ROWS", 2)
    monkeypatch.setattr("phaze.services.review._REVIEW_SCAN_BATCH", 5)
    for i in range(5):  # all 5 land in one batch; the cap (2) trips mid-batch, 3 siblings unexamined
        await _seed_qualifying_applied_file(session, filename=f"aaa_{i} - Title.mp3")

    page = await get_tagwrite_review_page(session)

    assert len(page.rows) == 2
    assert page.partial is True, "unexamined siblings remain in the batch the cap tripped inside"


@pytest.mark.asyncio
async def test_row_cap_hit_at_full_batch_boundary_reports_partial(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) The row cap lands exactly on a FULL batch's last row -- a later keyset page may remain,
    which the builder cannot rule out without another round-trip, so it must still say partial.

    This is the exact shape from the bug report: N qualifying rows accumulate within full
    ``_REVIEW_SCAN_BATCH``-sized batches, so neither the inner-loop unexamined-sibling check nor the
    outer short-batch check fires -- the pre-fix code left ``partial`` at its default False here.
    """
    monkeypatch.setattr("phaze.services.review._MAX_REVIEW_ROWS", 3)
    monkeypatch.setattr("phaze.services.review._REVIEW_SCAN_BATCH", 3)
    for i in range(3):  # exactly one full batch, cap trips on its last (and only) qualifying row
        await _seed_qualifying_applied_file(session, filename=f"aaa_{i} - Title.mp3")

    page = await get_tagwrite_review_page(session)

    assert len(page.rows) == 3
    assert page.partial is True, "a full-sized batch cannot prove the candidate set is exhausted"


@pytest.mark.asyncio
async def test_row_cap_hit_exactly_as_candidates_exhausted_is_not_partial(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) The row cap lands exactly on a SHORT batch's last row -- the candidate set is provably
    exhausted right there, so the render is complete despite hitting the cap.
    """
    monkeypatch.setattr("phaze.services.review._MAX_REVIEW_ROWS", 3)
    monkeypatch.setattr("phaze.services.review._REVIEW_SCAN_BATCH", 5)
    for i in range(3):  # fewer than the scan batch, so the returned batch is short (< 5)
        await _seed_qualifying_applied_file(session, filename=f"aaa_{i} - Title.mp3")

    page = await get_tagwrite_review_page(session)

    assert len(page.rows) == 3
    assert page.partial is False, "the short batch proves no candidates remain, even though the cap was hit"


# ---------------------------------------------------------------------------
# phaze-hcsb — per-card isolation in get_cue_review_cards
# ---------------------------------------------------------------------------


async def _seed_eligible_cue_tracklist(session: AsyncSession, *, artist: str) -> Tracklist:
    """Insert an approved + applied tracklist with one timestamped track (an eligible cue card)."""
    file_id = uuid.uuid4()
    filename = f"{artist}.mp3"
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/music/{uuid.uuid4().hex}/{filename}",
            original_filename=filename,
            current_path=f"/dest/{filename}",
            file_type="mp3",
            file_size=1_000_000,
        )
    )
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=filename,
            status=ProposalStatus.EXECUTED.value,
        )
    )

    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()
    tracklist = Tracklist(
        id=tracklist_id,
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        source_url=f"https://www.1001tracklists.com/tracklist/{uuid.uuid4().hex[:6]}",
        file_id=file_id,
        match_confidence=95,
        artist=artist,
        event="Test Event",
        latest_version_id=version_id,
        source="1001tracklists",
        status="approved",
    )
    session.add(tracklist)
    session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
    await session.flush()
    session.add(
        TracklistTrack(
            id=uuid.uuid4(),
            version_id=version_id,
            position=1,
            artist=f"{artist} Track",
            title="Track Title",
            timestamp="0:01:00",
        )
    )
    await session.commit()
    return tracklist


@pytest.mark.asyncio
async def test_get_cue_review_cards_isolates_one_bad_card_from_the_rest(session: AsyncSession, caplog: pytest.LogCaptureFixture) -> None:
    """phaze-hcsb: a single card's build failure must not blank the whole Cue review workspace.

    Before the fix, the per-card ``_build_cue_tracks``/``generate_cue_content`` calls lived inside
    the SAME ``try`` as the SAVEPOINT open -- any exception there hit the outer ``except Exception:
    return []`` and dropped EVERY other eligible + gated card, not just the offending one.
    """
    good = await _seed_eligible_cue_tracklist(session, artist="Good Artist")
    bad = await _seed_eligible_cue_tracklist(session, artist="Bad Artist")

    def _boom_for_bad(audio_filename: str, file_type: str, tracks: list) -> str:  # type: ignore[type-arg]
        if audio_filename.startswith("Bad Artist"):
            raise ValueError("simulated per-card build failure")
        return _real_generate_cue_content(audio_filename, file_type, tracks)

    with caplog.at_level(logging.WARNING), patch("phaze.services.review.generate_cue_content", side_effect=_boom_for_bad):
        cards = await get_cue_review_cards(session)

    by_id = {card["tracklist_id"]: card for card in cards}

    # The workspace is NOT blanked -- both tracklists still produce a card.
    assert good.id in by_id
    assert bad.id in by_id

    # The good card is unaffected: still eligible with a real in-memory preview.
    assert by_id[good.id]["eligible"] is True
    assert by_id[good.id]["cue_text"]

    # The bad card is distinct from a genuine missing-timestamps gate.
    assert by_id[bad.id]["eligible"] is False
    assert by_id[bad.id]["build_error"] is True
    assert by_id[bad.id]["cue_text"] is None

    assert any("cue_review_card_build_failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# phaze-dboy — get_cue_review_cards' gated set scoped to latest_version_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cue_review_cards_gates_stale_version_timestamps(session: AsyncSession) -> None:
    """phaze-dboy: review.py's gated_stmt shares the same defective any-version predicate as
    cue.py -- a tracklist whose only timestamped track is on an OLDER version than
    ``latest_version_id`` must render as a GATED card (no approve control), not be omitted from
    both sets entirely.
    """
    tracklist = await _seed_eligible_cue_tracklist(session, artist="Stale Version")

    v2_id = uuid.uuid4()
    session.add(TracklistVersion(id=v2_id, tracklist_id=tracklist.id, version_number=2))
    await session.flush()
    session.add(
        TracklistTrack(
            id=uuid.uuid4(),
            version_id=v2_id,
            position=1,
            artist="Untimed Track",
            title="Untimed Title",
            timestamp=None,
        )
    )
    tracklist.latest_version_id = v2_id
    await session.commit()

    cards = await get_cue_review_cards(session)
    by_id = {card["tracklist_id"]: card for card in cards}

    assert tracklist.id in by_id, "the tracklist must still surface -- as gated, not silently dropped"
    assert by_id[tracklist.id]["eligible"] is False
    assert by_id[tracklist.id]["cue_text"] is None
