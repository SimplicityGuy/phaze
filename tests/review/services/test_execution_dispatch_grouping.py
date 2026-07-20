"""Unit tests for src/phaze/services/execution_dispatch.py (Phase 28 D-09 steps 1-3).

Three exports under test:

- ``get_approved_proposals_grouped_by_agent(session)`` — SELECT approved proposals
  JOIN FileRecord JOIN Agent, GROUP BY ``FileRecord.agent_id``, EXCLUDING any
  proposal whose Agent has ``revoked_at IS NOT NULL`` (D-09 step 2).
- ``count_revoked_skipped_proposals(session)`` — companion counter that returns the
  number of approved proposals filtered out by the revoked-agent predicate; the
  controller renders this into the banner copy.
- ``chunk_proposals(items, size=500)`` — pure list-slicing helper that splits a
  per-agent group into sub-lists of length ``<= size`` (D-09 step 3).

Test IDs satisfied:

- 28-V-01 — :func:`test_groups_by_agent_id`
- 28-V-02 — :func:`test_revoked_agent_filtered_with_count`
- 28-V-03 — :func:`test_1000_proposals_split_into_2_chunks`

Tests use the real PostgreSQL ``session`` fixture from ``tests/conftest.py``;
seeding helpers below construct ``Agent`` + ``FileRecord`` + ``RenameProposal``
rows directly via the ORM. The conftest pre-seeds the LEGACY agent, so test
agents use distinct kebab-case slugs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.schemas.agent_tasks import ExecuteBatchProposalItem
from phaze.services.execution_dispatch import (
    chunk_proposals,
    count_revoked_skipped_proposals,
    get_approved_proposals_grouped_by_agent,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_agent(
    session: AsyncSession,
    *,
    agent_id: str,
    revoked: bool = False,
) -> Agent:
    """Insert a kebab-case test agent. ``revoked=True`` sets ``revoked_at`` to now."""
    agent = Agent(
        id=agent_id,
        name=agent_id,
        token_hash=None,
        scan_roots=[],
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _seed_proposal(
    session: AsyncSession,
    *,
    agent_id: str,
    path_suffix: str,
    status: str = ProposalStatus.APPROVED,
    sha256: str | None = None,
    proposal_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> RenameProposal:
    """Insert a (FileRecord, RenameProposal) pair owned by ``agent_id``.

    ``path_suffix`` must be unique within a test to avoid the
    ``uq_files_agent_id_original_path`` partial-UQ collision.

    ``proposal_id`` / ``created_at`` are injectable so a test can force the
    exact primary key and timestamp instead of relying on ``uuid4()`` and the
    ``server_default`` clock -- the tiebreaker regression test needs an
    IDENTICAL ``created_at`` across rows, set explicitly rather than hoped for.
    """
    file_id = uuid.uuid4()
    fr = FileRecord(
        id=file_id,
        sha256_hash=sha256 if sha256 is not None else (uuid.uuid4().hex + uuid.uuid4().hex),
        original_path=f"/music/{agent_id}/{path_suffix}.mp3",
        original_filename=f"{path_suffix}.mp3",
        current_path=f"/music/{agent_id}/{path_suffix}.mp3",
        file_type="music",
        file_size=1_000_000,
        agent_id=agent_id,
    )
    session.add(fr)
    await session.flush()

    prop = RenameProposal(
        id=proposal_id if proposal_id is not None else uuid.uuid4(),
        file_id=file_id,
        proposed_filename=f"{path_suffix}-renamed.mp3",
        proposed_path=f"organized/{agent_id}",
        status=status,
        confidence=0.9,
    )
    if created_at is not None:
        prop.created_at = created_at
    session.add(prop)
    await session.commit()
    await session.refresh(prop)
    return prop


# ---------------------------------------------------------------------------
# get_approved_proposals_grouped_by_agent + count_revoked_skipped_proposals
# ---------------------------------------------------------------------------


async def test_empty_input_returns_empty_dict_and_zero_skipped(session: AsyncSession) -> None:
    """No approved proposals seeded → groups == {} and skipped == 0."""
    groups = await get_approved_proposals_grouped_by_agent(session)
    skipped = await count_revoked_skipped_proposals(session)
    assert groups == {}
    assert skipped == 0


async def test_groups_by_agent_id(session: AsyncSession) -> None:
    """28-V-01: 3 approved proposals on agent A, 2 on agent B → grouped dict.

    Asserts the per-agent partition is correct AND the values are
    ``ExecuteBatchProposalItem`` instances carrying the schema-required fields.
    """
    await _seed_agent(session, agent_id="agent-aaa")
    await _seed_agent(session, agent_id="agent-bbb")
    for i in range(3):
        await _seed_proposal(session, agent_id="agent-aaa", path_suffix=f"a-{i}")
    for i in range(2):
        await _seed_proposal(session, agent_id="agent-bbb", path_suffix=f"b-{i}")

    groups = await get_approved_proposals_grouped_by_agent(session)

    assert set(groups.keys()) == {"agent-aaa", "agent-bbb"}
    assert len(groups["agent-aaa"]) == 3
    assert len(groups["agent-bbb"]) == 2
    # Every value is an ExecuteBatchProposalItem with all required fields.
    for items in groups.values():
        for item in items:
            assert isinstance(item, ExecuteBatchProposalItem)
            assert isinstance(item.proposal_id, uuid.UUID)
            assert isinstance(item.file_id, uuid.UUID)
            assert item.original_path.startswith("/music/")
            assert item.proposed_path.startswith("organized/")
            # proposed_filename is carried on the wire (bug fix: the executor
            # needs it to build the real destination, not just the directory).
            assert item.proposed_filename.endswith("-renamed.mp3")


async def test_revoked_agent_filtered_with_count(session: AsyncSession) -> None:
    """28-V-02: revoked agent A's 3 proposals excluded; active agent B's 2 returned.

    ``count_revoked_skipped_proposals`` returns 3.
    """
    await _seed_agent(session, agent_id="agent-revoked", revoked=True)
    await _seed_agent(session, agent_id="agent-active")
    for i in range(3):
        await _seed_proposal(session, agent_id="agent-revoked", path_suffix=f"r-{i}")
    for i in range(2):
        await _seed_proposal(session, agent_id="agent-active", path_suffix=f"a-{i}")

    groups = await get_approved_proposals_grouped_by_agent(session)
    skipped = await count_revoked_skipped_proposals(session)

    assert set(groups.keys()) == {"agent-active"}
    assert len(groups["agent-active"]) == 2
    assert skipped == 3


async def test_non_approved_proposals_excluded(session: AsyncSession) -> None:
    """PENDING / REJECTED / EXECUTED / FAILED proposals are never returned."""
    await _seed_agent(session, agent_id="agent-mix")
    await _seed_proposal(session, agent_id="agent-mix", path_suffix="p1", status=ProposalStatus.PENDING)
    await _seed_proposal(session, agent_id="agent-mix", path_suffix="p2", status=ProposalStatus.REJECTED)
    await _seed_proposal(session, agent_id="agent-mix", path_suffix="p3", status=ProposalStatus.EXECUTED)
    await _seed_proposal(session, agent_id="agent-mix", path_suffix="p4", status=ProposalStatus.FAILED)
    await _seed_proposal(session, agent_id="agent-mix", path_suffix="p5", status=ProposalStatus.APPROVED)

    groups = await get_approved_proposals_grouped_by_agent(session)
    assert set(groups.keys()) == {"agent-mix"}
    assert len(groups["agent-mix"]) == 1


async def test_sha256_hash_populated_from_file_record(session: AsyncSession) -> None:
    """RESEARCH L1: always-populate sha256_hash from FileRecord.sha256_hash."""
    await _seed_agent(session, agent_id="agent-sha")
    known_hash = "a" * 64
    await _seed_proposal(session, agent_id="agent-sha", path_suffix="only", sha256=known_hash)

    groups = await get_approved_proposals_grouped_by_agent(session)
    assert groups["agent-sha"][0].sha256_hash == known_hash


async def test_tiebreaker_orders_tied_created_at_by_proposal_id(session: AsyncSession) -> None:
    """Rows with an IDENTICAL created_at come back ordered by RenameProposal.id ASC.

    This proves the ORDER BY's stable ``RenameProposal.id`` tiebreaker, NOT the
    wall clock. Every row is seeded with the SAME explicit ``created_at`` so
    ``created_at`` alone leaves the block tied; only the primary-key suffix makes
    the order total. Insertion order is deliberately scrambled relative to id
    order so a query that fell back to heap/insertion order would produce a
    DIFFERENT sequence and fail.

    Regression guard for phaze-lrwz: reverting the ``, RenameProposal.id`` suffix
    on execution_dispatch.get_approved_proposals_grouped_by_agent makes the
    returned order depend on Postgres heap order and this assertion becomes
    nondeterministic (verified: it fails without the tiebreaker).

    Postgres orders ``uuid`` by byte comparison, which matches Python's
    ``uuid.UUID`` ordering (both compare the 128-bit big-endian value), so the
    expected order is simply the seeded ids sorted ascending.
    """
    await _seed_agent(session, agent_id="agent-tie")

    # One shared timestamp -> created_at ties for every row.
    tied_at = datetime(2026, 7, 20, 12, 0, 0)
    # Fixed, distinct ids. Insertion order (below) is intentionally NOT id order.
    proposal_ids = [uuid.UUID(f"00000000-0000-0000-0000-0000000000{n:02d}") for n in (30, 10, 50, 20, 40)]

    for i, pid in enumerate(proposal_ids):
        await _seed_proposal(
            session,
            agent_id="agent-tie",
            path_suffix=f"tie-{i:02d}",
            proposal_id=pid,
            created_at=tied_at,
        )

    groups = await get_approved_proposals_grouped_by_agent(session)
    actual_ids = [item.proposal_id for item in groups["agent-tie"]]

    # Total order comes from the id tiebreaker: ascending id, regardless of
    # insertion order or heap layout.
    assert actual_ids == sorted(proposal_ids)


async def test_ordering_within_agent_group_by_created_at_then_id(session: AsyncSession) -> None:
    """Distinct created_at values dominate; the id tiebreaker only breaks exact ties.

    Seeds explicit, strictly-increasing ``created_at`` values (not clock-derived)
    with ids in the OPPOSITE order, and asserts the result follows created_at ASC
    -- confirming the primary sort key still wins when timestamps differ.
    """
    await _seed_agent(session, agent_id="agent-order")
    base = datetime(2026, 7, 20, 9, 0, 0)
    # created_at increases with i; id decreases with i -> the two keys disagree.
    for i in range(5):
        await _seed_proposal(
            session,
            agent_id="agent-order",
            path_suffix=f"order-{i:02d}",
            proposal_id=uuid.UUID(f"00000000-0000-0000-0000-0000000000{(90 - i * 10):02d}"),
            created_at=base + timedelta(seconds=i),
        )

    groups = await get_approved_proposals_grouped_by_agent(session)
    actual = [item.original_path.rsplit("/", 1)[-1] for item in groups["agent-order"]]
    assert actual == [f"order-{i:02d}.mp3" for i in range(5)]


# ---------------------------------------------------------------------------
# chunk_proposals (pure / synchronous)
# ---------------------------------------------------------------------------


def _make_items(n: int) -> list[ExecuteBatchProposalItem]:
    """Synthetic items for chunk math tests (no DB)."""
    return [
        ExecuteBatchProposalItem(
            proposal_id=uuid.uuid4(),
            file_id=uuid.uuid4(),
            original_path=f"/x/{i}.mp3",
            proposed_path="y",
            proposed_filename=f"{i}.mp3",
            sha256_hash="b" * 64,
        )
        for i in range(n)
    ]


def test_chunk_empty_list_returns_empty_list() -> None:
    assert chunk_proposals([], 500) == []


def test_chunk_smaller_than_size_returns_single_chunk() -> None:
    items = _make_items(7)
    result = chunk_proposals(items, 500)
    assert len(result) == 1
    assert len(result[0]) == 7


def test_chunks_at_500() -> None:
    """1000 items, size=500 → 2 chunks of exactly 500."""
    items = _make_items(1000)
    result = chunk_proposals(items, 500)
    assert len(result) == 2
    assert len(result[0]) == 500
    assert len(result[1]) == 500


def test_chunk_off_by_one_above_size() -> None:
    """501 items, size=500 → first chunk full, second chunk of length 1."""
    items = _make_items(501)
    result = chunk_proposals(items, 500)
    assert len(result) == 2
    assert len(result[0]) == 500
    assert len(result[1]) == 1


def test_chunk_at_size_returns_single_chunk() -> None:
    """Exactly 500 items, size=500 → single chunk of 500."""
    items = _make_items(500)
    result = chunk_proposals(items, 500)
    assert len(result) == 1
    assert len(result[0]) == 500


@pytest.mark.parametrize(
    ("n", "expected_chunks"),
    [
        (0, 0),
        (1, 1),
        (499, 1),
        (500, 1),
        (501, 2),
        (999, 2),
        (1000, 2),
        (1500, 3),
    ],
)
def test_chunk_count_matches_ceil_n_over_500(n: int, expected_chunks: int) -> None:
    """Verification math: chunk count == ceil(n / 500)."""
    items = _make_items(n)
    result = chunk_proposals(items, 500)
    assert len(result) == expected_chunks
    if n > 0:
        # Every chunk except possibly the last is exactly the chunk size.
        for c in result[:-1]:
            assert len(c) == 500
        assert 1 <= len(result[-1]) <= 500


# ---------------------------------------------------------------------------
# Integration: grouping + chunking together (28-V-03)
# ---------------------------------------------------------------------------


async def test_1000_proposals_split_into_2_chunks(session: AsyncSession) -> None:
    """28-V-03: 1000 approved proposals on one agent.

    Grouped helper returns a single agent key with 1000 items;
    feeding that list into ``chunk_proposals`` yields 2 chunks of 500.
    """
    await _seed_agent(session, agent_id="agent-big")
    for i in range(1000):
        await _seed_proposal(session, agent_id="agent-big", path_suffix=f"big-{i:04d}")

    groups = await get_approved_proposals_grouped_by_agent(session)
    assert set(groups.keys()) == {"agent-big"}
    assert len(groups["agent-big"]) == 1000

    chunks = chunk_proposals(groups["agent-big"], 500)
    assert len(chunks) == 2
    assert len(chunks[0]) == 500
    assert len(chunks[1]) == 500
