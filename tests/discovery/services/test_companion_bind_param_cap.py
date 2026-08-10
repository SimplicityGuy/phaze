"""phaze-p3qr: the asyncpg 32767-bind-parameter cap sweep for ``associate_companions``' bulk INSERT.

``associate_companions`` used to accumulate every new (companion, media) pair for a directory into
ONE list and execute it as a single ``pg_insert(FileCompanion).values(rows)`` -- a multi-row VALUES
that SQLAlchemy renders with 3 bind parameters per row. PostgreSQL's extended-protocol Bind message
carries the argument count as a signed int16, capping any statement at 32767 parameters, so any run
producing more than 10,922 new link rows (32,769 parameters at row 10,923) raised
``InterfaceError`` before a single row was inserted. The verifier corrected the finder's
"3,000-companion-file" framing: the realistic ``COMPANION_TYPES`` set (folder art + a couple of
scene-release sidecars, ~4 companions against a dozen tracks) crosses the break in roughly 230
album directories on the large personal archive this project targets -- and because the failure is
atomic, every retry recomputed the identical oversized unlinked set and failed identically, a
permanent wedge, not a transient error.

The fix chunks the insert via the shared ``services.bulk_insert.chunk_rows`` helper (the
``agent_analysis.py:256`` precedent), keeping every chunk on the SAME session inside the single
transaction ``associate_companions`` already commits once, per ``bulk_insert.py``'s "atomicity is
the caller's job" rule.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import func, select

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.services.companion import COMPANION_TYPES, MEDIA_TYPES, associate_companions


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _make_file(original_path: str, file_type: str, agent_id: str = "test-fileserver") -> FileRecord:
    uid = uuid.uuid4()
    filename = original_path.rsplit("/", 1)[-1]
    return FileRecord(
        agent_id=agent_id,
        id=uid,
        sha256_hash=uid.hex,
        original_path=original_path,
        original_filename=filename,
        current_path=original_path,
        file_type=file_type,
        file_size=1000,
    )


# 105 companions x 105 media = 11,025 pair rows, past the 10,922-row break (3 bind params/row ->
# 32767 cap) while keeping the seeded file count (210) small for test runtime -- chosen close to
# sqrt(10,923) to minimize total seeded rows for a product past the break, unlike the finder's
# realistic-but-file-heavy "3,000 companion files" framing.
_NUM_COMPANIONS = 105
_NUM_MEDIA = 105
_EXPECTED_PAIRS = _NUM_COMPANIONS * _NUM_MEDIA
assert _EXPECTED_PAIRS > 10_922, "the fixture must actually cross the pre-fix break"


def _seed_one_big_directory() -> tuple[list[FileRecord], list[FileRecord]]:
    """Build ``_NUM_COMPANIONS`` companions + ``_NUM_MEDIA`` media files in ONE directory (so every
    companion links to every media file -- the full cross-product ``associate_companions`` builds)."""
    companion_cycle = itertools.cycle(sorted(COMPANION_TYPES))
    media_cycle = itertools.cycle(sorted(MEDIA_TYPES))
    companions = []
    for i in range(_NUM_COMPANIONS):
        ext = next(companion_cycle)
        companions.append(_make_file(f"/music/bigalbum/companion_{i}.{ext}", ext))
    media = []
    for i in range(_NUM_MEDIA):
        ext = next(media_cycle)
        media.append(_make_file(f"/music/bigalbum/media_{i}.{ext}", ext))
    return companions, media


@pytest.mark.asyncio
async def test_associate_companions_past_bind_param_cap_creates_every_pair(session: AsyncSession) -> None:
    """A directory whose companion x media cross-product exceeds the bind-parameter cap must still
    create EVERY pair, not raise (phaze-p3qr regression)."""
    companions, media = _seed_one_big_directory()
    session.add_all(companions + media)
    await session.flush()

    count = await associate_companions(session)

    assert count == _EXPECTED_PAIRS
    result = await session.execute(select(func.count()).select_from(FileCompanion))
    assert result.scalar_one() == _EXPECTED_PAIRS


@pytest.mark.asyncio
async def test_associate_companions_chunks_the_insert_into_multiple_statements(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the CHUNKING itself, not just the end-to-end row count -- mirrors
    ``tests/agents/routers/test_agent_analysis.py``'s chunk-boundary suite. Monkeypatches
    ``chunk_rows`` to prove the >10,922-row pair set is actually split into multiple INSERT
    statements (never one, which would silently reintroduce the bug if a future edit bypassed the
    helper), and that every chunk respects ``bulk_insert.py``'s soft per-statement row cap.
    """
    import phaze.services.companion as companion_module

    real_chunk_rows = companion_module.chunk_rows
    seen_chunk_sizes: list[int] = []

    def _counting_chunk_rows(rows: object) -> object:
        for chunk in real_chunk_rows(rows):  # type: ignore[arg-type]
            seen_chunk_sizes.append(len(chunk))
            yield chunk

    monkeypatch.setattr(companion_module, "chunk_rows", _counting_chunk_rows)

    companions, media = _seed_one_big_directory()
    session.add_all(companions + media)
    await session.flush()

    count = await associate_companions(session)

    assert count == _EXPECTED_PAIRS
    assert len(seen_chunk_sizes) > 1, "an unchunked single statement would have raised before reaching here"
    assert sum(seen_chunk_sizes) == _EXPECTED_PAIRS
    # bulk_insert.py's MAX_ROWS_PER_STATEMENT soft cap (1000) is well under the 10,922-row bind
    # break for this model's 3-param rows, so every chunk must be <= 1000.
    assert all(size <= 1000 for size in seen_chunk_sizes)


@pytest.mark.asyncio
async def test_associate_companions_chunked_insert_is_atomic_on_mid_write_failure(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure BETWEEN chunks commits nothing (mirrors the phaze-syxv precedent,
    ``test_analysis_window_chunked_insert_is_atomic_on_mid_write_failure``). Chunking splits one
    statement into several, which would be a regression if the chunks could land independently:
    ``bulk_insert.py``'s "atomicity is the caller's job" rule holds here because every chunk lands
    on THIS session inside the SAME page's transaction. ``associate_companions`` now commits per
    keyset PAGE rather than once for the whole run (phaze-yiwq5), but this fixture's 105 companions
    all fit in one page under the default ``batch_size``, so the page boundary and the whole-run
    boundary coincide here -- a mid-page raise must leave zero links committed.
    """
    import phaze.services.companion as companion_module

    real_chunk_rows = companion_module.chunk_rows

    def _explode_after_first_chunk(rows: object) -> object:
        for index, chunk in enumerate(real_chunk_rows(rows)):  # type: ignore[arg-type]
            if index > 0:
                raise RuntimeError("simulated mid-write failure")
            yield chunk

    monkeypatch.setattr(companion_module, "chunk_rows", _explode_after_first_chunk)

    companions, media = _seed_one_big_directory()
    session.add_all(companions + media)
    await session.flush()

    with pytest.raises(RuntimeError, match="simulated mid-write failure"):
        await associate_companions(session)

    await session.rollback()
    result = await session.execute(select(func.count()).select_from(FileCompanion))
    assert result.scalar_one() == 0
