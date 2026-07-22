"""Tests for collision detection service and directory tree builder."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock
import uuid

import pytest

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.collision import build_tree


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ensure_agent(session: AsyncSession, agent_id: str, scan_roots: list[str]) -> None:
    """Create an agent with the given scan_roots if it does not already exist."""
    if await session.get(Agent, agent_id) is None:
        session.add(Agent(id=agent_id, name=agent_id, kind="fileserver", scan_roots=scan_roots))
        await session.flush()


async def _create_proposal(
    session: AsyncSession,
    *,
    proposed_filename: str = "Artist - Track.mp3",
    proposed_path: str | None = None,
    status: str = ProposalStatus.APPROVED,
    original_filename: str = "test.mp3",
    original_dir: str | None = None,
    agent_id: str = "test-fileserver",
) -> RenameProposal:
    """Create a FileRecord + RenameProposal pair for testing.

    ``original_dir`` pins the file's source directory (used to reproduce two
    in-place renames sharing a directory); when omitted each file gets a unique
    directory so it never collides with another by accident. ``agent_id`` selects
    the owning agent (whose ``scan_roots`` drive the owning-root normalization).
    """
    file_id = uuid.uuid4()
    source_dir = original_dir if original_dir is not None else f"/music/{uuid.uuid4().hex}"
    file_record = FileRecord(
        agent_id=agent_id,
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"{source_dir}/{original_filename}",
        original_filename=original_filename,
        current_path=f"/music/{original_filename}",
        file_type="music",
        file_size=1_000_000,
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
    async def test_null_path_different_dirs_no_collision(self, session: AsyncSession) -> None:
        from phaze.services.collision import detect_collisions

        # Two in-place renames (null path) with the same filename but DIFFERENT source
        # directories resolve to different destinations -> no collision.
        await _create_proposal(session, proposed_filename="Track.mp3", proposed_path=None, original_dir="/music/setA")
        await _create_proposal(session, proposed_filename="Track.mp3", proposed_path=None, original_filename="other.mp3", original_dir="/music/setB")

        result = await detect_collisions(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_null_path_same_dir_collides(self, session: AsyncSession) -> None:
        """Two in-place renames in the SAME directory targeting the same filename collide (phaze-7czn)."""
        from phaze.services.collision import detect_collisions

        await _create_proposal(session, proposed_filename="Artist - Track.mp3", proposed_path=None, original_dir="/music/coachella")
        await _create_proposal(
            session,
            proposed_filename="Artist - Track.mp3",
            proposed_path=None,
            original_filename="set (1).mp3",
            original_dir="/music/coachella",
        )

        result = await detect_collisions(session)
        assert len(result) == 1
        assert result[0][0] == "/music/coachella/Artist - Track.mp3"
        assert result[0][1] == 2

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

    @pytest.mark.asyncio
    async def test_returns_null_path_collision_proposal_ids(self, session: AsyncSession) -> None:
        """In-place (null-path) collisions also surface their proposal ids for the badge (phaze-7czn)."""
        from phaze.services.collision import get_collision_ids

        p1 = await _create_proposal(session, proposed_filename="Canonical.mp3", proposed_path=None, original_dir="/music/edc")
        p2 = await _create_proposal(
            session, proposed_filename="Canonical.mp3", proposed_path=None, original_filename="dupe.mp3", original_dir="/music/edc"
        )

        ids = await get_collision_ids(session)
        assert str(p1.id) in ids
        assert str(p2.id) in ids


# ---------------------------------------------------------------------------
# phaze-dqx8 — the collision key must identify a REAL destination: keyed per
# agent + owning scan_root, with the in-place arm normalized into the same
# root-relative namespace as proposed_path. Catches cross-form collisions;
# never invents cross-agent / cross-scan-root phantoms.
# ---------------------------------------------------------------------------


class TestCollisionKeyNormalization:
    """phaze-dqx8: like-with-like collision keying per agent/scan-root."""

    @pytest.mark.asyncio
    async def test_inplace_and_path_proposal_to_same_dest_collide(self, session: AsyncSession) -> None:
        """A rename-in-place and a path proposal resolving to the SAME on-disk file now collide.

        P1: in-place rename of ``/data/music/X/a.mp3`` -> ``b.mp3`` (dest
        ``/data/music/X/b.mp3``). P2: path proposal for ``/data/music/Y/c.mp3``
        with proposed_path ``X`` and filename ``b.mp3`` (dest
        ``/data/music/X/b.mp3``). Pre-fix these keyed as ``/data/music/X/b.mp3``
        vs ``X/b.mp3`` and never matched -- the missed collision.
        """
        from phaze.services.collision import detect_collisions

        await _ensure_agent(session, "srv-norm", ["/data/music"])
        await _create_proposal(
            session,
            proposed_filename="b.mp3",
            proposed_path=None,  # in-place rename
            original_filename="a.mp3",
            original_dir="/data/music/X",
            agent_id="srv-norm",
        )
        await _create_proposal(
            session,
            proposed_filename="b.mp3",
            proposed_path="X",  # path proposal into the same dir
            original_filename="c.mp3",
            original_dir="/data/music/Y",
            agent_id="srv-norm",
        )

        result = await detect_collisions(session)
        assert len(result) == 1
        assert result[0][1] == 2
        assert result[0][0] == "X/b.mp3"

    @pytest.mark.asyncio
    async def test_same_relative_path_on_different_agents_is_not_a_phantom_collision(self, session: AsyncSession) -> None:
        """Two agents each targeting the same RELATIVE dest are unrelated -> no phantom collision."""
        from phaze.services.collision import detect_collisions

        await _ensure_agent(session, "srv-a", ["/data/music"])
        await _ensure_agent(session, "srv-b", ["/data/music"])
        await _create_proposal(
            session,
            proposed_filename="set.mp3",
            proposed_path="Coachella 2024",
            original_filename="f.mp3",
            original_dir="/data/music/A",
            agent_id="srv-a",
        )
        await _create_proposal(
            session,
            proposed_filename="set.mp3",
            proposed_path="Coachella 2024",
            original_filename="g.mp3",
            original_dir="/data/music/B",
            agent_id="srv-b",
        )

        result = await detect_collisions(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_same_relative_path_under_different_scan_roots_of_one_agent_is_not_a_collision(self, session: AsyncSession) -> None:
        """One agent with two scan_roots: the same relative dest under each root is NOT a collision."""
        from phaze.services.collision import detect_collisions

        await _ensure_agent(session, "srv-multi", ["/data/music", "/mnt/archive"])
        await _create_proposal(
            session,
            proposed_filename="set.mp3",
            proposed_path="Coachella 2024",
            original_filename="f.mp3",
            original_dir="/data/music/A",
            agent_id="srv-multi",
        )
        await _create_proposal(
            session,
            proposed_filename="set.mp3",
            proposed_path="Coachella 2024",
            original_filename="g.mp3",
            original_dir="/mnt/archive/B",
            agent_id="srv-multi",
        )

        result = await detect_collisions(session)
        assert result == []

    @pytest.mark.asyncio
    async def test_same_agent_same_scan_root_same_relative_path_collides(self, session: AsyncSession) -> None:
        """Positive control: same agent + scan_root + relative dest DOES collide."""
        from phaze.services.collision import detect_collisions

        await _ensure_agent(session, "srv-one", ["/data/music"])
        await _create_proposal(
            session,
            proposed_filename="set.mp3",
            proposed_path="Coachella 2024",
            original_filename="f.mp3",
            original_dir="/data/music/A",
            agent_id="srv-one",
        )
        await _create_proposal(
            session,
            proposed_filename="set.mp3",
            proposed_path="Coachella 2024",
            original_filename="g.mp3",
            original_dir="/data/music/B",
            agent_id="srv-one",
        )

        result = await detect_collisions(session)
        assert len(result) == 1
        assert result[0][1] == 2
        assert result[0][0] == "Coachella 2024/set.mp3"

    @pytest.mark.asyncio
    async def test_cross_form_collision_ids_flag_both_proposals(self, session: AsyncSession) -> None:
        """get_collision_ids flags BOTH the in-place and the path proposal of a cross-form collision."""
        from phaze.services.collision import get_collision_ids

        await _ensure_agent(session, "srv-ids", ["/data/music"])
        p1 = await _create_proposal(
            session,
            proposed_filename="b.mp3",
            proposed_path=None,
            original_filename="a.mp3",
            original_dir="/data/music/X",
            agent_id="srv-ids",
        )
        p2 = await _create_proposal(
            session,
            proposed_filename="b.mp3",
            proposed_path="X",
            original_filename="c.mp3",
            original_dir="/data/music/Y",
            agent_id="srv-ids",
        )

        ids = await get_collision_ids(session)
        assert str(p1.id) in ids
        assert str(p2.id) in ids

    @pytest.mark.asyncio
    async def test_phantom_cross_agent_ids_are_not_flagged(self, session: AsyncSession) -> None:
        """get_collision_ids must NOT badge unrelated cross-agent proposals sharing a relative dest."""
        from phaze.services.collision import get_collision_ids

        await _ensure_agent(session, "srv-x", ["/data/music"])
        await _ensure_agent(session, "srv-y", ["/data/music"])
        await _create_proposal(
            session,
            proposed_filename="set.mp3",
            proposed_path="Coachella 2024",
            original_filename="f.mp3",
            original_dir="/data/music/A",
            agent_id="srv-x",
        )
        await _create_proposal(
            session,
            proposed_filename="set.mp3",
            proposed_path="Coachella 2024",
            original_filename="g.mp3",
            original_dir="/data/music/B",
            agent_id="srv-y",
        )

        ids = await get_collision_ids(session)
        assert ids == set()


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
