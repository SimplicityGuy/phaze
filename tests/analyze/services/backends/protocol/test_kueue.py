"""Tests for `services/backends/kueue.py` (split from test_backends.py, phaze-7l8jh).

`KueueBackend.is_available` / `in_flight_count` / `dispatch` / `reconcile` / its stranded-staging S3 reaper -- `services/backends/kueue.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.analyze.services.backends.protocol._shared import (
    Any,
    AsyncMock,
    CloudJob,
    CloudJobStatus,
    DedupFakeTaskRouter,
    _cloud_job_for,
    _kueue,
    _kueue_with_buckets,
    _make_file,
    _RaisingTaskRouter,
    _seed_cloud_job,
    _seed_live_saq_job,
    _seed_staging_cloud_job,
    _stub_kube_available,
    _stub_s3,
    backends,
    backends_kueue,
    kube_staging,
    pytest,
    s3_staging,
    seed_active_agent,
    uuid,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_kueue_is_available_probes_kube_with_no_compute_dependency(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """KueueBackend.is_available probes the LocalQueue and has NO compute-agent dependency (D-01a)."""
    _stub_kube_available(monkeypatch)
    # Deliberately NO compute agent online -- kueue must still report available.
    assert await _kueue().is_available(session) is True


@pytest.mark.asyncio
async def test_kueue_is_available_false_on_probe_error_never_raises(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A kube probe failure degrades to False, never propagates (returns bool, never raises)."""
    monkeypatch.setattr(kube_staging, "get_local_queue", AsyncMock(side_effect=RuntimeError("kube down")))
    assert await _kueue().is_available(session) is False


@pytest.mark.asyncio
async def test_kueue_in_flight_count_filters_by_backend_id(session: AsyncSession) -> None:
    """Kueue in_flight_count counts only its own backend_id rows in the in-flight set."""
    backend = _kueue(id="kueue-x64")
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.SUBMITTED)
    await _seed_cloud_job(session, backend_id="compute-a1", status=CloudJobStatus.RUNNING)  # other backend
    assert await backend.in_flight_count(session) == 2


@pytest.mark.asyncio
async def test_kueue_dispatch_stages_s3_and_upserts_uploading(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any) -> None:
    """Kueue dispatch runs the no-commit S3 core: cloud_job UPLOADING + a PARKED s3_upload enqueue, no commit."""
    from phaze.services import cloud_staging

    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.commit()

    router = DedupFakeTaskRouter()
    await backend.dispatch(file, session, router)

    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.status == CloudJobStatus.UPLOADING.value
    # phaze-grzo: the enqueue is PARKED until the row commits, not fired inline by dispatch.
    assert router.captures == []
    await session.commit()
    assert await cloud_staging.flush_pending_s3_enqueues(session) == 1
    assert [t for t, _ in router.queues["nox-io"].captured] == ["s3_upload"]


@pytest.mark.asyncio
async def test_kueue_dispatch_records_picked_staging_bucket_and_backend_id(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """MKUE-02/D-06: dispatch stamps staging_bucket == pick_bucket(file.id, sorted(config.buckets)) + backend_id.

    Over an N=2-bucket backend the recorded bucket is EXACTLY the deterministic pick over the sorted
    bound set, and backend_id is this backend's id -- both written in the same uncommitted session.
    """
    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    bucket_ids = ["staging-b", "staging-a"]  # unsorted on purpose -- pick_bucket sorts internally
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=bucket_ids, backend_id="kueue-x64")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.commit()

    await backend.dispatch(file, session, DedupFakeTaskRouter())

    from sqlalchemy import select

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one_or_none()
    assert job is not None
    assert job.backend_id == "kueue-x64"
    assert job.staging_bucket == s3_staging.pick_bucket(file.id, bucket_ids)  # authoritative D-06 pick


@pytest.mark.asyncio
async def test_kueue_dispatch_bucket_is_deterministic_per_file(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """D-06: the same file always lands on the same bucket; two files may land on different buckets.

    Determinism is proven by re-staging the SAME file (idempotent FK upsert) -- the recorded bucket is
    stable -- and by the pure ``pick_bucket`` mapping two distinct ids into the 2-bucket set (at least one
    of many random files lands on each member, so the set is genuinely partitioned, not collapsed to one).
    """
    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    bucket_ids = ["staging-a", "staging-b"]
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=bucket_ids, backend_id="kueue-x64")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.commit()

    from sqlalchemy import select

    await backend.dispatch(file, session, DedupFakeTaskRouter())
    first = (await session.execute(select(CloudJob.staging_bucket).where(CloudJob.file_id == file.id))).scalar_one()
    await backend.dispatch(file, session, DedupFakeTaskRouter())  # idempotent re-stage
    second = (await session.execute(select(CloudJob.staging_bucket).where(CloudJob.file_id == file.id))).scalar_one()
    assert first == second == s3_staging.pick_bucket(file.id, bucket_ids)  # same file -> same bucket

    # The 2-bucket set is genuinely partitioned across many files (not collapsed to a single member).
    landed = {s3_staging.pick_bucket(uuid.uuid4(), bucket_ids) for _ in range(200)}
    assert landed == set(bucket_ids)


@pytest.mark.asyncio
async def test_kueue_dispatch_no_fileserver_agent_leaves_file_untouched(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """CR-01 (gate-before-mutate): no fileserver agent -> dispatch raises, file stays AWAITING_CLOUD, no cloud_job.

    Regression for the pre-fix limbo bug: KueueBackend.dispatch used to flip ``file.state = PUSHING``
    UNCONDITIONALLY as its first statement, before ``_stage_file_to_s3`` gated on the fileserver agent.
    Under SQLAlchemy autoflush that pending PUSHING change was flushed as a side effect of the gate's
    SELECT, so a ``NoActiveAgentError`` (then a break-without-rollback in the drain) committed a PUSHING
    file with NO ``cloud_job`` row -- exactly the Pitfall-4 limbo the module docstring forbids. Post-fix
    the flip lands only AFTER ``_stage_file_to_s3`` returns, so the raising path is mutation-free: the
    file stays AWAITING_CLOUD and no cloud_job row exists even after the drain's post-loop commit.
    """
    from sqlalchemy import select

    from phaze.services.enqueue_router import NoActiveAgentError

    _stub_s3(monkeypatch)  # unreached: the fileserver gate raises before any S3 call
    # Deliberately NO fileserver agent seeded.
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.commit()
    file_id = file.id  # capture before expire_all() so the re-read query builds without a lazy load

    with pytest.raises(NoActiveAgentError):
        await backend.dispatch(file, session, DedupFakeTaskRouter())

    # Emulate the drain's single post-loop commit + a fresh DB read: no PUSHING flip may survive.
    await session.commit()
    session.expire_all()
    # Post-MIG-04 the atomicity guarantee is purely about the sidecar: a failed dispatch leaves NO cloud_job row.
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    assert job is None


@pytest.mark.asyncio
async def test_kueue_dispatch_defers_enqueue_past_the_committed_row(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """phaze-grzo: ``KueueBackend.dispatch`` no longer FIRES the ``s3_upload`` enqueue -- it PARKS it.

    Supersedes the phaze-uciu.3 SAVEPOINT twin: pre-grzo the S3-staging core enqueued ``s3_upload``
    inline (on SAQ's OWN psycopg pool, which commits the job durably + IMMEDIATELY), so a fast agent
    could dequeue and POST ``/uploaded`` before this asyncpg session committed the ``cloud_job``
    UPLOADING row -- ``report_uploaded`` then saw no UPLOADING row and no-op'd, stranding the file.
    Post-grzo ``dispatch`` upserts the row and PARKS the enqueue; the drain commits FIRST and only then
    flushes it, so the worker-visible job strictly follows its committed row. Because the enqueue is no
    longer in the transaction, a raising enqueue can NOT roll back the upsert -- ``dispatch`` itself
    never fires it, so it never raises here.
    """
    from sqlalchemy import select

    from phaze.services import cloud_staging

    _stub_s3(monkeypatch)
    await seed_active_agent(session, agent_id="nox", kind="fileserver")
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file = _make_file(file_type="flac")
    session.add(file)
    await session.flush()
    await backends.hold_awaiting_cloud(session, file)
    await session.commit()

    # A router whose enqueue always raises: dispatch must NOT touch it (the enqueue is deferred), so
    # dispatch completes cleanly and stamps the UPLOADING row + backend_id + staging_bucket.
    router = _RaisingTaskRouter()
    await backend.dispatch(file, session, router)  # no raise: the enqueue is parked, not fired

    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id))).scalar_one()
    assert job.status == CloudJobStatus.UPLOADING.value
    assert job.backend_id == "kueue-x64"
    assert job.staging_bucket == "staging-a"
    assert await backend.in_flight_count(session) == 1  # the staged row holds its cap slot

    await session.commit()
    # Flushing a parked enqueue whose queue raises is best-effort: it fires 0, swallows the error (never
    # re-raises), and leaves the committed UPLOADING row for the age-bounded staging reaper to spill back.
    assert await cloud_staging.flush_pending_s3_enqueues(session) == 0
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file.id).execution_options(populate_existing=True))).scalar_one()
    assert job.status == CloudJobStatus.UPLOADING.value  # still staged; the reaper (not a rollback) recovers it


@pytest.mark.asyncio
async def test_kueue_reconcile_reads_own_backend_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kueue.reconcile iterates its own {SUBMITTED, RUNNING} cloud_job rows and returns a per-backend tally."""
    _stub_kube_available(monkeypatch)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.SUBMITTED)
    # Phase 69 (SCHED-02/05): a backend_id-aware reconcile runs cleanly under the per-row advisory lock and
    # returns its tally (the cron aggregates it) rather than None.
    tally = await _kueue(id="kueue-x64").reconcile(session)
    assert tally is not None
    assert tally["reconciled"] == 1


@pytest.mark.asyncio
async def test_kueue_reconcile_scope_ignores_other_backend_rows(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """SCHED-05: KueueBackend.reconcile touches ONLY its own ``backend_id`` rows; a compute row stays untouched.

    Removing the cron's global un-scoped ``cloud_job`` query means a compute row is owned solely by its
    ``/pushed`` callback. Proven here: a kueue SUBMITTED row is reconciled (tally ``reconciled == 1``)
    while a sibling compute SUBMITTED row's status is byte-untouched by the kueue reconcile pass.
    """
    from sqlalchemy import select

    _stub_kube_available(monkeypatch)
    await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.SUBMITTED)
    compute_fid = await _seed_cloud_job(session, backend_id="compute-a1", status=CloudJobStatus.SUBMITTED)

    tally = await _kueue(id="kueue-x64").reconcile(session)

    assert tally is not None
    assert tally["reconciled"] == 1  # only the kueue-scoped row was reconciled
    session.expire_all()
    compute_row = (await session.execute(select(CloudJob).where(CloudJob.file_id == compute_fid))).scalar_one()
    assert compute_row.status == CloudJobStatus.SUBMITTED.value  # the compute row is left for its /pushed callback


@pytest.mark.asyncio
async def test_reconcile_releases_the_advisory_lock_on_a_row_deleted_mid_sweep(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-c1u7: a row removed by a concurrent ``delete_scan`` cascade between snapshot and re-read
    must roll back (releasing ``pg_advisory_xact_lock(5_000_504)``), not leak it past the row boundary.

    Mirrors ``_reap_stranded_staging``'s identical vanished-row skip. Pre-fix the reconcile loop's
    vanished-row branch was a bare ``continue`` with no rollback: the transaction that acquired the
    drain advisory lock at the top of the per-row unit stayed open, stalling every subsequent row's kube
    I/O (and the rest of the tick, if this was the last row) until some LATER commit or session close.
    The load-bearing assertion is that the session is NOT left inside an open transaction after the skip.
    """
    from sqlalchemy import delete, select

    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    fid = await _seed_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.SUBMITTED)
    row = (await session.execute(select(CloudJob).where(CloudJob.file_id == fid))).scalar_one()
    cloud_job_id = row.id

    real_get = session.get

    async def _delete_then_get(entity: Any, ident: Any, **kwargs: Any) -> Any:
        if ident == cloud_job_id:
            await session.execute(delete(CloudJob).where(CloudJob.id == cloud_job_id))
            session.expire_all()
        return await real_get(entity, ident, **kwargs)

    monkeypatch.setattr(session, "get", _delete_then_get)

    tally = await backend.reconcile(session)

    monkeypatch.undo()
    assert tally is not None
    assert tally["reconciled"] == 0  # the vanished row is skipped, never counted
    assert session.in_transaction() is False  # the skip released the lock rather than leaking it


# === models_pvc_name: optional per-Kueue-backend PVC mount knob (round-trip through TOML) ====


def test_kube_models_pvc_name_round_trips_from_backends_toml(backends_toml_env: Any) -> None:
    """An optional ``models_pvc_name`` in ``[backends.kube]`` parses and round-trips onto the resolved
    backend's KubeConfig (a plain PVC object name -- build_job_manifest mounts it read-only at /models)."""
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    # Baseline: the helper omits models_pvc_name, so the default is None (byte-identical-manifest path).
    assert backend.config.kube.models_pvc_name is None

    from phaze.config import ControlSettings

    backends_toml_env(
        """
        [[backends]]
        kind = "kueue"
        id = "kueue-x64"
        rank = 20
        cap = 5
        buckets = ["staging-a"]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"
        models_pvc_name = "phaze-essentia-models"

        [[buckets]]
        id = "staging-a"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-staging-a"
        """
    )
    settings = ControlSettings()
    [with_pvc] = [b for b in backends.resolve_backends(settings) if b.id == "kueue-x64"]
    assert with_pvc.config.kube.models_pvc_name == "phaze-essentia-models"


@pytest.mark.asyncio
async def test_reconcile_reaps_stranded_uploading_row_and_frees_the_cap_slot(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-ul2v: an UPLOADING row older than the bound (agent job gone) spills to awaiting + frees its slot.

    The bug: the fileserver agent dies mid-upload, so neither ``/uploaded`` nor ``/failed`` ever fires.
    Pre-fix, reconcile selected only {SUBMITTED, RUNNING}, so this row sat UPLOADING forever while
    ``in_flight_count`` kept counting it -- a permanently leaked burst-lane cap slot. Post-fix the age-bounded
    reaper re-stamps it to ``'awaiting'`` (deliberately OUT of IN_FLIGHT), so the count drops to 0.
    """
    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING, age_sec=90_000)

    assert await backend.in_flight_count(session) == 1  # the leaked slot, pre-reap

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 1
    row = await _cloud_job_for(session, file_id)
    assert row.status == CloudJobStatus.AWAITING.value  # spilled back onto the drain
    assert row.cloud_phase is None  # cleared off the "Running" tile (D-12)
    assert row.attempts == 1  # one re-drive attempt SPENT -> the loop is bounded
    assert await backend.in_flight_count(session) == 0  # the cap slot is released


@pytest.mark.asyncio
async def test_reconcile_reaps_stranded_uploaded_row_with_no_submit(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-ul2v: an UPLOADED row whose ``submit_cloud_job`` was lost also spills within its (tighter) bound.

    ``report_uploaded`` enqueues the submit in the same transaction that flips UPLOADED, so an UPLOADED row
    should reach SUBMITTED within one controller hop. A row still UPLOADED long after that means the submit
    was lost -- and, pre-fix, it held a cap slot forever exactly like the UPLOADING case.
    """
    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    # 3600s: past the 900s UPLOADED bound but well UNDER the 21600s UPLOADING bound -- so this cell also
    # proves the two bounds are read per-status, not shared.
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADED, age_sec=3_600)

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 1
    row = await _cloud_job_for(session, file_id)
    assert row.status == CloudJobStatus.AWAITING.value
    assert await backend.in_flight_count(session) == 0


@pytest.mark.asyncio
async def test_reconcile_never_reaps_a_staging_row_younger_than_its_bound(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-ul2v ACCEPTANCE: the callback path stays PRIMARY -- the reaper never fires inside the bound.

    A multi-GB multipart upload legitimately transfers for hours while bumping no timestamp, and an
    UPLOADED row is normally submitted within seconds. Both young rows here MUST be left completely alone
    so the in-flight agent callback is the one that terminalizes them.
    """
    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    uploading_fid = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING, age_sec=3_600)
    uploaded_fid = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADED, age_sec=60)

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 0  # nothing reaped
    assert (await _cloud_job_for(session, uploading_fid)).status == CloudJobStatus.UPLOADING.value
    assert (await _cloud_job_for(session, uploaded_fid)).status == CloudJobStatus.UPLOADED.value
    assert await backend.in_flight_count(session) == 2  # both still hold their slots, as they should


@pytest.mark.asyncio
async def test_reap_scopes_to_own_backend_id(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reaper is backend_id-scoped like the rest of reconcile: a sibling backend's row is untouched."""
    _stub_kube_available(monkeypatch)
    other_fid = await _seed_staging_cloud_job(session, backend_id="kueue-other", status=CloudJobStatus.UPLOADING, age_sec=90_000)

    tally = await _kueue(id="kueue-x64").reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 0
    assert (await _cloud_job_for(session, other_fid)).status == CloudJobStatus.UPLOADING.value


@pytest.mark.asyncio
async def test_reap_cleans_up_the_staged_s3_object_and_multipart(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """A reaped row with a RECORDED staging bucket aborts its multipart + deletes the object (KSTAGE-04).

    Mirrors ``report_upload_failed``'s over-cap spill: the spill re-stages from scratch, so leaving the
    half-finished multipart and the staged object behind would leak both.
    """
    _stub_kube_available(monkeypatch)
    abort = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file_id = await _seed_staging_cloud_job(
        session,
        backend_id="kueue-x64",
        status=CloudJobStatus.UPLOADING,
        age_sec=90_000,
        staging_bucket="staging-a",
        upload_id="upload-xyz",
    )

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 1
    assert abort.await_count == 1
    assert delete.await_count == 1
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_reap_loses_the_race_to_a_live_callback_and_takes_a_full_noop(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """phaze-ul2v ACCEPTANCE: the happy-path callback WINS the race -- the reaper's CAS misses and it no-ops.

    Simulates ``report_uploaded`` landing between the reaper's read and its update: the row advances out of
    the OBSERVED status, the ``expect_status``-pinned CAS in ``hold_awaiting_cloud`` matches 0 rows, and the
    reaper must take a FULL no-op -- crucially NO S3 cleanup, since the callback's burst now owns the object.
    """
    from sqlalchemy import update as sa_update

    _stub_kube_available(monkeypatch)
    abort = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file_id = await _seed_staging_cloud_job(
        session,
        backend_id="kueue-x64",
        status=CloudJobStatus.UPLOADING,
        age_sec=90_000,
        staging_bucket="staging-a",
        upload_id="upload-xyz",
    )

    real_hold = backends.hold_awaiting_cloud

    async def _callback_wins_first(*args: Any, **kwargs: Any) -> bool:
        # The "callback": advance the row out of UPLOADING in a sibling transaction-visible write, then
        # let the REAL CAS run -- it now matches 0 rows exactly as it would in production.
        await session.execute(sa_update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.SUBMITTED.value))
        return await real_hold(*args, **kwargs)

    monkeypatch.setattr(backends_kueue, "hold_awaiting_cloud", _callback_wins_first)

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 0  # the reaper lost the race and did nothing
    assert abort.await_count == 0  # FULL no-op: no cleanup of an object the live burst owns
    assert delete.await_count == 0
    assert (await _cloud_job_for(session, file_id)).status != CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_reap_per_row_guard_survives_a_bad_row(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """One exploding row never aborts the sweep (per-row rollback guard, mirroring reconcile's)."""
    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING, age_sec=90_000)

    monkeypatch.setattr(backends_kueue, "hold_awaiting_cloud", AsyncMock(side_effect=RuntimeError("boom")))

    tally = await backend.reconcile(session)  # must NOT raise

    assert tally is not None
    assert tally["staging_reaped"] == 0


@pytest.mark.asyncio
async def test_reap_skips_a_row_that_left_staging_between_snapshot_and_reread(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row terminalized by a callback AFTER the sweep's snapshot is re-read fresh and skipped.

    The sweep captures primitive ids up front, then re-reads each row inside the loop precisely so a
    callback landing mid-sweep is honoured. Here the row advances to SUCCEEDED between snapshot and
    re-read: the reaper must drop it without ever reaching the CAS.
    """
    from sqlalchemy import update as sa_update

    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING, age_sec=90_000)

    real_get = session.get

    async def _advance_then_get(entity: Any, ident: Any, **kwargs: Any) -> Any:
        await session.execute(sa_update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.SUCCEEDED.value))
        session.expire_all()
        return await real_get(entity, ident, **kwargs)

    monkeypatch.setattr(session, "get", _advance_then_get)

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 0
    monkeypatch.undo()
    # The skip branch rolls back (releasing the xact lock), which also undoes this cell's simulated
    # mid-sweep advance -- the load-bearing assertion is that the reaper NEVER spilled the row.
    assert (await _cloud_job_for(session, file_id)).status != CloudJobStatus.AWAITING.value


# === phaze-7lpb: the per-row re-read is FRESH under the lock (populate_existing), not the cached snapshot ==


@pytest.mark.asyncio
async def test_reap_re_reads_fresh_under_the_lock_and_honors_a_mid_sweep_restamp(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-7lpb: the per-row ``session.get`` re-reads FRESH under the lock (populate_existing), not the cached snapshot.

    The snapshot ``select`` populates the identity map and the sessionmaker is ``expire_on_commit=False``, so a
    plain ``get`` would return the sweep-start-stale cached row. Here a re-drive lands between snapshot and
    re-read, re-stamping the row back into the SAME status with a FRESH (young) ``updated_at`` (phaze-2hv9).
    The fresh read must surface that young timestamp so the age check fails and the LIVE re-driven upload is
    left alone. Pre-fix (cached read) the stale old timestamp passed the age check and the row was reaped.
    """
    from sqlalchemy import text as sa_text

    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING, age_sec=90_000)

    real_get = session.get

    async def _restage_then_get(entity: Any, ident: Any, **kwargs: Any) -> Any:
        # Simulate a concurrent redrive re-stamping updated_at to now WITHOUT changing status (UPLOADING).
        # A RAW text UPDATE (not an ORM ``update()`` with synchronize_session='fetch') so it does NOT expire
        # the reaper's snapshot-cached row: only ``populate_existing=True`` on the get can surface the fresh
        # timestamp. If the reaper used a plain ``get``, it would keep reading the stale (aged) cached row.
        await session.execute(sa_text("UPDATE cloud_job SET updated_at = now() WHERE file_id = :fid"), {"fid": file_id})
        return await real_get(entity, ident, **kwargs)

    monkeypatch.setattr(session, "get", _restage_then_get)

    tally = await backend.reconcile(session)

    assert tally is not None
    # populate_existing surfaced the fresh (young) updated_at -> the age check fails -> NOT reaped.
    assert tally["staging_reaped"] == 0
    monkeypatch.undo()
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.UPLOADING.value


@pytest.mark.asyncio
async def test_reap_skips_a_row_whose_s3_upload_job_is_live_in_the_broker(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-31q3: an aged UPLOADING row with a LIVE ``s3_upload`` broker key is NEVER reaped.

    The age bound alone cannot tell a lost callback from live work: ``updated_at`` bumps only at
    dispatch/re-stage, so a multi-GB upload that legitimately transfers past the bound (or a job still
    queued in the io-lane backlog) reads UPLOADING with an old timestamp. If the broker still holds the
    ``s3_upload:<file_id>`` key queued/active, the callback path owns the row -- reaping it would abort a
    live multipart mid-transfer. The reaper must consult :func:`get_live_job_keys` and skip.
    """
    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING, age_sec=90_000)
    await _seed_live_saq_job(session, key=f"s3_upload:{file_id}", status="active")

    assert await backend.in_flight_count(session) == 1

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 0  # live broker key -> NOT reaped despite age
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.UPLOADING.value
    assert await backend.in_flight_count(session) == 1  # slot still (legitimately) held by the live job


@pytest.mark.asyncio
async def test_reap_still_fires_when_the_broker_key_is_terminal_not_live(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-31q3: a FAILED/terminal ``s3_upload`` broker row does NOT protect the aged row (only live keys do).

    ``get_live_job_keys`` returns only ``queued``/``active`` keys. A row whose job settled ``failed`` (the
    retries=0 terminal state) is genuinely lost, so the reaper's safety net must still fire.
    """
    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADING, age_sec=90_000)
    await _seed_live_saq_job(session, key=f"s3_upload:{file_id}", status="failed")  # terminal, NOT live

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 1  # terminal key is not live -> the lost row is reaped
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.AWAITING.value


@pytest.mark.asyncio
async def test_reap_skips_an_uploaded_row_whose_submit_job_is_live_in_the_broker(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-1k0i: an aged UPLOADED row with a LIVE ``submit_cloud_job`` broker key is NEVER reaped.

    ``report_uploaded`` flips UPLOADING -> UPLOADED and enqueues ``submit_cloud_job:<file_id>`` in the SAME
    transaction; the completed ``s3_upload`` job's key is swept from ``saq_jobs``. So for an UPLOADED row the
    live owner the phaze-31q3 gate must consult is the SUBMIT key, not the (already-gone) upload key -- a
    ``submit_cloud_job`` stuck behind a controller-queue backlog reads UPLOADED with a frozen ``updated_at``
    and must not be reaped out from under it.
    """
    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADED, age_sec=3_600)
    await _seed_live_saq_job(session, key=f"submit_cloud_job:{file_id}", status="queued")

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 0  # live submit key -> NOT reaped despite age
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.UPLOADED.value


@pytest.mark.asyncio
async def test_reap_still_fires_on_an_uploaded_row_with_a_live_but_irrelevant_s3_upload_key(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """phaze-1k0i: a live ``s3_upload`` broker key does NOT protect an UPLOADED row (wrong key for this status).

    Pre-fix the gate checked ONLY ``s3_upload:<file_id>`` regardless of status, so a stale/unrelated
    ``s3_upload`` entry (e.g. left behind by an unrelated retry bookkeeping quirk) would have falsely shielded
    an UPLOADED row whose actual owner (``submit_cloud_job``) is genuinely gone. The gate must key off the
    OBSERVED status, not a single hardcoded key.
    """
    _stub_kube_available(monkeypatch)
    backend = _kueue(id="kueue-x64")
    file_id = await _seed_staging_cloud_job(session, backend_id="kueue-x64", status=CloudJobStatus.UPLOADED, age_sec=3_600)
    await _seed_live_saq_job(session, key=f"s3_upload:{file_id}", status="active")  # wrong key for UPLOADED

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 1  # s3_upload key is irrelevant once UPLOADED -> genuinely lost, reaped
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.AWAITING.value


# === phaze-jwz0: the reaper commits the spill BEFORE the S3 cleanup (outside the txn/lock) ============


@pytest.mark.asyncio
async def test_reap_commits_the_spill_before_s3_io_so_a_failing_bucket_never_undoes_it(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """phaze-jwz0: the CAS spill + ledger clear COMMIT before the S3 cleanup, which runs outside the txn/lock.

    Pre-fix, ``abort_multipart_upload`` / ``delete_staged_object`` ran INSIDE the still-open transaction under
    the global drain lock (wedging every drain tick behind a hung bucket) and BEFORE the commit -- so an S3
    raise hit the per-row ``except`` and rolled the spill back. Post-fix the spill is durable first; a failing
    (or hung) bucket only leaks the old object (idempotently re-cleaned on a later pass), never un-spills.
    """
    _stub_kube_available(monkeypatch)
    abort = AsyncMock(side_effect=RuntimeError("bucket unreachable"))
    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file_id = await _seed_staging_cloud_job(
        session,
        backend_id="kueue-x64",
        status=CloudJobStatus.UPLOADING,
        age_sec=90_000,
        staging_bucket="staging-a",
        upload_id="upload-xyz",
    )

    tally = await backend.reconcile(session)  # must NOT raise despite the failing abort

    assert tally is not None
    assert tally["staging_reaped"] == 1  # the spill is counted -- it committed BEFORE the S3 failure
    assert abort.await_count == 1  # cleanup was attempted (post-commit)
    assert delete.await_count == 0  # abort raised first, so delete was skipped (both idempotent, retried later)
    # The load-bearing assertion: the failing bucket did NOT roll the durable spill back.
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.AWAITING.value
    assert await backend.in_flight_count(session) == 0  # the cap slot is genuinely released


# === phaze-wa9x: the post-commit delete is generation-safe against a concurrent re-dispatch =============


@pytest.mark.asyncio
async def test_reap_skips_delete_when_a_new_staging_cycle_claims_the_key_before_it_runs(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """phaze-wa9x: a re-dispatch that claims the SAME bucket/key between commit and delete must not be deleted.

    ``delete_staged_object`` is keyed only by file_id; bucket and object key are deterministic and identical
    across staging generations of the same file, so once the row is 'awaiting' a concurrent drain tick can
    stage a FRESH object at the same key while a stalled delete is still in flight. Simulate the race inside
    the (idempotent) ``abort_multipart_upload`` call: a concurrent cycle re-stages the file (fresh upload_id,
    back to UPLOADING) before the reaper's delete runs. The re-read-before-delete guard must see the row is
    no longer 'awaiting' with the observed upload_id and skip the delete entirely.
    """
    from sqlalchemy import update as sa_update

    _stub_kube_available(monkeypatch)
    file_id = await _seed_staging_cloud_job(
        session,
        backend_id="kueue-x64",
        status=CloudJobStatus.UPLOADING,
        age_sec=90_000,
        staging_bucket="staging-a",
        upload_id="upload-old",
    )

    async def _restage_during_abort(*_args: Any, **_kwargs: Any) -> None:
        # Simulate a concurrent stage cycle claiming the row: fresh upload_id, back to UPLOADING.
        await session.execute(
            sa_update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.UPLOADING.value, upload_id="upload-new")
        )
        await session.commit()

    abort = AsyncMock(side_effect=_restage_during_abort)
    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 1  # the spill itself is unaffected -- it already committed
    assert abort.await_count == 1
    assert delete.await_count == 0  # the fresh cycle's object must NOT be deleted
    row = await _cloud_job_for(session, file_id)
    assert row.status == CloudJobStatus.UPLOADING.value  # the new cycle's row survives untouched
    assert row.upload_id == "upload-new"


@pytest.mark.asyncio
async def test_reap_still_deletes_when_no_new_cycle_has_claimed_the_key(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, backends_toml_env: Any
) -> None:
    """phaze-wa9x: the re-read guard is not a regression -- an untouched 'awaiting' row still gets cleaned up.

    Companion to the race test: when nothing claims the row between commit and delete (the common case),
    the re-read observes the SAME status/upload_id the reaper itself just wrote and the delete proceeds.
    """
    _stub_kube_available(monkeypatch)
    abort = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(s3_staging, "abort_multipart_upload", abort)
    monkeypatch.setattr(s3_staging, "delete_staged_object", delete)
    backend = _kueue_with_buckets(backends_toml_env, bucket_ids=["staging-a"], backend_id="kueue-x64")
    file_id = await _seed_staging_cloud_job(
        session,
        backend_id="kueue-x64",
        status=CloudJobStatus.UPLOADING,
        age_sec=90_000,
        staging_bucket="staging-a",
        upload_id="upload-only",
    )

    tally = await backend.reconcile(session)

    assert tally is not None
    assert tally["staging_reaped"] == 1
    assert abort.await_count == 1
    assert delete.await_count == 1  # no race -> the (idempotent) cleanup still runs
    assert (await _cloud_job_for(session, file_id)).status == CloudJobStatus.AWAITING.value
