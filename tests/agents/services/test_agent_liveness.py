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
from phaze.services import agent_liveness as liveness_mod, pipeline as pipeline_mod
from phaze.services.agent_liveness import (
    AgentStatus,
    ComputeLane,
    classify,
    derive_compute_lane_identities,
    get_compute_lane_running_jobs,
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
# derive_compute_lane_identities(session) — degrade branch (D-05/D-07 / KDEPLOY-04)
# ---------------------------------------------------------------------------


class _RaisingSession:
    """Stub session whose ``begin_nested`` raises ``SQLAlchemyError`` the moment control enters the ``try``.

    ``derive_compute_lane_identities`` opens ``async with session.begin_nested():`` as the first
    statement inside its ``try``. Raising synchronously from ``begin_nested`` drives control straight
    into the ``except SQLAlchemyError`` degrade branch, which returns the registry lanes all-``IDLE``
    — a DB hiccup must NEVER paint a compute lane DEAD/red.
    """

    def begin_nested(self) -> object:
        raise SQLAlchemyError("db down")


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

    # phaze-5c6i2: queued/working derive from the SAME phaze-zyoag seam the lane cards use -- a plain
    # RUNNING row is unconditionally "working" regardless of backend kind, so both lanes are fully
    # working with nothing queued.
    assert lanes == [
        ComputeLane(backend_id="k8s-a", kind="kueue", state="ACTIVE", running=2, waiting=0, queued=0, working=2),
        ComputeLane(backend_id="k8s-b", kind="kueue", state="ACTIVE", running=1, waiting=0, queued=0, working=1),
    ]


@pytest.mark.asyncio
async def test_derive_idle_configured_cluster_still_listed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured cluster with NO in-flight rows still appears as an IDLE lane (registry-composed, no probe)."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue"), _backend("k8s-idle", "kueue")))
    await _seed(session, ("k8s-a", CloudJobStatus.RUNNING.value, False))

    lanes = await derive_compute_lane_identities(session)

    assert lanes == [
        ComputeLane(backend_id="k8s-a", kind="kueue", state="ACTIVE", running=1, waiting=0, queued=0, working=1),
        ComputeLane(backend_id="k8s-idle", kind="kueue", state="IDLE", running=0, waiting=0, queued=0, working=0),
    ]


@pytest.mark.asyncio
async def test_derive_waiting_via_submitted_inadmissible(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SUBMITTED+inadmissible row (and no running) → WAITING; a plain SUBMITTED row does NOT count as waiting."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))
    # phaze-5c6i2: _cloud_window_clauses (pipeline.py) resolves kueue-ness via ITS OWN bound
    # ``get_settings`` -- a separate name from liveness_mod's, so both must carry the SAME registry for
    # queued/working to reflect "k8s-a is a kueue lane" (mirrors tests/shared/services/test_pipeline.py's
    # ``monkeypatch.setattr(pipeline_mod, "get_settings", ...)`` idiom for the same helper).
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))
    await _seed(
        session,
        ("k8s-a", CloudJobStatus.SUBMITTED.value, True),
        ("k8s-a", CloudJobStatus.SUBMITTED.value, False),  # admissible submitted -> not waiting, not running
    )

    lanes = await derive_compute_lane_identities(session)

    # phaze-5c6i2: BOTH SUBMITTED rows are kueue-attributed, so both count as "working" under the
    # zyoag option-(a) definition (post-submit, in the cloud window) regardless of the admissible flag
    # that drives the SEPARATE running/waiting admission-fault fields above.
    assert lanes == [ComputeLane(backend_id="k8s-a", kind="kueue", state="WAITING", running=0, waiting=1, queued=0, working=2)]


@pytest.mark.asyncio
async def test_derive_running_takes_precedence_over_waiting(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lane with BOTH a running and a waiting row is ACTIVE (running≥1 dominates)."""
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))
    monkeypatch.setattr(pipeline_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))
    await _seed(
        session,
        ("k8s-a", CloudJobStatus.RUNNING.value, False),
        ("k8s-a", CloudJobStatus.SUBMITTED.value, True),
    )

    lanes = await derive_compute_lane_identities(session)

    # phaze-5c6i2: the RUNNING row and the kueue-attributed SUBMITTED row are BOTH "working".
    assert lanes == [ComputeLane(backend_id="k8s-a", kind="kueue", state="ACTIVE", running=1, waiting=1, queued=0, working=2)]


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

    # phaze-5c6i2: a NULL backend_id can never be treated as kueue-attributed (the split predicate
    # requires a non-NULL id IN the kueue set), so the unattributed group's SUBMITTED row falls to
    # "queued" (the compute-shaped fallback) while its RUNNING row is unconditionally "working".
    assert lanes == [
        ComputeLane(backend_id="k8s-a", kind="kueue", state="ACTIVE", running=1, waiting=0, queued=0, working=1),
        ComputeLane(backend_id="unattributed", kind="cloud", state="ACTIVE", running=1, waiting=1, queued=1, working=1),
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
        ComputeLane(backend_id="k8s-a", kind="kueue", state="IDLE", running=0, waiting=0, queued=0, working=0),
        ComputeLane(backend_id="a1", kind="compute", state="IDLE", running=0, waiting=0, queued=0, working=0),
    ]


@pytest.mark.asyncio
async def test_derive_degrade_preserves_caller_loaded_agent_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-01: a CloudJob-read failure degrades via a SAVEPOINT that rolls back the NESTED scope ALONE.

    ``admin_agents`` loads Agent rows on this SAME session BEFORE calling ``derive_compute_lane_identities``
    and renders their attributes AFTER. The degrade must NOT roll back the outer transaction/session — a
    plain ``session.rollback()`` there expires every mapped instance in the identity map, 500-ing the
    template render on the next lazy load (MissingGreenlet from a sync context).

    Distinguishing signal (fixture never commits, so ``inspect().expired`` cannot tell a SAVEPOINT rollback
    apart from a plain one — a plain rollback expunges the pending flush to *transient*, not *expired*):
    flush an Agent row, then force ONLY the inner CloudJob SELECT to fail. Under the SAVEPOINT fix the
    earlier flush survives in the intact outer transaction, so ``session.get`` still finds the agent. Under
    a plain ``session.rollback()`` the whole outer transaction unwinds and ``session.get`` returns ``None``.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setattr(liveness_mod, "get_settings", lambda: _settings(_backend("k8s-a", "kueue")))

    agent = Agent(id="cr01-lane-agent", name="Cr01LaneBox", scan_roots=[], last_seen_at=datetime.now(UTC), kind="fileserver")
    session.add(agent)
    await session.flush()

    # Force ONLY the inner CloudJob SELECT to fail; the flush above already happened on the real execute.
    real_execute = session.execute
    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=SQLAlchemyError("boom")))
    lanes = await derive_compute_lane_identities(session)
    monkeypatch.setattr(session, "execute", real_execute)  # restore for the assertion query

    # Degrades to the registry lanes all-IDLE, never raises (KDEPLOY-04).
    assert lanes == [ComputeLane(backend_id="k8s-a", kind="kueue", state="IDLE", running=0, waiting=0, queued=0, working=0)]
    # CR-01: the outer transaction (and the earlier flush of the agent) must survive the degrade. A plain
    # ``session.rollback()`` would unwind the outer txn and this lookup would be None.
    assert await session.get(Agent, "cr01-lane-agent") is not None


@pytest.mark.asyncio
async def test_derive_returns_empty_on_registry_failure(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings/registry read failure returns ``[]`` (the hot poll never sees the error)."""

    def _boom() -> object:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(liveness_mod, "get_settings", _boom)
    assert await derive_compute_lane_identities(session) == []


async def test_derive_degrades_to_all_idle_lanes_on_window_clauses_failure(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``_cloud_window_clauses`` failure (distinct from the registry read above) still lists every
    configured non-local backend, each forced to state=IDLE / running=waiting=queued=working=0 --
    never an empty list and never a raise, so the page renders the known lanes instead of going blank.
    """
    settings = _settings(_backend("k8s-a", "kueue"), _backend("a1", "compute"))
    monkeypatch.setattr(liveness_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline_mod, "_cloud_window_clauses", lambda: (_ for _ in ()).throw(RuntimeError("clauses unavailable")))

    lanes = await derive_compute_lane_identities(session)

    assert {lane.backend_id: lane.kind for lane in lanes} == {"k8s-a": "kueue", "a1": "compute"}
    assert all(lane.state == "IDLE" for lane in lanes)
    assert all((lane.running, lane.waiting, lane.queued, lane.working) == (0, 0, 0, 0) for lane in lanes)


# ---------------------------------------------------------------------------
# get_compute_lane_running_jobs(session, backend_id) — phaze-2u8v.5 burst-lane workload drill-down
# ---------------------------------------------------------------------------


async def _seed_named_job(session: AsyncSession, *, backend_id: str | None, filename: str, status: str = CloudJobStatus.RUNNING.value) -> FileRecord:
    """Seed one FileRecord + CloudJob attributed to ``backend_id``, returning the FileRecord."""
    file = FileRecord(
        agent_id="test-fileserver",
        id=uuid.uuid4(),
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/{filename}",
        original_filename=filename,
        current_path=f"/music/{filename}",
        file_type="mp3",
        file_size=1000,
    )
    session.add(file)
    await session.flush()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file.id, s3_key=f"staging/{file.id}", status=status, backend_id=backend_id))
    await session.commit()
    return file


@pytest.mark.asyncio
async def test_running_jobs_lists_running_files_for_backend(session: AsyncSession) -> None:
    """RUNNING rows attributed to ``backend_id`` come back with their display filename (the drill-down itself)."""
    await _seed_named_job(session, backend_id="vox", filename="one.mp3")
    await _seed_named_job(session, backend_id="vox", filename="two.mp3")
    # Noise that MUST be excluded: a different backend, and a non-RUNNING status on the SAME backend.
    await _seed_named_job(session, backend_id="xenolab", filename="other-backend.mp3")
    await _seed_named_job(session, backend_id="vox", filename="succeeded.mp3", status=CloudJobStatus.SUCCEEDED.value)

    jobs = await get_compute_lane_running_jobs(session, "vox")

    filenames = {job["filename"] for job in jobs}
    assert filenames == {"one.mp3", "two.mp3"}
    assert len(jobs) == 2


@pytest.mark.asyncio
async def test_running_jobs_uses_repaired_filename_when_present(session: AsyncSession) -> None:
    """The display filename prefers ``original_filename_repaired`` (COALESCE, the search_queries convention)."""
    file = await _seed_named_job(session, backend_id="vox", filename="mojibake.mp3")
    file.original_filename_repaired = "repaired.mp3"
    await session.commit()

    jobs = await get_compute_lane_running_jobs(session, "vox")

    assert jobs[0]["filename"] == "repaired.mp3"


@pytest.mark.asyncio
async def test_running_jobs_idle_lane_is_empty(session: AsyncSession) -> None:
    """A backend with no RUNNING rows returns ``[]`` — the drill-down's idle-empty-state acceptance."""
    await _seed_named_job(session, backend_id="vox", filename="queued.mp3", status=CloudJobStatus.SUBMITTED.value)

    assert await get_compute_lane_running_jobs(session, "vox") == []
    assert await get_compute_lane_running_jobs(session, "never-configured") == []


@pytest.mark.asyncio
async def test_running_jobs_unattributed_queries_null_backend_id(session: AsyncSession) -> None:
    """``UNATTRIBUTED_LANE_ID`` queries the NULL-``backend_id`` rows, mirroring the derivation's own collapse."""
    from phaze.services.agent_liveness import UNATTRIBUTED_LANE_ID

    await _seed_named_job(session, backend_id=None, filename="unattributed.mp3")
    await _seed_named_job(session, backend_id="vox", filename="attributed.mp3")

    jobs = await get_compute_lane_running_jobs(session, UNATTRIBUTED_LANE_ID)

    assert [job["filename"] for job in jobs] == ["unattributed.mp3"]


@pytest.mark.asyncio
async def test_running_jobs_degrades_on_error(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A forced query error degrades to ``[]`` with a guarded rollback — never raises (D-00b)."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=SQLAlchemyError("boom")))
    assert await get_compute_lane_running_jobs(session, "vox") == []


@pytest.mark.asyncio
async def test_running_jobs_degrades_on_error_even_when_the_guarded_rollback_also_fails(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query AND its own recovery rollback both failing still degrades to ``[]``, never raises.

    The guarded ``try: await session.rollback() except SQLAlchemyError`` around the recovery attempt
    exists for exactly this case -- a session so broken even the cleanup can't run.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setattr(session, "execute", AsyncMock(side_effect=SQLAlchemyError("boom")))
    monkeypatch.setattr(session, "rollback", AsyncMock(side_effect=SQLAlchemyError("connection already closed")))
    assert await get_compute_lane_running_jobs(session, "vox") == []
