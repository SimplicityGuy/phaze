"""Tag-write review scenarios moved from ``tests/review/services/test_review_degrade.py``."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.services.review import (
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
async def test_get_tagwrite_review_rows_degrades_to_empty_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = await get_tagwrite_review_rows(_RaisingSession())  # type: ignore[arg-type]
    assert result == []
    assert any("tagwrite_review_rows_degraded" in r.getMessage() for r in caplog.records)


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
