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
