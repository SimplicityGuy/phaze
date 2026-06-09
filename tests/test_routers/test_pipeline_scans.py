"""Controller-side contract tests for Phase 27 D-05..D-08 pipeline_scans router.

Covers:
- POST /pipeline/scans -- form validation (T-27-03 ``..`` rejection, prefix check,
  agent-revoked guard), ScanBatch creation, AgentTaskRouter.enqueue_for_agent
  call assertion, atomicity on rejection paths.
- GET /pipeline/scans/{batch_id} -- HTMX poll partial; running carries
  hx-trigger="every 2s" + hx-swap="outerHTML", terminal states OMIT both
  (Pitfall 6 invariant verified at the controller level).
- GET /pipeline/scans/agent-roots -- HTMX swap partial; empty-state copy for
  agents with no scan_roots configured.
- Dashboard render -- Trigger Scan card heading + Recent Scans heading present
  on /pipeline/ output.

Uses a self-contained smoke-app fixture (mirrors test_agent_files.py:53-65)
that installs an ``AsyncMock`` at ``app.state.task_router`` so tests can
assert against ``enqueue_for_agent.await_args_list`` without a real Redis
connection. The fixture seeds a single non-revoked agent with scan_roots
configured so most happy-path tests need no extra setup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy import select

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import pipeline, pipeline_scans


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession


def _make_smoke_app(session: AsyncSession) -> tuple[FastAPI, AsyncMock]:
    """Build a smoke FastAPI app mounting pipeline_scans + pipeline routers.

    Returns the app AND the AsyncMock installed at ``app.state.task_router``
    so happy-path tests can assert against ``enqueue_for_agent`` call args.
    """
    app = FastAPI(title="pipeline-scans-smoke", version="test")
    app.include_router(pipeline_scans.router)
    app.include_router(pipeline.router)
    app.dependency_overrides[get_session] = lambda: session
    mock_router = AsyncMock()
    app.state.task_router = mock_router
    # The pipeline router's existing trigger endpoints reference app.state.queue;
    # install a benign mock to keep the dashboard handler import-safe even
    # though dashboard tests do not exercise the queue.
    app.state.queue = AsyncMock()
    return app, mock_router


@pytest_asyncio.fixture
async def smoke(session: AsyncSession) -> AsyncGenerator[tuple[AsyncClient, AsyncMock]]:
    """Smoke client + mock task_router; seeds one non-revoked agent with scan_roots."""
    # Seed a known test agent. Use a kebab-case slug compatible with the
    # Agent.id_charset check constraint.
    agent = Agent(
        id="test-agent",
        name="Test Agent",
        token_hash=None,
        scan_roots=["/data/music", "/data/videos"],
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    app, mock_router = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, mock_router


async def _count_batches(session: AsyncSession) -> int:
    """Count ScanBatch rows in the test session."""
    rows = (await session.execute(select(ScanBatch))).scalars().all()
    return len(rows)


# ---------------------------------------------------------------------------
# Unit: _elapsed_seconds must handle production TIMESTAMP WITH TIME ZONE
# ---------------------------------------------------------------------------


def test_elapsed_seconds_handles_tz_aware_created_at() -> None:
    """Phase 27 UAT Test 2: _elapsed_seconds must NOT crash on tz-aware datetimes.

    The production postgres schema declares `created_at` as TIMESTAMP WITH TIME
    ZONE (from Alembic migrations), so asyncpg materializes it as a tz-aware
    `datetime`. Earlier code did `datetime.now(UTC).replace(tzinfo=None) -
    batch.created_at`, which crashes with
    `TypeError: can't subtract offset-naive and offset-aware datetimes` —
    the scan_progress endpoint then returned 500 and the admin UI's polling
    card went blank.

    Test fixtures use SQLAlchemy's `create_all` which generates TIMESTAMP
    WITHOUT TIME ZONE columns, hiding the divergence. This unit test forces
    a tz-aware `created_at` regardless of DB schema so the bug surfaces.
    """
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import elapsed_seconds

    aware = ScanBatch(
        id=uuid.uuid4(),
        agent_id="dev-agent",
        scan_path="/data/music",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    aware.created_at = datetime.now(UTC) - timedelta(seconds=42)

    elapsed = elapsed_seconds(aware)
    # Allow generous slack for clock drift between the assignment and the call.
    assert 40 <= elapsed <= 60, f"expected elapsed near 42s, got {elapsed}"


def test_no_router_uses_tz_naive_now_antipattern() -> None:
    """Phase 27 UAT gap-14: no router file may strip tzinfo from `datetime.now(UTC)`.

    Gap-12 fixed this in `pipeline_scans._elapsed_seconds` but a sibling copy
    lived inline in `pipeline.dashboard` and crashed the Recent Scans table
    the first time it loaded a real tz-aware `created_at`. Both routers now
    share `phaze.routers.pipeline_scans.elapsed_seconds` -- the helper compares
    aware-to-aware. This test forbids the regression antipattern across the
    entire router package so a third sibling cannot reappear silently.
    """
    from pathlib import Path

    routers_dir = Path(__file__).parent.parent.parent / "src" / "phaze" / "routers"
    offenders: list[str] = []
    for py in routers_dir.rglob("*.py"):
        text = py.read_text()
        # Strip the docstrings/comments to keep the test from flagging the
        # very explanation lines that ARE supposed to call out the antipattern.
        # We only care about call sites in executable code, not narrative prose.
        # A simple line-by-line filter: ignore lines whose first non-whitespace
        # char is `#` or that sit inside triple-quoted strings. The latter is
        # too coarse to parse precisely without an AST walk, so we just scan
        # the AST for matching Call expressions instead.
        import ast

        tree = ast.parse(text)
        for node in ast.walk(tree):
            # Match `<expr>.replace(tzinfo=None)` where `<expr>` is a
            # `datetime.now(...)` call.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace"
                and any(kw.arg == "tzinfo" and isinstance(kw.value, ast.Constant) and kw.value.value is None for kw in node.keywords)
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Attribute)
                and node.func.value.func.attr == "now"
            ):
                offenders.append(f"{py.relative_to(routers_dir.parent.parent.parent)}:{node.lineno}")

    assert not offenders, (
        "Routers must not strip tzinfo from datetime.now() -- production "
        "`created_at` is TIMESTAMP WITH TIME ZONE (tz-aware). Use "
        "phaze.routers.pipeline_scans.elapsed_seconds instead. Offenders: " + ", ".join(offenders)
    )


def test_elapsed_seconds_handles_tz_naive_created_at_as_utc() -> None:
    """Defensive fallback: a tz-naive `created_at` (e.g. from a fixture) is treated as UTC.

    Test schemas use TIMESTAMP WITHOUT TIME ZONE so loaded ScanBatch rows
    have tz-naive `created_at`. The helper must still produce a meaningful
    elapsed value rather than crashing or returning negative numbers.
    """
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import elapsed_seconds

    naive = ScanBatch(
        id=uuid.uuid4(),
        agent_id="dev-agent",
        scan_path="/data/music",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    naive.created_at = (datetime.now(UTC) - timedelta(seconds=42)).replace(tzinfo=None)

    elapsed = elapsed_seconds(naive)
    assert 40 <= elapsed <= 60, f"expected elapsed near 42s, got {elapsed}"


def test_elapsed_seconds_freezes_when_completed_at_set() -> None:
    """Incident 260608: elapsed_seconds freezes at completed_at once set.

    A terminal batch's elapsed timer must stop at the moment it completed,
    independent of wall-clock time. created_at = now-100s, completed_at = now-40s
    -> elapsed is ~60s regardless of how long ago the batch finished.
    """
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import elapsed_seconds

    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="dev-agent",
        scan_path="/data/music",
        status=ScanStatus.COMPLETED.value,
        total_files=0,
        processed_files=0,
    )
    now = datetime.now(UTC)
    batch.created_at = now - timedelta(seconds=100)
    batch.completed_at = now - timedelta(seconds=40)

    elapsed = elapsed_seconds(batch)
    assert 58 <= elapsed <= 62, f"expected frozen elapsed near 60s, got {elapsed}"


def test_elapsed_seconds_tracks_now_when_completed_at_none() -> None:
    """A RUNNING batch (completed_at None) still tracks now - created_at."""
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import elapsed_seconds

    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="dev-agent",
        scan_path="/data/music",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    batch.created_at = datetime.now(UTC) - timedelta(seconds=42)
    batch.completed_at = None

    elapsed = elapsed_seconds(batch)
    assert 40 <= elapsed <= 60, f"expected elapsed near 42s, got {elapsed}"


def test_elapsed_seconds_handles_tz_naive_completed_at_as_utc() -> None:
    """A tz-naive completed_at (test fixture / bypassed coercion) is treated as UTC."""
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import elapsed_seconds

    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="dev-agent",
        scan_path="/data/music",
        status=ScanStatus.COMPLETED.value,
        total_files=0,
        processed_files=0,
    )
    now = datetime.now(UTC)
    batch.created_at = (now - timedelta(seconds=100)).replace(tzinfo=None)
    batch.completed_at = (now - timedelta(seconds=40)).replace(tzinfo=None)

    elapsed = elapsed_seconds(batch)
    assert 58 <= elapsed <= 62, f"expected frozen elapsed near 60s, got {elapsed}"


# ---------------------------------------------------------------------------
# Task 1 (router contract) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_scans_happy_path(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """POST /pipeline/scans creates a RUNNING ScanBatch and enqueues scan_directory."""
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert response.status_code == 200, response.text
    # Body contains the running-state markup (heading + RUNNING pill).
    assert "Scan in progress" in response.text
    assert "RUNNING" in response.text
    assert 'hx-trigger="every 2s"' in response.text

    # AgentTaskRouter.enqueue_for_agent called exactly once with the documented contract.
    mock_router.enqueue_for_agent.assert_awaited_once()
    call = mock_router.enqueue_for_agent.await_args
    assert call.kwargs["agent_id"] == "test-agent"
    assert call.kwargs["task_name"] == "scan_directory"
    payload = call.kwargs["payload"]
    assert payload.scan_path == "/data/music/2026/"
    assert payload.agent_id == "test-agent"
    assert isinstance(payload.batch_id, uuid.UUID)

    # Exactly one new ScanBatch row.
    post_count = await _count_batches(session)
    assert post_count == pre_count + 1
    new_batch = (await session.execute(select(ScanBatch).where(ScanBatch.scan_path == "/data/music/2026/"))).scalar_one()
    assert new_batch.status == "running"
    assert new_batch.agent_id == "test-agent"


@pytest.mark.asyncio
async def test_post_scans_subpath_rejects_dotdot(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """T-27-03: subpath containing ``..`` rejects with 400 + error card; NO batch created."""
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "../../etc"},
    )
    assert response.status_code == 400
    assert 'role="alert"' in response.text
    # Jinja autoescapes `'` to `&#39;`, so check on a substring that survives escaping.
    assert "Subpath must not contain" in response.text
    assert "path traversal" in response.text

    # Atomicity: NO ScanBatch row created on rejection.
    post_count = await _count_batches(session)
    assert post_count == pre_count
    # And NO enqueue.
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_subpath_allows_triple_dot_filename(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WR-01 regression: subpath containing literal ``..`` as a non-component substring is allowed.

    The traversal guard rejects ``..`` *path components* only; legitimate
    filenames/directories containing the substring ``..`` (e.g.,
    ``...thinking.mp3`` for triple-dot, ``Album...Live`` for torrent-archive
    naming) must NOT 400. Previously the simple ``".." in joined`` substring
    check rejected these false-positives.
    """
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "...thinking.mp3"},
    )
    # Should succeed (200 RUNNING) -- the triple-dot filename is a legitimate
    # path component and must not trip the traversal guard.
    assert response.status_code == 200, response.text
    assert "Scan in progress" in response.text
    mock_router.enqueue_for_agent.assert_awaited_once()
    call = mock_router.enqueue_for_agent.await_args
    assert call.kwargs["payload"].scan_path == "/data/music/...thinking.mp3"


@pytest.mark.asyncio
async def test_post_scans_path_outside_scan_root(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """T-27-03: scan_root not in agent.scan_roots rejects with 400."""
    ac, mock_router = smoke

    # /data/photos is NOT in the seeded agent's scan_roots (which are
    # /data/music + /data/videos). The literal-membership check fails.
    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/photos", "subpath": "vacation/"},
    )
    assert response.status_code == 400
    # WR-05: scan_root membership check fires before the prefix check.
    assert "Selected scan root is not configured for this agent." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_unknown_agent_400(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Unknown agent_id rejects with 400 + 'Unknown or revoked agent.'."""
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "nonexistent-agent", "scan_root": "/data/music", "subpath": ""},
    )
    assert response.status_code == 400
    assert "Unknown or revoked agent." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_scan_root_not_in_agent_roots(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WR-05: scan_root NOT literally in agent.scan_roots rejects with 400."""
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        # /etc is not in seeded agent's scan_roots.
        data={"agent_id": "test-agent", "scan_root": "/etc", "subpath": ""},
    )
    assert response.status_code == 400
    # WR-05: literal-membership check fires before the prefix check.
    assert "Selected scan root is not configured for this agent." in response.text
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_enqueue_failure_marks_batch_failed(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WR-06: enqueue failure flips batch to FAILED + returns 503 (no DELETE).

    Previously the failure path tried to DELETE the just-created batch and
    commit; if that secondary commit also raised, the original 500-via-
    unhandled-exception bubble obscured the failure cause AND left an orphan
    RUNNING row that no agent would ever PATCH. The new failure path marks
    the batch FAILED instead, surfacing the attempt in Recent Scans for the
    operator to triage.
    """
    ac, mock_router = smoke
    mock_router.enqueue_for_agent.side_effect = RuntimeError("redis down")

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert response.status_code == 503, response.text
    assert "could not enqueue the scan" in response.text

    # The batch row survives but is FAILED with the documented error_message
    # so the operator sees what happened in Recent Scans.
    rows = (await session.execute(select(ScanBatch).where(ScanBatch.scan_path == "/data/music/2026/"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == ScanStatus.FAILED.value
    assert rows[0].error_message == "controller could not enqueue scan to agent worker"


# ---------------------------------------------------------------------------
# Coverage gap fills (Codecov PR #59): pipeline_scans.py:120, 207, 255-260
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scan_progress_unknown_id_returns_404(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/scans/{unknown_id} returns 404 (pipeline_scans.py:120)."""
    ac, _ = smoke
    unknown_id = uuid.uuid4()
    response = await ac.get(f"/pipeline/scans/{unknown_id}")
    assert response.status_code == 404
    # Detail surfaces in the error envelope so operators can correlate logs to UI.
    assert "scan batch not found" in response.text.lower()


async def test_post_scans_prefix_mismatch_via_direct_handler_invocation(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive prefix check (pipeline_scans.py:207-212).

    The prefix-mismatch branch is structurally defensive: the literal-membership
    check on line 195 dominates the normal failure mode, and well-formed
    subpaths always join to a path that starts with the scan_root. To reach
    the prefix-fail branch we monkeypatch ``unicodedata.normalize`` so the
    NFC pass rewrites the joined path out from under the prefix predicate
    (simulating a hypothetical normalization edge case that today's inputs
    cannot produce). Pins the 400 envelope so a real future normalization
    quirk surfaces as a clean 400, not a 500 or a silent enqueue.
    """
    ac, mock_router = smoke

    from phaze.routers import pipeline_scans as ps_mod

    original_normalize = ps_mod.unicodedata.normalize

    def _normalize_rewriting_joined(form: str, text: str) -> str:
        # Only rewrite the joined path; leave the agent-side normalize
        # passes alone so the literal-membership check still passes.
        if text.startswith("/data/music/"):
            return "/elsewhere/x"  # force prefix mismatch on the joined path
        return original_normalize(form, text)

    monkeypatch.setattr(ps_mod.unicodedata, "normalize", _normalize_rewriting_joined)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert response.status_code == 400, response.text
    assert "Resolved path is outside the selected scan root." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_enqueue_failure_with_secondary_commit_also_failing(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WR-06 inner-except: when enqueue fails AND the secondary commit also
    raises, the handler MUST still return the 503 envelope (no 500 escape).

    Covers pipeline_scans.py:255-260 — the defensive ``try/except`` around
    ``await session.commit()`` after marking the batch FAILED. A Postgres-down
    scenario can plausibly knock out the secondary commit too; the operator's
    503 envelope is more important than the orphan-row cleanup.
    """
    import logging as _logging

    ac, mock_router = smoke
    mock_router.enqueue_for_agent.side_effect = RuntimeError("redis down")

    # Force the SECOND commit (the one that flips batch -> FAILED) to raise.
    # The first commit happens earlier (saves the initial RUNNING batch); we
    # want that to succeed so we reach the inner try/except.
    original_commit = session.commit
    call_state = {"n": 0}

    async def _commit_fails_on_nth_call() -> None:
        call_state["n"] += 1
        if call_state["n"] >= 2:
            raise RuntimeError("postgres down")
        await original_commit()

    monkeypatch.setattr(session, "commit", _commit_fails_on_nth_call)
    rollback_calls = {"n": 0}
    original_rollback = session.rollback

    async def _record_rollback() -> None:
        rollback_calls["n"] += 1
        await original_rollback()

    monkeypatch.setattr(session, "rollback", _record_rollback)

    with caplog.at_level(_logging.ERROR, logger="phaze.routers.pipeline_scans"):
        response = await ac.post(
            "/pipeline/scans",
            data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
        )

    # 503 envelope still surfaces; no 500 leak.
    assert response.status_code == 503, response.text
    assert "could not enqueue the scan" in response.text
    # The secondary-commit failure was logged for triage.
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secondary commit failed" in text, f"missing secondary-commit log: {text!r}"
    # Rollback executed at least once (the handler explicitly issues it on failure).
    assert rollback_calls["n"] >= 1


@pytest.mark.asyncio
async def test_post_scans_rejects_partial_scan_root_prefix(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WR-05 regression: scan_root="/data" + subpath="music/foo" must reject.

    The agent's scan_roots are ["/data/music", "/data/videos"]; "/data" alone
    is a parent path that was never authorized. Previously the joined-path
    prefix check passed because ``"/data/music/foo".startswith("/data/music/")``
    is True, so the audit log would have recorded ``scan_root="/data"`` for a
    scan against ``/data/music/foo`` -- a surprising mode where unconfigured
    scan_roots can authorize sub-trees that happen to fall inside a configured
    one. Tighten the validator to require literal membership.
    """
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        # /data is the *parent* of a real scan_root but is not configured itself.
        data={"agent_id": "test-agent", "scan_root": "/data", "subpath": "music/foo"},
    )
    assert response.status_code == 400
    assert "Selected scan root is not configured for this agent." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_scan_progress_running_returns_polling_partial(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """GET /pipeline/scans/{batch_id} for RUNNING batch carries hx-trigger + hx-swap=outerHTML."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/2026/",
        status=ScanStatus.RUNNING.value,
        total_files=10,
        processed_files=3,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    assert 'hx-trigger="every 2s"' in response.text
    assert 'hx-swap="outerHTML"' in response.text
    assert f'hx-get="/pipeline/scans/{batch.id}"' in response.text
    assert "RUNNING" in response.text


@pytest.mark.asyncio
async def test_get_scan_progress_completed_omits_hx_trigger(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Pitfall 6: COMPLETED batch response OMITS hx-trigger and hx-get (HTMX halts polling)."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/2026/",
        status=ScanStatus.COMPLETED.value,
        total_files=10,
        processed_files=10,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    # Pitfall 6 invariant: NO HTMX polling attributes in terminal-state markup.
    assert "hx-trigger" not in response.text
    assert "hx-get" not in response.text
    assert "Scan complete" in response.text
    assert "COMPLETED" in response.text


@pytest.mark.asyncio
async def test_get_scan_progress_failed_renders_error_message(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """FAILED batch renders error_message AND omits hx-trigger."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/missing/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="path missing",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get(f"/pipeline/scans/{batch.id}")
    assert response.status_code == 200
    assert "path missing" in response.text
    assert "FAILED" in response.text
    assert "hx-trigger" not in response.text
    assert "hx-get" not in response.text


@pytest.mark.asyncio
async def test_agent_roots_swap_returns_partial(smoke: tuple[AsyncClient, AsyncMock]) -> None:
    """GET /pipeline/scans/agent-roots returns scan_path_picker.html with the agent's scan_roots."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/agent-roots", params={"agent_id": "test-agent"})
    assert response.status_code == 200
    assert '<select id="scan-root"' in response.text
    assert '<option value="/data/music">/data/music</option>' in response.text
    assert '<option value="/data/videos">/data/videos</option>' in response.text


@pytest.mark.asyncio
async def test_agent_roots_swap_unknown_agent_yields_empty_state(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """Unknown agent or empty scan_roots yields the empty-state copy."""
    ac, _ = smoke

    response = await ac.get("/pipeline/scans/agent-roots", params={"agent_id": "totally-bogus-agent"})
    assert response.status_code == 200
    # Unknown agent renders the agent=None branch (placeholder "Select an agent first").
    assert "Select an agent first" in response.text


# ---------------------------------------------------------------------------
# Task 2 (template / UI-SPEC) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_trigger_scan_card(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/ surfaces the Trigger Scan card heading + agent dropdown + picker slot."""
    ac, _ = smoke

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert 'id="trigger-scan-heading"' in response.text
    assert ">Trigger Scan</h2>" in response.text
    assert '<select id="scan-agent"' in response.text
    assert 'id="scan-path-picker"' in response.text
    # Agent option populated as "{name} ({id})" per CONTEXT D-Discretion.
    assert "Test Agent (test-agent)" in response.text


@pytest.mark.asyncio
async def test_dashboard_renders_recent_scans_section(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """GET /pipeline/ surfaces the Recent Scans heading + empty state when no batches."""
    ac, _ = smoke

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert 'id="recent-scans-heading"' in response.text
    assert ">Recent Scans</h2>" in response.text
    # No batches seeded -> empty state.
    assert "No scans yet" in response.text


@pytest.mark.asyncio
async def test_dashboard_recent_scans_shows_failed_row_with_inline_error(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Failed batch renders the second inline-error <tr> with red surface + error_message."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/oops/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="path missing",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert 'colspan="6"' in response.text
    assert "bg-red-50" in response.text
    assert "path missing" in response.text


@pytest.mark.asyncio
async def test_dashboard_recent_scans_excludes_live_batches(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """LIVE sentinel batches MUST be excluded from Recent Scans (CONTEXT D-05 / UI-SPEC line 401)."""
    ac, _ = smoke
    live_batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="<watcher>",
        status=ScanStatus.LIVE.value,
        total_files=0,
        processed_files=0,
    )
    session.add(live_batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    # The LIVE sentinel must not surface; the table renders the empty state.
    assert "<watcher>" not in response.text
    assert "No scans yet" in response.text


@pytest.mark.asyncio
async def test_status_pill_running_uses_blue_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """RUNNING status pill renders with bg-blue-100 dark:bg-blue-950 + aria-label."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/",
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert "bg-blue-100 dark:bg-blue-950" in response.text
    assert 'aria-label="Status: running"' in response.text


@pytest.mark.asyncio
async def test_status_pill_completed_uses_green_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """COMPLETED status pill renders with bg-green-100."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/done/",
        status=ScanStatus.COMPLETED.value,
        total_files=5,
        processed_files=5,
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert "bg-green-100" in response.text
    assert 'aria-label="Status: completed"' in response.text


@pytest.mark.asyncio
async def test_status_pill_failed_uses_red_surface(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """FAILED status pill renders with bg-red-100."""
    ac, _ = smoke
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="test-agent",
        scan_path="/data/music/oops/",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
        error_message="oops",
    )
    session.add(batch)
    await session.commit()

    response = await ac.get("/pipeline/")
    assert response.status_code == 200
    assert "bg-red-100" in response.text
    assert 'aria-label="Status: failed"' in response.text


@pytest.mark.asyncio
async def test_router_registered_in_main_app() -> None:
    """pipeline_scans.router is registered in main.create_app() (production wiring)."""
    from phaze.main import create_app

    app = create_app()
    paths = {route.path for route in app.routes if hasattr(route, "path")}  # type: ignore[attr-defined]
    # All three handlers must be reachable on the production app.
    assert "/pipeline/scans" in paths
    assert "/pipeline/scans/{batch_id}" in paths
    assert "/pipeline/scans/agent-roots" in paths
