"""phaze-2mwyo: the cloud retry budget must OUTLIVE the D-14 reaper that deletes the sidecar row.

THE DEFECT, in the exact production shape this suite drives end to end:

1. a long file burns its whole cloud budget -- ``cloud_job.attempts`` reaches ``cloud_submit_max_attempts``
   and the row spills to ``status='awaiting'`` (the single go-forward writer, ``hold_awaiting_cloud``);
2. ``select_backend`` sees the spent budget and routes it to LOCAL (the guaranteed safety net);
3. local analysis FAILS, so the agent POSTs ``/analysis/{file_id}/failed``;
4. that handler's **D-14 reaper** runs -- ``DELETE FROM cloud_job WHERE file_id = ... AND
   status = 'awaiting'`` -- and takes the file's entire ``attempts`` history with it;
5. the file is re-analysed later, gets a brand-new ``cloud_job`` row at ``attempts = 0``, and is free to
   spend the WHOLE cloud budget again. And again.

That is not hypothetical: spike ``phaze-wcrb`` recorded cloud job ``713a368e`` producing eight pods over
five days against a ceiling of three, and ``phaze-1q4g`` showed those eight are TWO CHAINS OF FOUR --
2026-07-24..07-25, then a fresh chain on 07-29 after a four-day gap -- while the other three affected
files show exactly one chain each. ``phaze-1q4g`` bounded a single chain. This bounds the number of them.

WHAT IS DELIBERATELY *NOT* TESTED AS A FIX: a narrower reaper. Retaining budget-spent ``awaiting`` rows
would leave the monotonically growing dead set that ``ix_cloud_job_awaiting`` scans -- the phaze-9sqa
head-of-line poison the reaper exists to prevent. So ``test_reaper_still_deletes_the_awaiting_row``
asserts the reaper's behavior is UNCHANGED, in the same run that asserts the budget survived it. Both
properties have to hold simultaneously or the fix is not the fix.

Uses the inline smoke-app + authed-agent client pattern from ``test_agent_analysis_failure.py`` so the
reaper under test is the REAL handler on the REAL transaction, not a hand-copied DELETE.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, select

from phaze.config import ControlSettings
from phaze.database import get_session
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.routers.agent_analysis import router as agent_analysis_router
from phaze.services.backend_selection import HOLD_CLOUD_ATTEMPTS_EXHAUSTED, BackendSlot, select_backend_with_reason
from phaze.services.backends import LocalBackend, hold_awaiting_cloud
from phaze.services.cloud_budget import (
    HOLD_CLOUD_BUDGET_CHAINS,
    HOLD_CLOUD_BUDGET_COOLDOWN,
    HOLD_CLOUD_BUDGET_NODE_LOSS,
)
from phaze.tasks.release_awaiting_cloud import _cloud_budget_for


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent
    from phaze.services.backends import Backend


_CAP = ControlSettings().cloud_submit_max_attempts  # 3 -- the same value every spill caller stamps.


def _make_smoke_app(session: AsyncSession) -> FastAPI:
    """Wire the real agent_analysis router (and therefore the real D-14 reaper) onto this session."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_analysis_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


async def _seed_file(session: AsyncSession, agent_id: str) -> FileRecord:
    """A long file, of the shape the cloud router sends to the burst path."""
    file = FileRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        sha256_hash="0" * 64,
        original_path=f"/test/music/{uuid.uuid4()}.flac",
        original_filename="set.flac",
        current_path="/test/music/set.flac",
        file_type="flac",
        file_size=2_000_000_000,
    )
    session.add(file)
    await session.commit()
    return file


async def _seed_submitted_chain(session: AsyncSession, file: FileRecord, *, attempts: int, node_loss_redrives: int = 0) -> None:
    """Put the file's sidecar in the state a live cloud burst leaves: SUBMITTED, mid-chain."""
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            status=CloudJobStatus.SUBMITTED.value,
            attempts=attempts,
            node_loss_redrives=node_loss_redrives,
        )
    )
    await session.commit()


async def _burn_one_chain(session: AsyncSession, file: FileRecord, *, node_loss_redrives: int = 0, attempts_before: int = _CAP) -> None:
    """Drive ONE whole cloud chain to its ceiling: the at-cap spill every terminal path takes.

    This is the real writer (``hold_awaiting_cloud`` in spill mode with ``attempts=cap``) -- exactly what
    ``reconcile_cloud_jobs``' at-ceiling terminal, both agent_push spills, both agent_s3 spills and
    ``_reap_stranded_staging`` all call. Nothing about the durable fold is re-implemented here.

    ``attempts_before`` defaults to the cap because that is the state the MOST COMMON chain end is in:
    the reconcile at-ceiling branch fires when ``attempts + 1 > cap``, so the last under-cap re-drive has
    ALREADY set ``attempts = cap`` and the ceiling branch deliberately does not increment again. A fold
    trigger keyed on "attempts crossed the cap" would silently skip exactly this path -- the one all four
    phaze-wcrb files took. ``attempts_before=0`` covers the other real shape (the agent_push/agent_s3
    over-cap spills, which stamp the cap onto a row that never re-drove at all).
    """
    await _seed_submitted_chain(session, file, attempts=attempts_before, node_loss_redrives=node_loss_redrives)
    spilled = await hold_awaiting_cloud(session, file, attempts=_CAP, expect_status=(CloudJobStatus.SUBMITTED.value,))
    assert spilled, "precondition: the spill CAS must match the SUBMITTED row"
    await session.commit()


class _Job(NamedTuple):
    """The three ``cloud_job`` fields these tests assert on."""

    status: str
    attempts: int
    node_loss_redrives: int


class _Ledger(NamedTuple):
    """The durable ``cloud_budget`` record as these tests assert on it."""

    chains_spent: int
    attempts_spent: int
    node_loss_spent: int
    budget_spent_at: datetime


async def _cloud_job(session: AsyncSession, file_id: uuid.UUID) -> _Job | None:
    """Read the sidecar as COLUMNS, never as an ORM entity.

    Deliberate: an entity read would come back through the identity map, which can hand back a cached
    object for a row this test has just watched the reaper delete -- the one thing these assertions must
    not be able to lie about. Column selects always reflect committed DB truth.
    """
    row = (await session.execute(select(CloudJob.status, CloudJob.attempts, CloudJob.node_loss_redrives).where(CloudJob.file_id == file_id))).first()
    return None if row is None else _Job(str(row[0]), int(row[1]), int(row[2]))


async def _budget(session: AsyncSession, file_id: uuid.UUID) -> _Ledger | None:
    """Read the durable ledger as COLUMNS (same reasoning as :func:`_cloud_job`)."""
    row = (
        await session.execute(
            select(CloudBudget.chains_spent, CloudBudget.attempts_spent, CloudBudget.node_loss_spent, CloudBudget.budget_spent_at).where(
                CloudBudget.file_id == file_id
            )
        )
    ).first()
    return None if row is None else _Ledger(int(row[0]), int(row[1]), int(row[2]), row[3])


async def _fail_analysis(session: AsyncSession, file_id: uuid.UUID, raw_token: str) -> int:
    """POST the real analyze-failure callback -- the seam that runs the D-14 reaper."""
    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.post(f"/api/internal/agent/analysis/{file_id}/failed", json={"reason": "timeout", "error": "local analyze died"})
    return r.status_code


class _CloudStub:
    """A duck-typed cloud ``Backend`` -- NOT a ``LocalBackend``, so the real policy treats it as cloud."""

    id = "kueue-1"
    rank = 10
    cap = 3

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: Any) -> bool:  # noqa: ARG002
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, Any] | None = None) -> dict[str, int] | None:  # noqa: ARG002
        return None


def _snapshot() -> dict[str, BackendSlot]:
    """A healthy registry: one cloud backend with free slots, plus local with a free slot.

    Both have capacity on purpose -- so a hold below can only be the BUDGET policy talking, never
    saturation, and so the routed-to-local assertion is a real routing decision rather than the only
    option left.
    """
    cloud: Backend = _CloudStub()  # type: ignore[assignment]  -- structural Backend, not a subclass
    local = LocalBackend(id="local", rank=99, cap=2)
    return {
        cloud.id: {"backend": cloud, "available": True, "remaining": 3, "cap": 3},
        local.id: {"backend": local, "available": True, "remaining": 2, "cap": 2},
    }


async def _route(session: AsyncSession, file_id: uuid.UUID, now: datetime, cfg: ControlSettings) -> tuple[str, str]:
    """Route the file exactly as the drain does and return ``(backend id or "", hold reason)``.

    Goes through ``_cloud_budget_for`` -- the drain's own per-candidate lookup -- so the test exercises
    the real read path (both sidecars, one round trip) rather than hand-assembling a policy input.
    """
    attempts, budget = await _cloud_budget_for(session, file_id)
    # lane_entered_at far in the past: the D-01/D-03 staleness gate is satisfied, so it can never be the
    # thing producing a hold here. The only variable under test is the budget.
    target, reason = select_backend_with_reason(now - timedelta(days=30), attempts, _snapshot(), now, cfg, cloud_budget=budget)
    return (target.id if target is not None else ""), reason


# ---------------------------------------------------------------------------
# THE REGRESSION: the production shape, start to finish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_full_chain_cannot_start_after_the_reaper_deletes_the_budget(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """Budget spent -> spill to local -> local FAILS -> reaper deletes the row -> re-analysis. No second chain.

    Walks all five steps of the incident with the real writers and the real HTTP callback, asserting at
    each one. The load-bearing assertion is the last: after the sidecar (and with it ``attempts``) is
    gone and a fresh ``cloud_job`` row exists at ``attempts = 0``, the file is STILL not offered a cloud
    backend -- and the hold names the durable budget, not the chain counter that no longer exists.
    """
    agent, raw_token = seed_test_agent
    cfg = ControlSettings()
    now = datetime.now(UTC)
    file = await _seed_file(session, agent.id)

    # (1) the file burns its whole cloud budget and spills to awaiting with attempts=cap.
    await _burn_one_chain(session, file, node_loss_redrives=1)
    job = await _cloud_job(session, file.id)
    assert job is not None and job.status == CloudJobStatus.AWAITING.value
    assert job.attempts == _CAP, "the spill stamps the budget-spent marker on the sidecar (D-03)"

    # ...and the SAME write folds that chain into the durable ledger.
    budget = await _budget(session, file.id)
    assert budget is not None, "burning a chain must record it somewhere the reaper cannot reach"
    assert budget.chains_spent == 1
    assert budget.attempts_spent == _CAP
    assert budget.node_loss_spent == 1, "phaze-1q4g's two causes stay SEPARATELY legible on the durable record"

    # (2) with the budget spent, the drain routes the file to LOCAL even though cloud has free slots.
    target, reason = await _route(session, file.id, now, cfg)
    assert target == "local", f"a budget-spent file routes to the local safety net, got {target!r} ({reason!r})"

    # (3)+(4) local analysis fails -> the real callback runs -> the D-14 reaper deletes the awaiting row.
    assert await _fail_analysis(session, file.id, raw_token) == 200
    assert await _cloud_job(session, file.id) is None, "the D-14 reaper is UNCHANGED: it still deletes the awaiting row"
    surviving = await _budget(session, file.id)
    assert surviving is not None, "THE FIX: the budget outlives the sidecar row the reaper deletes"
    assert (surviving.chains_spent, surviving.attempts_spent, surviving.node_loss_spent) == (1, _CAP, 1)

    # (5) re-analysis: the file is held for cloud again, which creates a FRESH sidecar row at attempts=0.
    await hold_awaiting_cloud(session, file)
    await session.commit()
    fresh = await _cloud_job(session, file.id)
    assert fresh is not None and fresh.attempts == 0, "precondition: the re-held sidecar genuinely has NO chain history"

    # THE ASSERTION THIS BEAD EXISTS FOR. Pre-fix, `attempts = 0` meant "never tried" and the file was
    # handed the whole cloud budget a second time -- 713a368e's second chain of four. Now the durable
    # record answers instead, so no second chain can start.
    attempts, durable = await _cloud_budget_for(session, file.id)
    assert attempts == 0, "the sidecar really did reset -- the fix must not depend on it having survived"
    assert durable is not None
    target, reason = await _route(session, file.id, now, cfg)
    assert target == "local", f"a re-analysed file whose budget was already spent must NOT get a fresh cloud chain (got {target!r})"
    assert reason == "", "routing to local is a dispatch, not a hold -- the file still makes progress"

    # And with local FULL, the hold names the DURABLE reason -- not `cloud_attempts_exhausted`, which is
    # the chain counter and is genuinely 0 here. Getting this label wrong would tell an operator to wait
    # for something that already happened.
    snapshot = _snapshot()
    snapshot["local"]["remaining"] = 0
    _, hold = select_backend_with_reason(now - timedelta(days=30), attempts, snapshot, now, cfg, cloud_budget=durable)
    assert hold == HOLD_CLOUD_BUDGET_COOLDOWN, f"expected the durable cooldown label, got {hold!r}"


@pytest.mark.asyncio
async def test_reaper_still_deletes_the_awaiting_row_on_the_success_seam(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """No phaze-9sqa regression: the OTHER analyze-terminal seam still reaps, and still leaves the budget.

    The tempting fix for this bead was to stop the reaper deleting budget-spent rows. That re-creates the
    growing dead set ``ix_cloud_job_awaiting`` scans, which is what starved the drain for >24 h. This test
    is the guard against a future "helpful" narrowing of either DELETE predicate: the row must go, the
    ledger must stay.
    """
    agent, raw_token = seed_test_agent
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file)
    assert await _budget(session, file.id) is not None

    app = _make_smoke_app(session)
    headers = {"Authorization": f"Bearer {raw_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as ac:
        r = await ac.put(f"/api/internal/agent/analysis/{file.id}", json={"bpm": 128.0})
    assert r.status_code == 200, r.text

    assert await _cloud_job(session, file.id) is None, "the result-PUT seam's D-14 reaper must be unchanged"
    assert await _budget(session, file.id) is not None, "and the durable budget must survive it too"


# ---------------------------------------------------------------------------
# The fold is EDGE-triggered -- a chain is charged once, and only a real chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ordinary_hold_never_records_a_spent_budget(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The hot path: holding a file for cloud (``attempts=0``) must record NOTHING.

    A level-triggered fold here would exile every healthy long file after its first hold -- the exact
    "too strict" failure direction this bead had to weigh against, and by far the more damaging one,
    since the files on this path are the large/slow ones that most need the burst lane.
    """
    agent, _ = seed_test_agent
    file = await _seed_file(session, agent.id)
    await hold_awaiting_cloud(session, file)
    await session.commit()
    assert await _budget(session, file.id) is None, "an ordinary hold has spent no budget and must leave no ledger row"


@pytest.mark.asyncio
async def test_the_reconcile_at_ceiling_shape_folds_even_though_attempts_never_moves(
    seed_test_agent: tuple[Agent, str], session: AsyncSession
) -> None:
    """The MOST COMMON chain end: the row is already at the cap when the ceiling is detected.

    ``reconcile_cloud_jobs`` increments ``attempts`` on each under-cap re-drive, so the last one sets it
    to the cap and re-submits; the ceiling is only noticed on the NEXT terminal (``attempts + 1 > cap``),
    which deliberately does NOT increment again. So at the moment the chain ends, ``attempts`` does not
    change -- only ``status`` does. A fold triggered on "attempts crossed the cap" records nothing here,
    which would leave the primary path (all four phaze-wcrb files) exactly as unbounded as before. The
    trigger is the row ENTERING the terminal budget-spent state instead.
    """
    agent, _ = seed_test_agent
    file = await _seed_file(session, agent.id)
    await _seed_submitted_chain(session, file, attempts=_CAP, node_loss_redrives=1)
    assert (await _cloud_job(session, file.id)) == _Job(CloudJobStatus.SUBMITTED.value, _CAP, 1), "precondition: already AT the cap, still in flight"

    assert await hold_awaiting_cloud(session, file, attempts=_CAP, expect_status=(CloudJobStatus.SUBMITTED.value,))
    await session.commit()

    ledger = await _budget(session, file.id)
    assert ledger is not None, "the at-ceiling terminal must record the burned chain even though attempts did not move"
    assert (ledger.chains_spent, ledger.attempts_spent, ledger.node_loss_spent) == (1, _CAP, 1)


@pytest.mark.asyncio
async def test_a_late_duplicate_spill_callback_folds_once(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A second at-cap write over the SAME already-parked chain must not charge it twice.

    Reachable in production as a late/duplicate agent callback re-running an over-cap spill branch.
    Double-counting would drag a file toward the lifetime ceilings for events that never happened --
    silently, and only visible weeks later as an unexplained exile.
    """
    agent, _ = seed_test_agent
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file)
    first = await _budget(session, file.id)
    assert first is not None and first.chains_spent == 1

    # The duplicate: the same spill CAS again. The row is now 'awaiting', which no caller's expect_status
    # contains, so it matches 0 rows -- a full no-op, exactly as the D-09/D-10 guard intends.
    assert not await hold_awaiting_cloud(session, file, attempts=_CAP, expect_status=(CloudJobStatus.SUBMITTED.value,))
    # And the belt to that braces: an UNCONDITIONAL at-cap hold over the parked row still folds nothing,
    # because the row is already IN the terminal budget-spent state.
    assert await hold_awaiting_cloud(session, file, attempts=_CAP)
    await session.commit()

    again = await _budget(session, file.id)
    assert again is not None
    assert again.chains_spent == 1, "the fold triggers on ENTERING the budget-spent state, not on being in it"
    assert again.attempts_spent == _CAP


@pytest.mark.asyncio
async def test_an_over_cap_spill_from_a_row_that_never_redrove_folds_once(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The other real chain end: agent_push / agent_s3 stamp the cap onto a row still at ``attempts=0``.

    Those spills declare the cloud budget spent because the PUSH leg exhausted ``push_max_attempts``, not
    because analysis re-drove -- so the sidecar goes 0 -> cap in one write. Both shapes must fold, and
    fold exactly one chain.
    """
    agent, _ = seed_test_agent
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file, attempts_before=0)

    ledger = await _budget(session, file.id)
    assert ledger is not None
    assert (ledger.chains_spent, ledger.attempts_spent, ledger.node_loss_spent) == (1, _CAP, 0)


@pytest.mark.asyncio
async def test_a_genuinely_new_chain_accumulates_and_restarts_the_cooldown(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A SECOND real chain (fresh row at 0 -> cap) accumulates onto the ledger and re-stamps the clock.

    Accumulation is what makes the lifetime ceilings mean anything, and re-stamping ``budget_spent_at``
    is what stops a file inheriting an already-expired cooldown from a burnout months ago.
    """
    agent, _ = seed_test_agent
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file, node_loss_redrives=1)
    first = await _budget(session, file.id)
    assert first is not None
    first_spent_at = first.budget_spent_at

    # The reaper clears the sidecar, then a re-analysis starts a genuinely fresh chain that also burns out.
    await session.execute(delete(CloudJob).where(CloudJob.file_id == file.id))
    await session.commit()
    await _burn_one_chain(session, file, node_loss_redrives=2)

    second = await _budget(session, file.id)
    assert second is not None
    assert second.chains_spent == 2, "two real chains must read as two"
    assert second.attempts_spent == 2 * _CAP
    assert second.node_loss_spent == 3, "node-loss stays its own accumulating tally (1 + 2), never folded into attempts"
    assert second.budget_spent_at >= first_spent_at, "the cooldown clock measures from the MOST RECENT burnout"


# ---------------------------------------------------------------------------
# Policy: both failure directions, and the config-only escape hatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cooldown_expires_and_re_admits_the_file_to_cloud(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """TOO STRICT is a real failure too: a file grounded by one bad node must fly again on its own.

    The affected files are the large/slow ones that most need the burst path, and phaze-6ck1 has not
    established that their memory growth is intrinsic rather than environmental. So the default policy is
    a cooldown, and this asserts it genuinely clears -- no operator action, no row edited.
    """
    agent, _ = seed_test_agent
    cfg = ControlSettings()
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file)
    # Clear the sidecar exactly as the reaper does, then re-hold: only the durable record can bar cloud now.
    await session.execute(delete(CloudJob).where(CloudJob.file_id == file.id))
    await hold_awaiting_cloud(session, file)
    await session.commit()

    inside = datetime.now(UTC) + timedelta(days=cfg.cloud_budget_cooldown_days - 1)
    assert (await _route(session, file.id, inside, cfg))[0] == "local", "inside the cooldown: cloud is barred"

    outside = datetime.now(UTC) + timedelta(days=cfg.cloud_budget_cooldown_days + 1)
    assert (await _route(session, file.id, outside, cfg))[0] == "kueue-1", "past the cooldown: the file is re-admitted to cloud"


@pytest.mark.asyncio
async def test_lifetime_ceilings_bar_cloud_and_are_liftable_by_config_alone(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """The intrinsic-cause backstop, and its escape hatch -- no migration, no row edit, no DB surgery.

    Three properties in one place because they only make sense together: a file past the chain ceiling is
    barred even once its cooldown has expired; the node-loss ceiling is reported SEPARATELY (phaze-1q4g's
    two causes); and raising or disabling either knob re-admits the file immediately. That last one is how
    "wait for phaze-6ck1's verdict" is discharged without a second migration -- the verdict changes a
    default, not the schema.
    """
    agent, _ = seed_test_agent
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file)
    await session.execute(delete(CloudJob).where(CloudJob.file_id == file.id))
    await hold_awaiting_cloud(session, file)
    await session.commit()
    # Well past any cooldown, so only the lifetime ceilings can be talking.
    now = datetime.now(UTC) + timedelta(days=365)

    at_chain_ceiling = ControlSettings(cloud_budget_max_chains=1)
    assert (await _route(session, file.id, now, at_chain_ceiling)) == ("local", ""), "past the chain ceiling: cloud is barred"

    lifted = ControlSettings(cloud_budget_max_chains=99)
    assert (await _route(session, file.id, now, lifted))[0] == "kueue-1", "raising the knob re-admits the file -- config only"

    disabled = ControlSettings(cloud_budget_max_chains=0, cloud_budget_max_node_loss=0, cloud_budget_cooldown_days=0)
    assert (await _route(session, file.id, now, disabled))[0] == "kueue-1", "0 disables each rule -- 'no durable policy' is configurable"


@pytest.mark.asyncio
async def test_the_two_causes_report_different_hold_reasons(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """A file exiled for repeated ANALYSIS FAILURE and one exiled for repeated NODE LOSS stay distinguishable.

    ``phaze-1q4g`` split ``attempts`` from ``node_loss_redrives`` on the row so an operator could tell
    "this file keeps failing" from "this file keeps taking nodes down". That distinction is worth more,
    not less, once it is the durable record read weeks after the fact -- so the labels must not collapse.
    """
    agent, _ = seed_test_agent
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file, node_loss_redrives=2)
    await session.execute(delete(CloudJob).where(CloudJob.file_id == file.id))
    await hold_awaiting_cloud(session, file)
    await session.commit()
    now = datetime.now(UTC) + timedelta(days=365)

    snapshot = _snapshot()
    snapshot["local"]["remaining"] = 0  # force the labelled hold rather than a local dispatch
    attempts, budget = await _cloud_budget_for(session, file.id)
    assert attempts == 0

    chains = ControlSettings(cloud_budget_max_chains=1, cloud_budget_max_node_loss=0)
    _, reason = select_backend_with_reason(now - timedelta(days=400), attempts, snapshot, now, chains, cloud_budget=budget)
    assert reason == HOLD_CLOUD_BUDGET_CHAINS

    node_loss = ControlSettings(cloud_budget_max_chains=0, cloud_budget_max_node_loss=2)
    _, reason = select_backend_with_reason(now - timedelta(days=400), attempts, snapshot, now, node_loss, cloud_budget=budget)
    assert reason == HOLD_CLOUD_BUDGET_NODE_LOSS, "node loss must NOT be reported as an analysis-failure ceiling"


@pytest.mark.asyncio
async def test_the_live_chain_cap_still_wins_the_label_while_it_applies(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """When BOTH budgets bar cloud, the per-chain cap reports -- it is the more actionable reading.

    Guards the ordering inside ``select_backend_with_reason``: the durable rule must not shadow
    ``cloud_attempts_exhausted``, whose phaze-9sqa meaning (permanent while local is full) drives a
    different operator response than a cooldown that clears itself.
    """
    agent, _ = seed_test_agent
    cfg = ControlSettings()
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file)  # leaves the sidecar at attempts=cap AND writes the ledger
    now = datetime.now(UTC)

    snapshot = _snapshot()
    snapshot["local"]["remaining"] = 0
    attempts, budget = await _cloud_budget_for(session, file.id)
    assert attempts == _CAP and budget is not None, "precondition: both budgets are spent"
    _, reason = select_backend_with_reason(now - timedelta(days=30), attempts, snapshot, now, cfg, cloud_budget=budget)
    assert reason == HOLD_CLOUD_ATTEMPTS_EXHAUSTED


@pytest.mark.asyncio
async def test_a_budget_barred_file_is_never_stranded(seed_test_agent: tuple[Agent, str], session: AsyncSession) -> None:
    """No row may be left in a state no writer can advance -- local is never excluded by any budget rule.

    The strictest configuration this policy can express (every ceiling at its tightest, cooldown at its
    longest) must still route the file somewhere, because local is the guaranteed safety net. If this
    ever fails, the fix has traded the incident for a permanent stall.
    """
    agent, _ = seed_test_agent
    file = await _seed_file(session, agent.id)
    await _burn_one_chain(session, file, node_loss_redrives=1)
    await session.execute(delete(CloudJob).where(CloudJob.file_id == file.id))
    await hold_awaiting_cloud(session, file)
    await session.commit()

    strictest = ControlSettings(cloud_budget_max_chains=1, cloud_budget_max_node_loss=1, cloud_budget_cooldown_days=3650)
    target, reason = await _route(session, file.id, datetime.now(UTC), strictest)
    assert target == "local", f"the harshest budget verdict must still leave a routable backend (got {target!r} / {reason!r})"
