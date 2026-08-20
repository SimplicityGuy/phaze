"""Controller-side tests for `pipeline_scans`'s RUNNING-batch/duplicate-guard + state-timer
phases (split from test_pipeline_scans.py, phaze-1i0h6.6).

Covers `_insert_running_scan_batch` (the partial-unique duplicate guard, phaze-1a71; the
no-refresh / `expire_on_commit=False` assumption, phaze-266lc) and the elapsed/stall timer
helpers (`elapsed_seconds`, `seconds_since_progress`, `is_scan_stalled`) that compute a batch's
displayed state -- plus the repo-wide guard (with seeded-mutation proofs) forbidding the
tz-naive-`.replace(tzinfo=None)` antipattern those helpers exist to avoid (Phase 27 UAT gap-14).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline_scans._shared import (
    _ROUTERS_DIR,
    Path,
    ScanBatch,
    ScanStatus,
    _assert_swappable_alert,
    _copy_routers,
    _count_batches,
    _naive_now_offenders,
    _running_batch,
    pipeline_scans,
    pytest,
    select,
    uuid,
)


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


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
    offenders = _naive_now_offenders(_ROUTERS_DIR)
    assert not offenders, (
        "Routers must not strip tzinfo from datetime.now() -- production "
        "`created_at` is TIMESTAMP WITH TIME ZONE (tz-aware). Use "
        "phaze.routers.pipeline_scans.elapsed_seconds instead. Offenders: " + ", ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Seeded-mutation proof (phaze-7l8jh): a guard with branching logic that
# cannot be shown to fail is worse than no guard, because it reads as
# coverage. Both tests mutate a COPY of the routers tree -- the real one is
# never touched -- following the same pattern as
# tests/shared/core/test_route_reachability.py's seeded-mutation tests.
# ---------------------------------------------------------------------------


def test_copied_routers_tree_is_clean(tmp_path: Path) -> None:
    """Control for the two mutation tests below: an unmutated copy is clean."""
    assert _naive_now_offenders(_copy_routers(tmp_path)) == []


def test_seeded_chained_naive_now_is_caught(tmp_path: Path) -> None:
    """Mutation 1 -- reseed the original gap-14 shape: `datetime.now(UTC).replace(tzinfo=None)`.

    This is the shape the original test always caught. Kept as a control so a
    future edit to `_naive_now_offenders` cannot silently stop catching the
    incident this test exists to prevent, even while the broader mutation
    below keeps passing.
    """
    root = _copy_routers(tmp_path)
    victim = root / "pipeline_scans.py"
    victim.write_text(
        victim.read_text()
        + "\n\ndef _seeded_offender() -> None:\n    from datetime import UTC, datetime\n\n    datetime.now(UTC).replace(tzinfo=None)\n",
    )

    offenders = _naive_now_offenders(root)
    assert any("pipeline_scans.py" in offender for offender in offenders), "the guard missed the directly-chained antipattern it was written for"


def test_seeded_assign_then_strip_naive_now_is_caught(tmp_path: Path) -> None:
    """Mutation 2 -- the fail-open gap: `dt = datetime.now(UTC); dt = dt.replace(tzinfo=None)`.

    Splitting the call across two statements produces the exact same runtime
    bug (an aware `datetime.now(UTC)` stripped back to naive) but the
    receiver of `.replace(tzinfo=None)` is a `Name`, not a `.now(...)` call
    expression. Before phaze-7l8jh this mutation passed `_naive_now_offenders`
    with an EMPTY offenders list -- the guard failed open on it. This test
    pins the fix; if the receiver-shape check is ever reintroduced, this is
    the test that goes red.
    """
    root = _copy_routers(tmp_path)
    victim = root / "pipeline_scans.py"
    victim.write_text(
        victim.read_text()
        + "\n\ndef _seeded_split_offender() -> None:\n    from datetime import UTC, datetime\n\n    now = datetime.now(UTC)\n    now = now.replace(tzinfo=None)\n",
    )

    offenders = _naive_now_offenders(root)
    assert any("pipeline_scans.py" in offender for offender in offenders), "the guard failed open on the assign-then-strip shape (phaze-7l8jh)"


def test_documented_inline_exemption_exempts_only_its_call(tmp_path: Path) -> None:
    """A justified marker exempts its call; removing it exposes the same call."""
    root = _copy_routers(tmp_path)
    victim = root / "pipeline_scans.py"
    original = victim.read_text()
    marker = "  # phaze: allow-naive-tz -- match a schema column that deliberately stores naive timestamps"
    seeded = original + f"\n\ndef _seeded_valid_normalization(value: object) -> None:\n    value.replace(tzinfo=None){marker}\n"
    victim.write_text(seeded)

    assert _naive_now_offenders(root) == []

    victim.write_text(seeded.replace(marker, ""))
    offenders = _naive_now_offenders(root)
    assert any("pipeline_scans.py" in offender for offender in offenders), "removing the exemption did not expose the normalization"


def test_bare_inline_exemption_is_not_accepted(tmp_path: Path) -> None:
    """The escape hatch must explain why stripping timezone awareness is correct."""
    root = _copy_routers(tmp_path)
    victim = root / "pipeline_scans.py"
    victim.write_text(
        victim.read_text() + "\n\ndef _seeded_bare_exemption(value: object) -> None:\n    value.replace(tzinfo=None)  # phaze: allow-naive-tz\n"
    )

    offenders = _naive_now_offenders(root)
    assert any("pipeline_scans.py" in offender for offender in offenders), "a bare, undocumented exemption was accepted"


def test_elapsed_seconds_handles_tz_naive_created_at_as_utc() -> None:
    """Defensive fallback: a tz-naive `created_at` (e.g. from a fixture) is treated as UTC.

    Post-phaze-cz3m the schema is uniformly timestamptz, so a DB-loaded ScanBatch no longer
    arrives naive. The remaining sources are hand-built fixtures and in-memory rows, which this
    covers. The helper must still produce a meaningful elapsed value rather than crashing or
    returning negative numbers.
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


def test_elapsed_seconds_freezes_terminal_completed_with_null_completed_at() -> None:
    """Incident 260609: a COMPLETED row with NULL completed_at freezes at updated_at.

    Legacy / pre-backfill terminal rows never stamped completed_at. The defensive
    read must freeze them at ``updated_at`` (the recorded transition time) rather
    than tracking ``now`` forever. created_at = now-100s, updated_at = now-40s
    -> elapsed ~= 60s, NOT ~100s.
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
    batch.updated_at = now - timedelta(seconds=40)
    batch.completed_at = None

    elapsed = elapsed_seconds(batch)
    assert 58 <= elapsed <= 62, f"expected frozen elapsed near 60s, got {elapsed}"


def test_elapsed_seconds_freezes_terminal_failed_with_null_completed_at() -> None:
    """A FAILED row with NULL completed_at also freezes at updated_at (terminal set)."""
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import elapsed_seconds

    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id="dev-agent",
        scan_path="/data/music",
        status=ScanStatus.FAILED.value,
        total_files=0,
        processed_files=0,
    )
    now = datetime.now(UTC)
    batch.created_at = now - timedelta(seconds=100)
    batch.updated_at = now - timedelta(seconds=40)
    batch.completed_at = None

    elapsed = elapsed_seconds(batch)
    assert 58 <= elapsed <= 62, f"expected frozen elapsed near 60s, got {elapsed}"


def test_elapsed_seconds_terminal_null_treats_tz_naive_updated_at_as_utc() -> None:
    """A terminal+NULL row with a tz-naive updated_at is treated as UTC and frozen."""
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
    batch.updated_at = (now - timedelta(seconds=40)).replace(tzinfo=None)
    batch.completed_at = None

    elapsed = elapsed_seconds(batch)
    assert 58 <= elapsed <= 62, f"expected frozen elapsed near 60s, got {elapsed}"


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
    # phaze-0wme: the trailing slash on the submitted subpath is canonicalized away before
    # persisting/dispatching, so the joined path collapses to its canonical (no trailing
    # slash) form.
    assert payload.scan_path == "/data/music/2026"
    assert payload.agent_id == "test-agent"
    assert isinstance(payload.batch_id, uuid.UUID)
    # scan_directory is a long-running bulk walk: enqueue MUST disable the SAQ
    # wall-clock timeout (timeout=0 -> unbounded) and retries (retries=0) so a
    # healthy, progressing scan is never killed/looped. Liveness is enforced by
    # the progress-based stall reaper (config.scan_stall_seconds).
    assert call.kwargs["timeout"] == 0
    assert call.kwargs["retries"] == 0

    # Exactly one new ScanBatch row.
    post_count = await _count_batches(session)
    assert post_count == pre_count + 1
    new_batch = (await session.execute(select(ScanBatch).where(ScanBatch.scan_path == "/data/music/2026"))).scalar_one()
    assert new_batch.status == "running"
    assert new_batch.agent_id == "test-agent"


@pytest.mark.asyncio
async def test_post_scans_does_not_refresh_before_enqueue(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-266lc: trigger_scan drops the redundant post-commit ``session.refresh(batch)``.

    The sessionmaker is ``expire_on_commit=False`` (database.py), so ``batch``'s attributes already
    survive the RUNNING-batch commit without a refresh -- the refresh's sole effect was to autobegin
    a NEW read transaction on the request session that then sat idle-in-transaction across the
    ``enqueue_for_agent`` broker call (the phaze-1v37 pool-drain class). Spy on both ``refresh`` and
    ``enqueue_for_agent`` and assert refresh is never called at all.
    """
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    ac, mock_router = smoke
    refresh_calls: list[str] = []
    real_refresh = _AsyncSession.refresh

    async def _spy_refresh(self: _AsyncSession, *args: object, **kwargs: object) -> None:
        refresh_calls.append("refresh")
        await real_refresh(self, *args, **kwargs)

    monkeypatch.setattr(_AsyncSession, "refresh", _spy_refresh)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert response.status_code == 200, response.text
    mock_router.enqueue_for_agent.assert_awaited_once()
    assert refresh_calls == [], "trigger_scan must not refresh() the RUNNING batch before enqueueing (phaze-266lc)"


# ---------------------------------------------------------------------------
# PR4: seconds_since_progress / is_scan_stalled helpers (pure, tz-safe)
# ---------------------------------------------------------------------------


def test_seconds_since_progress_uses_last_progress_at() -> None:
    """seconds_since_progress measures from last_progress_at when present (tz-aware)."""
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import seconds_since_progress

    b = _running_batch(datetime.now(UTC) - timedelta(seconds=42))
    assert 40 <= seconds_since_progress(b) <= 60


def test_seconds_since_progress_falls_back_to_created_at() -> None:
    """With last_progress_at NULL, seconds_since_progress falls back to created_at."""
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import seconds_since_progress

    b = _running_batch(None)
    b.created_at = datetime.now(UTC) - timedelta(seconds=42)
    assert 40 <= seconds_since_progress(b) <= 60


def test_seconds_since_progress_handles_tz_naive_as_utc() -> None:
    """A tz-naive last_progress_at (test-schema TIMESTAMP WITHOUT TIME ZONE) is assumed UTC."""
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import seconds_since_progress

    b = _running_batch((datetime.now(UTC) - timedelta(seconds=42)).replace(tzinfo=None))
    assert 40 <= seconds_since_progress(b) <= 60


def test_is_scan_stalled_true_when_quiet_past_warn_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A RUNNING batch quiet past half scan_stall_seconds (pinned 600 -> 300s) is stalled.

    The production default is now 86400 (24h); pin it to 600 here so the 400s-quiet
    batch is unambiguously past the half-threshold warn line regardless of the default.
    """
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from phaze.routers.pipeline_scans import is_scan_stalled

    monkeypatch.setattr(pipeline_scans, "get_settings", lambda: SimpleNamespace(scan_stall_seconds=600))
    b = _running_batch(datetime.now(UTC) - timedelta(seconds=400))
    assert is_scan_stalled(b) is True


def test_is_scan_stalled_false_when_fresh() -> None:
    """A RUNNING batch with a recent heartbeat is not stalled."""
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import is_scan_stalled

    b = _running_batch(datetime.now(UTC) - timedelta(seconds=10))
    assert is_scan_stalled(b) is False


def test_is_scan_stalled_false_for_non_running() -> None:
    """Only RUNNING batches can be 'stalled' in the UI sense; terminal/LIVE return False."""
    from datetime import UTC, datetime, timedelta

    from phaze.routers.pipeline_scans import is_scan_stalled

    for status in (ScanStatus.COMPLETED, ScanStatus.FAILED, ScanStatus.LIVE):
        b = _running_batch(datetime.now(UTC) - timedelta(days=1))
        b.status = status.value
        assert is_scan_stalled(b) is False, f"{status} must never be UI-stalled"


@pytest.mark.asyncio
async def test_post_scans_duplicate_running_batch_is_a_swappable_alert(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Branch 6/6 -- a RUNNING batch already exists for this agent+path (phaze-1a71).

    A double submit for the same resolved path (a slow first request re-clicked, or a
    re-submit an hour into a scan that looks stalled) must not dispatch a second concurrent
    full SHA-256 archive walk of the same tree: the second insert fails the durable
    `uq_scan_batches_agent_id_scan_path_running` partial unique index (migration 044), and the
    handler renders the same swappable alert shape as every other rejection -- NOT a second
    RUNNING batch, NOT a second enqueue.
    """
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    first = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert first.status_code == 200, first.text
    assert "RUNNING" in first.text
    mock_router.enqueue_for_agent.assert_awaited_once()

    second = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    _assert_swappable_alert(second, "already running")

    # Exactly one enqueue happened (the first submit); the second is refused before enqueue.
    mock_router.enqueue_for_agent.assert_awaited_once()
    # Exactly one new ScanBatch row -- the durable index refused the second insert.
    post_count = await _count_batches(session)
    assert post_count == pre_count + 1


@pytest.mark.asyncio
async def test_post_scans_duplicate_running_batch_survives_a_trailing_slash_respelling(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """phaze-0wme -- a differently-spelled resubmit of the SAME resolved path must still

    collide with the running batch. Before the fix, `joined` was never canonicalized: a
    first submit of `subpath="2026"` and a second of `subpath="2026/"` produced two
    byte-different `scan_path` strings, both passed the `uq_scan_batches_agent_id_scan_path_running`
    partial-unique index (migration 044), and a second full SHA-256 archive walk was
    dispatched concurrently with the first. Canonicalizing `joined` with
    `str(PurePosixPath(joined))` before it is persisted / compared collapses the trailing
    slash so the second submit hits the same guard as a byte-identical resubmit.
    """
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    first = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026"},
    )
    assert first.status_code == 200, first.text
    assert "RUNNING" in first.text
    mock_router.enqueue_for_agent.assert_awaited_once()

    second = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    _assert_swappable_alert(second, "already running")

    # Exactly one enqueue happened -- the respelled resubmit must not dispatch a second
    # concurrent full-archive walk.
    mock_router.enqueue_for_agent.assert_awaited_once()
    post_count = await _count_batches(session)
    assert post_count == pre_count + 1
