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
from types import SimpleNamespace
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy.exc import SQLAlchemyError

from phaze.models.agent import Agent
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import agent_liveness as liveness_mod
from phaze.services.agent_liveness import (
    AgentStatus,
    ComputeLane,
    classify,
    classify_compute_lanes,
    derive_compute_lane_identities,
    non_local_backend_kinds,
    sort_key,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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


# ---------------------------------------------------------------------------
# non_local_backend_kinds(settings) — pure registry projection (COMPUTE-01)
# ---------------------------------------------------------------------------


def _backend(backend_id: str, kind: str) -> SimpleNamespace:
    """A minimal registry-entry stand-in — non_local_backend_kinds reads only ``.id`` / ``.kind``."""
    return SimpleNamespace(id=backend_id, kind=kind)


def _settings(*backends: SimpleNamespace) -> SimpleNamespace:
    """A minimal settings stand-in carrying only ``.backends`` (the sole attribute the derivation reads)."""
    return SimpleNamespace(backends=list(backends))


def test_non_local_backend_kinds_filters_local_preserving_order() -> None:
    """Only non-local entries survive, keyed by id → kind, in registry (insertion) order."""
    settings = _settings(
        _backend("local", "local"),
        _backend("k8s-a", "kueue"),
        _backend("k8s-b", "kueue"),
        _backend("a1", "compute"),
    )
    result = non_local_backend_kinds(settings)  # type: ignore[arg-type]
    assert result == {"k8s-a": "kueue", "k8s-b": "kueue", "a1": "compute"}
    assert list(result) == ["k8s-a", "k8s-b", "a1"]  # local excluded, registry order preserved


def test_non_local_backend_kinds_all_local_is_empty() -> None:
    """An all-local registry projects to an empty map (no cloud lanes)."""
    assert non_local_backend_kinds(_settings(_backend("local", "local"))) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# derive_compute_lane_identities(session) — per-cluster identity derivation (COMPUTE-01)
# ---------------------------------------------------------------------------


def _file(i: int) -> FileRecord:
    """Build a minimal FileRecord seed (CloudJob.file_id is a unique FK to files.id)."""
    return FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=f"c{i:063d}"[:64],
        original_path=f"/music/lane{i}.mp3",
        original_filename=f"lane{i}.mp3",
        current_path=f"/music/lane{i}.mp3",
        file_type="mp3",
        file_size=1000,
    )


def _cloud_job(
    file_id: uuid.UUID,
    *,
    backend_id: str | None,
    status: str = CloudJobStatus.SUBMITTED.value,
    inadmissible: bool = False,
) -> CloudJob:
    """Build a CloudJob seed attributed to ``backend_id`` with the given liveness attributes."""
    return CloudJob(
        id=uuid.uuid4(),
        file_id=file_id,
        s3_key=f"staging/{file_id}",
        status=status,
        backend_id=backend_id,
        inadmissible=inadmissible,
    )


async def _seed(session: AsyncSession, *jobs_for: tuple[str | None, str, bool]) -> None:
    """Seed one FileRecord + CloudJob per ``(backend_id, status, inadmissible)`` triple, then commit."""
    files = [_file(i) for i in range(len(jobs_for))]
    session.add_all(files)
    await session.flush()
    session.add_all(
        [
            _cloud_job(files[i].id, backend_id=backend_id, status=status, inadmissible=inadmissible)
            for i, (backend_id, status, inadmissible) in enumerate(jobs_for)
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_derive_two_kueue_registry_both_running(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2-Kueue registry with a RUNNING row on each cluster → both lanes ACTIVE with per-cluster counts."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue"), _backend("k8s-b", "kueue")))
    await _seed(
        session,
        ("k8s-a", CloudJobStatus.RUNNING.value, False),
        ("k8s-a", CloudJobStatus.RUNNING.value, False),
        ("k8s-b", CloudJobStatus.RUNNING.value, False),
    )

    lanes = await derive_compute_lane_identities(session)

    assert lanes == [
        ComputeLane(backend_id="k8s-a", kind="kueue", state="ACTIVE", running=2, waiting=0),
        ComputeLane(backend_id="k8s-b", kind="kueue", state="ACTIVE", running=1, waiting=0),
    ]


@pytest.mark.asyncio
async def test_derive_idle_configured_cluster_still_listed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured cluster with NO in-flight rows still appears as an IDLE lane (registry-composed, no probe)."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue"), _backend("k8s-idle", "kueue")))
    await _seed(session, ("k8s-a", CloudJobStatus.RUNNING.value, False))

    lanes = await derive_compute_lane_identities(session)

    assert lanes == [
        ComputeLane(backend_id="k8s-a", kind="kueue", state="ACTIVE", running=1, waiting=0),
        ComputeLane(backend_id="k8s-idle", kind="kueue", state="IDLE", running=0, waiting=0),
    ]


@pytest.mark.asyncio
async def test_derive_waiting_via_submitted_inadmissible(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SUBMITTED+inadmissible row (and no running) → WAITING; a plain SUBMITTED row does NOT count as waiting."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))
    await _seed(
        session,
        ("k8s-a", CloudJobStatus.SUBMITTED.value, True),
        ("k8s-a", CloudJobStatus.SUBMITTED.value, False),  # admissible submitted -> not waiting, not running
    )

    lanes = await derive_compute_lane_identities(session)

    assert lanes == [ComputeLane(backend_id="k8s-a", kind="kueue", state="WAITING", running=0, waiting=1)]


@pytest.mark.asyncio
async def test_derive_running_takes_precedence_over_waiting(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lane with BOTH a running and a waiting row is ACTIVE (running≥1 dominates)."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))
    await _seed(
        session,
        ("k8s-a", CloudJobStatus.RUNNING.value, False),
        ("k8s-a", CloudJobStatus.SUBMITTED.value, True),
    )

    lanes = await derive_compute_lane_identities(session)

    assert lanes == [ComputeLane(backend_id="k8s-a", kind="kueue", state="ACTIVE", running=1, waiting=1)]


@pytest.mark.asyncio
async def test_derive_unattributed_lane_only_when_null_rows_in_flight(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A NULL-backend_id in-flight row appends ONE trailing 'unattributed'/'cloud' lane after the registry lanes."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))
    await _seed(
        session,
        ("k8s-a", CloudJobStatus.RUNNING.value, False),
        (None, CloudJobStatus.RUNNING.value, False),
        (None, CloudJobStatus.SUBMITTED.value, True),
    )

    lanes = await derive_compute_lane_identities(session)

    assert lanes == [
        ComputeLane(backend_id="k8s-a", kind="kueue", state="ACTIVE", running=1, waiting=0),
        ComputeLane(backend_id="unattributed", kind="cloud", state="ACTIVE", running=1, waiting=1),
    ]


@pytest.mark.asyncio
async def test_derive_no_unattributed_lane_when_no_null_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """No NULL-backend in-flight rows → NO trailing unattributed lane (never a phantom 'unattributed'/'a1')."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))
    await _seed(session, ("k8s-a", CloudJobStatus.RUNNING.value, False))

    lanes = await derive_compute_lane_identities(session)

    assert [lane.backend_id for lane in lanes] == ["k8s-a"]  # no 'unattributed' lane


@pytest.mark.asyncio
async def test_derive_degrades_to_registry_all_idle_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB error rolls back and returns the registry lanes ALL-IDLE — never raises, never DEAD (KDEPLOY-04)."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue"), _backend("a1", "compute")))

    lanes = await derive_compute_lane_identities(_RaisingSession())  # type: ignore[arg-type]

    assert lanes == [
        ComputeLane(backend_id="k8s-a", kind="kueue", state="IDLE", running=0, waiting=0),
        ComputeLane(backend_id="a1", kind="compute", state="IDLE", running=0, waiting=0),
    ]


@pytest.mark.asyncio
async def test_derive_returns_empty_on_registry_failure(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings/registry read failure returns ``[]`` (the hot poll never sees the error)."""

    def _boom() -> object:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(liveness_mod, "get_settings", _boom)
    assert await derive_compute_lane_identities(session) == []
