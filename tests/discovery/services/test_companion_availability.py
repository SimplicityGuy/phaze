"""Unavailable inventory through real locate/reconcile, extraction, association and junk consumers.

Synthetic files and the isolated Postgres seat verify implementation behavior, not production
coverage. Availability is explicit evidence about absence; duplicate content alone is not absence.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
import os
from typing import TYPE_CHECKING
import unicodedata

import pytest
from sqlalchemy import func, select

from phaze import cli
from phaze.agent_watcher.locate import locate_stale
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.routers.agent_files import _upsert_rows
from phaze.services.companion import associate_companions
from phaze.services.companion_content import (
    count_backfill,
    linked_copy_fingerprints,
    refresh_known_stamps,
    select_backfill_page,
    store_companion_features,
)
from phaze.services.companion_junk_review import detect_junk_reviews
from phaze.services.stale_rows import Verdict, reconcile_stale_rows, stale_row_candidates
from tests.discovery.services.test_companion_content import _record
from tests.discovery.services.test_stale_rows import _agent, _reconcile, _row, _write


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def test_missing_and_ambiguous_are_reported_separately_and_reappearance_restores_extraction(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    missing_path = _write(root / "absent" / "note.txt", b"lost note")
    ambiguous_path = _write(root / "old" / "copy.nfo", b"copy bytes")
    pending_path = _write(root / "pending" / "notes.txt", b"unread note")
    missing = await _row(session, agent_id, missing_path)
    ambiguous = await _row(session, agent_id, ambiguous_path)
    pending = await _row(session, agent_id, pending_path)
    missing_id, ambiguous_id, pending_id = missing.id, ambiguous.id, pending.id
    missing_path.unlink()
    ambiguous_path.unlink()
    _write(root / "a" / "copy.nfo", b"copy bytes")
    _write(root / "b" / "copy.nfo", b"copy bytes")

    dry = await _reconcile(session, agent_id, apply=False)
    assert {action.verdict for action in dry.actions} == {Verdict.MISSING, Verdict.AMBIGUOUS}
    assert (await session.get(FileRecord, missing_id)).missing_at is None
    assert (await session.get(FileRecord, ambiguous_id)).companion_ambiguous_at is None
    await _reconcile(session, agent_id, apply=True)
    stamp = (await session.get(FileRecord, ambiguous_id)).companion_ambiguous_at
    assert stamp is not None
    again = await _reconcile(session, agent_id, apply=True)
    assert again.already_missing == 1
    assert (await session.get(FileRecord, ambiguous_id)).companion_ambiguous_at == stamp
    counts = (await count_backfill(session))[0]
    assert (counts.companions, counts.current, counts.pending, counts.unavailable_missing, counts.unavailable_ambiguous) == (3, 0, 1, 1, 1)
    assert await select_backfill_page(session, agent_id, after=None, limit=10) == [(pending_id, str(pending_path))]
    association = await associate_companions(session, agent_id=agent_id, apply=False)
    assert (association.awaiting_features, association.unavailable_missing, association.unavailable_ambiguous) == (1, 1, 1)

    # The real CLI consumers use the real services. Their rollback remains a savepoint inside the
    # fixture's outer transaction, so commit seeded state before each command opens its transaction.
    await session.commit()

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    monkeypatch.setattr(cli, "async_session", factory)
    assert await cli._run_companion_features(apply=False, page_size=50) == 0
    output = capsys.readouterr().out
    assert "pending=1 jobs=1" in output
    assert "unavailable_missing=1 unavailable_ambiguous=1" in output
    assert "retained, not extracted" in output
    assert await cli._run_companion_links(apply=False) == 0
    output = capsys.readouterr().out
    assert "awaiting features=1" in output
    assert "unavailable_missing=1 unavailable_ambiguous=1" in output

    _write(missing_path, b"lost note")
    _write(ambiguous_path, b"copy bytes")
    restored = await _reconcile(session, agent_id, apply=True)
    assert [action.verdict for action in restored.actions] == [Verdict.RESTORED, Verdict.RESTORED]
    for file_id in (missing_id, ambiguous_id):
        row = await session.get(FileRecord, file_id)
        assert row.missing_at is None and row.companion_ambiguous_at is None
    assert (await count_backfill(session))[0].pending == 3
    await store_companion_features(session, agent_id, [_record(path) for path in (missing_path, ambiguous_path, pending_path)])
    assert (await count_backfill(session))[0].current == 3
    assert (await _reconcile(session, agent_id, apply=True)).actions == []


@pytest.mark.parametrize("incomplete", ["walk_error", "missing_root", "unknown_errors"])
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_partial_locate_cannot_certify_companion_absence_or_uniqueness(
    session: AsyncSession, tmp_path: Path, incomplete: str, ambiguous: bool
) -> None:
    root = tmp_path / "root"
    path = _write(root / "old" / "note.nfo", b"retained bytes")
    agent_id = await _agent(session, root)
    row = await _row(session, agent_id, path)
    path.unlink()
    if ambiguous:
        _write(root / "a" / "note.nfo", b"retained bytes")
        _write(root / "b" / "note.nfo", b"retained bytes")
    located = json.loads(json.dumps(locate_stale(await stale_row_candidates(session, agent_id))))
    if incomplete == "walk_error":
        located["walk_errors"] = 1
    elif incomplete == "missing_root":
        located["walked_roots"] = []
    else:
        del located["walk_errors"]
    report = await reconcile_stale_rows(session, agent_id, located, apply=True)
    assert report.unverifiable == 1 and report.actions == []
    assert row.missing_at is None and row.companion_ambiguous_at is None


async def test_present_duplicate_content_never_becomes_ambiguous(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    rows = [await _row(session, agent_id, _write(root / folder / "copy.nfo", b"same bytes")) for folder in ("a", "b", "c")]
    report = await _reconcile(session, agent_id, apply=True)
    assert report.present == 3 and report.actions == []
    for row in rows:
        await session.refresh(row)
        assert row.companion_ambiguous_at is None and row.missing_at is None


@pytest.mark.parametrize("marker", ["missing_at", "companion_ambiguous_at"])
async def test_unavailable_links_and_pending_reviews_are_preserved_and_do_not_count_as_linked_copies(
    session: AsyncSession, tmp_path: Path, marker: str
) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    path = _write(root / "release" / "note.nfo", b"useful release notes")
    row = await _row(session, agent_id, path)
    media = await _row(session, agent_id, _write(root / "release" / "song.mp3", b"media"))
    await store_companion_features(session, agent_id, [_record(path)])
    session.add(FileCompanion(companion_id=row.id, media_id=media.id))
    review = CompanionJunkReview(
        agent_id=agent_id,
        original_path=str(path),
        sha256_hash=row.sha256_hash,
        file_id=row.id,
        file_type="nfo",
        file_size=row.file_size,
        reason="duplicate",
        content_group=row.sha256_hash,
    )
    session.add(review)
    setattr(row, marker, datetime.now(UTC))
    await session.flush()
    assert await linked_copy_fingerprints(session, agent_id, {row.sha256_hash}) == set()
    association = await associate_companions(session, agent_id=agent_id, apply=True)
    assert association.decided == {} and association.links_removed == 0
    assert await session.scalar(select(func.count()).select_from(FileCompanion).where(FileCompanion.companion_id == row.id)) == 1
    detection = await detect_junk_reviews(session, agent_id, apply=True)
    assert detection.created == {} and detection.withdrawn == 0
    assert await session.get(CompanionJunkReview, review.id) is not None
    assert (await count_backfill(session))[0].current == 0


async def test_real_upsert_clears_both_unavailable_markers_without_rekeying(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    path = _write(root / "note.txt", b"back again")
    row = await _row(session, agent_id, path)
    row.missing_at = row.companion_ambiguous_at = datetime.now(UTC)
    await session.flush()
    file_id = row.id
    await _upsert_rows(
        session,
        [
            {
                "agent_id": agent_id,
                "original_path": str(path),
                "original_filename": path.name,
                "current_path": str(path),
                "file_type": "txt",
                "file_size": row.file_size,
                "sha256_hash": row.sha256_hash,
            }
        ],
    )
    await session.refresh(row)
    assert row.id == file_id and row.missing_at is None and row.companion_ambiguous_at is None


async def test_plural_byte_copies_are_ambiguous_even_when_only_one_keeps_the_filename(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _write(root / "old" / "note.nfo", b"same bytes")
    agent_id = await _agent(session, root)
    row = await _row(session, agent_id, path)
    file_id = row.id
    path.unlink()
    _write(root / "a" / "note.nfo", b"same bytes")
    _write(root / "b" / "other.nfo", b"same bytes")
    report = await _reconcile(session, agent_id, apply=True)
    assert [action.verdict for action in report.actions] == [Verdict.AMBIGUOUS]
    row = await session.get(FileRecord, file_id)
    assert row.current_path == str(path) and row.companion_ambiguous_at is not None


@pytest.mark.parametrize("readmitted", [False, True])
async def test_unique_verified_companion_destination_preserves_source_identity_and_clears_ambiguity(
    session: AsyncSession, tmp_path: Path, readmitted: bool
) -> None:
    root = tmp_path / "root"
    path = _write(root / "old" / "note.nfo", b"unique moved bytes")
    agent_id = await _agent(session, root)
    row = await _row(session, agent_id, path)
    file_id = row.id
    row.companion_ambiguous_at = datetime.now(UTC)
    target = root / "new" / "note.nfo"
    target.parent.mkdir()
    path.rename(target)
    if readmitted:
        await _row(session, agent_id, target)
    report = await _reconcile(session, agent_id, apply=True)
    assert [action.verdict for action in report.actions] == [Verdict.MERGE if readmitted else Verdict.REPOINT]
    row = await session.get(FileRecord, file_id)
    assert row.original_path == str(target) and row.current_path == str(target)
    assert row.missing_at is None and row.companion_ambiguous_at is None
    assert await session.scalar(select(func.count()).select_from(FileRecord).where(FileRecord.agent_id == agent_id)) == 1
    assert (await _reconcile(session, agent_id, apply=True)).actions == []


@pytest.mark.parametrize("field", ["current_path", "sha256_hash", "file_size"])
async def test_ambiguous_mark_refuses_a_row_changed_since_locate(session: AsyncSession, tmp_path: Path, field: str) -> None:
    root = tmp_path / "root"
    path = _write(root / "old" / "note.nfo", b"same bytes")
    agent_id = await _agent(session, root)
    row = await _row(session, agent_id, path)
    path.unlink()
    _write(root / "a" / "note.nfo", b"same bytes")
    _write(root / "b" / "note.nfo", b"same bytes")
    located = locate_stale(await stale_row_candidates(session, agent_id))
    setattr(row, field, 99 if field == "file_size" else "changed")
    await session.flush()
    report = await reconcile_stale_rows(session, agent_id, located, apply=True)
    assert [action.verdict for action in report.actions] == [Verdict.CHANGED]
    assert row.missing_at is None and row.companion_ambiguous_at is None


@pytest.mark.parametrize("shape", ["escaping_symlink", "fifo", "oversized_copy"])
async def test_unsafe_or_unbounded_companion_reads_are_unverifiable_not_missing(session: AsyncSession, tmp_path: Path, shape: str) -> None:
    root = tmp_path / "root"
    path = _write(root / "old" / "note.nfo", b"same bytes")
    agent_id = await _agent(session, root)
    row = await _row(session, agent_id, path)
    path.unlink()
    if shape == "escaping_symlink":
        outside = _write(tmp_path / "outside" / "note.nfo", b"same bytes")
        path.symlink_to(outside)
    elif shape == "fifo":
        os.mkfifo(path)
    else:
        # A matching-size file above the bounded verifier's cap must prevent a missing verdict.
        row.file_size = 1_048_577
        row.sha256_hash = "a" * 64
        await session.flush()
        copy = root / "large" / "note.nfo"
        copy.parent.mkdir()
        with copy.open("wb") as stream:
            stream.truncate(row.file_size)
    report = await _reconcile(session, agent_id, apply=True)
    assert report.unverifiable == 1 and report.actions == [] and report.walk_errors > 0
    await session.refresh(row)
    assert row.missing_at is None and row.companion_ambiguous_at is None


async def test_real_locate_opens_the_contained_nfd_twin_of_the_stored_nfc_path(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    disk = _write(root / unicodedata.normalize("NFD", "caf\u00e9") / unicodedata.normalize("NFD", "not\u00e9.txt"), b"real twin bytes")
    agent_id = await _agent(session, root)
    row = await _row(session, agent_id, disk)
    row.original_path = row.current_path = unicodedata.normalize("NFC", str(disk))
    await session.flush()
    report = await _reconcile(session, agent_id, apply=True)
    assert report.present == 1 and report.actions == [] and report.walk_errors == 0


async def test_unavailable_copy_is_not_a_stamp_member_and_its_feature_history_is_preserved(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    agent_id = await _agent(session, root)
    paths = [_write(root / name / "site.nfo", b"Downloaded from www.example-release-site.test\nJoin our forum!\n") for name in ("a", "b", "c")]
    rows = [await _row(session, agent_id, path) for path in paths]
    await store_companion_features(session, agent_id, [_record(path) for path in paths])
    fingerprint = rows[0].sha256_hash
    assert await refresh_known_stamps(session, agent_id, {fingerprint}) == {fingerprint}
    rows[0].missing_at = datetime.now(UTC)
    await session.flush()
    assert await refresh_known_stamps(session, agent_id, {fingerprint}) == set()
    features = {
        entry.file_id: entry.junk_class
        for entry in await session.execute(select(CompanionContentFeatures.file_id, CompanionContentFeatures.junk_class))
    }
    assert features[rows[0].id] == "known_stamp", "unavailable feature history remains untouched"
    assert all(features[row.id] != "known_stamp" for row in rows[1:])


async def test_locate_never_repoints_a_companion_into_quarantine(session: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = _write(root / "old" / "note.txt", b"quarantined bytes")
    agent_id = await _agent(session, root)
    row = await _row(session, agent_id, path)
    file_id = row.id
    path.unlink()
    _write(root / ".phaze-quarantine" / "note.txt", b"quarantined bytes")
    report = await _reconcile(session, agent_id, apply=True)
    assert [action.verdict for action in report.actions] == [Verdict.MISSING]
    row = await session.get(FileRecord, file_id)
    assert row.current_path == str(path) and row.missing_at is not None
