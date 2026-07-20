"""Integration tests for execution endpoints -- execute trigger, SSE progress, audit log."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from phaze.models.agent import Agent
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.pagination import MIN_PAGE_SIZE


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_test_execution_log(
    session: AsyncSession,
    *,
    operation: str = "copy",
    source_path: str = "/music/old.mp3",
    destination_path: str = "/music/new.mp3",
    sha256_verified: bool = True,
    status: str = ExecutionStatus.COMPLETED,
    error_message: str | None = None,
) -> ExecutionLog:
    """Create an ExecutionLog entry for testing."""
    # Create prerequisite file and proposal
    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/test.mp3",
        original_filename="test.mp3",
        current_path=source_path,
        file_type="music",
        file_size=1_000_000,
    )
    session.add(file_record)
    await session.flush()

    proposal_id = uuid.uuid4()
    proposal = RenameProposal(
        id=proposal_id,
        file_id=file_id,
        proposed_filename="new.mp3",
        confidence=0.9,
        status=ProposalStatus.APPROVED,
        context_used={"artist": "Test"},
        reason="Test",
    )
    session.add(proposal)
    await session.flush()

    log_entry = ExecutionLog(
        id=uuid.uuid4(),
        proposal_id=proposal_id,
        operation=operation,
        source_path=source_path,
        destination_path=destination_path,
        sha256_verified=sha256_verified,
        status=status,
        error_message=error_message,
        executed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    session.add(log_entry)
    await session.commit()
    return log_entry


async def create_approved_proposal_for_agent(session: AsyncSession, *, agent_id: str, agent_name: str, proposed_filename: str) -> None:
    """Seed one APPROVED proposal (with its Agent + FileRecord) for phaze-a6hm.8's sort tests.

    ``get_approved_proposals_grouped_by_agent`` INNER JOINs through ``Agent``, so a proposal whose
    agent_id has no matching row is silently dropped from dispatch -- the Agent row is not optional.
    """
    existing = await session.get(Agent, agent_id)
    if existing is None:
        session.add(Agent(id=agent_id, name=agent_name))
        await session.flush()

    file_id = uuid.uuid4()
    file_record = FileRecord(
        agent_id=agent_id,
        id=file_id,
        sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        original_path=f"/music/{uuid.uuid4().hex}/test.mp3",
        original_filename="test.mp3",
        current_path=f"/music/{uuid.uuid4().hex}/test.mp3",
        file_type="music",
        file_size=1_000_000,
    )
    session.add(file_record)
    await session.flush()

    proposal = RenameProposal(
        id=uuid.uuid4(),
        file_id=file_id,
        proposed_filename=proposed_filename,
        confidence=0.9,
        status=ProposalStatus.APPROVED,
        context_used={"artist": "Test"},
        reason="Test",
    )
    session.add(proposal)
    await session.commit()


@pytest.mark.asyncio
async def test_audit_log_page(client: AsyncClient, session: AsyncSession) -> None:
    """GET /audit/ returns 200 with HTML containing Audit Log heading."""
    await create_test_execution_log(session)
    response = await client.get("/audit/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Audit Log" in response.text


@pytest.mark.asyncio
async def test_audit_log_page_htmx(client: AsyncClient, session: AsyncSession) -> None:
    """GET /audit/ with HX-Request header returns partial (audit_table only)."""
    await create_test_execution_log(session)
    response = await client.get("/audit/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text.lower()
    assert "audit-table-container" in response.text


@pytest.mark.asyncio
async def test_audit_log_filter(client: AsyncClient, session: AsyncSession) -> None:
    """GET /audit/?status=completed returns filtered results."""
    await create_test_execution_log(session, status=ExecutionStatus.COMPLETED, source_path="/music/completed.mp3")
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, source_path="/music/failed.mp3", error_message="Hash mismatch")
    response = await client.get("/audit/?status=completed")
    assert response.status_code == 200
    assert "/music/completed.mp3" in response.text
    assert "/music/failed.mp3" not in response.text


@pytest.mark.asyncio
async def test_audit_log_empty_state(client: AsyncClient) -> None:
    """GET /audit/ with no logs returns empty state message."""
    response = await client.get("/audit/")
    assert response.status_code == 200
    assert "No operations recorded" in response.text


@pytest.mark.asyncio
async def test_execute_approved(client: AsyncClient) -> None:
    """POST /execution/start returns HTML with SSE progress container.

    Phase 28: dispatch now writes to ``app.state.redis`` and enqueues per-agent
    via ``app.state.task_router.enqueue_for_agent``. With no approved proposals
    seeded, ``groups`` is empty -- the controller renders the progress card
    with the empty-state copy, no Redis seed, no enqueues.
    """
    mock_task_router = AsyncMock()
    mock_redis = AsyncMock()
    client._transport.app.state.task_router = mock_task_router  # type: ignore[union-attr]
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    response = await client.post("/execution/start")
    assert response.status_code == 200
    assert "sse-connect" in response.text
    assert "execution/progress/" in response.text
    # Empty fixture DB -> no enqueues.
    mock_task_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_sse_progress(client: AsyncClient) -> None:
    """GET /execution/progress/{batch_id} returns text/event-stream content type.

    Phase 28: the SSE reader switched from ``queue.redis`` to ``app.state.redis``
    (decode_responses=True, returns str directly).
    """
    batch_id = uuid.uuid4().hex

    mock_redis = MagicMock()
    mock_redis.hgetall = AsyncMock(
        return_value={
            "total": "10",
            "completed": "5",
            "failed": "0",
            "status": "complete",
            "subjobs_expected": "1",
            "started_at": "2026-05-15T00:00:00+00:00",
            "dispatch_summary": "[]",
        },
    )
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    response = await client.get(f"/execution/progress/{batch_id}")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_execute_button_disabled(client: AsyncClient) -> None:
    """Phase 57 (SHELL-05): a plain GET /proposals/ 302-redirects into the shell.

    The Execute Approved button is proposals stats-bar chrome that lives on the propose
    workspace node -- a documented Phase-57 placeholder (real content lands in 58-61). Its
    disabled render is unchanged and remains covered by the approve/reject OOB stats tests
    in test_proposals.py. Here we assert the route resolves into the shell (the bookmark
    still lands somewhere live).
    """
    response = await client.get("/proposals/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/s/propose"


@pytest.mark.asyncio
async def test_audit_log_stats_in_filter_tabs(client: AsyncClient, session: AsyncSession) -> None:
    """GET /audit/ shows correct counts in filter tabs."""
    await create_test_execution_log(session, status=ExecutionStatus.COMPLETED, source_path="/a.mp3")
    await create_test_execution_log(session, status=ExecutionStatus.COMPLETED, source_path="/b.mp3")
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, source_path="/c.mp3", error_message="err")
    response = await client.get("/audit/")
    assert response.status_code == 200
    # Should show total of 3 and 2 completed
    assert "All (3)" in response.text
    assert "Completed (2)" in response.text
    assert "Failed (1)" in response.text


@pytest.mark.asyncio
async def test_collision_gate_blocks_execution(client: AsyncClient) -> None:
    """POST /execution/start returns collision block HTML when collisions exist."""
    mock_task_router = AsyncMock()
    mock_redis = AsyncMock()
    client._transport.app.state.task_router = mock_task_router  # type: ignore[union-attr]
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    with patch("phaze.routers.execution.detect_collisions", new_callable=AsyncMock) as mock_detect:
        mock_detect.return_value = [("performances/artists/Disclosure/file.mp3", 2)]
        response = await client.post("/execution/start")

    assert response.status_code == 200
    assert "Path collisions detected" in response.text
    assert "performances/artists/Disclosure/file.mp3" in response.text
    mock_task_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_collision_proceeds_normally(client: AsyncClient) -> None:
    """POST /execution/start proceeds with the progress card when no collisions detected.

    Phase 28: with no approved proposals seeded, dispatch fans out to zero agents
    and returns the progress card with the empty-state copy. The pre-Phase-28
    expectation that a single ``queue.enqueue`` fired was Phase-25 behavior.
    """
    mock_task_router = AsyncMock()
    mock_redis = AsyncMock()
    client._transport.app.state.task_router = mock_task_router  # type: ignore[union-attr]
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    with patch("phaze.routers.execution.detect_collisions", new_callable=AsyncMock) as mock_detect:
        mock_detect.return_value = []
        response = await client.post("/execution/start")

    assert response.status_code == 200
    assert "sse-connect" in response.text
    # No approved proposals in this empty fixture -> no enqueues.
    mock_task_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_log_history_restore_returns_full_page(client: AsyncClient, session: AsyncSession) -> None:
    """A history-restore GET returns the FULL page, chrome included (phaze-qi9j).

    The filter tabs push ``/audit/?status=...`` via ``hx-push-url``. On a history-cache miss htmx
    re-fetches that URL with BOTH ``HX-Request`` and ``HX-History-Restore-Request`` set, ignores
    ``hx-target``, and swaps the response into ``<body>``. A fragment here replaces the whole page
    with an orphaned tab bar and table.

    Asserts the CHROME, not merely a 200 -- the buggy handler returned 200 too.
    """
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, error_message="Hash mismatch")
    response = await client.get(
        "/audit/?status=failed",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "history restore must return a full document, not a fragment"
    assert "<h1" in body, "the <h1> page heading must survive a history restore"
    assert 'aria-label="Main navigation"' in body, "the app nav must survive a history restore"
    assert 'id="audit-content"' in body, "the swap target itself must be present in the full page"


@pytest.mark.asyncio
async def test_audit_log_plain_htmx_still_returns_fragment(client: AsyncClient, session: AsyncSession) -> None:
    """HX-Request WITHOUT the restore header still gets the chrome-less fragment (phaze-qi9j).

    Guards the other direction: the fix must not turn every htmx swap into a full page.
    """
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, error_message="Hash mismatch")
    response = await client.get("/audit/?status=failed", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text.lower()
    assert "audit-table-container" in response.text


# phaze-a6hm.5: the audit table joins the shared sortable-column contract (column_sort.py). The
# structural rule-2 guarantee (an unwhitelisted key can never reach a column) already ships a
# generic regression in tests/shared/routers/test_column_sort.py -- these tests cover the wiring
# specific to THIS table: that sorting is server-side and reaches the whitelisted column, that a
# sort composes with the audit filter tabs in BOTH directions, and that a history restore of a
# SORTED url still returns a full document (response_shape.py rule 2).


def _compiled_audit_order_by(sort_value: str | None, order_value: str | None = None) -> str:
    """Compile AUDIT_SORT's resolved ORDER BY against a real SELECT; return just that clause.

    Mirrors ``test_column_sort.py``'s ``_compiled_order_by`` helper: the assertion that matters is
    over the emitted SQL, not merely a status code, so an implementation that quietly regressed to
    ``getattr()`` would still fail this even though it "worked".
    """
    from sqlalchemy import select

    from phaze.routers.execution import AUDIT_SORT

    state = AUDIT_SORT.resolve(sort=sort_value, order=order_value)
    stmt = select(ExecutionLog).order_by(*state.order_by())
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    return sql.split("ORDER BY", 1)[1]


class TestAuditSortReachesOnlyWhitelistedColumns:
    """Contract rule 2, specialised to AUDIT_SORT's actual wiring in routers/execution.py."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "id",  # a real ExecutionLog column deliberately NOT offered
            "proposal_id",  # a real column, not offered
            "__class__",  # would resolve under getattr()
            "status; DROP TABLE execution_log",  # catastrophic under text() interpolation
        ],
    )
    def test_unwhitelisted_value_never_reaches_a_column(self, hostile: str) -> None:
        sql = _compiled_audit_order_by(hostile)
        assert hostile not in sql
        assert "execution_log.executed_at DESC" in sql  # AUDIT_SORT's default

    def test_every_whitelisted_key_reaches_its_own_column(self) -> None:
        assert "execution_log.operation ASC" in _compiled_audit_order_by("operation", "asc")
        assert "execution_log.status DESC" in _compiled_audit_order_by("status", "desc")
        assert "execution_log.source_path ASC" in _compiled_audit_order_by("source_path", "asc")


@pytest.mark.asyncio
async def test_audit_log_sorts_server_side_by_source_path(client: AsyncClient, session: AsyncSession) -> None:
    """A ``sort``/``order`` query pair reorders the rendered rows via SQL, not client-side JS."""
    await create_test_execution_log(session, source_path="/music/c-third.mp3")
    await create_test_execution_log(session, source_path="/music/a-first.mp3")
    await create_test_execution_log(session, source_path="/music/b-second.mp3")

    response = await client.get("/audit/?sort=source_path&order=asc")
    assert response.status_code == 200
    body = response.text
    first, second, third = (body.index(name) for name in ("a-first.mp3", "b-second.mp3", "c-third.mp3"))
    assert first < second < third


@pytest.mark.asyncio
async def test_audit_log_unwhitelisted_sort_degrades_to_default_instead_of_422(client: AsyncClient, session: AsyncSession) -> None:
    """Contract rule 3: an unrecognised sort key never 422s the render, it just uses the default."""
    await create_test_execution_log(session)
    response = await client.get("/audit/?sort=proposal_id&order=sideways")
    assert response.status_code == 200
    assert "Audit Log" in response.text


@pytest.mark.asyncio
async def test_audit_log_headers_announce_sort_state_via_aria_sort(client: AsyncClient, session: AsyncSession) -> None:
    """Contract rule 5: the active column's header carries the ARIA state, inactive ones say 'none'."""
    await create_test_execution_log(session)
    response = await client.get("/audit/?sort=status&order=asc")
    assert response.status_code == 200
    body = response.text
    assert 'aria-sort="ascending"' in body
    assert 'aria-sort="none"' in body
    # "Error" is deliberately NOT wired into AUDIT_SORT (sparse free-text column) and stays plain.
    assert '<th scope="col" class="px-4 py-3">Error</th>' in body


@pytest.mark.asyncio
async def test_audit_log_sort_click_preserves_the_active_filter_tab(client: AsyncClient, session: AsyncSession) -> None:
    """A sort header's own hx-get carries the active ``status`` filter forward (contract rule 4)."""
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, error_message="boom")
    response = await client.get("/audit/?status=failed", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "status=failed" in response.text
    assert 'hx-get="/audit/?status=failed&amp;page_size=50&amp;sort=status&amp;order=asc"' in response.text


@pytest.mark.asyncio
async def test_audit_log_filter_tab_click_preserves_the_active_sort(client: AsyncClient, session: AsyncSession) -> None:
    """The inverse (contract rule 4): switching filter tabs must not silently drop the active sort."""
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, error_message="boom")
    response = await client.get("/audit/?status=failed&sort=operation&order=desc", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'hx-get="/audit/?status=all&amp;sort=operation&amp;order=desc"' in response.text


@pytest.mark.asyncio
async def test_audit_log_pager_preserves_the_active_sort(client: AsyncClient, session: AsyncSession) -> None:
    """The pager (contract rule 4, easy-to-forget direction): Prev/Next must carry the sort forward."""
    for i in range(MIN_PAGE_SIZE + 1):
        await create_test_execution_log(session, source_path=f"/music/{i}.mp3")

    response = await client.get(f"/audit/?page_size={MIN_PAGE_SIZE}&sort=operation&order=asc", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "sort=operation&amp;order=asc" in response.text


@pytest.mark.asyncio
async def test_audit_log_history_restore_of_a_sorted_url_returns_a_full_document(client: AsyncClient, session: AsyncSession) -> None:
    """response_shape.py rule 2: a history restore returns the full document even when SORTED.

    Guards the exact composition this bead was assigned: sorting must not reopen the phaze-qi9j
    defect for a URL that also carries ``sort``/``order``.
    """
    await create_test_execution_log(session, status=ExecutionStatus.FAILED, error_message="Hash mismatch")
    response = await client.get(
        "/audit/?status=failed&sort=status&order=desc",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert response.status_code == 200
    body = response.text
    assert "<html" in body.lower(), "history restore of a sorted url must return a full document, not a fragment"
    assert "<h1" in body, "the <h1> page heading must survive a sorted history restore"
    assert 'aria-label="Main navigation"' in body, "the app nav must survive a sorted history restore"
    assert 'id="audit-content"' in body, "the swap target itself must be present in the full page"


# --- phaze-a6hm.8: execution agents table sort ---------------------------------------------------
# EXEC_AGENTS_SORT composes column_sort.py's whitelist/resolve/aria-sort machinery; the generic
# resolution mechanics (unwhitelisted -> default, equality-only matching, aria-sort tokens, url
# encoding) are already covered exhaustively by tests/shared/routers/test_column_sort.py against the
# shared SortContract/SortState classes themselves. These tests cover what is SPECIFIC to this
# table's composition: the actual server-side reorder (Python, not SQL -- there is no backing
# SELECT), the persist-for-future-SSE-ticks behavior, and that an unwhitelisted sort key never
# reaches anything but the default column.


@pytest.mark.asyncio
async def test_start_execution_agents_table_default_sort_is_name_ascending(client: AsyncClient, session: AsyncSession) -> None:
    """POST /execution/start's first render sorts by the contract's default (Agent, ascending)."""
    await create_approved_proposal_for_agent(session, agent_id="zeta-agent", agent_name="Zeta Agent", proposed_filename="z.mp3")
    await create_approved_proposal_for_agent(session, agent_id="alpha-agent", agent_name="Alpha Agent", proposed_filename="a.mp3")

    mock_task_router = AsyncMock()
    mock_redis = AsyncMock()
    # Non-empty `groups` (two approved proposals seeded above) drives start_execution into the
    # `redis.pipeline(transaction=True)` branch, which is used as an async context manager -- a
    # bare AsyncMock().pipeline(...) returns a coroutine, not a context manager, so it needs its
    # own mock: an async context manager whose queuing methods (hset/expire) are synchronous, as
    # the real redis-py pipeline API is -- only `.execute()` is awaited.
    mock_pipe = MagicMock()
    mock_pipe.hset = MagicMock()
    mock_pipe.expire = MagicMock()
    mock_pipe.execute = AsyncMock()
    mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
    mock_pipe.__aexit__ = AsyncMock(return_value=None)
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)
    client._transport.app.state.task_router = mock_task_router  # type: ignore[union-attr]
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    response = await client.post("/execution/start")
    assert response.status_code == 200
    body = response.text
    assert body.index("Alpha Agent") < body.index("Zeta Agent"), "default sort is name ascending"
    assert 'aria-sort="ascending"' in body, "the default-active Agent header announces its own state"


@pytest.mark.asyncio
async def test_agents_table_sort_reorders_by_completed_descending(client: AsyncClient) -> None:
    """GET /execution/agents-table?sort=completed&order=desc reorders the WHOLE rollup server-side."""
    batch_id = uuid.uuid4().hex
    dispatch_summary = [
        {"agent_id": "a1", "name": "Agent One", "total": 10},
        {"agent_id": "a2", "name": "Agent Two", "total": 20},
    ]
    mock_redis = MagicMock()
    mock_redis.hgetall = AsyncMock(
        return_value={
            "dispatch_summary": json.dumps(dispatch_summary),
            "agent:a1:completed": "3",
            "agent:a1:failed": "0",
            "agent:a1:total": "10",
            "agent:a2:completed": "15",
            "agent:a2:failed": "0",
            "agent:a2:total": "20",
        },
    )
    mock_redis.hset = AsyncMock()
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    response = await client.get(f"/execution/agents-table?batch_id={batch_id}&sort=completed&order=desc")
    assert response.status_code == 200
    body = response.text
    assert body.index("Agent Two") < body.index("Agent One"), "Agent Two (completed=15) sorts before Agent One (completed=3)"
    assert 'aria-sort="descending"' in body

    # Persisted so the NEXT SSE tick (which re-resolves from this same hash) keeps honouring the
    # click instead of reverting to the default order within ~1s.
    mock_redis.hset.assert_awaited_once_with(f"exec:{batch_id}", mapping={"agents_sort": "completed", "agents_order": "desc"})


@pytest.mark.asyncio
async def test_agents_table_sort_unwhitelisted_key_degrades_to_default_not_422(client: AsyncClient) -> None:
    """An unwhitelisted ``sort`` value can never reach a column -- it degrades, it does not 422 or 500.

    Regression per column_sort.py rule 7: asserting the STATUS alone would pass against an
    implementation that happily reached an arbitrary attribute via ``getattr``/``__class__``-style
    lookup. This asserts the actual row ORDER stayed at the contract's default too.
    """
    batch_id = uuid.uuid4().hex
    dispatch_summary = [
        {"agent_id": "a1", "name": "Agent One", "total": 10},
        {"agent_id": "a2", "name": "Agent Two", "total": 20},
    ]
    mock_redis = MagicMock()
    mock_redis.hgetall = AsyncMock(
        return_value={
            "dispatch_summary": json.dumps(dispatch_summary),
            "agent:a1:completed": "3",
            "agent:a1:failed": "0",
            "agent:a1:total": "10",
            "agent:a2:completed": "15",
            "agent:a2:failed": "0",
            "agent:a2:total": "20",
        },
    )
    mock_redis.hset = AsyncMock()
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    response = await client.get(f"/execution/agents-table?batch_id={batch_id}&sort=__class__.__mro__")
    assert response.status_code == 200
    body = response.text
    assert body.index("Agent One") < body.index("Agent Two"), "unwhitelisted sort degrades to the default (name asc)"
    mock_redis.hset.assert_awaited_once_with(f"exec:{batch_id}", mapping={"agents_sort": "name", "agents_order": "asc"})


@pytest.mark.asyncio
async def test_agents_table_sort_unknown_batch_renders_empty_state(client: AsyncClient) -> None:
    """A batch with no (or already-reaped) Redis hash renders the same empty state, not a 404/500."""
    mock_redis = MagicMock()
    mock_redis.hgetall = AsyncMock(return_value={})
    mock_redis.hset = AsyncMock()
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    response = await client.get("/execution/agents-table?batch_id=does-not-exist&sort=total")
    assert response.status_code == 200
    assert "No active sub-jobs." in response.text
    mock_redis.hset.assert_not_awaited()


@pytest.mark.asyncio
async def test_sse_progress_agents_table_honors_persisted_sort(client: AsyncClient) -> None:
    """The SSE ``agents_table`` event re-resolves sort from the SAME hash every tick.

    A header click (the previous test) persists ``agents_sort``/``agents_order`` onto the batch's
    hash; this asserts the long-lived SSE generator -- which never sees that HTTP request -- still
    honours it on its own next read, and that the header's aria-sort/caret state travels with it
    (not just the row order).
    """
    batch_id = uuid.uuid4().hex
    dispatch_summary = [
        {"agent_id": "a1", "name": "Agent One", "total": 10},
        {"agent_id": "a2", "name": "Agent Two", "total": 20},
    ]
    mock_redis = MagicMock()
    mock_redis.hgetall = AsyncMock(
        return_value={
            "total": "30",
            "completed": "18",
            "failed": "0",
            "status": "complete",
            "subjobs_expected": "1",
            "dispatch_summary": json.dumps(dispatch_summary),
            "agents_sort": "total",
            "agents_order": "desc",
            "agent:a1:completed": "3",
            "agent:a1:failed": "0",
            "agent:a1:total": "10",
            "agent:a2:completed": "15",
            "agent:a2:failed": "0",
            "agent:a2:total": "20",
        },
    )
    client._transport.app.state.redis = mock_redis  # type: ignore[union-attr]

    response = await client.get(f"/execution/progress/{batch_id}")
    assert response.status_code == 200
    body = response.text
    assert body.index("Agent Two") < body.index("Agent One"), "total=20 sorts before total=10, descending"
    assert 'aria-sort="descending"' in body
