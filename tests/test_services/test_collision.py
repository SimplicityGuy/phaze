"""Tests for collision detection service and directory tree builder."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
import uuid

import pytest

from phaze.models.file import FileRecord, FileState
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.collision import TreeNode, build_tree


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_proposal(
    session: AsyncSession,
    *,
    proposed_filename: str = "Artist - Track.mp3",
    proposed_path: str | None = None,
    status: str = ProposalStatus.APPROVED,
    original_filename: str = "test.mp3",
) -> RenameProposal:
    """Create a FileRecord + RenameProposal pair for testing."""
    file_id = uuid.uuid4()
    file_record = FileRecord(
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=1_000_000,
        state=FileState.PROPOSAL_GENERATED,
    )
    session.add(file_record)
    await session.flush()

    proposal = RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename=proposed_filename,
        proposed_path=proposed_path,
        confidence=0.9,
        status=status,
        context_used={"artist": "Test"},
        reason="test",
    )
    session.add(proposal)
    await session.commit()
    return proposal


def _mock_proposal(
    *,
    proposed_filename: str = "Track.mp3",
    proposed_path: str | None = None,
) -> MagicMock:
    """Create a mock RenameProposal for tree builder tests."""
    p = MagicMock(spec=RenameProposal)
    p.proposed_filename = proposed_filename
    p.proposed_path = proposed_path
    return p


# ---------------------------------------------------------------------------
# detect_collisions tests (require database)
# ---------------------------------------------------------------------------


class TestDetectCollisions:
    """Tests for the detect_collisions SQL query."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_approved_proposals(self, session: AsyncSession) -> None:
        from phaze.services.collision import detect_collisions

        result = await detect_collisions(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_duplicates(self, session: AsyncSession) -> None:
        from phaze.services.collision import detect_collisions

        await _create_proposal(session, proposed_filename="A.mp3", proposed_path="performances/artists/A")
        await _create_proposal(session, proposed_filename="B.mp3", proposed_path="performances/artists/B")

        result = await detect_collisions(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_detects_duplicate_destinations(self, session: AsyncSession) -> None:
        from phaze.services.collision import detect_collisions

        await _create_proposal(session, proposed_filename="Live.mp3", proposed_path="performances/artists/Disclosure")
        await _create_proposal(
            session,
            proposed_filename="Live.mp3",
            proposed_path="performances/artists/Disclosure",
            original_filename="dupe.mp3",
        )

        result = await detect_collisions(session)
        assert len(result) == 1
        assert result[0][0] == "performances/artists/Disclosure/Live.mp3"
        assert result[0][1] == 2

    @pytest.mark.asyncio
    async def test_excludes_null_proposed_path(self, session: AsyncSession) -> None:
        from phaze.services.collision import detect_collisions

        # Two proposals with null path and same filename should NOT collide
        await _create_proposal(session, proposed_filename="Track.mp3", proposed_path=None)
        await _create_proposal(session, proposed_filename="Track.mp3", proposed_path=None, original_filename="other.mp3")

        result = await detect_collisions(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_excludes_non_approved_proposals(self, session: AsyncSession) -> None:
        from phaze.services.collision import detect_collisions

        path = "performances/artists/Deadmau5"
        await _create_proposal(session, proposed_filename="Set.mp3", proposed_path=path)
        await _create_proposal(
            session,
            proposed_filename="Set.mp3",
            proposed_path=path,
            status=ProposalStatus.PENDING,
            original_filename="pending.mp3",
        )
        await _create_proposal(
            session,
            proposed_filename="Set.mp3",
            proposed_path=path,
            status=ProposalStatus.REJECTED,
            original_filename="rejected.mp3",
        )

        result = await detect_collisions(session)
        # Only 1 approved, so no collision
        assert result == []


# ---------------------------------------------------------------------------
# get_collision_ids tests (require database)
# ---------------------------------------------------------------------------


class TestGetCollisionIds:
    """Tests for get_collision_ids returning UUIDs of colliding proposals."""

    @pytest.mark.asyncio
    async def test_returns_collision_proposal_ids(self, session: AsyncSession) -> None:
        from phaze.services.collision import get_collision_ids

        path = "performances/artists/Disclosure"
        p1 = await _create_proposal(session, proposed_filename="Live.mp3", proposed_path=path)
        p2 = await _create_proposal(
            session,
            proposed_filename="Live.mp3",
            proposed_path=path,
            original_filename="dupe.mp3",
        )

        ids = await get_collision_ids(session)
        assert str(p1.id) in ids
        assert str(p2.id) in ids


# ---------------------------------------------------------------------------
# build_tree tests (pure Python, no database)
# ---------------------------------------------------------------------------


class TestBuildTree:
    """Tests for the directory tree builder."""

    def test_empty_list_returns_root_with_no_children(self) -> None:
        root = build_tree([])
        assert root.name == "output"
        assert root.children == {}
        assert root.files == []
        assert root.file_count == 0

    def test_nests_files_under_correct_directory_path(self) -> None:
        p = _mock_proposal(proposed_filename="Live.mp3", proposed_path="performances/artists/Disclosure")
        root = build_tree([p])

        assert "performances" in root.children
        perf = root.children["performances"]
        assert "artists" in perf.children
        artists = perf.children["artists"]
        assert "Disclosure" in artists.children
        disclosure = artists.children["Disclosure"]
        assert "Live.mp3" in disclosure.files

    def test_null_path_goes_to_root_files(self) -> None:
        p = _mock_proposal(proposed_filename="Unknown.mp3", proposed_path=None)
        root = build_tree([p])
        assert "Unknown.mp3" in root.files

    def test_recursive_file_count(self) -> None:
        proposals = [
            _mock_proposal(proposed_filename="A.mp3", proposed_path="music/Artist"),
            _mock_proposal(proposed_filename="B.mp3", proposed_path="music/Artist"),
            _mock_proposal(proposed_filename="C.mp3", proposed_path=None),
        ]
        root = build_tree(proposals)
        assert root.file_count == 3
        assert root.children["music"].file_count == 2
        assert root.children["music"].children["Artist"].file_count == 2

    def test_multiple_files_same_directory_no_duplicate_nodes(self) -> None:
        proposals = [
            _mock_proposal(proposed_filename="Track1.mp3", proposed_path="music/Album"),
            _mock_proposal(proposed_filename="Track2.mp3", proposed_path="music/Album"),
        ]
        root = build_tree(proposals)
        album = root.children["music"].children["Album"]
        assert len(album.files) == 2
        assert "Track1.mp3" in album.files
        assert "Track2.mp3" in album.files
