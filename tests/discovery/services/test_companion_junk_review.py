"""The junk-companion review queue: the shared predicate, the detector, the decisions and the table (phaze-bk5jp).

Content features are produced by the REAL extractor over synthetic files in ``tmp_path``
(``read_companion`` -> ``store_companion_features``), exactly as an agent report would land them;
only the ``files`` rows the scan would have upserted, and the ``file_companions`` links the linking
chain would have written, are seeded directly. Everything runs against real Postgres.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from phaze.constants import QUARANTINE_DIRNAME
from phaze.enums.junk_review import TERMINAL_STATUSES, TRANSITIONS, JunkReviewStatus, allowed_from
from phaze.models.agent import Agent
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.scan_batch import ScanBatch
from phaze.schemas.agent_companion_features import CompanionFeaturesRecord
from phaze.services.companion import _media_index
from phaze.services.companion_autolink import run_agent_association
from phaze.services.companion_content import linked_copy_fingerprints, store_companion_features
from phaze.services.companion_features import read_companion
from phaze.services.companion_junk_review import (
    DUPLICATE_REASON,
    DetectionOutcome,
    JunkReviewTransitionRefused,
    decide_content_group,
    detect_junk_reviews,
    transition_review,
)
from phaze.services.companion_linking import LinkInput, link_companion
from phaze.services.scan_deletion import delete_file_cascade, delete_scan_cascade


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


_AGENT = "test-fileserver"
_OTHER = "other-fileserver"
_STAMP = b"Downloaded from www.example-release-site.test\r\nVisit us for more free sets!\r\nJoin us on the forum.\r\n"
_TRACKLIST = b"\r\nTracklist:\r\n01. Example Artist - First Tune\r\n02. Other Artist - Second Tune\r\n03. Third Artist - Third Tune\r\n"
_INFO = b"Artist ....: Example Artist\r\nGenre .....: Trance\r\nSource ....: FM\r\nAir date ..: 2024-01-01\r\n"


def _put(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


async def _row(session: AsyncSession, path: Path, *, agent_id: str = _AGENT, batch_id: uuid.UUID | None = None) -> FileRecord:
    """The files row the scan would have upserted for ``path`` (current bytes, real hash)."""
    payload = path.read_bytes()
    record = FileRecord(
        agent_id=agent_id,
        sha256_hash=hashlib.sha256(payload).hexdigest(),
        original_path=str(path),
        original_filename=path.name,
        current_path=str(path),
        file_type=path.suffix.lstrip(".").lower(),
        file_size=len(payload),
        batch_id=batch_id,
    )
    session.add(record)
    await session.flush()
    return record


async def _companion(session: AsyncSession, path: Path, payload: bytes, *, agent_id: str = _AGENT, batch_id: uuid.UUID | None = None) -> FileRecord:
    """Write a companion, ingest its row, and store the features the real extractor reads from it."""
    row = await _row(session, _put(path, payload), agent_id=agent_id, batch_id=batch_id)
    await store_companion_features(session, agent_id, [CompanionFeaturesRecord.from_reading(str(path), read_companion(str(path)))])
    return row


async def _link(session: AsyncSession, companion: FileRecord, media: FileRecord) -> None:
    session.add(FileCompanion(companion_id=companion.id, media_id=media.id))
    await session.flush()


async def _reviews(session: AsyncSession, **filters: Any) -> list[CompanionJunkReview]:
    statement = select(CompanionJunkReview).filter_by(**filters).order_by(CompanionJunkReview.original_path, CompanionJunkReview.created_at)
    return list((await session.execute(statement.execution_options(populate_existing=True))).scalars())


async def _pending(session: AsyncSession) -> dict[str, str]:
    """``basename-with-parent -> reason`` of every pending row, for compact assertions."""
    return {"/".join(row.original_path.split("/")[-2:]): row.reason for row in await _reviews(session, status="pending")}


# The table.


def test_the_table_has_no_foreign_key_and_no_actor_column() -> None:
    """FK-free (survives both cascades) and approval records a time only (decision 7)."""
    table = CompanionJunkReview.__table__
    assert table.foreign_keys == set()
    assert {column.name for column in table.columns} == {
        "id",
        "agent_id",
        "original_path",
        "sha256_hash",
        "file_id",
        "file_type",
        "file_size",
        "reason",
        "content_group",
        "status",
        "decided_at",
        "executed_at",
        "error_message",
        "created_at",
        "updated_at",
    }


async def test_the_live_identity_index_is_the_index_migration_080_builds(session: AsyncSession) -> None:
    """The model's partial-index predicate is a column expression; Postgres stores the same index from it as from the migration's SQL.

    The compiled DDL differs only by one pair of parentheses around the predicate, so the comparison
    is made on what Postgres keeps (``pg_get_indexdef``): both statements are run against a scratch
    copy of the table, under two names.
    """
    from sqlalchemy import text
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex

    model = next(index for index in CompanionJunkReview.__table__.indexes if index.name == "uq_companion_junk_review_live_identity")
    from_model = str(CreateIndex(model).compile(dialect=postgresql.dialect()))
    assert "status NOT IN ('failed', 'quarantined')" in from_model  # values rendered as literals, never binds
    from_migration = (
        "CREATE UNIQUE INDEX uq_companion_junk_review_live_identity ON companion_junk_review (agent_id, original_path, sha256_hash) "
        "WHERE status NOT IN ('failed', 'quarantined')"
    )
    await session.execute(text("CREATE TEMP TABLE scratch_review (LIKE companion_junk_review)"))
    stored = []
    for name, ddl in (("from_model", from_model), ("from_migration", from_migration)):
        await session.execute(
            text(ddl.replace("uq_companion_junk_review_live_identity", name).replace("ON companion_junk_review ", "ON scratch_review "))
        )
        definition = (await session.execute(text(f"SELECT pg_get_indexdef('{name}'::regclass)"))).scalar_one()
        stored.append(definition.replace(name, "<index>"))
    assert stored[0] == stored[1], stored


def test_no_transition_leaves_a_terminal_status() -> None:
    assert set(TRANSITIONS) == set(JunkReviewStatus)
    for status in TERMINAL_STATUSES:
        assert TRANSITIONS[status] == frozenset()
        assert all(status not in allowed_from(target) for target in JunkReviewStatus)


async def test_review_rows_survive_scan_and_file_deletion(session: AsyncSession, tmp_path: Path) -> None:
    """The guard: neither cascade is blocked by a review row, and neither erases one."""
    batch = ScanBatch(agent_id=_AGENT, scan_path=str(tmp_path))
    session.add(batch)
    await session.flush()
    in_batch = await _companion(session, tmp_path / "a" / "empty.nfo", b"", batch_id=batch.id)
    single = await _companion(session, tmp_path / "b" / "empty.txt", b"\x00" * 64)
    await detect_junk_reviews(session, _AGENT, apply=True)
    rows = await _reviews(session)
    assert {row.file_id for row in rows} == {in_batch.id, single.id}

    await delete_scan_cascade(session, batch.id)
    await delete_file_cascade(session, single.id)

    remaining = (await session.execute(select(FileRecord.id).where(FileRecord.id.in_([in_batch.id, single.id])))).scalars().all()
    assert remaining == []
    survivors = await _reviews(session)
    assert [(row.id, row.status, row.file_id) for row in survivors] == [(row.id, "pending", row.file_id) for row in rows]


# The detector.


async def test_the_detector_proposes_every_junk_class_and_nothing_else(session: AsyncSession, tmp_path: Path) -> None:
    for index in range(3):  # one stamp content across three releases beside different recordings
        _put(tmp_path / f"release-{index}" / f"set-{index}.mp3", b"audio")
        await _companion(session, tmp_path / f"release-{index}" / "site.nfo", _STAMP)
    _put(tmp_path / "release-x" / "set-x.mp3", b"audio")
    await _companion(session, tmp_path / "release-x" / "site.nfo", _STAMP + _TRACKLIST)  # stamp + real text: kept
    await _companion(session, tmp_path / "ad" / "promo.txt", b"Downloaded from www.example-ads.test\r\nVisit us for more free sets!\r\n")
    await _companion(session, tmp_path / "e" / "empty.nfo", b"")
    await _companion(session, tmp_path / "n" / "nul.m3u", b"\x00" * 256)
    await _companion(session, tmp_path / "i" / "info.nfo", _INFO)

    outcome = await detect_junk_reviews(session, _AGENT, apply=True)

    assert await _pending(session) == {
        "release-0/site.nfo": "known_stamp",
        "release-1/site.nfo": "known_stamp",
        "release-2/site.nfo": "known_stamp",
        "ad/promo.txt": "site_ad",
        "e/empty.nfo": "empty",
        "n/nul.m3u": "all_nul",
    }
    assert outcome.created == {"known_stamp": 3, "site_ad": 1, "empty": 1, "all_nul": 1}
    stamp = (await _reviews(session, reason="known_stamp"))[0]
    assert stamp.content_group == stamp.sha256_hash == hashlib.sha256(_STAMP).hexdigest()
    assert (stamp.file_size, stamp.file_type, stamp.decided_at, stamp.executed_at) == (len(_STAMP), "nfo", None, None)


async def test_media_stale_features_and_quarantined_paths_are_never_proposed(session: AsyncSession, tmp_path: Path) -> None:
    media = await _row(session, _put(tmp_path / "rel" / "set.mp3", b""))
    # A features row on a media row can only be planted by hand; the detector still never reads it.
    planted = await _companion(session, tmp_path / "y" / "empty.txt", b"")
    planted.file_type = "mp3"
    stale = await _companion(session, tmp_path / "s" / "was-empty.nfo", b"")
    stale.sha256_hash = "f" * 64  # the bytes changed after the features were read
    await _companion(session, tmp_path / QUARANTINE_DIRNAME / "rel" / "empty.nfo", b"")
    await session.flush()

    outcome = await detect_junk_reviews(session, _AGENT, apply=True)

    assert outcome.created == {}
    assert await _reviews(session) == []
    assert media.file_type == planted.file_type == "mp3"


async def test_a_truncated_read_is_not_junk_by_its_head(session: AsyncSession, tmp_path: Path) -> None:
    row = await _companion(session, tmp_path / "big" / "head.nfo", b"\x00" * 128)
    stored = await session.get(CompanionContentFeatures, row.id)
    assert stored is not None
    stored.truncated = True
    await session.flush()

    assert (await detect_junk_reviews(session, _AGENT, apply=True)).created == {}


async def test_duplicate_linked_then_detect(session: AsyncSession, tmp_path: Path) -> None:
    """Decision 1: the copy whose twin is LINKED is proposed; the linked twin is not."""
    media = await _row(session, _put(tmp_path / "keep" / "set.mp3", b"audio"))
    linked = await _companion(session, tmp_path / "keep" / "info.nfo", _INFO)
    await _link(session, linked, media)
    orphan = await _companion(session, tmp_path / "leftover" / "info.nfo", _INFO)
    beside_media = tmp_path / "copy" / "info.nfo"
    await _row(session, _put(tmp_path / "copy" / "set.mp3", b"audio"))
    await _companion(session, beside_media, _INFO)  # a duplicated release beside its own media: not junk
    await _companion(session, tmp_path / "other-agent" / "info.nfo", _INFO, agent_id=await _other_agent(session, tmp_path))

    outcome = await detect_junk_reviews(session, _AGENT, apply=True)

    assert await _pending(session) == {"leftover/info.nfo": DUPLICATE_REASON}
    assert outcome.created == {DUPLICATE_REASON: 1}
    assert (await _reviews(session))[0].file_id == orphan.id
    assert (await detect_junk_reviews(session, _OTHER, apply=True)).created == {}  # the twin is linked on ANOTHER agent


async def _junk_classes(session: AsyncSession, *rows: FileRecord) -> list[str | None]:
    statement = select(CompanionContentFeatures).where(CompanionContentFeatures.file_id.in_([row.id for row in rows]))
    by_id = {
        features.file_id: features.junk_class for features in (await session.execute(statement.execution_options(populate_existing=True))).scalars()
    }
    return [by_id[row.id] for row in rows]


_RELEASES = ("Release One", "release-two (web)", "Release.Three.FM")
"""Three differently named folders: the folder-name half of the stamp rule holds for any content in them."""


async def test_a_release_nfo_stamped_before_its_media_arrives_is_un_stamped_and_its_reviews_withdrawn(session: AsyncSession, tmp_path: Path) -> None:
    """phaze-4x319.5: the watcher posts a companion before its media, so its folders first hold none.

    A release NFO copied beside the same recording in three differently named folders reads as a
    stamp while no folder holds media yet. Once the recording lands beside every copy, the media
    sets intersect: the association run requested by those inserts re-decides the group on the
    media stored NOW, links each copy to its own recording, and the pending rows are withdrawn.
    """
    copies = [await _companion(session, tmp_path / name / "info.nfo", _INFO) for name in _RELEASES]
    assert await _junk_classes(session, *copies) == ["known_stamp"] * 3  # no folder holds media yet
    await run_agent_association(session, _AGENT, apply=True)
    assert set((await _pending(session)).values()) == {"known_stamp"}

    media = [await _row(session, _put(tmp_path / name / "set.mp3", b"audio")) for name in _RELEASES]
    await run_agent_association(session, _AGENT, apply=True)

    assert await _junk_classes(session, *copies) == [None, None, None]
    assert await _pending(session) == {}
    links = set((await session.execute(select(FileCompanion.companion_id, FileCompanion.media_id))).tuples())
    assert links == {(copy.id, recording.id) for copy, recording in zip(copies, media, strict=True)}


async def test_media_landing_beside_one_copy_un_stamps_the_group_and_the_rest_become_duplicates(session: AsyncSession, tmp_path: Path) -> None:
    """Some media, but fewer than three distinct sets: no stamp (survey rule 2). The copy beside media links;
    the media-less copies are then byte-identical copies of a linked companion (operator decision 1, 2026-10-07,
    epic phaze-4x319), so their rows stay pending under that reason."""
    copies = [await _companion(session, tmp_path / name / "info.nfo", _INFO) for name in _RELEASES]
    await run_agent_association(session, _AGENT, apply=True)
    assert len(await _pending(session)) == 3

    await _row(session, _put(tmp_path / _RELEASES[0] / "set.mp3", b"audio"))
    await run_agent_association(session, _AGENT, apply=True)

    assert await _junk_classes(session, *copies) == [None, None, None]
    assert await _pending(session) == {f"{name}/info.nfo": DUPLICATE_REASON for name in _RELEASES[1:]}


async def test_the_detector_never_calls_duplicate_what_the_linker_would_not(session: AsyncSession, tmp_path: Path) -> None:
    """phaze-4x319.5: one stored state, two readers, one answer to "does this folder hold media".

    A tracklist read before its episodes arrived carries an agent listing of NO media; the episodes
    are now stored beside it. The detector once judged it from that listing and proposed it as a
    duplicate while the linker saw the media. A genuine orphan copy (no media, ever) is the positive
    control: both must call it a duplicate.
    """
    _put(tmp_path / "keep" / "episode.mp3", b"audio")
    kept = await _companion(session, tmp_path / "keep" / "list.txt", _TRACKLIST)
    await _link(session, kept, await _row(session, tmp_path / "keep" / "episode.mp3"))
    early = await _companion(session, tmp_path / "early" / "list.txt", _TRACKLIST)  # read while "early" held no media
    await _row(session, _put(tmp_path / "early" / "episode.mp3", b"audio"))  # the episode lands afterwards
    orphan = await _companion(session, tmp_path / "orphan" / "list.txt", _TRACKLIST)
    assert (await session.get(CompanionContentFeatures, early.id)).folder_media_count == 0  # type: ignore[union-attr]

    await detect_junk_reviews(session, _AGENT, apply=True)
    detected = {row.file_id for row in await _reviews(session, reason=DUPLICATE_REASON)}

    index = await _media_index(session, _AGENT)
    linker: dict[uuid.UUID, str] = {}
    for row in (kept, early, orphan):
        others_linked = await linked_copy_fingerprints(session, _AGENT, {row.sha256_hash}, exclude_file_ids=[row.id])
        decision = link_companion(
            LinkInput(path=row.original_path, references=(), junk_class=None, is_tracklist=True, identical_copy_linked=bool(others_linked)), index
        )
        linker[row.id] = decision.step
    assert detected == {orphan.id}
    assert detected <= {file_id for file_id, step in linker.items() if step == DUPLICATE_REASON}
    assert linker[early.id] == "folder"


async def test_duplicate_detect_then_linked_then_withdrawn(session: AsyncSession, tmp_path: Path) -> None:
    """The rule reads the STORED links: detection before the link proposes nothing, after it proposes the copy,
    and when the copy itself gains a link its pending row is withdrawn."""
    media = await _row(session, _put(tmp_path / "keep" / "set.mp3", b"audio"))
    twin = await _companion(session, tmp_path / "keep" / "info.nfo", _INFO)
    orphan = await _companion(session, tmp_path / "leftover" / "info.nfo", _INFO)

    assert (await detect_junk_reviews(session, _AGENT, apply=True)).created == {}

    await _link(session, twin, media)
    assert (await detect_junk_reviews(session, _AGENT, apply=True)).created == {DUPLICATE_REASON: 1}
    assert (await detect_junk_reviews(session, _AGENT, apply=True)) == DetectionOutcome(agent_id=_AGENT, refreshed=1)

    await _link(session, orphan, media)
    outcome = await detect_junk_reviews(session, _AGENT, apply=True)
    assert (outcome.created, outcome.withdrawn) == ({}, 1)
    assert await _reviews(session) == []


async def test_two_linked_copies_are_never_both_proposed(session: AsyncSession, tmp_path: Path) -> None:
    """Each of two LINKED copies in media-less folders has "an identical copy linked"; neither is proposed,
    because a linked companion is the copy that survives -- proposing both would delete the content."""
    media = await _row(session, _put(tmp_path / "rec" / "set.mp3", b"audio"))
    # One release-folder name throughout, so the three copies are no stamp (that needs three names).
    for name in ("one", "two"):
        await _link(session, await _companion(session, tmp_path / name / "rel" / "info.nfo", _INFO), media)
    unlinked = await _companion(session, tmp_path / "three" / "rel" / "info.nfo", _INFO)

    assert (await detect_junk_reviews(session, _AGENT, apply=True)).created == {DUPLICATE_REASON: 1}
    assert [row.file_id for row in await _reviews(session)] == [unlinked.id]


async def test_approval_re_judges_a_duplicate_on_the_links_stored_now(session: AsyncSession, tmp_path: Path) -> None:
    """The linking chain re-derives every link (phaze-rmhfr): a duplicate whose linked copy has lost its
    link since detection is withdrawn at approval, never approved; one that still holds is approved."""
    media = await _row(session, _put(tmp_path / "rec" / "set.mp3", b"audio"))
    twin = await _companion(session, tmp_path / "rec" / "info.nfo", _INFO)
    await _link(session, twin, media)
    await _companion(session, tmp_path / "left-a" / "info.nfo", _INFO)
    await _companion(session, tmp_path / "left-b" / "info.nfo", _INFO)
    assert (await detect_junk_reviews(session, _AGENT, apply=True)).created == {DUPLICATE_REASON: 2}
    sha = twin.sha256_hash

    # The links are re-derived and the twin loses its link before the operator approves.
    await session.execute(delete(FileCompanion).where(FileCompanion.companion_id == twin.id))
    assert await decide_content_group(session, sha, JunkReviewStatus.APPROVED) == 0
    assert await _reviews(session) == []

    # Re-linked and re-detected: now the approval goes through, for both copies.
    await _link(session, twin, media)
    await detect_junk_reviews(session, _AGENT, apply=True)
    assert await decide_content_group(session, sha, JunkReviewStatus.APPROVED) == 2
    assert {row.status for row in await _reviews(session)} == {"approved"}


async def test_approval_keeps_a_former_duplicate_that_is_now_junk_under_its_new_reason(session: AsyncSession, tmp_path: Path) -> None:
    media = await _row(session, _put(tmp_path / "rec" / "set.mp3", b"audio"))
    twin = await _companion(session, tmp_path / "rec" / "info.nfo", _INFO)
    await _link(session, twin, media)
    orphan = await _companion(session, tmp_path / "left" / "info.nfo", _INFO)
    await detect_junk_reviews(session, _AGENT, apply=True)
    features = await session.get(CompanionContentFeatures, orphan.id)
    assert features is not None
    features.junk_class = "site_ad"  # its class changed since detection
    await session.execute(delete(FileCompanion).where(FileCompanion.companion_id == twin.id))
    await session.flush()

    assert await decide_content_group(session, orphan.sha256_hash, JunkReviewStatus.APPROVED) == 1
    assert [(row.status, row.reason) for row in await _reviews(session)] == [("approved", "site_ad")]


async def test_a_dry_run_counts_and_writes_nothing(session: AsyncSession, tmp_path: Path) -> None:
    await _companion(session, tmp_path / "e" / "empty.nfo", b"")

    outcome = await detect_junk_reviews(session, _AGENT, apply=False)

    assert outcome.created == {"empty": 1}
    assert await _reviews(session) == []


async def test_a_pending_row_follows_its_current_reason(session: AsyncSession, tmp_path: Path) -> None:
    rows = []
    for index in range(3):
        _put(tmp_path / f"release-{index}" / f"set-{index}.mp3", b"audio")
        rows.append(await _companion(session, tmp_path / f"release-{index}" / "site.nfo", _STAMP))
    await detect_junk_reviews(session, _AGENT, apply=True)
    assert set((await _pending(session)).values()) == {"known_stamp"}

    await delete_file_cascade(session, rows[2].id)  # two copies left: no longer a stamp, still a site ad
    outcome = await detect_junk_reviews(session, _AGENT, apply=True)

    assert (outcome.refreshed, outcome.withdrawn) == (2, 1)
    assert await _pending(session) == {"release-0/site.nfo": "site_ad", "release-1/site.nfo": "site_ad"}


# Decisions.


async def _other_agent(session: AsyncSession, tmp_path: Path) -> str:
    if await session.get(Agent, _OTHER) is None:
        session.add(Agent(id=_OTHER, name=_OTHER, token_hash=uuid.uuid4().hex * 2, scan_roots=[str(tmp_path)]))
        await session.flush()
    return _OTHER


async def test_a_rejection_covers_every_identical_copy(session: AsyncSession, tmp_path: Path) -> None:
    """Decision 5: one rejection covers every row with that SHA-256 -- pending or approved, on every agent --
    and no copy of that content is proposed again, at any path, until the rejection is undone."""
    other = await _other_agent(session, tmp_path)
    await _companion(session, tmp_path / "a" / "empty.nfo", b"")
    await _companion(session, tmp_path / "b" / "empty.nfo", b"")
    await _companion(session, tmp_path / "c" / "empty.txt", b"", agent_id=other)
    await _companion(session, tmp_path / "d" / "nul.nfo", b"\x00" * 32)
    for agent in (_AGENT, other):
        await detect_junk_reviews(session, agent, apply=True)
    empty_sha = hashlib.sha256(b"").hexdigest()
    first = (await _reviews(session, sha256_hash=empty_sha))[0]
    assert await transition_review(session, first.id, JunkReviewStatus.APPROVED) is True

    assert await decide_content_group(session, empty_sha, JunkReviewStatus.REJECTED) == 3

    assert {row.status for row in await _reviews(session, sha256_hash=empty_sha)} == {"rejected"}
    assert all(row.decided_at is not None for row in await _reviews(session, sha256_hash=empty_sha))
    assert [row.status for row in await _reviews(session, reason="all_nul")] == ["pending"]  # other content untouched
    await _companion(session, tmp_path / "late" / "empty.cue", b"")  # a new copy turns up later
    outcome = await detect_junk_reviews(session, _AGENT, apply=True)
    assert (outcome.created, outcome.rejected_content) == ({}, 3)

    assert await decide_content_group(session, empty_sha, JunkReviewStatus.PENDING) == 3  # undo
    undone = await _reviews(session, sha256_hash=empty_sha)
    assert {(row.status, row.decided_at) for row in undone} == {("pending", None)}
    assert (await detect_junk_reviews(session, _AGENT, apply=True)).created == {"empty": 1}  # the late copy now joins


async def test_approval_records_a_time_and_only_from_pending(session: AsyncSession, tmp_path: Path) -> None:
    await _companion(session, tmp_path / "a" / "empty.nfo", b"")
    await _companion(session, tmp_path / "b" / "empty.nfo", b"")
    await detect_junk_reviews(session, _AGENT, apply=True)
    empty_sha = hashlib.sha256(b"").hexdigest()

    assert await decide_content_group(session, empty_sha, JunkReviewStatus.APPROVED) == 2
    assert await decide_content_group(session, empty_sha, JunkReviewStatus.APPROVED) == 0
    rows = await _reviews(session)
    assert {row.status for row in rows} == {"approved"}
    assert all(row.decided_at is not None and row.executed_at is None for row in rows)
    with pytest.raises(ValueError, match="not a review decision"):
        await decide_content_group(session, empty_sha, JunkReviewStatus.QUARANTINED)


async def _quarantine(session: AsyncSession, review_id: uuid.UUID) -> None:
    for status in (JunkReviewStatus.APPROVED, JunkReviewStatus.EXECUTING, JunkReviewStatus.QUARANTINED):
        assert await transition_review(session, review_id, status) is True


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
async def test_the_state_machine_refuses_to_leave_a_terminal_status(session: AsyncSession, tmp_path: Path, terminal: JunkReviewStatus) -> None:
    empty = await _companion(session, tmp_path / "a" / "empty.nfo", b"")
    await detect_junk_reviews(session, _AGENT, apply=True)
    row = (await _reviews(session))[0]
    for status in (JunkReviewStatus.APPROVED, JunkReviewStatus.EXECUTING):
        await transition_review(session, row.id, status)
    assert await transition_review(session, row.id, terminal, error_message="boom" if terminal == "failed" else None) is True

    for target in JunkReviewStatus:
        if target == terminal:
            assert await transition_review(session, row.id, target) is False  # a replayed report is a no-op
            continue
        with pytest.raises(JunkReviewTransitionRefused):
            await transition_review(session, row.id, target)
    # The group decisions skip it too.
    for target in (JunkReviewStatus.APPROVED, JunkReviewStatus.REJECTED, JunkReviewStatus.PENDING):
        assert await decide_content_group(session, empty.sha256_hash, target) == 0
    final = (await _reviews(session))[0]
    assert (final.status, final.executed_at is not None, final.decided_at is not None) == (terminal, True, True)
    assert final.error_message == ("boom" if terminal == "failed" else None)


async def test_transitions_follow_the_table_and_refuse_skipped_edges(session: AsyncSession, tmp_path: Path) -> None:
    await _companion(session, tmp_path / "a" / "empty.nfo", b"")
    await detect_junk_reviews(session, _AGENT, apply=True)
    row = (await _reviews(session))[0]

    with pytest.raises(JunkReviewTransitionRefused, match="pending; it cannot become executing"):
        await transition_review(session, row.id, JunkReviewStatus.EXECUTING)  # never without an approval
    with pytest.raises(LookupError):
        await transition_review(session, uuid.uuid4(), JunkReviewStatus.APPROVED)
    assert await transition_review(session, row.id, JunkReviewStatus.APPROVED) is True
    assert await transition_review(session, row.id, JunkReviewStatus.PENDING) is True  # undo
    assert (await _reviews(session))[0].decided_at is None


async def test_a_quarantined_identity_that_reappears_goes_back_to_pending(session: AsyncSession, tmp_path: Path) -> None:
    """Decision 4: a new PENDING row beside the quarantined audit row -- never approved automatically, even
    while the rest of its content group is approved."""
    path = tmp_path / "a" / "empty.nfo"
    gone = await _companion(session, path, b"")
    await _companion(session, tmp_path / "b" / "empty.nfo", b"")
    await detect_junk_reviews(session, _AGENT, apply=True)
    first = next(row for row in await _reviews(session) if row.file_id == gone.id)
    await _quarantine(session, first.id)
    await delete_file_cascade(session, gone.id)  # the quarantine task retires the row
    await decide_content_group(session, first.sha256_hash, JunkReviewStatus.APPROVED)  # the other copy is approved

    back = await _row(session, path)  # re-downloaded: a new files row, no features read yet
    outcome = await detect_junk_reviews(session, _AGENT, apply=True)

    assert (outcome.created, outcome.reappeared) == ({"empty": 1}, 1)
    rows = await _reviews(session, original_path=str(path))
    assert sorted((row.status, row.file_id, row.decided_at is None) for row in rows) == [("pending", back.id, True), ("quarantined", gone.id, False)]
    # Its features land later: still the same one pending row.
    await store_companion_features(session, _AGENT, [CompanionFeaturesRecord.from_reading(str(path), read_companion(str(path)))])
    assert (await detect_junk_reviews(session, _AGENT, apply=True)).created == {}


async def test_a_failed_identity_still_on_disk_goes_back_to_review(session: AsyncSession, tmp_path: Path) -> None:
    await _companion(session, tmp_path / "a" / "empty.nfo", b"")
    await detect_junk_reviews(session, _AGENT, apply=True)
    row = (await _reviews(session))[0]
    for status in (JunkReviewStatus.APPROVED, JunkReviewStatus.EXECUTING, JunkReviewStatus.FAILED):
        await transition_review(session, row.id, status)

    assert (await detect_junk_reviews(session, _AGENT, apply=True)).created == {"empty": 1}
    assert sorted(r.status for r in await _reviews(session)) == ["failed", "pending"]


async def test_one_live_row_per_identity_is_enforced_by_the_database(session: AsyncSession, tmp_path: Path) -> None:
    await _companion(session, tmp_path / "a" / "empty.nfo", b"")
    await detect_junk_reviews(session, _AGENT, apply=True)
    row = (await _reviews(session))[0]

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                CompanionJunkReview(
                    agent_id=row.agent_id,
                    original_path=row.original_path,
                    sha256_hash=row.sha256_hash,
                    file_type="nfo",
                    file_size=0,
                    reason="empty",
                    content_group=row.sha256_hash,
                )
            )
            await session.flush()
