"""Tests for `services/backends/compute_agent.py` (split from test_backends.py, phaze-7l8jh).

`ComputeAgentBackend.is_available` / `in_flight_count` / `dispatch` / `reconcile` / its stranded-submitted reaper -- `services/backends/compute_agent.py`. Also the `select_agent_by_id` cells (see `_shared.py`'s docstring for why they live here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.analyze.services.backends.protocol._shared import (
    IN_FLIGHT_STATUSES,
    TERMINAL_STATUSES,
    UTC,
    Any,
    AsyncMock,
    CloudJob,
    CloudJobStatus,
    DedupFakeTaskRouter,
    _cloud_job_for,
    _compute,
    _ledger_row_exists,
    _make_file,
    _RaisingTaskRouter,
    _seed_agent_row,
    _seed_cloud_job,
    _seed_live_saq_job,
    _seed_push_file_ledger,
    _seed_submitted_cloud_job,
    backends,
    backends_compute_agent,
    datetime,
    pytest,
    seed_active_agent,
    timedelta,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# === select_agent_by_id (per-entry binding, D-01) ========================================


@pytest.mark.asyncio
async def test_select_agent_by_id_returns_agent_matched_on_id(session: AsyncSession) -> None:
    """D-01: select_agent_by_id resolves the online agent whose Agent.id equals the arg."""
    from phaze.services.enqueue_router import select_agent_by_id

    await _seed_agent_row(session, agent_id="oci-a1", kind="compute")
    agent = await select_agent_by_id(session, "oci-a1", kind="compute")
    assert agent.id == "oci-a1"


@pytest.mark.asyncio
async def test_select_agent_by_id_matches_id_only_never_name(session: AsyncSession) -> None:
    """D-01 (no fallback): an agent whose NAME (not id) equals the arg does NOT match -> raises."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    # id="oci-real", name="oci-a1" -- the arg "oci-a1" collides with the free-form NAME only.
    await _seed_agent_row(session, agent_id="oci-real", name="oci-a1", kind="compute")
    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "oci-a1", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_revoked_agent_raises(session: AsyncSession) -> None:
    """A matching-id agent that is revoked (revoked_at set) does NOT match -> raises (liveness filter)."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    await _seed_agent_row(session, agent_id="oci-a1", kind="compute", revoked=True)
    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "oci-a1", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_never_seen_agent_raises(session: AsyncSession) -> None:
    """A matching-id agent that never checked in (last_seen_at NULL) does NOT match -> raises."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    await _seed_agent_row(session, agent_id="oci-a1", kind="compute", online=False)
    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "oci-a1", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_absent_agent_raises(session: AsyncSession) -> None:
    """An id with NO matching agent raises NoActiveAgentError (the degrade-to-hold signal D-05 consumes)."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "nope", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_honors_kind_filter(session: AsyncSession) -> None:
    """When kind is given, a same-id agent of a different kind does not cross-match -> raises."""
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    # A fileserver agent with the same id must NOT satisfy a kind="compute" lookup.
    await _seed_agent_row(session, agent_id="oci-a1", kind="fileserver")
    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, "oci-a1", kind="compute")


@pytest.mark.asyncio
async def test_select_agent_by_id_treats_sql_metacharacters_as_a_literal_value(session: AsyncSession) -> None:
    """D-01: an ``agent_id`` shaped like a SQL-injection payload is bound as a literal, never executed.

    The docstring's "parameterized query" claim has no dedicated adversarial cell elsewhere in this
    suite -- every existing D-01 test passes an ordinary slug. This cell feeds a classic
    tautology/statement-injection payload as the ``agent_id`` argument and proves TWO things a
    string-interpolated (unparameterized) query would fail: (1) the lookup raises
    ``NoActiveAgentError`` -- the payload matches no row rather than short-circuiting a tautology like
    ``OR '1'='1'`` into matching every row -- and (2) a genuine, unrelated agent seeded in the SAME
    session survives the call untouched (a `; DROP TABLE agents; --`-shaped value never reaches the
    database as executable SQL).
    """
    from phaze.services.enqueue_router import NoActiveAgentError, select_agent_by_id

    survivor = await _seed_agent_row(session, agent_id="oci-real", kind="compute")
    payload = "oci-real' OR '1'='1'; DROP TABLE agents; --"

    with pytest.raises(NoActiveAgentError):
        await select_agent_by_id(session, payload, kind="compute")

    # The unrelated legitimate agent must still resolve -- proof no injected statement executed.
    resolved = await select_agent_by_id(session, survivor.id, kind="compute")
    assert resolved.id == "oci-real"


@pytest.mark.asyncio
async def test_compute_is_available_true_when_bound_agent_online(session: AsyncSession) -> None:
    """D-02: is_available is True when the bound ``agent_ref`` names an ONLINE compute agent (Agent.id)."""
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    # Bind the backend to THIS agent's id -- the per-entry reference, not "the single active compute agent".
    assert await _compute(id="compute-a1", agent_ref="cloud-1").is_available(session) is True


@pytest.mark.asyncio
async def test_compute_is_available_false_when_bound_agent_absent(session: AsyncSession) -> None:
    """D-05: bound agent absent / not-yet-registered -> is_available False, NEVER raises (degrade-to-hold)."""
    assert await _compute(id="compute-a1", agent_ref="cloud-1").is_available(session) is False


@pytest.mark.asyncio
async def test_compute_is_available_false_when_online_agent_id_mismatches_ref(session: AsyncSession) -> None:
    """D-02 behavior change: a compute agent is online but its id != agent_ref -> False (not the retired pick).

    The intended change vs the retired ``select_active_agent(kind="compute")`` single-active pick: a
    DIFFERENT online compute agent no longer satisfies THIS backend's binding. Only the specifically-bound
    node counts.
    """
    await seed_active_agent(session, agent_id="some-other-compute", kind="compute")
    assert await _compute(id="compute-a1", agent_ref="cloud-1").is_available(session) is False


@pytest.mark.asyncio
async def test_compute_is_available_reads_bound_ref_not_single_active_pick(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-02 record-don't-rederive: is_available resolves the bound ref and does NOT call select_active_agent."""
    # phaze-dr9df: patch the module that RESOLVES the name, not the re-export facade (see the
    # ``backends_compute_agent`` alias at the top of this file).
    sentinel = AsyncMock(side_effect=AssertionError("is_available must not use the single-active pick"))
    monkeypatch.setattr(backends_compute_agent, "select_active_agent", sentinel)
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    assert await _compute(id="compute-a1", agent_ref="cloud-1").is_available(session) is True
    sentinel.assert_not_awaited()


@pytest.mark.asyncio
async def test_compute_is_available_fails_loud_when_no_agent_ref_bound(session: AsyncSession) -> None:
    """A defensively-unbound compute backend (no agent_ref) fails loud via the accessor, mirroring _kube()."""
    # config=None -> the accessor has nothing to resolve -> a clear raise (NOT a silent False).
    backend = _compute(id="compute-a1", config=None)
    with pytest.raises(ValueError, match="compute-a1"):
        await backend.is_available(session)


@pytest.mark.asyncio
async def test_mcomp02_two_compute_backends_only_the_online_bound_agent_is_available(session: AsyncSession) -> None:
    """MCOMP-02 (per-agent liveness): N compute backends -> an offline bound agent makes ONLY that lane unavailable.

    A local + 2-compute deploy where compute-a's bound agent (``cloud-a``) is ONLINE and compute-b's bound
    agent (``cloud-b``) is OFFLINE (never registered). Per-entry gating (Phase 72 ``is_available`` resolves
    ``self.config.agent_ref`` against ``Agent.id``, NOT a single-active pick) must report backend-a available
    and backend-b UNAVAILABLE -- so the drain snapshot leaves the healthy compute lane eligible while the
    offline one contributes 0 slots. This is the N-compute twin of the single-active liveness the retired
    ``select_active_agent(kind="compute")`` pick could not express.
    """
    # Only compute-a's bound node is online; compute-b's is absent.
    await seed_active_agent(session, agent_id="cloud-a", kind="compute")

    backend_a = _compute(id="compute-a", agent_ref="cloud-a")
    backend_b = _compute(id="compute-b", agent_ref="cloud-b")

    assert await backend_a.is_available(session) is True
    assert await backend_b.is_available(session) is False


@pytest.mark.asyncio
async def test_compute_in_flight_count_filters_by_backend_id_and_status(session: AsyncSession) -> None:
    """Compute in_flight_count counts only its own backend_id rows in the D-10 in-flight set."""
    backend = _compute(id="compute-a1")
    for status in IN_FLIGHT_STATUSES:
        await _seed_cloud_job(session, backend_id="compute-a1", status=status)
    for status in TERMINAL_STATUSES:
        await _seed_cloud_job(session, backend_id="compute-a1", status=status)  # excluded (terminal)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.RUNNING)  # other backend
    assert await backend.in_flight_count(session) == len(IN_FLIGHT_STATUSES)


# === dispatch (3 impls; D-03 atomicity) ==================================================


@pytest.mark.asyncio
async def test_compute_dispatch_writes_cloud_job_in_txn(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-03: compute dispatch upserts a non-terminal cloud_job in the SAME session.

    Phase 90 (D-09): the paired PUSHING files.state flip was removed; the cloud_job row is the sole
    in-flight authority. The row must be visible (via autoflush) within the uncommitted transaction --
    there is never a committed in-flight dispatch without a live cloud_job row (Pitfall 4 limbo guard).
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    backend = _compute(id="compute-a1")
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.backend_id == "compute-a1"
    assert job.status not in {s.value for s in TERMINAL_STATUSES}


@pytest.mark.asyncio
async def test_compute_redispatch_bumps_updated_at_not_created_at(session: AsyncSession) -> None:
    """phaze-7634: a re-dispatch (conflicting upsert) bumps CloudJob.updated_at; created_at stays pinned.

    Same defect class as phaze-c8nz: this compute-dispatch upsert's `set_` clause used to omit
    `updated_at` (unlike the sibling hold-mode upsert in ``ComputeAgentBackend.select_backend``,
    which was already fixed). Backdate both columns after a first dispatch, re-dispatch, and
    assert updated_at moves forward while created_at is untouched.
    """
    from sqlalchemy import select, update

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    backend = _compute(id="compute-a1")
    file = _make_file()
    file_id = file.id
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)
    await session.commit()

    # Backdate created_at/updated_at directly (bypassing the ORM/onupdate hook) to a fixed point
    # well in the past. Bind a tz-AWARE value: since phaze-cz3m / migration 049 every timestamp
    # column is timestamptz, and a NAIVE datetime bound to one is silently reinterpreted as the
    # session's local time rather than UTC -- a same-shape defect to the one 049 fixed.
    outage_time = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=12)
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(created_at=outage_time, updated_at=outage_time))
    await session.commit()

    before_redispatch = datetime.now(UTC)

    await backend.dispatch(file, session, router)
    await session.commit()

    session.expire_all()
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one()
    assert row.created_at == outage_time, "created_at must stay pinned to the first-write value"
    assert row.updated_at > outage_time, "updated_at must move forward off the stale outage-window value"
    assert row.updated_at >= before_redispatch - timedelta(seconds=5), (
        "updated_at must reflect the server clock at conflict-resolution time, not the stale backdated value"
    )


@pytest.mark.asyncio
async def test_compute_dispatch_stamps_destination_on_push_payload(session: AsyncSession) -> None:
    """D-02: dispatch stamps dest_host/dest_scratch_dir/dest_ssh_user off self.config onto the push_file payload.

    Record-don't-rederive originates here: the enqueued push carries THIS backend's own push_host /
    scratch_dir / ssh_user (read off the bound ComputeBackend), so every downstream reader (the Plan-02
    rsync argv) uses the RECORDED destination rather than re-deriving it.

    phaze-s5sz: the enqueue is PARKED, not fired inline -- assert it is queued only after the commit +
    an explicit ``flush_pending_push_file_enqueues`` (mirrors the kueue s3_upload twin).
    """
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    backend = _compute(id="compute-a1", scratch_dir="/srv/scratch", push_host="a1.push.example", ssh_user="phaze")
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)
    assert router.captures == [], "the push_file enqueue must be PARKED, not fired inline"

    await session.commit()
    assert await backends.flush_pending_push_file_enqueues(session) == 1

    pushes = [(task, payload) for task, payload in router.queues["nox-io"].captured if task == "push_file"]
    assert len(pushes) == 1
    _task, payload = pushes[0]
    assert payload["dest_host"] == "a1.push.example"
    assert payload["dest_scratch_dir"] == "/srv/scratch"
    assert payload["dest_ssh_user"] == "phaze"


@pytest.mark.asyncio
async def test_compute_dispatch_stamps_none_ssh_user_when_unset(session: AsyncSession) -> None:
    """dest_ssh_user is None on the push payload when the backend omits ssh_user (D-01 optional)."""
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    await seed_active_agent(session, agent_id="cloud-1", kind="compute")
    backend = _compute(id="compute-a1", scratch_dir="/srv/scratch", push_host="a1.push.example")
    file = _make_file()
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)
    await session.commit()
    assert await backends.flush_pending_push_file_enqueues(session) == 1

    _task, payload = next((t, p) for t, p in router.queues["nox-io"].captured if t == "push_file")
    assert payload["dest_host"] == "a1.push.example"
    assert payload["dest_ssh_user"] is None


# === phaze-s5sz: dispatch PARKS (never fires) the push_file enqueue =====================================


@pytest.mark.asyncio
async def test_compute_dispatch_defers_enqueue_past_the_committed_row(session: AsyncSession) -> None:
    """phaze-s5sz: ``ComputeAgentBackend.dispatch`` no longer FIRES the ``push_file`` enqueue -- it PARKS it.

    Supersedes the phaze-uciu.3 SAVEPOINT twin (regression test renamed off
    ``test_compute_dispatch_enqueue_failure_rolls_back_write_via_savepoint``): pre-fix ``dispatch``
    enqueued ``push_file`` INLINE (on SAQ's OWN psycopg pool, which commits the job durably +
    IMMEDIATELY), so a fast rsync push could complete and POST ``/pushed`` before this asyncpg session
    committed the ``cloud_job`` SUBMITTED row -- ``report_pushed``'s ONLY guard (``status == 'submitted'``)
    then matched 0 rows and took the idempotent-no-op hold FOREVER (compute has no reconcile/orphan-
    recovery backstop for an in-flight cloud_job). Post-fix ``dispatch`` upserts the row and PARKS the
    enqueue; the drain commits FIRST and only then flushes it, so the worker-visible job strictly follows
    its committed row. Because the enqueue is no longer in the transaction, a raising queue can NOT roll
    back the upsert -- ``dispatch`` itself never fires it, so it never raises here (mirrors
    ``test_kueue_dispatch_defers_enqueue_past_the_committed_row``).
    """
    from sqlalchemy import select

    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _compute(id="compute-a1")
    file = _make_file()
    session.add(file)
    await session.flush()
    # The real precondition: an AWAITING_CLOUD file already carries an ``awaiting`` cloud_job sidecar
    # row before the drain ever calls dispatch (Phase 77, D-04).
    await backends.hold_awaiting_cloud(session, file)
    await session.commit()

    # A router whose enqueue always raises: dispatch must NOT touch it (the enqueue is deferred), so
    # dispatch completes cleanly and stamps the SUBMITTED row + backend_id.
    router = _RaisingTaskRouter()
    await backend.dispatch(file, session, router)  # no raise: the enqueue is parked, not fired

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert job.status == CloudJobStatus.SUBMITTED.value
    assert job.backend_id == "compute-a1"
    assert await backend.in_flight_count(session) == 1  # the staged row holds its cap slot

    await session.commit()
    # Flushing a parked enqueue whose queue raises is best-effort: it fires 0, swallows the error (never
    # re-raises), and leaves the committed SUBMITTED row (no compute-lane reaper recovers it today --
    # tracked separately; the row is still durably correct, not phantom/limbo).
    assert await backends.flush_pending_push_file_enqueues(session) == 0
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id).execution_options(populate_existing=True))).scalar_one()
    assert job.status == CloudJobStatus.SUBMITTED.value  # unchanged by the failed flush


@pytest.mark.asyncio
async def test_compute_reconcile_is_a_noop_with_no_stranded_rows(session: AsyncSession) -> None:
    """phaze-j7m18: with nothing to reap, reconcile is STILL callback-driven -- it touches nothing.

    Compute terminalization stays PRIMARILY the ``/pushed``/``/mismatch``/``/failed`` callback path;
    ``reconcile`` (post phaze-j7m18) returns a tally dict (not ``None``, unlike ``LocalBackend``) but a
    tick with no age-stranded SUBMITTED row reaps nothing.
    """
    tally = await _compute().reconcile(session)
    assert tally == {"submitted_reaped": 0}


@pytest.mark.asyncio
async def test_compute_reconcile_reaps_stranded_submitted_row_and_frees_the_cap_slot(session: AsyncSession) -> None:
    """phaze-j7m18: a SUBMITTED row older than the bound (agent host gone) spills to awaiting + frees its slot.

    The bug: the fileserver agent host dies mid-rsync, so neither /pushed nor /mismatch nor /failed ever
    fires. Pre-fix, ComputeAgentBackend.reconcile was a documented no-op, so this row sat SUBMITTED
    forever while in_flight_count kept counting it -- a permanently leaked compute-lane cap slot.
    Post-fix the age-bounded reaper re-stamps it to 'awaiting' (OUT of IN_FLIGHT), so the count drops.
    """
    backend = _compute(id="compute-a1")
    file_id = await _seed_submitted_cloud_job(session, backend_id="compute-a1", age_sec=90_000)
    await _seed_push_file_ledger(session, file_id=file_id)

    assert await backend.in_flight_count(session) == 1  # the leaked slot, pre-reap

    tally = await backend.reconcile(session)

    assert tally == {"submitted_reaped": 1}
    row = await _cloud_job_for(session, file_id)
    assert row.status == CloudJobStatus.AWAITING.value  # spilled back onto the drain
    assert row.attempts == 1  # one re-drive attempt SPENT -> the loop is bounded
    assert await backend.in_flight_count(session) == 0  # the cap slot is released
    assert not await _ledger_row_exists(session, f"push_file:{file_id}")  # ledger row cleared


@pytest.mark.asyncio
async def test_compute_reconcile_never_reaps_a_submitted_row_younger_than_its_bound(session: AsyncSession) -> None:
    """phaze-j7m18 ACCEPTANCE: the callback path stays PRIMARY -- the reaper never fires inside the bound.

    A multi-GB rsync push over a slow link legitimately transfers for hours while bumping no timestamp,
    so a young SUBMITTED row must be left completely alone for the /pushed callback to terminalize.
    """
    backend = _compute(id="compute-a1")
    file_id = await _seed_submitted_cloud_job(session, backend_id="compute-a1", age_sec=3_600)

    tally = await backend.reconcile(session)

    assert tally == {"submitted_reaped": 0}
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.SUBMITTED.value
    assert await backend.in_flight_count(session) == 1  # the slot is still (legitimately) held


@pytest.mark.asyncio
async def test_compute_reap_scopes_to_own_backend_id(session: AsyncSession) -> None:
    """The reaper is backend_id-scoped like the rest of reconcile: a sibling backend's row is untouched."""
    other_fid = await _seed_submitted_cloud_job(session, backend_id="compute-other", age_sec=90_000)

    tally = await _compute(id="compute-a1").reconcile(session)

    assert tally == {"submitted_reaped": 0}
    assert (await _cloud_job_for(session, other_fid)).status == CloudJobStatus.SUBMITTED.value


@pytest.mark.asyncio
async def test_compute_reap_skips_a_row_whose_push_file_job_is_live_in_the_broker(session: AsyncSession) -> None:
    """phaze-j7m18 (mirrors phaze-31q3): an aged SUBMITTED row with a LIVE push_file broker key is NEVER reaped.

    The age bound alone cannot tell a lost callback from live work: updated_at bumps only at dispatch,
    so a multi-GB rsync push that legitimately transfers past the bound reads SUBMITTED with an old
    timestamp. If the broker still holds the push_file:<file_id> key queued/active, the callback path
    owns the row -- reaping it would abort a live transfer. The reaper must consult get_live_job_keys.
    """
    backend = _compute(id="compute-a1")
    file_id = await _seed_submitted_cloud_job(session, backend_id="compute-a1", age_sec=90_000)
    await _seed_live_saq_job(session, key=f"push_file:{file_id}", status="active")

    assert await backend.in_flight_count(session) == 1

    tally = await backend.reconcile(session)

    assert tally == {"submitted_reaped": 0}  # live broker key -> NOT reaped despite age
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.SUBMITTED.value
    assert await backend.in_flight_count(session) == 1  # slot still (legitimately) held by the live job


@pytest.mark.asyncio
async def test_compute_reap_still_fires_when_the_broker_key_is_terminal_not_live(session: AsyncSession) -> None:
    """phaze-j7m18: a FAILED/terminal push_file broker row does NOT protect the aged row (only live keys do)."""
    backend = _compute(id="compute-a1")
    file_id = await _seed_submitted_cloud_job(session, backend_id="compute-a1", age_sec=90_000)
    await _seed_live_saq_job(session, key=f"push_file:{file_id}", status="failed")  # terminal, NOT live

    tally = await backend.reconcile(session)

    assert tally == {"submitted_reaped": 1}  # terminal key is not live -> the lost row is reaped
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_compute_reap_loses_the_race_to_a_live_callback_and_takes_a_full_noop(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-j7m18 ACCEPTANCE: the happy-path callback WINS the race -- the reaper's CAS misses and it no-ops.

    Simulates /pushed landing between the reaper's read and its update: the row advances out of
    SUBMITTED, the expect_status-pinned CAS in hold_awaiting_cloud matches 0 rows, and the reaper takes a
    FULL no-op -- crucially no ledger clear, since the callback's own path already owns that cleanup.
    """
    from sqlalchemy import update as sa_update

    backend = _compute(id="compute-a1")
    file_id = await _seed_submitted_cloud_job(session, backend_id="compute-a1", age_sec=90_000)
    await _seed_push_file_ledger(session, file_id=file_id)

    real_hold = backends.hold_awaiting_cloud

    async def _callback_wins_first(*args: Any, **kwargs: Any) -> bool:
        # The "callback": advance the row out of SUBMITTED in a sibling transaction-visible write, then
        # let the REAL CAS run -- it now matches 0 rows exactly as it would in production.
        await session.execute(sa_update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.SUCCEEDED.value))
        return await real_hold(*args, **kwargs)

    monkeypatch.setattr(backends_compute_agent, "hold_awaiting_cloud", _callback_wins_first)

    tally = await backend.reconcile(session)

    assert tally == {"submitted_reaped": 0}  # the reaper lost the race and did nothing
    # The mocked "callback" advance shares the reaper's own transaction (test-only artifact), so the
    # reaper's own rollback on the lost CAS undoes it too -- mirrors the kueue twin's identical
    # `!= AWAITING` assertion rather than pinning the intermediate SUCCEEDED value.
    assert (await _cloud_job_for(session, file_id)).status != CloudJobStatus.AWAITING.value
    assert await _ledger_row_exists(session, f"push_file:{file_id}")  # NOT cleared -- the reaper no-op'd


@pytest.mark.asyncio
async def test_compute_reap_per_row_guard_survives_a_bad_row(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """One exploding row never aborts the sweep (per-row rollback guard, mirroring reconcile's)."""
    backend = _compute(id="compute-a1")
    await _seed_submitted_cloud_job(session, backend_id="compute-a1", age_sec=90_000)

    monkeypatch.setattr(backends_compute_agent, "hold_awaiting_cloud", AsyncMock(side_effect=RuntimeError("boom")))

    tally = await backend.reconcile(session)  # must NOT raise

    assert tally == {"submitted_reaped": 0}
