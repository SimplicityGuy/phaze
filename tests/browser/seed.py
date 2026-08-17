"""phaze-8p1uq: data seeding for the real-browser suite.

Why this module exists
======================

``phaze-tzy6s.14`` built the harness -- uvicorn, migrations, Chromium -- and left the database
empty. ``conftest.py`` DROPs and CREATEs the browser database at session start and the app migrates
it on boot, so every workspace rendered its empty state and nothing else. That is not a gap in the
tests, it is a gap in what the tests can *reach*: proposal approval, a real execute dispatch, scan
management, duplicate resolution and every OOB/SSE path are structurally unreachable from an empty
corpus. Four of the five fragile flows this bead named were uncovered for exactly that reason.

The seeding contract
====================

Seeding writes through **the application's own SQLAlchemy models**, over a connection this test
process owns, to the SAME database the live uvicorn is serving. It deliberately does not go through
the HTTP API: the API is the thing under test, and half the states worth covering (a FAILED scan
batch carrying an ``error_message``, a proposal already APPROVED, a file whose only marker is an
EXECUTED proposal) have no operator-reachable route that produces them on demand.

Two consequences follow and both are load-bearing:

* **The app must not be holding a stale read.** It won't -- every request opens its own session and
  the app runs ``READ COMMITTED``, so a committed row is visible to the next request. But nothing
  here may be seeded *after* the page that reads it has already rendered; seed first, navigate
  second. Tests that need a mid-flight change (the live-refresh state) seed and then wait for the
  shell's own 5s poll to pick it up, which is the real refresh path rather than a reload.
* **Every seeder commits.** A held-open transaction would be invisible to the app and the test
  would fail as though the feature were broken.

Isolation
=========

:func:`reset` truncates every application table before each test. It is the whole isolation story
for this suite -- there is no per-test transaction to roll back, because the writes have to be
visible to a different process. ``alembic_version`` and SAQ's own tables are excluded: dropping the
former would make the app think it is unmigrated, and the latter is queue state the app's lifespan
owns.

No private archive identifiers
==============================

Every path, filename and hash below is synthetic and follows the ``docs/spikes`` placeholder
vocabulary (``<set-NN>``, ``<track-NN>``, ``<archive-mount>``). Nothing here was copied from, or
derived from, a real archive. Keep it that way: a browser test that needs "a realistic filename"
needs an *invented* realistic filename.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from phaze.models.agent import Agent
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.scan_batch import ScanBatch, ScanStatus


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


# The archive mount every seeded path hangs off. A PLACEHOLDER, per CLAUDE.md's conventions -- it is
# not where anybody's music actually lives and must never be replaced with a path that is.
ARCHIVE_MOUNT = "/archive-mount"

# The agent every seeded file belongs to unless a test says otherwise. A single default keeps
# execute-dispatch grouping (which groups by ``FileRecord.agent_id``) predictable: one agent means
# one group, which is what a golden-path assertion wants to be able to count.
DEFAULT_AGENT_ID = "browser-fileserver"

# Tables the app owns but this module must not touch. ``alembic_version`` is the app's own record of
# a successful migration -- truncating it makes the next boot believe the schema is unbuilt. The SAQ
# tables are queue state owned by the lifespan of the *running* server, not by any test.
_PRESERVED_TABLES = frozenset({"alembic_version", "saq_jobs", "saq_versions", "saq_stats"})


def _sha256() -> str:
    """A unique, syntactically valid 64-hex-char digest.

    Synthetic by construction: it is two UUID4 hexes concatenated, never a digest of real content.
    Content digests taken from live data are on CLAUDE.md's never-commit list, and a *derived*
    identifier is exactly the shape that gets committed by accident.
    """
    return uuid.uuid4().hex + uuid.uuid4().hex


_RESET_DEADLOCK_RETRIES = 4


def _is_deadlock(exc: BaseException) -> bool:
    """True for Postgres SQLSTATE 40P01 (deadlock_detected), whatever driver wrapped it."""
    codes = {getattr(e, "sqlstate", None) or getattr(getattr(e, "orig", None), "sqlstate", None) for e in (exc, exc.__cause__)}
    return "40P01" in codes or "deadlock detected" in str(exc).lower()


async def reset(session: AsyncSession) -> None:
    """TRUNCATE every application table so each test starts from the empty state the app booted on.

    ``CASCADE`` is required rather than tidy: ``files`` is referenced by nine tables and
    ``scan_batches``/``agents`` sit upstream of it, so a per-table DELETE would need a topological
    order that changes every time a model is added. One TRUNCATE of the whole set, computed from the
    live catalogue, cannot drift out of date with the schema.

    The table list is read from ``information_schema`` rather than from ``Base.metadata`` on
    purpose. The authority on what exists in this database is the database -- a model this test
    process happens not to import would be silently left populated, and cross-test bleed through a
    table nobody remembered is precisely the failure this function exists to prevent.
    """
    result = await session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"),
    )
    tables = sorted(name for (name,) in result.all() if name not in _PRESERVED_TABLES)
    if not tables:
        return
    # Interpolated rather than bound because identifiers cannot be parameters in SQL. Safe: every
    # name came from pg_tables on this connection a statement ago, not from any caller input.
    quoted = ", ".join(f'public."{name}"' for name in tables)

    # Retried on deadlock, which is EXPECTED here rather than exceptional. TRUNCATE takes an
    # AccessExclusiveLock on every table at once, while the live app under test is concurrently
    # holding AccessShareLock on some of them -- the shell runs a 5s stats poll and keeps SSE
    # streams open, so it is never quiescent. Postgres resolves the resulting lock cycle by killing
    # one side, and which side it picks is arbitrary:
    #
    #   DeadlockDetectedError: Process A waits for AccessExclusiveLock ... blocked by process B;
    #   Process B waits for AccessShareLock ... blocked by process A.
    #
    # Observed once in 124 browser tests, on one parametrized cell -- i.e. a flake that reports as
    # a setup ERROR on an unrelated test and reads as that test being broken. Retrying is correct
    # rather than a paper-over: the truncate is idempotent, the loser of the deadlock has already
    # been rolled back by the server, and the only alternative (quiescing the app first) would mean
    # not testing it live.
    for attempt in range(_RESET_DEADLOCK_RETRIES):
        try:
            await session.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
            await session.commit()
            return
        except DBAPIError as exc:
            await session.rollback()
            if not _is_deadlock(exc) or attempt == _RESET_DEADLOCK_RETRIES - 1:
                raise
            await asyncio.sleep(0.25 * (attempt + 1))


@dataclass
class Seeder:
    """Model-level factories for the browser suite, bound to one session on the browser database.

    Every method commits before returning. See the module docstring for why that is not optional.
    """

    session: AsyncSession
    _agents: set[str] = field(default_factory=set)

    # --- agents ---------------------------------------------------------------------------

    async def agent(
        self,
        agent_id: str = DEFAULT_AGENT_ID,
        *,
        name: str | None = None,
        scan_roots: Sequence[str] | None = None,
        revoked: bool = False,
    ) -> Agent:
        """Insert (or return) one registered agent.

        Idempotent within a test: ``file()`` calls this implicitly, so a test that also wants to
        name the agent explicitly does not have to order the two calls carefully.
        """
        if agent_id in self._agents:
            existing = await self.session.get(Agent, agent_id)
            if existing is not None:
                return existing
        record = Agent(
            id=agent_id,
            name=name or agent_id,
            kind="fileserver",
            scan_roots=list(scan_roots) if scan_roots is not None else [f"{ARCHIVE_MOUNT}/incoming"],
            last_seen_at=datetime.now(UTC),
            revoked_at=datetime.now(UTC) if revoked else None,
        )
        self.session.add(record)
        await self.session.commit()
        self._agents.add(agent_id)
        return record

    # --- files ----------------------------------------------------------------------------

    async def file(
        self,
        *,
        filename: str = "<set-01>.mp3",
        agent_id: str = DEFAULT_AGENT_ID,
        sha256: str | None = None,
        file_type: str = "mp3",
        file_size: int = 8_388_608,
        subdirectory: str | None = None,
    ) -> FileRecord:
        """Insert one file record, creating its agent if needed.

        ``subdirectory`` defaults to a fresh UUID segment so the ``(agent_id, original_path)``
        unique index never collides across calls that share a filename -- which duplicate-group
        seeding does by design.
        """
        await self.agent(agent_id)
        segment = subdirectory if subdirectory is not None else uuid.uuid4().hex[:12]
        record = FileRecord(
            id=uuid.uuid4(),
            agent_id=agent_id,
            sha256_hash=sha256 or _sha256(),
            original_path=f"{ARCHIVE_MOUNT}/incoming/{segment}/{filename}",
            original_filename=filename,
            current_path=f"{ARCHIVE_MOUNT}/incoming/{segment}/{filename}",
            file_type=file_type,
            file_size=file_size,
        )
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record

    async def metadata(
        self,
        file: FileRecord,
        *,
        artist: str | None = "Example Artist",
        title: str | None = "Example Title",
        album: str | None = None,
        year: int | None = None,
        genre: str | None = None,
        track_number: int | None = None,
        duration: float | None = 2_400.0,
        bitrate: int | None = 320_000,
    ) -> FileMetadata:
        """Attach tag metadata to a file (the dedupe ranking's bitrate/tag-completeness input)."""
        record = FileMetadata(
            id=uuid.uuid4(),
            file_id=file.id,
            artist=artist,
            title=title,
            album=album,
            year=year,
            genre=genre,
            track_number=track_number,
            duration=duration,
            bitrate=bitrate,
        )
        self.session.add(record)
        await self.session.commit()
        return record

    # --- proposals ------------------------------------------------------------------------

    async def proposal(
        self,
        *,
        status: ProposalStatus = ProposalStatus.PENDING,
        confidence: float | None = 0.95,
        filename: str = "<set-01>.mp3",
        proposed_filename: str | None = None,
        proposed_path: str | None = None,
        agent_id: str = DEFAULT_AGENT_ID,
        file: FileRecord | None = None,
    ) -> RenameProposal:
        """Insert one rename proposal (and, unless one is supplied, the file it renames.

        ``proposed_path`` defaults to a destination unique to this proposal. That is not cosmetic:
        ``/execution/start`` runs ``detect_collisions`` before it will dispatch anything, and two
        proposals sharing a destination short-circuit the whole flow into the collision-block
        partial -- which looks, from a test, exactly like a broken Execute button.
        """
        target = file if file is not None else await self.file(filename=filename, agent_id=agent_id)
        name = proposed_filename or f"Renamed {filename}"
        record = RenameProposal(
            id=uuid.uuid4(),
            file_id=target.id,
            proposed_filename=name,
            proposed_path=proposed_path if proposed_path is not None else f"{ARCHIVE_MOUNT}/organized/{uuid.uuid4().hex[:12]}/{name}",
            confidence=confidence,
            status=status.value,
        )
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record

    async def approved_proposals(self, count: int = 2, *, agent_id: str = DEFAULT_AGENT_ID) -> list[RenameProposal]:
        """Seed ``count`` APPROVED proposals -- the precondition for a real execute dispatch.

        This is the state ``preflight.can_execute`` gates on, and therefore the state that makes
        ``#apply-confirm`` exist at all. Without it the Execute control is disabled and every
        browser assertion downstream of a dispatch is unreachable, which is the situation
        ``test_shell_contract.py`` documents at its ``test_execute_always_states_the_scope`` docstring.
        """
        return [
            await self.proposal(status=ProposalStatus.APPROVED, filename=f"<set-{index + 1:02d}>.mp3", agent_id=agent_id) for index in range(count)
        ]

    # --- duplicates -----------------------------------------------------------------------

    async def duplicate_group(self, *, count: int = 2, bitrates: Sequence[int] | None = None) -> list[FileRecord]:
        """Seed ``count`` files sharing one sha256 -- one unresolved duplicate group.

        Distinct bitrates give ``score_group`` an unambiguous keeper, so the card's recommendation
        (and therefore what the review/resolve round-trip asserts) is deterministic rather than
        decided by a tie-break on path ordering.
        """
        shared = _sha256()
        rates = list(bitrates) if bitrates is not None else [320_000 - (index * 64_000) for index in range(count)]
        files: list[FileRecord] = []
        for index in range(count):
            record = await self.file(filename=f"<track-{index + 1:02d}>.mp3", sha256=shared)
            await self.metadata(record, bitrate=rates[index], title=f"Example Title {index + 1}")
            files.append(record)
        return files

    async def resolve_duplicate(self, *, keeper: FileRecord, archived: Sequence[FileRecord]) -> None:
        """Mark a duplicate group already resolved (the state the dedupe reader filters OUT)."""
        for record in archived:
            self.session.add(
                DedupResolution(
                    id=uuid.uuid4(),
                    file_id=record.id,
                    canonical_file_id=keeper.id,
                )
            )
        await self.session.commit()

    # --- scans ----------------------------------------------------------------------------

    async def scan_batch(
        self,
        *,
        status: ScanStatus = ScanStatus.COMPLETED,
        agent_id: str = DEFAULT_AGENT_ID,
        scan_path: str | None = None,
        total_files: int = 12,
        processed_files: int | None = None,
        error_message: str | None = None,
        age: timedelta = timedelta(minutes=5),
    ) -> ScanBatch:
        """Insert one scan batch.

        ``age`` backdates ``created_at`` so a test seeding several batches gets a deterministic
        Recent-Scans ordering instead of three rows sharing one ``now()`` and sorting arbitrarily.
        """
        await self.agent(agent_id)
        now = datetime.now(UTC)
        record = ScanBatch(
            id=uuid.uuid4(),
            agent_id=agent_id,
            scan_path=scan_path or f"{ARCHIVE_MOUNT}/incoming",
            status=status.value,
            total_files=total_files,
            processed_files=total_files if processed_files is None else processed_files,
            error_message=error_message,
            completed_at=now - age if status in {ScanStatus.COMPLETED, ScanStatus.FAILED} else None,
            last_progress_at=now - age,
        )
        record.created_at = now - age
        record.updated_at = now - age
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record


def sessionmaker_for(dsn: str) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """Build a throwaway engine + sessionmaker for ``dsn``.

    A fresh engine per test rather than a shared session-scoped one, for the same reason
    ``conftest.live_server`` is a sync fixture: under ``asyncio_mode = "auto"`` each test gets its
    own event loop, and an asyncpg pool created on one loop and awaited from another hangs rather
    than erroring. Binding the engine's lifetime to the test's loop sidesteps that entirely. The
    cost is one connection setup per test, which is noise next to launching Chromium.

    ``pool_size`` is deliberately small: this engine exists to run a handful of INSERTs, and every
    connection it holds is one the live app cannot have.
    """
    engine = create_async_engine(dsn, pool_size=1, max_overflow=1, poolclass=None)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
