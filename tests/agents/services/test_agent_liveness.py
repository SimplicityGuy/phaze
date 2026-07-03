"""Tests for phaze.services.agent_liveness — pure-function classifier + sort_key.

Phase 29 D-12 LOCKED thresholds:
    - alive: now - last_seen_at < 90s (AGENT_LIVENESS_ALIVE_SECONDS)
    - stale: 90s <= delta < 300s (AGENT_LIVENESS_STALE_SECONDS)
    - dead: delta >= 300s
    - revoked: revoked_at IS NOT NULL (precedence over all last_seen_at math)
    - never: revoked_at IS NULL AND last_seen_at IS NULL

UI-SPEC §Status Pill Component sort order:
    revoked agents last; within non-revoked: status_rank ascending
    (alive→stale→dead→never); within same status: last_seen_at descending.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import SQLAlchemyError

from phaze.models.agent import Agent
from phaze.services.agent_liveness import AgentStatus, classify, classify_compute_lanes, sort_key


NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)


def _make_agent(
    agent_id: str,
    *,
    last_seen_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> Agent:
    return Agent(
        id=agent_id,
        name=agent_id,
        scan_roots=[],
        last_seen_at=last_seen_at,
        revoked_at=revoked_at,
    )


# ---------------------------------------------------------------------------
# classify(agent, now) — 5-state matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta_seconds", "expected"),
    [
        (0, "alive"),
        (1, "alive"),
        (60, "alive"),
        (89, "alive"),
        # 90s boundary: alive < 90, stale >= 90
        (90, "stale"),
        (120, "stale"),
        (200, "stale"),
        (299, "stale"),
        # 300s boundary: stale < 300, dead >= 300
        (300, "dead"),
        (600, "dead"),
        (86400, "dead"),
    ],
)
def test_classify_thresholds(delta_seconds: int, expected: str) -> None:
    """5-state thresholds at all boundary cases (D-12)."""
    agent = _make_agent("test", last_seen_at=NOW - timedelta(seconds=delta_seconds))
    assert classify(agent, NOW) == expected


def test_classify_never_when_last_seen_at_is_none() -> None:
    """Agent registered but never heartbeated → 'never' (revoked_at also NULL)."""
    agent = _make_agent("never-agent")
    assert classify(agent, NOW) == "never"


@pytest.mark.parametrize(
    "elapsed",
    [
        timedelta(0),
        timedelta(seconds=1),
        timedelta(seconds=300),
        timedelta(hours=1),
        timedelta(days=365),
        timedelta(days=36500),  # far-future now: a century later
    ],
)
def test_classify_never_not_dead_when_last_seen_at_none(elapsed: timedelta) -> None:
    """D-07 structural DEAD-suppression invariant: an agent that never heartbeated
    (``last_seen_at IS NULL``) classifies as 'never', NEVER 'dead' — at ANY ``now``,
    including a now far in the future. The 'never' branch precedes the threshold
    math in ``classify`` (agent_liveness.py:79-80), so no elapsed time can ever
    promote a no-signal agent to 'dead'. This is the executable proof that the k8s
    burst lane (a non-heartbeating bearer-token Agent row) can never render a
    perpetually-DEAD pill.
    """
    agent = _make_agent("k8s-burst-lane")  # last_seen_at is None by default
    now = NOW + elapsed
    status = classify(agent, now)
    assert status != "dead"
    assert status == "never"


def test_classify_revoked_takes_precedence_over_alive() -> None:
    """Revoked agent with recent last_seen_at still classifies as 'revoked'."""
    agent = _make_agent(
        "revoked-agent",
        last_seen_at=NOW,
        revoked_at=NOW - timedelta(seconds=10),
    )
    assert classify(agent, NOW) == "revoked"


def test_classify_revoked_takes_precedence_over_never() -> None:
    """Revoked + never-heartbeated agent classifies as 'revoked' (precedence)."""
    agent = _make_agent(
        "revoked-never",
        last_seen_at=None,
        revoked_at=NOW,
    )
    assert classify(agent, NOW) == "revoked"


def test_classify_returns_literal_type() -> None:
    """classify return is one of the 5 AgentStatus literal members."""
    agent = _make_agent("test", last_seen_at=NOW)
    result: AgentStatus = classify(agent, NOW)
    assert result in {"alive", "stale", "dead", "revoked", "never"}


# ---------------------------------------------------------------------------
# sort_key(agent, now) — ordering invariants
# ---------------------------------------------------------------------------


def test_sort_key_revoked_last() -> None:
    """Revoked agents sort AFTER every non-revoked agent regardless of last_seen."""
    alive = _make_agent("alive", last_seen_at=NOW)
    revoked = _make_agent("revoked", last_seen_at=NOW, revoked_at=NOW)
    assert sort_key(alive, NOW) < sort_key(revoked, NOW)


def test_sort_key_status_rank_alive_before_stale_before_dead() -> None:
    """Non-revoked agents sort by status: alive < stale < dead < never."""
    alive = _make_agent("alive", last_seen_at=NOW)
    stale = _make_agent("stale", last_seen_at=NOW - timedelta(seconds=150))
    dead = _make_agent("dead", last_seen_at=NOW - timedelta(seconds=600))
    never = _make_agent("never")
    assert sort_key(alive, NOW) < sort_key(stale, NOW)
    assert sort_key(stale, NOW) < sort_key(dead, NOW)
    assert sort_key(dead, NOW) < sort_key(never, NOW)


def test_sort_key_within_same_status_last_seen_descending() -> None:
    """Within the same status bucket, more-recently-seen agents sort first."""
    recent = _make_agent("recent", last_seen_at=NOW - timedelta(seconds=10))
    older = _make_agent("older", last_seen_at=NOW - timedelta(seconds=60))
    # Both are alive (<90s); recent should come BEFORE older.
    assert sort_key(recent, NOW) < sort_key(older, NOW)


def test_sort_key_full_sort_order() -> None:
    """End-to-end: sort a mixed list and assert expected order."""
    alive_recent = _make_agent("alive-recent", last_seen_at=NOW)
    alive_older = _make_agent("alive-older", last_seen_at=NOW - timedelta(seconds=30))
    stale = _make_agent("stale", last_seen_at=NOW - timedelta(seconds=120))
    dead = _make_agent("dead", last_seen_at=NOW - timedelta(seconds=600))
    never = _make_agent("never")
    revoked_recent = _make_agent("revoked-recent", last_seen_at=NOW, revoked_at=NOW)
    revoked_old = _make_agent(
        "revoked-old",
        last_seen_at=NOW - timedelta(seconds=600),
        revoked_at=NOW,
    )

    unsorted = [revoked_old, dead, alive_older, stale, revoked_recent, alive_recent, never]
    sorted_agents = sorted(unsorted, key=lambda a: sort_key(a, NOW))
    sorted_ids = [a.id for a in sorted_agents]
    # alive_recent (alive, most recent) → alive_older → stale → dead → never → revoked_recent → revoked_old
    assert sorted_ids == [
        "alive-recent",
        "alive-older",
        "stale",
        "dead",
        "never",
        "revoked-recent",
        "revoked-old",
    ]


def test_sort_key_never_after_dead_within_non_revoked() -> None:
    """'never' has same rank as 'revoked' (3) but lives in the non-revoked group."""
    dead = _make_agent("dead", last_seen_at=NOW - timedelta(seconds=600))
    never = _make_agent("never")
    assert sort_key(dead, NOW) < sort_key(never, NOW)


# ---------------------------------------------------------------------------
# classify_compute_lanes(session) — degrade branch (optional margin, D-05/D-07)
# ---------------------------------------------------------------------------


class _RaisingSession:
    """Stub session whose ``execute`` raises ``SQLAlchemyError`` and whose ``rollback`` is a no-op.

    Drives ``classify_compute_lanes`` straight into its ``except SQLAlchemyError`` degrade branch
    (agent_liveness.py:174-180), which rolls back and returns ``("IDLE", 0)`` — a DB hiccup must
    NEVER paint the compute lane DEAD/red.
    """

    async def execute(self, *_args: object, **_kwargs: object) -> object:
        raise SQLAlchemyError("db down")

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_classify_compute_lanes_degrades_to_idle_on_db_error() -> None:
    """SQLAlchemyError → rollback → observable ``("IDLE", 0)`` return (D-07 observable outcome)."""
    result = await classify_compute_lanes(_RaisingSession())  # type: ignore[arg-type]
    assert result == ("IDLE", 0)
