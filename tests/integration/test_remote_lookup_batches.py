"""Synthetic durable workflow through real Postgres, registry, transport and review persistence."""

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlsplit
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from phaze.models.remote_lookup_batch import RemoteLookupMembership, RemoteLookupRead, RemoteLookupWork
from phaze.models.remote_transport import RemoteTransportAttempt, RemoteTransportLedger
from phaze.models.runtime_config_override import RuntimeConfigOverride
from phaze.routers.remote_settings import router
from phaze.schemas.remote_lookup import LookupCommand
from phaze.services.remote_lookup_batches import batch_status, create_batch, dispatch_pending, execute_work, expand_batch
from phaze.services.remote_policy import ProviderPolicy
from phaze.services.remote_providers import RemoteProviderBinding
from phaze.tracklist_providers.domain import Completeness, DiscoveryOutcome, OutcomeStatus
from phaze.tracklist_providers.host_profile import RemoteSearchRoute
from tests.integration.test_remote_lookup import (
    EXAMPLE,
    EXAMPLE_PROFILE,
    ExampleCatalog,
    ExampleUpstream,
    count,
    enabled,
    harness,
    seed,
)


pytestmark = pytest.mark.integration


def policy(enabled_value: bool = True, interval: float = 6.0, budget: int = 16) -> ProviderPolicy:
    return ProviderPolicy(enabled=enabled_value, min_interval_seconds=interval, max_requests_per_run=budget)


async def test_policy_is_live_across_process_compositions_and_invalid_change_is_atomic(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as first, harness(committed_db, ExampleUpstream().routes()) as second:
        media = await seed(first.sessions)
        await first.lookup.policy.set(EXAMPLE, policy(False), actor="admin")
        assert not (await second.lookup.provider_status())[0].enabled
        result = await second.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert result.outcome.code == "disabled_provider" and not second.socket.requests
        with pytest.raises(ValueError, match="interval"):
            await first.lookup.policy.set(EXAMPLE, policy(True, 0.1), actor="admin")
        assert not (await first.lookup.provider_status())[0].enabled
        with pytest.raises(ValueError, match="Unknown"):
            await first.lookup.policy.set("unknown", policy(), actor="admin")
        await first.lookup.policy.set(EXAMPLE, policy(True, 12.0, 3), actor="admin")
        async with first.sessions() as session, session.begin():
            session.add(RuntimeConfigOverride(key="provider_unknown_policy", value=policy().model_dump()))
        assert (await second.lookup.provider_status())[0].min_interval_seconds == 6.0, (
            "invalid full snapshot retains second process's last-good snapshot"
        )


async def test_expansion_beyond_render_page_restart_and_concurrent_dedup(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        for _ in range(105):
            await seed(h.sessions)
        first, second = await asyncio.gather(create_batch(h.sessions, h.lookup), create_batch(h.sessions, h.lookup))
        assert not await expand_batch(h.sessions, first)
        assert await expand_batch(h.sessions, first)
        assert not await expand_batch(h.sessions, second)
        assert await expand_batch(h.sessions, second)
        assert await count(h.sessions, RemoteLookupWork) == 105
        assert await count(h.sessions, RemoteLookupMembership) == 210
        one, two = await batch_status(h.sessions, first), await batch_status(h.sessions, second)
        assert one["batch"].selected == 105 and two["batch"].existing == 105
        assert len(one["work"]) == 50 and sum(one["counts"].values()) == 105
        assert not h.socket.requests


class TwoPages(ExampleCatalog):
    async def discover(self, context: Any, budget: Any, cursor: Any, effects: Any) -> DiscoveryOutcome:
        del cursor
        for page in (1, 2):
            source = effects.minted[0]
            source = source.model_copy(update={"request": source.request.model_copy(update={"page": page})})
            read = await effects.read(source, budget)
            if read.status != OutcomeStatus.FOUND:
                return DiscoveryOutcome(status=read.status, code=read.code, scope=context.recording_id, evidence=read.evidence)
        return DiscoveryOutcome(
            status="absent", code="search_exhausted", scope=context.recording_id, completeness=Completeness(state="complete", reason="two pages read")
        )


async def test_durable_continuation_replays_page_one_and_preserves_budget_across_restart(committed_db: Any) -> None:
    profile = EXAMPLE_PROFILE.model_copy(
        update={
            "search": RemoteSearchRoute(
                fields=frozenset({"artist", "date"}),
                format="example_search_json",
                path="/search",
                term_parameters=(("artist", "q"), ("date", "year")),
                max_page=2,
                page_parameter="page",
            )
        }
    )
    binding = RemoteProviderBinding(TwoPages(), profile)
    async with harness(committed_db, ExampleUpstream().routes(), bindings=(binding,)) as h:
        await seed(h.sessions)
        batch = await create_batch(h.sessions, h.lookup)
        await expand_batch(h.sessions, batch)
        async with h.sessions() as session:
            work_id = await session.scalar(select(RemoteLookupWork.id))
        assert await execute_work(h.sessions, h.lookup, work_id) == "deferred"
        await h.clock.sleep(6)
        assert await execute_work(h.sessions, h.lookup, work_id) == "deferred"
        assert len(h.socket.requests) == 1
        await h.lookup.policy.set(EXAMPLE, policy(True, budget=1), actor="admin")
        await h.clock.sleep(6)
        assert await execute_work(h.sessions, h.lookup, work_id) == "blocked"
        assert len(h.socket.requests) == 1
    async with harness(committed_db, ExampleUpstream().routes(), bindings=(binding,)) as restarted:
        await restarted.clock.sleep(18)
        await restarted.lookup.policy.set(EXAMPLE, policy(True, budget=2), actor="admin")
        assert await execute_work(restarted.sessions, restarted.lookup, work_id) == "completed"
        pages = [parse_qs(urlsplit(request.url).query)["page"] for request in restarted.socket.requests]
        assert pages == [["2"]]
        assert await count(restarted.sessions, RemoteTransportAttempt, RemoteTransportAttempt.run_id == work_id) == 2
        assert await count(restarted.sessions, RemoteLookupRead) == 2


async def test_disable_during_inflight_finishes_sent_request_and_stops_next_send(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)

        async def disable() -> None:
            await h.lookup.policy.set(EXAMPLE, policy(False), actor="admin")

        h.socket.during_send = disable
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert result.candidates and len(h.socket.requests) == 1
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert result.outcome.code == "disabled_provider" and len(h.socket.requests) == 1
        assert await count(h.sessions, RemoteTransportAttempt) == 1


async def test_settings_get_no_requests_global_forms_and_durable_empty_batch(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes(), settings=enabled()) as h:
        app = FastAPI()
        app.state.remote_lookup = h.lookup
        app.include_router(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.get("/admin/remote-providers")
            assert response.status_code == 200 and "Disabled" in response.text
            assert "/api/remote-lookups/providers" in response.text
            response = await client.get("/pipeline/remote-lookup-queue")
            assert response.status_code == 200 and "Queue tracklist lookup" in response.text
            response = await client.post(
                "/admin/remote-providers/example_catalog/policy",
                headers={"Origin": "http://testserver"},
                data={"enabled": "true", "min_interval_seconds": "1", "max_requests_per_run": "16"},
            )
            assert "interval must be at least" in response.text
            assert not (await h.lookup.provider_status())[0].enabled
        assert not h.socket.requests
        batch = await create_batch(h.sessions, h.lookup)
        status = await batch_status(h.sessions, batch)
        assert status["batch"].expanded and not status["batch"].providers


async def test_recovery_queue_delivery_failure_keeps_durable_intent(committed_db: Any) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        await seed(h.sessions)
        batch = await create_batch(h.sessions, h.lookup)

        class BrokenQueue:
            async def enqueue(self, *args: Any, **kwargs: Any) -> None:
                del args, kwargs
                raise ConnectionError("synthetic queue outage")

        with pytest.raises(ConnectionError):
            await dispatch_pending(h.sessions, h.lookup, BrokenQueue())
        assert not (await batch_status(h.sessions, batch))["batch"].expanded
        assert await expand_batch(h.sessions, batch)
        assert await count(h.sessions, RemoteLookupWork) == 1


async def test_real_saq_delivery_worker_restart_and_independent_provider_stop(committed_db: Any) -> None:
    from saq import Worker
    from saq.queue.postgres import PostgresQueue

    from phaze.tasks.remote_lookup import expand_remote_lookup_batch, lookup_remote_tracklist
    from tests.integration.test_remote_lookup import SECOND_PROFILE, Reply, SecondCatalog, second_routes

    engine, sessions = committed_db
    queue = PostgresQueue.from_url(
        engine.url.render_as_string(hide_password=False).replace("postgresql+asyncpg", "postgresql"), name=f"provider-test-{uuid.uuid4().hex[:8]}"
    )
    await queue.connect()
    first = None
    try:
        bindings = (RemoteProviderBinding(ExampleCatalog(), EXAMPLE_PROFILE), RemoteProviderBinding(SecondCatalog(), SECOND_PROFILE))
        routes = {**ExampleUpstream(search_reply=Reply(status=429, headers={"retry-after": "30"})).routes(), **second_routes()}
        async with harness(committed_db, routes, bindings=bindings) as h:
            await seed(sessions)
            batch_id = await create_batch(sessions, h.lookup)
            await dispatch_pending(sessions, h.lookup, queue)
            first = Worker(queue, [expand_remote_lookup_batch, lookup_remote_tracklist], dequeue_timeout=1)
            first.context.update(async_session=sessions, remote_lookup=h.lookup, queue=queue)
            assert await first.process()
            assert (await batch_status(sessions, batch_id))["batch"].expanded
            await h.clock.sleep(10)
            assert await first.process()
            assert await first.process()
            status = await batch_status(sessions, batch_id)
            assert status["counts"] == {"blocked": 1, "completed": 1}
            async with sessions() as session:
                ledger = await session.get(RemoteTransportLedger, EXAMPLE)
                before = (ledger.ordinal, ledger.last_reserved_at, ledger.not_before, ledger.stop_code)
            await h.lookup.policy.set(EXAMPLE, policy(False, 12, 2), actor="admin")
            await h.lookup.policy.set(EXAMPLE, policy(True, 6, 16), actor="admin")
            async with sessions() as session:
                ledger = await session.get(RemoteTransportLedger, EXAMPLE)
                assert (ledger.ordinal, ledger.last_reserved_at, ledger.not_before, ledger.stop_code) == before
            requests = len(h.socket.requests)
            await dispatch_pending(sessions, h.lookup, queue)
            assert len(h.socket.requests) == requests
            await h.lookup.clear_stop(EXAMPLE, operator="admin", reason="Synthetic test: rate limit resolved")
            await h.clock.sleep(30)
            await dispatch_pending(sessions, h.lookup, queue)
            restarted = Worker(queue, [expand_remote_lookup_batch, lookup_remote_tracklist], dequeue_timeout=1)
            restarted.context.update(async_session=sessions, remote_lookup=h.lookup, queue=queue)
            try:
                assert await restarted.process()
            finally:
                restarted.pool.shutdown(wait=False)
            assert await count(sessions, RemoteTransportAttempt) == 3
            assert all(not probe for probe in h.socket.held_across_io)
    finally:
        if first:
            first.pool.shutdown(wait=False)
        async with queue.pool.connection() as conn:
            await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()


async def test_robots_continuation_and_missing_recording_explicit_outcome(committed_db: Any) -> None:
    from sqlalchemy import update

    from phaze.models.file import FileRecord
    from tests.integration.test_remote_lookup import Reply

    profile = EXAMPLE_PROFILE.model_copy(update={"robots": "respect"})
    binding = RemoteProviderBinding(ExampleCatalog(), profile)
    routes = {
        **ExampleUpstream().routes(),
        "/robots.txt": lambda _request: Reply(body=b"User-agent: *\nAllow: /", headers={"content-type": "text/plain"}),
    }
    async with harness(committed_db, routes, bindings=(binding,)) as h:
        media = await seed(h.sessions)
        batch = await create_batch(h.sessions, h.lookup)
        await expand_batch(h.sessions, batch)
        async with h.sessions() as session:
            work_id = await session.scalar(select(RemoteLookupWork.id))
        assert await execute_work(h.sessions, h.lookup, work_id) == "deferred"
        await h.clock.sleep(6)
        assert await execute_work(h.sessions, h.lookup, work_id) == "deferred"
        await h.clock.sleep(6)
        assert await execute_work(h.sessions, h.lookup, work_id) == "completed"
        assert [urlsplit(request.url).path for request in h.socket.requests] == ["/robots.txt", "/search"]
        next_batch = await create_batch(h.sessions, h.lookup)
        await expand_batch(h.sessions, next_batch)
        async with h.sessions() as session, session.begin():
            work_id = await session.scalar(select(RemoteLookupWork.id).where(RemoteLookupWork.status == "queued"))
            await session.execute(update(FileRecord).where(FileRecord.id == media.id).values(missing_at=h.clock.wall()))
        assert await execute_work(h.sessions, h.lookup, work_id) == "recording_unavailable"
        assert len(h.socket.requests) == 2


async def test_independent_worker_process_observes_live_snapshot_without_restart(committed_db: Any) -> None:
    import json

    from phaze.services.remote_providers import REMOTE_PROVIDERS

    code = """
import asyncio,json,os,sys
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from phaze.config_remote_providers import load_remote_provider_settings
from phaze.services.remote_policy import RemotePolicy
from phaze.services.remote_providers import REMOTE_PROVIDERS
async def main():
    engine=create_async_engine(os.environ['TEST_DATABASE_URL'])
    resolver=RemotePolicy(REMOTE_PROVIDERS,load_remote_provider_settings((binding.spec for binding in REMOTE_PROVIDERS),environ={}),async_sessionmaker(engine))
    for _ in range(2):
        snapshot=(await resolver.refresh())['mixesdb'].configured
        print(snapshot.model_dump_json(),flush=True)
        if _ == 0:
            await asyncio.to_thread(sys.stdin.readline)
    await engine.dispose()
asyncio.run(main())
"""
    async with harness(committed_db, {}, bindings=REMOTE_PROVIDERS, settings={}) as h:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "--no-sync",
            "python",
            "-c",
            code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            first = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
            assert not first["enabled"]
            await h.lookup.policy.set("mixesdb", policy(True, 12.0, 3), actor="admin")
            process.stdin.write(b"refresh\n")
            await process.stdin.drain()
            second = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
            assert second == policy(True, 12.0, 3).model_dump()
            assert await asyncio.wait_for(process.wait(), 30) == 0
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        assert not h.socket.requests


async def test_queue_routes_persist_delivery_outage_and_apply_valid_live_policy(committed_db: Any) -> None:
    class Queue:
        fail = False

        async def enqueue(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            if self.fail:
                raise ConnectionError("synthetic outage")

    async with harness(committed_db, ExampleUpstream().routes()) as h:
        await seed(h.sessions)
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/admin/remote-providers")).status_code == 503
            app.state.remote_lookup = h.lookup
            app.state.controller_queue = Queue()
            response = await client.post(
                f"/admin/remote-providers/{EXAMPLE}/policy",
                headers={"Origin": "http://testserver"},
                data={"enabled": "true", "min_interval_seconds": "12", "max_requests_per_run": "3"},
            )
            assert "Saved globally" in response.text
            for fail in (False, True):
                app.state.controller_queue.fail = fail
                response = await client.post("/pipeline/remote-lookup-queue", headers={"Origin": "http://testserver"})
                assert response.status_code == 200
                assert ("queue delivery failed" if fail else "Batch saved") in response.text
            response = await client.post(
                f"/admin/remote-providers/{EXAMPLE}/clear-stop", headers={"Origin": "http://testserver"}, data={"reason": ""}
            )
            assert response.status_code == 422
        assert not h.socket.requests


async def test_work_claim_guards_metadata_change_and_bounded_failures(committed_db: Any, monkeypatch: Any) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    from phaze.models.metadata import FileMetadata

    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        batch_id = await create_batch(h.sessions, h.lookup)
        await expand_batch(h.sessions, batch_id)
        assert await expand_batch(h.sessions, batch_id)
        assert await expand_batch(h.sessions, uuid.uuid4())
        async with h.sessions() as session:
            work_id = await session.scalar(select(RemoteLookupWork.id))
        assert await execute_work(h.sessions, h.lookup, uuid.uuid4()) == "inactive"
        assert await execute_work(h.sessions, h.lookup, work_id) == "deferred"
        assert await execute_work(h.sessions, h.lookup, work_id) == "deferred", "not-before blocks premature redelivery"
        async with h.sessions() as session, session.begin():
            await session.execute(
                update(RemoteLookupWork).where(RemoteLookupWork.id == work_id).values(lease_until=h.clock.wall() + timedelta(seconds=60))
            )
        assert await execute_work(h.sessions, h.lookup, work_id) == "busy"
        async with h.sessions() as session, session.begin():
            await session.execute(update(RemoteLookupWork).where(RemoteLookupWork.id == work_id).values(lease_until=None))
            await session.execute(update(FileMetadata).where(FileMetadata.file_id == media.id).values(artist="Changed Synthetic Artist"))
        await h.clock.sleep(6)
        assert await execute_work(h.sessions, h.lookup, work_id) == "recording_metadata_changed"
        assert await execute_work(h.sessions, h.lookup, work_id) == "inactive"
        batch_id = await create_batch(h.sessions, h.lookup)
        await expand_batch(h.sessions, batch_id)
        async with h.sessions() as session:
            work_id = await session.scalar(select(RemoteLookupWork.id).where(RemoteLookupWork.status == "queued"))

        async def broken(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("synthetic worker failure")

        monkeypatch.setattr(h.lookup, "queued_lookup", broken)
        for attempt in range(5):
            assert await execute_work(h.sessions, h.lookup, work_id) == ("failed" if attempt == 4 else "deferred")
            await h.clock.sleep(60)
        async with h.sessions() as session:
            work = await session.get(RemoteLookupWork, work_id)
            assert work.failures == 5 and work.not_before is None
        assert await batch_status(h.sessions, uuid.uuid4()) is None
        assert not h.socket.requests


@pytest.mark.parametrize("changed", ["disabled", "interval", "budget"])
async def test_policy_change_after_reservation_preserves_charge_and_prevents_send(committed_db: Any, changed: str) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        await h.clock.sleep(6)
        original = h.lookup.policy.resolve
        calls = 0

        async def resolve(provider_id: str) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                await h.lookup.policy.set(
                    EXAMPLE, policy(changed != "disabled", 12 if changed == "interval" else 6, 1 if changed == "budget" else 16), actor="admin"
                )
            return await original(provider_id)

        h.lookup.transport._policy = resolve
        # Budget reduction at the first reservation allows its already-charged one request;
        # disable or a wider gap suppresses a not-yet-sent reservation.
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        assert len(h.socket.requests) == (1 if changed == "budget" else 0)
        assert await count(h.sessions, RemoteTransportAttempt) == 1
        if changed == "interval":
            assert result.outcome.code == "paced"
        elif changed == "disabled":
            assert result.outcome.code == "disabled_provider"


async def test_reviewed_selection_excluded_from_new_and_already_queued_work(committed_db: Any) -> None:
    from phaze.schemas.remote_lookup import LoadCommand
    from tests.integration.test_remote_lookup import decide

    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        batch_id = await create_batch(h.sessions, h.lookup)
        await expand_batch(h.sessions, batch_id)
        async with h.sessions() as session:
            work_id = await session.scalar(select(RemoteLookupWork.id))
        discovery = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        loaded = await h.lookup.load(LoadCommand(candidate_id=discovery.candidates[0].candidate_id))
        selection = await decide(h.sessions, media, loaded.observation_id)
        requests = len(h.socket.requests)
        assert await execute_work(h.sessions, h.lookup, work_id) == "reviewed_selection"
        next_batch = await create_batch(h.sessions, h.lookup)
        await expand_batch(h.sessions, next_batch)
        status = await batch_status(h.sessions, next_batch)
        assert status["batch"].excluded == 1 and not status["work"]
        assert len(h.socket.requests) == requests
        assert selection is not None


async def test_lowered_budget_between_second_reservation_and_send_is_conserved(committed_db: Any) -> None:
    from phaze.tracklist_providers.domain import DiscoveryBudget
    from tests.integration.test_remote_transport import search_reference

    async with harness(committed_db, ExampleUpstream().routes()) as h:
        await h.clock.sleep(6)
        run = h.lookup.transport.run()
        assert (await run.read(search_reference(), DiscoveryBudget())).status == OutcomeStatus.FOUND
        await h.clock.sleep(6)
        original = h.lookup.policy.resolve
        calls = 0

        async def resolve(provider_id: str) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                await h.lookup.policy.set(EXAMPLE, policy(True, budget=1), actor="admin")
            return await original(provider_id)

        h.lookup.transport._policy = resolve
        refused = await run.read(search_reference(), DiscoveryBudget())
        assert refused.code == "run_budget_exhausted"
        assert len(h.socket.requests) == 1
        assert await count(h.sessions, RemoteTransportAttempt) == 2
        assert run.spent[EXAMPLE] == 2


@pytest.mark.parametrize("disable", [False, True])
async def test_direct_lookup_rechecks_live_policy_after_pacing_wait(committed_db: Any, monkeypatch: Any, disable: bool) -> None:
    async with harness(committed_db, ExampleUpstream().routes()) as h:
        media = await seed(h.sessions)
        sleep = h.clock.sleep
        changed = False

        async def during_wait(seconds: float) -> None:
            nonlocal changed
            await sleep(seconds)
            if not changed:
                changed = True
                await h.lookup.policy.set(EXAMPLE, policy(not disable, 12.0 if not disable else 6.0), actor="admin")

        monkeypatch.setattr(h.clock, "sleep", during_wait)
        result = await h.lookup.lookup(LookupCommand(media_id=media.id, provider_id=EXAMPLE))
        if disable:
            assert result.outcome.code == "disabled_provider"
            assert not h.socket.requests and await count(h.sessions, RemoteTransportAttempt) == 0
        else:
            assert result.candidates
            assert (h.socket.sent_at[0] - h.clock._wall.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() == 12
            assert await count(h.sessions, RemoteTransportAttempt) == 1


async def test_transport_boundary_refuses_disabled_policy_even_for_previously_minted_read(committed_db: Any) -> None:
    from phaze.tracklist_providers.domain import DiscoveryBudget
    from tests.integration.test_remote_transport import search_reference

    async with harness(committed_db, ExampleUpstream().routes()) as h:
        await h.lookup.policy.set(EXAMPLE, policy(False), actor="admin")
        read = await h.lookup.transport.run().read(search_reference(), DiscoveryBudget())
        assert read.code == "disabled_provider"
        assert not h.socket.requests and await count(h.sessions, RemoteTransportAttempt) == 0
