"""phaze-yiwq5: keyset-paged reads + per-page commits for ``associate_companions``.

The unfixed version issued one unbounded ``select(FileRecord)...`` and materialized the WHOLE
unlinked-companion corpus with ``.scalars().all()``, then built the full per-directory link
cross-product from it in memory before a single INSERT/commit at the end. On an archive with a
large never-associated companion backlog that is an unbounded allocation in the api process,
reachable from an operator-triggered ``POST /associate``.

The fix pages the unlinked-companion read by ``FileRecord.id`` (``LIMIT batch_size`` + a keyset
cursor), bounding peak memory to one page's companions plus the media cross-product for the
directories that page touches, and commits after each page rather than once for the whole run.
These tests exercise the paging itself (multiple pages, correctness preserved across page
boundaries, one page's commit count) rather than re-covering the bind-parameter chunking already
covered by ``test_companion_bind_param_cap.py``.
"""

from __future__ import annotations

import math
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


@pytest.mark.asyncio
async def test_associate_companions_commits_once_per_keyset_page(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backlog spread across more directories than fit in one page must be read in MULTIPLE
    keyset pages, each committed separately -- never one unbounded read + one commit at the end."""
    num_directories = 12
    companion_exts = sorted(COMPANION_TYPES)
    media_exts = sorted(MEDIA_TYPES)
    companions: list[FileRecord] = []
    media: list[FileRecord] = []
    for i in range(num_directories):
        companions.append(_make_file(f"/music/dir_{i}/companion.{companion_exts[i % len(companion_exts)]}", companion_exts[i % len(companion_exts)]))
        media.append(_make_file(f"/music/dir_{i}/media.{media_exts[i % len(media_exts)]}", media_exts[i % len(media_exts)]))
    session.add_all(companions + media)
    await session.flush()

    real_commit = session.commit
    commit_calls = 0

    async def _counting_commit() -> None:
        nonlocal commit_calls
        commit_calls += 1
        await real_commit()

    monkeypatch.setattr(session, "commit", _counting_commit)

    batch_size = 5
    count = await associate_companions(session, batch_size=batch_size)

    assert count == num_directories, "one companion x one media per directory -> one link each"
    assert commit_calls == math.ceil(num_directories / batch_size), (
        "paging by batch_size must produce one commit per page, not one commit for the whole run"
    )

    result = await session.execute(select(func.count()).select_from(FileCompanion))
    assert result.scalar_one() == num_directories


@pytest.mark.asyncio
async def test_associate_companions_correct_when_one_directory_spans_multiple_pages(
    session: AsyncSession,
) -> None:
    """A single directory's companions can land on different keyset pages (paging is by
    FileRecord.id, not by directory) -- every companion in it must still link to every media file
    in that directory, exactly as the unpaged cross-product would have produced."""
    num_companions = 13
    num_media = 3
    companion_exts = sorted(COMPANION_TYPES)
    media_exts = sorted(MEDIA_TYPES)
    companions = [
        _make_file(f"/music/bigdir/companion_{i}.{companion_exts[i % len(companion_exts)]}", companion_exts[i % len(companion_exts)])
        for i in range(num_companions)
    ]
    media = [_make_file(f"/music/bigdir/media_{i}.{media_exts[i % len(media_exts)]}", media_exts[i % len(media_exts)]) for i in range(num_media)]
    session.add_all(companions + media)
    await session.flush()

    # batch_size smaller than num_companions guarantees this one directory's companions are split
    # across at least two keyset pages.
    count = await associate_companions(session, batch_size=4)

    assert count == num_companions * num_media
    result = await session.execute(select(func.count()).select_from(FileCompanion))
    assert result.scalar_one() == num_companions * num_media


@pytest.mark.asyncio
async def test_associate_companions_paged_run_is_idempotent(session: AsyncSession) -> None:
    """Running a paged sweep twice must create no duplicate links, mirroring the unpaged
    idempotency guarantee the docstring already promises for concurrent/repeat POST /associate."""
    companions = [_make_file(f"/music/idem/companion_{i}.nfo", "nfo") for i in range(9)]
    media = [_make_file("/music/idem/media.flac", "flac")]
    session.add_all(companions + media)
    await session.flush()

    first_count = await associate_companions(session, batch_size=3)
    second_count = await associate_companions(session, batch_size=3)

    assert first_count == len(companions)
    assert second_count == 0
    result = await session.execute(select(func.count()).select_from(FileCompanion))
    assert result.scalar_one() == len(companions)


@pytest.mark.asyncio
async def test_associate_companions_no_backlog_returns_zero_without_querying_media(session: AsyncSession) -> None:
    """The empty-backlog case (first page returns nothing) must return 0 immediately -- a
    regression guard for the removed early-return special case now folded into the page loop."""
    count = await associate_companions(session)
    assert count == 0
