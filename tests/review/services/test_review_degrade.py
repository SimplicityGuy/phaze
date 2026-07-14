"""Behavior-asserting degrade + formatter tests for phaze.services.review (COV-01, D-07).

Raises the ``services/review.py`` combined coverage above the 90% per-module floor (it was
the ONLY sub-floor module at 83.16%). Every test asserts an OBSERVABLE outcome (D-07): each
degrade branch returns ``[]`` AND emits its named ``*_degraded`` warning; each formatter
returns the documented string. No ``src/phaze`` edit — the degrade tests inject a raising
stub session (no D-08 seam needed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.services.review import (
    _format_quality,
    _format_size,
    get_cue_review_cards,
    get_dedupe_groups,
    get_pending_proposal_rows,
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
    assert result == []
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
    assert _format_quality({"file_size": 22_400_000, "bitrate": 320}).startswith("320 kbps · ")
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
