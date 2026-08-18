"""Controller-side tests for `routers/pipeline/recovery.py` (split from test_pipeline.py, phaze-7l8jh).

POST /pipeline/recover, GET /pipeline/recover/status, and `_run_recovery` -- `routers/pipeline/recovery.py`.
"""

from __future__ import annotations

from tests.shared.routers.pipeline._shared import *


# ---------------------------------------------------------------------------
# Phase 42 (REQ-42-1/REQ-42-4/REQ-42-5): the manual /pipeline/recover endpoint calls the
# SAME gated recover_orphaned_work producer (force=True) the controller startup runs, on a
# worker-shaped ctx built from app state; the global DAG "Recover" button renders end-to-end.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_invokes_recover_orphaned_work_forced(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /pipeline/recover schedules recover_orphaned_work(force=True) on a worker-shaped ctx.

    The endpoint must call the SAME producer as controller startup (D-03 — manual and automatic
    paths cannot drift), forced (D-05 cold-boot safety net), with a ctx wired from app state: the
    lifespan ``controller_queue`` (controller stages) + ``task_router`` (per-agent stages) + the
    module-level ``async_session`` sessionmaker. The producer is patched so no real DB/queue work
    runs — we only assert the wiring and force flag.
    """
    # phaze-oau1o: `routers/pipeline.py` is now a package; `recover_orphaned_work` is read from the
    # `recovery` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.recovery as pipeline_mod

    captured: dict[str, object] = {}

    async def fake_recover(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        captured["ctx"] = ctx
        captured["force"] = force
        return {"detected_loss": True, "forced": force, "stages": {}}

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", fake_recover)
    controller_queue, task_router = install_fake_queues(client)

    response = await client.post("/pipeline/recover")
    assert response.status_code == 200
    assert "Recovery started" in response.text
    assert "nothing will double-enqueue" in response.text

    await drain_router_background_tasks()
    assert captured["force"] is True, "manual Recover must force=True (bypass the no-op detect gate, not the dedup)"
    ctx = captured["ctx"]
    assert isinstance(ctx, dict)
    assert ctx["queue"] is controller_queue, "ctx['queue'] must be the lifespan controller queue (controller stages)"
    assert ctx["task_router"] is task_router, "ctx['task_router'] must be the lifespan task_router (per-agent stages)"
    assert "async_session" in ctx, "ctx must carry the async_session sessionmaker for the worker-shaped recovery"


@pytest.mark.asyncio
async def test_recover_returns_200_when_producer_raises_is_isolated(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing background recovery never reaches the HTTP response — the endpoint still returns 200.

    The producer runs fire-and-forget in a background task, so even a raising recover_orphaned_work
    cannot 500 the request (T-42-06): the operator always gets the "recovery started" fragment.
    """
    # phaze-oau1o: `routers/pipeline.py` is now a package; `recover_orphaned_work` is read from the
    # `recovery` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.recovery as pipeline_mod

    async def boom(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        raise RuntimeError("recovery boom")

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", boom)
    install_fake_queues(client)

    response = await client.post("/pipeline/recover")
    assert response.status_code == 200
    assert "Recovery started" in response.text

    await drain_router_background_tasks()


@pytest.mark.asyncio
async def test_run_recovery_logs_producer_exception_instead_of_letting_it_vanish(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_run_recovery logs a failing producer instead of leaving it an unretrieved task exception (phaze-o1xx).

    Pre-fix, ``_run_recovery`` awaited ``recover_orphaned_work`` with no try/except, and the
    fire-and-forget ``asyncio.create_task``'s only done-callback discarded it from
    ``_background_tasks`` -- so a genuine forced-recovery failure (as opposed to the isolated
    per-row failures ``recover_orphaned_work`` itself now tallies) surfaced nowhere the operator or
    an on-call engineer could see except asyncio's default "Task exception was never retrieved" at
    GC. This pins that ``_run_recovery`` itself never raises and DOES log.
    """
    # phaze-oau1o: `routers/pipeline.py` is now a package; `recover_orphaned_work` is read from the
    # `recovery` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.recovery as pipeline_mod

    async def boom(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        raise RuntimeError("recovery boom")

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", boom)

    with caplog.at_level("ERROR", logger="phaze.routers.pipeline"):
        await pipeline_mod._run_recovery({})  # never raises -- the whole point of the fix

    assert "manual recovery trigger failed" in caplog.text


@pytest.mark.asyncio
async def test_run_recovery_logs_the_final_tally_on_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_run_recovery logs the producer's return value on the happy path (visibility parity with startup)."""
    # phaze-oau1o: `routers/pipeline.py` is now a package; `recover_orphaned_work` is read from the
    # `recovery` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.recovery as pipeline_mod

    async def fake_recover(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        return {"detected_loss": True, "forced": force, "stages": {"process_file": {"reenqueued": 3, "skipped": 1, "errored": 0, "unreplayable": 0}}}

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", fake_recover)

    with caplog.at_level("INFO", logger="phaze.routers.pipeline"):
        await pipeline_mod._run_recovery({})

    assert "manual recovery trigger complete" in caplog.text


@pytest.mark.asyncio
async def test_recover_response_polls_for_the_final_outcome(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The POST fragment carries the poll that will replace it with the real tally."""
    # phaze-oau1o: `routers/pipeline.py` is now a package; `recover_orphaned_work` is read from the
    # `recovery` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.recovery as pipeline_mod

    async def fake_recover(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        return _recovery_result()

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", fake_recover)
    install_fake_queues(client)

    response = await client.post("/pipeline/recover")
    assert response.status_code == 200
    assert 'hx-get="/pipeline/recover/status"' in response.text
    await drain_router_background_tasks()


@pytest.mark.asyncio
async def test_recover_status_does_not_read_as_success_when_a_stage_was_skipped(client: AsyncClient) -> None:
    """THE bead's operator assertion: an unreplayable stage must not render the success copy.

    Not "the same words plus a footnote" -- a different message. The operator pressing Recover during
    an incident has to learn, without reading controller logs, that some of the work they asked to be
    recovered was deliberately left alone and by which stage.
    """
    _set_recovery_state(result=_recovery_result(s3_upload={"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 430}))

    response = await client.get("/pipeline/recover/status")
    assert response.status_code == 200
    body = response.text

    assert "Recovery started — re-enqueuing any orphaned work across all stages." not in body
    assert "Recovery complete" not in body
    assert "430" in body
    assert "could NOT be recovered" in body
    assert "s3_upload" in body, "the operator must be told WHICH stage was skipped"


@pytest.mark.asyncio
async def test_recover_status_reads_as_success_when_everything_recovered(client: AsyncClient) -> None:
    """The clean run keeps its plain success copy -- the warning branch must not fire on zero skips."""
    _set_recovery_state(result=_recovery_result(process_file={"reenqueued": 2512, "skipped": 3, "errored": 0, "unreplayable": 0}))

    body = (await client.get("/pipeline/recover/status")).text
    assert "Recovery complete" in body
    assert "2512" in body
    assert "could NOT be recovered" not in body


@pytest.mark.asyncio
async def test_recover_status_keeps_polling_while_the_run_is_in_flight(client: AsyncClient) -> None:
    """While running, the fragment re-arms its own poll; every terminal branch drops it."""
    _set_recovery_state(running=True)
    running_body = (await client.get("/pipeline/recover/status")).text
    assert 'hx-get="/pipeline/recover/status"' in running_body

    _set_recovery_state(result=_recovery_result(process_file={"reenqueued": 1, "skipped": 0, "errored": 0, "unreplayable": 0}))
    done_body = (await client.get("/pipeline/recover/status")).text
    assert 'hx-get="/pipeline/recover/status"' not in done_body, "a settled fragment must stop polling"


@pytest.mark.asyncio
async def test_recover_status_surfaces_a_failed_run(client: AsyncClient) -> None:
    """A recovery that RAISED is reported as failed, not as started and not as complete."""
    _set_recovery_state(failed=True)
    body = (await client.get("/pipeline/recover/status")).text
    assert "Recovery failed" in body
    assert "Recovery complete" not in body


@pytest.mark.asyncio
async def test_recover_status_before_any_run(client: AsyncClient) -> None:
    """A direct hit with no recovery in this process says so rather than inventing a tally."""
    _set_recovery_state()
    body = (await client.get("/pipeline/recover/status")).text
    assert "No recovery has been run" in body


@pytest.mark.asyncio
async def test_run_recovery_publishes_the_tally_for_the_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_run_recovery`` publishes the producer's result -- the controller log is no longer the only surface."""
    # phaze-oau1o: `routers/pipeline.py` is now a package; `recover_orphaned_work` is read from the
    # `recovery` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.recovery as pipeline_mod

    result = _recovery_result(s3_upload={"reenqueued": 0, "skipped": 0, "errored": 0, "unreplayable": 2})

    async def fake_recover(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        return result

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", fake_recover)
    _set_recovery_state(running=True)

    await pipeline_mod._run_recovery({})

    assert pipeline_mod._recovery_state["running"] is False
    assert pipeline_mod._recovery_state["failed"] is False
    assert pipeline_mod._recovery_state["result"] is result


@pytest.mark.asyncio
async def test_run_recovery_clears_running_when_the_producer_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed run must settle the fragment, not leave it polling forever on a dead task."""
    # phaze-oau1o: `routers/pipeline.py` is now a package; `recover_orphaned_work` is read from the
    # `recovery` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.recovery as pipeline_mod

    async def boom(ctx: dict[str, object], *, force: bool = False) -> dict[str, object]:
        raise RuntimeError("recovery boom")

    monkeypatch.setattr(pipeline_mod, "recover_orphaned_work", boom)
    _set_recovery_state(running=True)

    await pipeline_mod._run_recovery({})

    assert pipeline_mod._recovery_state["running"] is False
    assert pipeline_mod._recovery_state["failed"] is True
