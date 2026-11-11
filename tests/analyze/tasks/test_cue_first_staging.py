"""phaze-tdton: the cloud staging window fills with CUE-linked held files before older non-CUE ones.

Production shape: nearly every CUE-linked unanalysed file is long, so it is held in AWAITING_CLOUD and only
``stage_cloud_window`` releases it. The window is bounded (``limit``) and its membership rule is unchanged;
only the ORDER within the awaiting set gains a CUE-first rank ahead of the FIFO ``created_at``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from phaze.services.pipeline import get_cloud_staging_candidates
from tests.analyze.tasks.test_drain_head_of_line import _BASE_TIME, _make_file, _seed
from tests.shared.services.pipeline.test_cue_first_ordering import _link


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_a_cue_linked_long_file_is_staged_before_an_older_non_cue_long_file(session: AsyncSession) -> None:
    older_plain = [_make_file(_BASE_TIME + timedelta(seconds=i)) for i in range(4)]
    newer_cue = [_make_file(_BASE_TIME + timedelta(hours=5, seconds=i)) for i in range(2)]
    await _seed(session, [*older_plain, *newer_cue], attempts=0)
    for f in newer_cue:
        await _link(session, f, "cue")
    await session.commit()

    window = await get_cloud_staging_candidates(session, 3)

    ids = [f.id for f, _ in window]
    assert ids[:2] == [f.id for f in newer_cue], "CUE-linked files fill the window first, FIFO among themselves"
    assert ids[2] == older_plain[0].id, "then the oldest non-CUE file: FIFO is unchanged outside the CUE rank"
    assert len(ids) == 3, "the window stays bounded by its limit"


@pytest.mark.asyncio
async def test_keyset_cursor_walks_the_cue_rank_boundary_exactly(session: AsyncSession) -> None:
    """Paging across the CUE -> non-CUE boundary neither skips nor repeats a row, and membership is the full awaiting set."""
    plain = [_make_file(_BASE_TIME + timedelta(seconds=i)) for i in range(5)]
    cue = [_make_file(_BASE_TIME + timedelta(hours=5, seconds=i)) for i in range(4)]
    await _seed(session, [*plain, *cue], attempts=0)
    for f in cue:
        await _link(session, f, "cue")
    await session.commit()

    seen: list[uuid.UUID] = []
    cursor = None
    for _ in range(3):
        page = await get_cloud_staging_candidates(session, 3, after=cursor)
        seen.extend(f.id for f, _ in page)
        cursor = (page[-1][0].created_at, page[-1][0].id)

    assert seen == [f.id for f in cue] + [f.id for f in plain]
    assert await get_cloud_staging_candidates(session, 3, after=cursor) == []
