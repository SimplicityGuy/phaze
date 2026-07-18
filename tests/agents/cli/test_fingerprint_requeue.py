"""Tests for `phaze fingerprint requeue` -- outage recovery (phaze-rf04.1).

The behaviour under test is the one the 2026-07-18 incident made expensive to get wrong:
a recovery command that re-drives the WRONG population either resurrects files an operator
deliberately skipped or re-burns genuinely-corrupt input. The window filter and the three
exclusions are therefore asserted individually, not as one happy path.
"""

import asyncio
import datetime
from typing import Any
import uuid

import pytest
from sqlalchemy import NullPool, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze import cli
from phaze.cli import parse_window
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.fingerprint import FingerprintResult
from phaze.services.enqueue_router import NoActiveAgentError
from phaze.services.fingerprint_requeue import enqueue_fingerprint_jobs, select_outage_failed_files
from tests.conftest import TEST_DATABASE_URL


OUTAGE_START = datetime.datetime(2026, 7, 18, 5, 0, tzinfo=datetime.UTC)
OUTAGE_END = datetime.datetime(2026, 7, 18, 13, 39, tzinfo=datetime.UTC)
IN_WINDOW = datetime.datetime(2026, 7, 18, 9, 0, tzinfo=datetime.UTC)
BEFORE_WINDOW = datetime.datetime(2026, 7, 17, 9, 0, tzinfo=datetime.UTC)


async def _fail_both_engines(session: AsyncSession, file_id: uuid.UUID, when: datetime.datetime) -> None:
    """Seed the file-level FAILED fact: both engines failed, none succeeded (ELIG-04)."""
    for engine in ("audfprint", "panako"):
        session.add(FingerprintResult(file_id=file_id, engine=engine, status="failed", error_message="engine down"))
    await session.commit()
    # `updated_at` is mixin-managed, so stamp the window explicitly rather than relying on
    # wall clock. The column is TIMESTAMP WITHOUT TIME ZONE, so write naive UTC.
    naive = when.astimezone(datetime.UTC).replace(tzinfo=None)
    await session.execute(
        FingerprintResult.__table__.update().where(FingerprintResult.file_id == file_id).values(updated_at=naive, created_at=naive),
    )
    await session.commit()


class _FakeQueue:
    """Records enqueue calls; returns ``None`` for ids in ``dedup`` (deterministic-key collapse)."""

    def __init__(self, dedup: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._dedup = dedup or set()

    async def connect(self) -> None:
        return None

    async def enqueue(self, function: str, **kwargs: Any) -> object | None:
        self.calls.append({"function": function, **kwargs})
        return None if str(kwargs.get("file_id")) in self._dedup else object()


# ---------------------------------------------------------------------------
# parse_window — pure, no DB.
# ---------------------------------------------------------------------------


def test_parse_window_treats_naive_input_as_utc() -> None:
    # Load-bearing: `updated_at` is UTC in Postgres. Applying the shell's local offset
    # would silently shift the window by hours and select the wrong population.
    parsed = parse_window("2026-07-18T05:00:00", "--since")
    assert parsed == OUTAGE_START
    assert parsed.tzinfo is datetime.UTC


def test_parse_window_normalizes_aware_input_to_utc() -> None:
    parsed = parse_window("2026-07-18T07:00:00+02:00", "--since")
    assert parsed == OUTAGE_START


@pytest.mark.parametrize("bad", ["", "not-a-date", "2026-13-45", "18/07/2026"])
def test_parse_window_rejects_non_iso(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid --since"):
        parse_window(bad, "--since")


# ---------------------------------------------------------------------------
# Selection — the exclusions are the point.
# ---------------------------------------------------------------------------


async def test_selects_file_failed_inside_window(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    f = await make_file()
    await _fail_both_engines(session, f.id, IN_WINDOW)

    found = await select_outage_failed_files(session, OUTAGE_START, OUTAGE_END)

    assert [x.id for x in found] == [f.id]


async def test_excludes_failure_outside_window(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    # A genuinely-corrupt file that a HEALTHY engine rejected the day before must not be
    # swept into an outage recovery -- this is the whole reason the window is required.
    f = await make_file()
    await _fail_both_engines(session, f.id, BEFORE_WINDOW)

    found = await select_outage_failed_files(session, OUTAGE_START, OUTAGE_END)

    assert found == []


async def test_excludes_file_with_a_successful_engine(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    # DERIV-05: any engine success wins, so the file is not stage-FAILED at all.
    f = await make_file()
    await _fail_both_engines(session, f.id, IN_WINDOW)
    # (file_id, engine) is unique, so flip the existing panako row rather than inserting.
    await session.execute(
        FingerprintResult.__table__.update().where(FingerprintResult.file_id == f.id, FingerprintResult.engine == "panako").values(status="success"),
    )
    await session.commit()

    found = await select_outage_failed_files(session, OUTAGE_START, OUTAGE_END)

    assert found == []


async def test_excludes_dedup_resolved_file(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    f = await make_file()
    await _fail_both_engines(session, f.id, IN_WINDOW)
    session.add(DedupResolution(file_id=f.id))
    await session.commit()

    found = await select_outage_failed_files(session, OUTAGE_START, OUTAGE_END)

    assert found == []


async def test_limit_caps_selection(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        f = await make_file()
        await _fail_both_engines(session, f.id, IN_WINDOW)

    found = await select_outage_failed_files(session, OUTAGE_START, OUTAGE_END, limit=2)

    assert len(found) == 2


# ---------------------------------------------------------------------------
# Enqueue funnel — the payload shape is what dead-letters if it drifts.
# ---------------------------------------------------------------------------


async def test_enqueue_sends_complete_payload(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    f = await make_file()
    queue = _FakeQueue()

    accepted = await enqueue_fingerprint_jobs(queue, [f], "test-fileserver")

    assert accepted == 1
    call = queue.calls[0]
    assert call["function"] == "fingerprint_file"
    # A file_id-only enqueue dead-letters every job; assert the full contract.
    assert call["file_id"] == str(f.id)
    assert call["original_path"] == f.original_path
    assert call["agent_id"] == "test-fileserver"


async def test_enqueue_does_not_count_deduped_jobs(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    # An already-in-flight file collapses to None. Reporting it as re-queued would
    # overstate the recovery.
    a = await make_file()
    b = await make_file()
    queue = _FakeQueue(dedup={str(a.id)})

    accepted = await enqueue_fingerprint_jobs(queue, [a, b], "test-fileserver")

    assert accepted == 1
    assert len(queue.calls) == 2


# ---------------------------------------------------------------------------
# main() argument validation — every error branch returns 1.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--since", "nope", "--until", "2026-07-18T13:39:00"], "invalid --since"),
        (["--since", "2026-07-18T05:00:00", "--until", "nope"], "invalid --until"),
        (["--since", "2026-07-18T13:39:00", "--until", "2026-07-18T05:00:00"], "must be after"),
        (["--since", "2026-07-18T05:00:00", "--until", "2026-07-18T05:00:00"], "must be after"),
        (["--since", "2026-07-18T05:00:00", "--until", "2026-07-18T13:39:00", "--limit", "0"], "--limit must be >= 1"),
    ],
)
def test_main_rejects_bad_arguments(argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["fingerprint", "requeue", *argv])

    assert rc == 1
    assert expected in capsys.readouterr().err


def test_main_dispatches_fingerprint_group_without_touching_agent_args(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard: main() used to read args.agent_id unconditionally, which
    # AttributeErrors the moment a second subcommand group exists.
    async def _unreachable(*_a: object, **_k: object) -> tuple[int, int, str | None]:
        raise AssertionError("validation must reject before any DB work")

    monkeypatch.setattr(cli, "_run_requeue", _unreachable)
    rc = cli.main(["fingerprint", "requeue", "--since", "bad", "--until", "bad"])

    assert rc == 1


# ---------------------------------------------------------------------------
# main() end-to-end against the real DB.
# ---------------------------------------------------------------------------


@pytest.fixture
def _cleanup_committed_fingerprints(async_engine: object) -> "object":  # type: ignore[misc]
    """Remove fingerprint rows the committing ``main()`` cells leave behind.

    ``main()`` runs its own ``asyncio.run`` against a real sessionmaker and COMMITS, so the
    hermetic ``session`` rollback cannot undo it (same reasoning as ``_cleanup_committed_agents``).
    """
    yield

    async def _clean() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(FingerprintResult))
        finally:
            await engine.dispose()

    asyncio.run(_clean())


def _install_committing_sessionmaker(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point cli.async_session at a NullPool sessionmaker safe for main()'s own loop."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(cli, "async_session", factory)
    return engine


def test_main_dry_run_reports_without_enqueueing(
    async_engine: object,
    _cleanup_committed_fingerprints: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[object] = []

    async def _record(*a: object, **_k: object) -> int:
        calls.append(a)
        return 0

    monkeypatch.setattr(cli, "enqueue_fingerprint_jobs", _record)
    engine = _install_committing_sessionmaker(monkeypatch)
    try:
        rc = cli.main(["fingerprint", "requeue", "--since", "2026-07-18T05:00:00", "--until", "2026-07-18T13:39:00", "--dry-run"])
    finally:
        asyncio.run(engine.dispose())

    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out
    assert calls == []


def test_main_reports_empty_window(
    async_engine: object,
    _cleanup_committed_fingerprints: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = _install_committing_sessionmaker(monkeypatch)
    try:
        rc = cli.main(["fingerprint", "requeue", "--since", "2020-01-01T00:00:00", "--until", "2020-01-02T00:00:00"])
    finally:
        asyncio.run(engine.dispose())

    assert rc == 0
    assert "nothing to re-queue" in capsys.readouterr().out


async def test_run_requeue_routes_to_the_agents_fingerprint_lane(
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live enqueue path: correct lane, connected queue, real counts."""
    f = await make_file()
    await _fail_both_engines(session, f.id, IN_WINDOW)

    queue = _FakeQueue()
    captured: dict[str, Any] = {}

    class _FakeRouter:
        def __init__(self, **_kw: object) -> None:
            pass

        def queue_for(self, agent_id: str, lane: str) -> _FakeQueue:
            captured["agent_id"] = agent_id
            captured["lane"] = lane
            return queue

    async def _agent(*_a: object, **_k: object) -> object:
        return type("A", (), {"id": "test-fileserver"})()

    # Reuse the hermetic session so the seeded rows are visible without committing.
    monkeypatch.setattr(cli, "async_session", lambda: _NullCtx(session))
    monkeypatch.setattr(cli, "AgentTaskRouter", _FakeRouter)
    monkeypatch.setattr(cli, "select_active_agent", _agent)

    selected, accepted, agent_id = await cli._run_requeue(OUTAGE_START, OUTAGE_END, None, dry_run=False)

    assert (selected, accepted, agent_id) == (1, 1, "test-fileserver")
    # Stranding fingerprint work on the analyze lane is the Phase-30 bug this guards.
    assert captured["lane"] == "fingerprint"
    assert queue.calls[0]["function"] == "fingerprint_file"


class _NullCtx:
    """Async-context wrapper yielding an existing session without closing it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def test_main_reports_no_active_agent_without_enqueueing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The ordering guarantee: agent resolution fails BEFORE anything is enqueued.
    async def _boom(*_a: object, **_k: object) -> tuple[int, int, str | None]:
        raise NoActiveAgentError

    monkeypatch.setattr(cli, "_run_requeue", _boom)
    rc = cli.main(["fingerprint", "requeue", "--since", "2026-07-18T05:00:00", "--until", "2026-07-18T13:39:00"])

    assert rc == 1
    assert "no active fileserver agent" in capsys.readouterr().err


def test_main_success_reports_counts_and_the_resume_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def _fake(*_a: object, **_k: object) -> tuple[int, int, str | None]:
        return 10, 7, "nox"

    monkeypatch.setattr(cli, "_run_requeue", _fake)
    rc = cli.main(["fingerprint", "requeue", "--since", "2026-07-18T05:00:00", "--until", "2026-07-18T13:39:00"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Re-queued 7 of 10" in out
    assert "3 skipped" in out
    # The parked-jobs warning is the operational safeguard against resuming too early.
    assert "/pipeline/stages/fingerprint/resume" in out
