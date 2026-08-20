"""Protocol-level tests for ``services/execution_dispatch_protocol`` (phaze-1i0h6.5).

These are deliberately NOT route tests. ``tests/review/routers/test_execution*.py`` already covers
the same protocol through ``POST /execution/start``; what those cannot do is pin the protocol's
*internal ordering* independently of FastAPI, and ordering is the whole contract here -- every one
of the invariants below is a shipped incident, and each of them is invisible in the final response.

Every test in the first section is a MUTATION check: it fails on a reordering that leaves the
outcome, the counters and the rendered HTML all identical. The single recorded call log
(``_RecordingRedis.calls``) is what makes that possible -- assertions compare INDICES in one
sequence, not per-mock await counts.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from phaze.routers.agent_exec_batches import ACTIVE_DISPATCH_KEY, BATCH_KEY_PREFIX
from phaze.schemas.agent_tasks import ExecuteBatchProposalItem
from phaze.services.agent_task_router import AmbiguousEnqueueError
from phaze.services.execution_dispatch_protocol import (
    DispatchOutcomeKind,
    DispatchScripts,
    dispatch_approved_batch,
)


if TYPE_CHECKING:
    from collections.abc import Callable


def _proposal(agent_id: str = "agent-a") -> ExecuteBatchProposalItem:
    """Tiny ExecuteBatchProposalItem with all required fields."""
    return ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        original_path=f"/in/{agent_id}.mp3",
        proposed_path="out",
        proposed_filename=f"{agent_id}.mp3",
    )


class _RecordingPipeline:
    """The transactional pipeline half of :class:`_RecordingRedis`.

    Queued commands are appended to the SHARED log at ``execute()`` time, in queue order, so the
    log reflects the order Redis actually applies them -- which is what the seed-before-claim and
    per-agent-counter-before-batch-counter invariants are about.
    """

    def __init__(self, calls: list[tuple[str, Any, ...]]) -> None:
        self._calls = calls
        self._queued: list[tuple[str, Any, ...]] = []
        self.fail_on_execute: BaseException | None = None

    async def __aenter__(self) -> _RecordingPipeline:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def hset(self, key: str, *args: Any, **kwargs: Any) -> None:
        if "mapping" in kwargs:
            self._queued.append(("hset_mapping", key, kwargs["mapping"]))
        else:
            self._queued.append(("hset", key, *args))

    def hincrby(self, key: str, field: str, amount: int) -> None:
        self._queued.append(("hincrby", key, field, amount))

    def expire(self, key: str, ttl: int) -> None:
        self._queued.append(("expire", key, ttl))

    async def execute(self) -> None:
        if self.fail_on_execute is not None:
            raise self.fail_on_execute
        self._calls.extend(self._queued)
        self._queued.clear()


class _RecordingRedis:
    """A Redis double that records every command into ONE ordered log.

    Only the commands this protocol issues are implemented; anything else is a deliberate
    AttributeError, so a new Redis call added to the protocol shows up as a test failure rather
    than as a silently-unrecorded step.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, ...]] = []
        self.pipe = _RecordingPipeline(self.calls)

    def pipeline(self, *, transaction: bool = False) -> _RecordingPipeline:
        assert transaction, "the seed and the reconcile must both be transactional"
        return self.pipe

    async def delete(self, key: str) -> int:
        self.calls.append(("delete", key))
        return 1

    def kinds(self) -> list[str]:
        """The command names, in order -- the sequence most assertions below index into."""
        return [call[0] for call in self.calls]

    def index_of(self, kind: str) -> int:
        """Position of the FIRST ``kind`` command in the log. Raises if it never ran."""
        return self.kinds().index(kind)


def _scripts(redis: _RecordingRedis, *, claim_result: int = 1) -> tuple[DispatchScripts, dict[str, AsyncMock]]:
    """Build a :class:`DispatchScripts` whose three Lua entry points record into ``redis.calls``."""

    def _record(name: str, result: object) -> AsyncMock:
        async def _call(**kwargs: Any) -> object:
            redis.calls.append((name, tuple(kwargs.get("keys", ())), tuple(kwargs.get("args", ()))))
            return result

        return AsyncMock(side_effect=_call)

    mocks = {
        "claim": _record("claim", claim_result),
        "release": _record("release", 1),
        "promote": _record("promote", 1),
    }

    def _factory(mock: AsyncMock) -> Callable[[Any], AsyncMock]:
        return lambda _client: mock

    return (
        DispatchScripts(claim=_factory(mocks["claim"]), release=_factory(mocks["release"]), promote=_factory(mocks["promote"])),
        mocks,
    )


def _task_router(side_effect: Any = None) -> AsyncMock:
    """A task router double whose ``enqueue_for_agent`` is the only method the protocol touches."""
    router = MagicMock()
    router.enqueue_for_agent = AsyncMock(side_effect=side_effect)
    return router


# ---------------------------------------------------------------------------
# Ordering invariants (mutation checks)
# ---------------------------------------------------------------------------


async def test_the_batch_hash_is_seeded_before_the_active_claim_is_taken() -> None:
    """phaze-0t2c: seed FIRST, claim second -- never the reverse.

    Claiming first is not merely "a different order": the claim and the seed are two separate
    commands, so a transient Redis fault on the seed leaves ``execdispatch:active`` naming a batch
    whose hash never existed. Nothing can then release it -- no sub-job was ever enqueued to POST a
    terminal event, and the claim-reconcile has no hash to judge -- so every Execute Approved is
    refused for the full 24h TTL. The response is byte-identical either way, which is exactly why
    this has to be asserted on the call log.
    """
    redis = _RecordingRedis()
    scripts, _mocks = _scripts(redis)

    outcome = await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=_task_router(),
        groups={"agent-a": [_proposal("agent-a")]},
        agent_names={},
        scripts=scripts,
    )

    assert outcome.kind is DispatchOutcomeKind.STARTED
    assert redis.index_of("hset_mapping") < redis.index_of("claim")
    # The TTL rides the same transaction as the seed, so it can never be the thing that is missing.
    assert redis.index_of("expire") < redis.index_of("claim")


async def test_a_failing_seed_leaves_no_claim_outstanding() -> None:
    """phaze-0t2c, the other half: if the seed raises, the claim must never have been attempted."""
    redis = _RecordingRedis()
    redis.pipe.fail_on_execute = RuntimeError("redis hiccup on the seed")
    scripts, mocks = _scripts(redis)

    with pytest.raises(RuntimeError, match="redis hiccup"):
        await dispatch_approved_batch(
            redis_client=redis,  # type: ignore[arg-type]
            task_router=_task_router(),
            groups={"agent-a": [_proposal("agent-a")]},
            agent_names={},
            scripts=scripts,
        )

    mocks["claim"].assert_not_awaited()


async def test_nothing_is_enqueued_before_the_claim_is_held() -> None:
    """The claim is the single-dispatch guard (phaze-fa2p) -- it must precede the first enqueue.

    Approved proposals stay APPROVED for the whole dispatch, so any chunk enqueued before the
    sentinel is held is a chunk a concurrent POST can duplicate.
    """
    redis = _RecordingRedis()
    scripts, _mocks = _scripts(redis)
    order: list[str] = []

    async def _enqueue(**_kw: Any) -> None:
        order.append("enqueue")

    router = _task_router(side_effect=_enqueue)

    async def _claim_then_note(**kwargs: Any) -> int:
        redis.calls.append(("claim", (), ()))
        order.append("claim")
        return 1

    scripts = DispatchScripts(claim=lambda _c: AsyncMock(side_effect=_claim_then_note), release=scripts.release, promote=scripts.promote)

    await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=router,
        groups={"agent-a": [_proposal("agent-a")]},
        agent_names={},
        scripts=scripts,
    )

    assert order == ["claim", "enqueue"]


async def test_a_lost_claim_reply_deletes_the_seeded_hash_before_re_raising() -> None:
    """phaze-tnp06: the claim await is inside the settled span.

    A TimeoutError after the EVALSHA landed server-side would otherwise leave the sentinel claimed
    with no chunk to release it, AND leave a live-looking hash for the next dispatch's reconcile to
    refuse against for 24h. Deleting our own hash turns that into shape 1 (``EXISTS == 0``), which
    the next click takes over outright.
    """
    redis = _RecordingRedis()

    async def _timeout(**_kw: Any) -> int:
        raise TimeoutError("redis response lost after EVALSHA landed")

    scripts = DispatchScripts(claim=lambda _c: AsyncMock(side_effect=_timeout), release=MagicMock(), promote=MagicMock())

    with pytest.raises(TimeoutError, match="response lost"):
        await dispatch_approved_batch(
            redis_client=redis,  # type: ignore[arg-type]
            task_router=_task_router(),
            groups={"agent-a": [_proposal("agent-a")]},
            agent_names={},
            scripts=scripts,
        )

    deletes = [call for call in redis.calls if call[0] == "delete"]
    assert len(deletes) == 1
    assert deletes[0][1].startswith(BATCH_KEY_PREFIX)


async def test_per_agent_failed_counters_are_applied_before_the_batch_counter() -> None:
    """phaze-1h6j ordering: ``agent:<id>:failed`` HINCRBYs precede the batch-level ``failed``.

    Callers read the batch counter positionally (the last hincrby of the reconcile transaction),
    and the per-agent counters are what let each affected agent's row reach completed+failed ==
    total and render a terminal pill instead of freezing on RUNNING.
    """
    redis = _RecordingRedis()
    scripts, _mocks = _scripts(redis)
    router = _task_router(side_effect=RuntimeError("broker down"))

    outcome = await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=router,
        groups={"agent-a": [_proposal("agent-a"), _proposal("agent-a")], "agent-b": [_proposal("agent-b")]},
        agent_names={},
        scripts=scripts,
    )

    assert outcome.kind is DispatchOutcomeKind.PARTIAL
    hincrbys = [call for call in redis.calls if call[0] == "hincrby"]
    fields = [call[2] for call in hincrbys]
    assert fields == ["agent:agent-a:failed", "agent:agent-b:failed", "failed"]
    assert [call[3] for call in hincrbys] == [2, 1, 3]


def test_the_dispatch_protocol_cannot_hold_a_database_session() -> None:
    """The protocol is structurally incapable of holding a transaction across the enqueue loop.

    phaze-266lc released the route's read-only transaction BEFORE the loop, which spans many
    suspension points and real wall time on a large approved set; sitting idle-in-transaction
    across it is the phaze-1v37 pool-drain class. The seam ENFORCES that rather than documenting
    it: nothing in this module binds an ``AsyncSession`` (not even under ``TYPE_CHECKING``) and
    ``dispatch_approved_batch`` takes no session, so there is no session left alive to hold. An
    edit that reintroduces one fails here, before it can reintroduce the drain.
    """
    import ast
    import inspect

    from phaze.services import execution_dispatch_protocol

    tree = ast.parse(inspect.getsource(execution_dispatch_protocol))
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            bound.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    assert "AsyncSession" not in bound
    assert not {name for name in bound if "session" in name.lower()}
    assert "session" not in inspect.signature(dispatch_approved_batch).parameters


async def test_the_session_is_committed_before_any_redis_or_broker_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route half of the same invariant: the commit lands before the protocol's first I/O.

    phaze-266lc. Asserted through the real route because the ORDER is what matters and it is not
    observable in the response: a commit moved to after the enqueue loop returns the identical
    progress card while holding a transaction open across every suspension point in it.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from phaze.database import get_session
    from phaze.routers import execution

    order: list[str] = []
    session = AsyncMock()
    name_result = MagicMock()
    name_result.all.return_value = []
    session.execute.return_value = name_result
    session.commit = AsyncMock(side_effect=lambda: order.append("commit"))

    redis = _RecordingRedis()
    scripts, _mocks = _scripts(redis)
    monkeypatch.setattr(execution, "_dispatch_scripts", lambda: scripts)

    async def _enqueue(**_kw: Any) -> None:
        order.append("enqueue")

    app = FastAPI()
    app.include_router(execution.router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.redis = redis
    app.state.task_router = _task_router(side_effect=_enqueue)

    async def _seed_watch() -> None:
        order.append("redis")

    original_execute = redis.pipe.execute

    async def _watched_execute() -> None:
        await _seed_watch()
        await original_execute()

    redis.pipe.execute = _watched_execute  # type: ignore[method-assign]

    monkeypatch.setattr(execution, "detect_collisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(execution, "get_approved_proposals_grouped_by_agent", AsyncMock(return_value={"agent-a": [_proposal("agent-a")]}))
    monkeypatch.setattr(execution, "count_revoked_skipped_proposals", AsyncMock(return_value=0))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/execution/start")

    assert response.status_code == 200
    assert order == ["commit", "redis", "enqueue"]


# ---------------------------------------------------------------------------
# Outcome shapes
# ---------------------------------------------------------------------------


async def test_an_empty_dispatch_terminates_without_touching_redis_or_the_broker() -> None:
    """phaze-5zyv's counterpart at dispatch time: nothing approved -> no hash, no claim, no enqueue.

    An ``exec:{batch_id}`` hash with status="running", a 24h TTL and no sub-jobs would leave the SSE
    reader rendering a live-looking batch that can never terminate, and there is nothing to
    double-dispatch, so there is nothing for the sentinel to guard either.
    """
    redis = _RecordingRedis()
    scripts, mocks = _scripts(redis)
    router = _task_router()

    outcome = await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=router,
        groups={},
        agent_names={},
        scripts=scripts,
    )

    assert outcome.kind is DispatchOutcomeKind.EMPTY
    assert (outcome.total, outcome.subjobs_expected, outcome.undispatched, outcome.status) == (0, 0, 0, "running")
    assert redis.calls == []
    mocks["claim"].assert_not_awaited()
    router.enqueue_for_agent.assert_not_awaited()


async def test_a_refused_claim_drops_the_seeded_hash_and_reports_refused() -> None:
    """Claim result 0 -> REFUSED, no enqueue, and the unreachable hash is deleted, not left to rot."""
    redis = _RecordingRedis()
    scripts, _mocks = _scripts(redis, claim_result=0)
    router = _task_router()

    outcome = await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=router,
        groups={"agent-a": [_proposal("agent-a")]},
        agent_names={},
        scripts=scripts,
    )

    assert outcome.kind is DispatchOutcomeKind.REFUSED
    assert redis.kinds() == ["hset_mapping", "expire", "claim", "delete"]
    router.enqueue_for_agent.assert_not_awaited()


async def test_a_reconciled_stale_sentinel_still_dispatches_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Claim result 2 (phaze-j7u8) proceeds -- the operator is not locked out -- but the leak is logged."""
    redis = _RecordingRedis()
    scripts, _mocks = _scripts(redis, claim_result=2)
    router = _task_router()

    with caplog.at_level("WARNING"):
        outcome = await dispatch_approved_batch(
            redis_client=redis,  # type: ignore[arg-type]
            task_router=router,
            groups={"agent-a": [_proposal("agent-a")]},
            agent_names={},
            scripts=scripts,
        )

    assert outcome.kind is DispatchOutcomeKind.STARTED
    router.enqueue_for_agent.assert_awaited_once()
    assert any("stale exec:active sentinel" in record.getMessage() for record in caplog.records)


async def test_a_clean_dispatch_seeds_the_full_wire_contract() -> None:
    """The seed mapping's key set, per-agent rollup fields and dispatch_summary are a wire contract.

    The SSE reader and the reattach path both project the per-agent table out of
    ``dispatch_summary``, so its shape is read by code this module never calls.
    """
    redis = _RecordingRedis()
    scripts, _mocks = _scripts(redis)

    outcome = await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=_task_router(),
        groups={"agent-a": [_proposal("agent-a")] * 501, "agent-b": [_proposal("agent-b")]},
        agent_names={"agent-a": "Agent Alpha"},
        scripts=scripts,
    )

    # 501 items chunk at <=500 into 2 sub-jobs, plus agent-b's 1.
    assert (outcome.total, outcome.subjobs_expected) == (502, 3)
    seed = next(call for call in redis.calls if call[0] == "hset_mapping")[2]
    assert seed["total"] == "502"
    assert seed["subjobs_expected"] == "3"
    assert seed["status"] == "running"
    assert seed["agent:agent-a:total"] == "501"
    assert seed["agent:agent-a:completed"] == "0"
    assert seed["agent:agent-a:failed"] == "0"
    assert json.loads(seed["dispatch_summary"]) == [
        {"agent_id": "agent-a", "name": "Agent Alpha", "chunks": 2, "total": 501},
        {"agent_id": "agent-b", "name": "agent-b", "chunks": 1, "total": 1},
    ]


# ---------------------------------------------------------------------------
# Ambiguous enqueue reconciliation (phaze-19u7g)
# ---------------------------------------------------------------------------


async def test_an_ambiguous_enqueue_stays_inside_subjobs_expected() -> None:
    """An ambiguous chunk counts as LANDED, so an all-ambiguous dispatch never reconciles at all.

    ``AmbiguousEnqueueError`` means the broker connection was already live when the call raised, so
    the ``saq_jobs`` INSERT may be durably committed. Lowering ``subjobs_expected`` past it would
    let ``sc >= se`` promote the batch terminal and release the sentinel while that phantom sub-job
    is still moving files -- an operator retry then double-dispatches the same proposals.
    """
    redis = _RecordingRedis()
    scripts, mocks = _scripts(redis)

    async def _enqueue(*, agent_id: str, **_kw: Any) -> None:
        if agent_id == "agent-b":
            raise AmbiguousEnqueueError("raised after the broker connection was live")

    outcome = await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=_task_router(side_effect=_enqueue),
        groups={"agent-a": [_proposal("agent-a")], "agent-b": [_proposal("agent-b")]},
        agent_names={},
        scripts=scripts,
    )

    assert outcome.kind is DispatchOutcomeKind.STARTED
    assert (outcome.subjobs_expected, outcome.undispatched) == (2, 0)
    # No reconcile ran: no correction HSET, no per-agent failed counter, no promote, no release.
    assert "hincrby" not in redis.kinds()
    assert "hset" not in redis.kinds()
    mocks["promote"].assert_not_awaited()
    mocks["release"].assert_not_awaited()


async def test_a_mixed_ambiguous_and_definite_failure_only_reconciles_the_definite_one() -> None:
    """The ambiguous chunk stays counted; only the definitely-lost one lowers ``subjobs_expected``."""
    redis = _RecordingRedis()
    scripts, mocks = _scripts(redis)

    async def _enqueue(*, agent_id: str, **_kw: Any) -> None:
        if agent_id == "agent-b":
            raise AmbiguousEnqueueError("broker connection was live")
        if agent_id == "agent-c":
            raise RuntimeError("broker refused the connection outright")

    outcome = await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=_task_router(side_effect=_enqueue),
        groups={
            "agent-a": [_proposal("agent-a")],
            "agent-b": [_proposal("agent-b")],
            "agent-c": [_proposal("agent-c")],
        },
        agent_names={},
        scripts=scripts,
    )

    assert outcome.kind is DispatchOutcomeKind.PARTIAL
    # a landed + b ambiguous == 2 kept; only c is reconciled away.
    assert (outcome.subjobs_expected, outcome.undispatched, outcome.status) == (2, 1, "running")
    correction = next(call for call in redis.calls if call[0] == "hset")
    assert correction[2:] == ("subjobs_expected", "2")
    hincrby_fields = [call[2] for call in redis.calls if call[0] == "hincrby"]
    assert hincrby_fields == ["agent:agent-c:failed", "failed"]
    # A landed sub-job may already have reported terminal against the stale, higher expected count.
    mocks["promote"].assert_awaited_once()
    mocks["release"].assert_not_awaited()


async def test_nothing_landing_at_all_promotes_terminal_and_releases_the_sentinel() -> None:
    """phaze-fa2p: enqueued_ok == 0 -> terminal here, and the CAS release runs instead of the promote.

    No sub-job will ever POST, so the sentinel has to be released now -- via the CAS release, never
    an unconditional DEL, so a LATER dispatch's claim cannot be dropped by a late crash path.
    """
    redis = _RecordingRedis()
    scripts, mocks = _scripts(redis)

    outcome = await dispatch_approved_batch(
        redis_client=redis,  # type: ignore[arg-type]
        task_router=_task_router(side_effect=RuntimeError("broker down")),
        groups={"agent-a": [_proposal("agent-a"), _proposal("agent-a")]},
        agent_names={},
        scripts=scripts,
    )

    assert outcome.kind is DispatchOutcomeKind.PARTIAL
    assert (outcome.subjobs_expected, outcome.undispatched, outcome.status) == (0, 2, "complete_with_errors")
    corrections = [call for call in redis.calls if call[0] == "hset"]
    assert ("hset", corrections[0][1], "subjobs_expected", "0") == corrections[0]
    assert corrections[1][2:] == ("status", "complete_with_errors")
    mocks["release"].assert_awaited_once()
    release_call = next(call for call in redis.calls if call[0] == "release")
    assert release_call[1] == (ACTIVE_DISPATCH_KEY,)
    mocks["promote"].assert_not_awaited()


async def test_a_crash_mid_await_settles_before_propagating_and_keeps_that_chunk_ambiguous() -> None:
    """phaze-0t2c + phaze-19u7g: a BaseException settles the batch on the way out, then re-raises.

    The chunk that was in flight may already have committed on the broker, so it stays inside
    ``subjobs_expected``; only the chunks strictly after it were genuinely never attempted.
    """
    redis = _RecordingRedis()
    scripts, mocks = _scripts(redis)
    groups = {"agent-a": [_proposal("agent-a")], "agent-b": [_proposal("agent-b")], "agent-c": [_proposal("agent-c")]}

    async def _enqueue(*, agent_id: str, **_kw: Any) -> None:
        if agent_id == "agent-b":
            raise BaseException("worker process died")

    with pytest.raises(BaseException, match="worker process died"):
        await dispatch_approved_batch(
            redis_client=redis,  # type: ignore[arg-type]
            task_router=_task_router(side_effect=_enqueue),
            groups=groups,
            agent_names={},
            scripts=scripts,
        )

    # agent-a landed, agent-b was in flight (ambiguous, kept), agent-c never started.
    correction = next(call for call in redis.calls if call[0] == "hset")
    assert correction[2:] == ("subjobs_expected", "2")
    hincrbys = [(call[2], call[3]) for call in redis.calls if call[0] == "hincrby"]
    assert hincrbys == [("agent:agent-c:failed", 1), ("failed", 1)]
    mocks["promote"].assert_awaited_once()
    mocks["release"].assert_not_awaited()


# ---------------------------------------------------------------------------
# Response equivalence -- the outcome -> template-context mapping
# ---------------------------------------------------------------------------


def _fake_request() -> Any:
    """Minimal ASGI request stub for templates that reference ``request``."""
    from starlette.requests import Request

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "app": None,
    }
    return Request(scope=scope)


@pytest.mark.parametrize(
    ("enqueue_side_effect", "expected_failed", "expected_expected", "expected_status"),
    [
        (None, 0, 2, "running"),
        (RuntimeError("broker down"), 3, 0, "complete_with_errors"),
    ],
    ids=["clean-dispatch", "nothing-landed"],
)
async def test_the_progress_card_matches_the_pre_refactor_context_exactly(
    monkeypatch: pytest.MonkeyPatch,
    enqueue_side_effect: BaseException | None,
    expected_failed: int,
    expected_expected: int,
    expected_status: str,
) -> None:
    """The route's rendered HTML is byte-identical to the pre-refactor context, rendered directly.

    The refactor's whole risk is the outcome -> template-context mapping: seven keys that used to
    be local variables inside ``start_execution`` and are now projected off a
    :class:`DispatchOutcome`. This renders ``progress.html`` from the context written the way the
    pre-refactor handler wrote it (literal expressions, not the outcome fields) and compares bytes,
    so a mis-wired key -- ``failed`` fed from the wrong counter, ``subjobs_expected`` left at the
    planned figure after a reconcile -- shows up as a diff rather than as a plausible-looking card.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from phaze.database import get_session
    from phaze.routers import execution

    groups = {"agent-a": [_proposal("agent-a"), _proposal("agent-a")], "agent-b": [_proposal("agent-b")]}
    agent_names = {"agent-a": "Agent Alpha"}

    session = AsyncMock()
    name_result = MagicMock()
    # ``name`` is reserved by the Mock constructor, so the row stub is a namespace, not a Mock.
    name_result.all.return_value = [SimpleNamespace(id="agent-a", name="Agent Alpha")]
    session.execute.return_value = name_result

    redis = _RecordingRedis()
    scripts, _mocks = _scripts(redis)
    monkeypatch.setattr(execution, "_dispatch_scripts", lambda: scripts)
    monkeypatch.setattr(execution, "detect_collisions", AsyncMock(return_value=[]))
    monkeypatch.setattr(execution, "get_approved_proposals_grouped_by_agent", AsyncMock(return_value=groups))
    monkeypatch.setattr(execution, "count_revoked_skipped_proposals", AsyncMock(return_value=4))

    app = FastAPI()
    app.include_router(execution.router)
    app.dependency_overrides[get_session] = lambda: session
    app.state.redis = redis
    app.state.task_router = _task_router(side_effect=enqueue_side_effect)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/execution/start")

    assert response.status_code == 200

    seed_key = next(call for call in redis.calls if call[0] == "hset_mapping")[1]
    batch_id = seed_key.removeprefix(BATCH_KEY_PREFIX)
    sort_state = execution.EXEC_AGENTS_SORT.resolve(view_state={"batch_id": batch_id})
    expected = execution._render_partial(
        _fake_request(),
        "execution/partials/progress.html",
        {
            "batch_id": batch_id,
            "skipped_revoked": 4,
            "total": 3,
            "completed": 0,
            "failed": expected_failed,
            "subjobs_expected": expected_expected,
            "agents": execution._sort_agents_view(execution._build_agents_view(groups, agent_names=agent_names), sort_state),
            "sort": sort_state,
            "status": expected_status,
        },
    )
    assert response.text == expected
