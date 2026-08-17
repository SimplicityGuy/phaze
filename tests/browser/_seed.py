"""phaze-fk1ww: state seeding for the browser matrix.

The browser suite's own database is the *live app's* database -- a separate process holds it, so
the unit suite's session-scoped ORM fixtures (``tests/conftest.py``) cannot reach it. This module is
the browser-side equivalent: a small, explicit writer that opens its own engine on the seat's
``_browser`` DSN and puts the schema into one of the named states the matrix walks.

Why states are seeded rather than mocked
========================================

The gap phaze-fk1ww exists to close is that every prior check ran against an **empty** database.
An empty workspace is the one state in which a layout contract is trivially satisfiable: no table
means no horizontal overflow, no rows mean no long cell text, no badge means no colour. Proving the
contract at a tablet width or in the dark theme is only evidence if the workspaces have something in
them, so the matrix seeds real rows through the real models and drives the real templates.

Identifier hygiene
==================

Every value here is synthetic and follows CLAUDE.md's placeholder vocabulary (``<track-01>``,
``<set-01>``). No path, filename or digest in this module came from the private archive.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid


_AGENT_ID = "browser-fileserver"

# Synthetic display names, deliberately in the shapes the product actually renders: a short track,
# a long multi-hour set with the punctuation an operator's real names carry, and a name long enough
# to push a table cell wide (the case a matrix that only ever sees an empty table never reaches).
_TRACK_NAMES = (
    "<track-01>.mp3",
    "<track-02>.m4a",
    "<track-03>.ogg",
    "Artist - Event - <track-04> (2024) [remastered, extended club edit, side B].mp3",
)
_SET_NAMES = ("<set-01>.mp3", "<set-02>.mp3")

# Every table the matrix writes, ordered so a plain TRUNCATE ... CASCADE from `files` and `agents`
# does not have to be reasoned about per-run. CASCADE covers the dependents; the two roots are
# listed because they are what the seeder itself creates.
_ROOT_TABLES = ("files", "agents", "tracklists")


def _sync_to_async(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    return dsn.replace("postgresql://", "postgresql+asyncpg://")


def _engine(dsn: str):  # type: ignore[no-untyped-def] - sqlalchemy's generics are import-heavy here
    """A short-lived engine on the LIVE APP's database, disposed by every caller.

    Not pooled and not shared: this writer runs between page loads while a separate uvicorn process
    holds its own pool on the same database, and a connection kept open across a TRUNCATE would sit
    in the app's way for the rest of the session.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(_sync_to_async(dsn))


async def reset(dsn: str) -> None:
    """Return the app's database to the empty state every workspace's empty view is rendered from.

    Called at the START and the END of each matrix test rather than only at the start. The browser
    database is session-scoped and shared with ``test_shell_contract.py``, whose Execute assertions
    depend on nothing being approved -- a matrix test that seeded and walked away would turn those
    into order-dependent failures that reproduce only when the whole file runs.
    """
    from sqlalchemy import text

    engine = _engine(dsn)
    try:
        async with engine.begin() as conn:
            for table in _ROOT_TABLES:
                await conn.execute(text(f'TRUNCATE TABLE "{table}" CASCADE'))
    finally:
        await engine.dispose()


async def seed_populated(dsn: str, *, with_failures: bool = False) -> None:
    """Put rows behind every rail destination the matrix visits.

    ``with_failures`` additionally seeds the two terminal-failure markers the Summary workspace
    turns into ``danger``-toned attention cards (``file_metadata.failed_at`` and
    ``analysis_results.failed_at``, per ``services/stage_status.failed_clause``). That is the
    product's real warning/degraded surface, which is why the matrix drives it rather than
    injecting a banner.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from phaze.models.agent import Agent
    from phaze.models.analysis import AnalysisResult
    from phaze.models.execution import ExecutionLog
    from phaze.models.file import FileRecord
    from phaze.models.metadata import FileMetadata
    from phaze.models.proposal import RenameProposal
    from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion

    now = datetime.now(UTC)
    engine = _engine(dsn)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            session.add(
                Agent(
                    id=_AGENT_ID,
                    name="browser fileserver",
                    kind="fileserver",
                    scan_roots=["<archive-mount-in-container>"],
                    last_seen_at=now,
                    last_status={"state": "online"},
                )
            )
            await session.commit()

            def _file(name: str, *, sha: str | None = None) -> FileRecord:
                return FileRecord(
                    id=uuid.uuid4(),
                    agent_id=_AGENT_ID,
                    sha256_hash=sha or (uuid.uuid4().hex + uuid.uuid4().hex),
                    original_path=f"<archive-mount-in-container>/{uuid.uuid4().hex}/{name}",
                    original_filename=name,
                    current_path=f"<archive-mount-in-container>/{name}",
                    file_type=name.rsplit(".", 1)[-1],
                    file_size=1024 * 1024,
                )

            tracks = [_file(name) for name in _TRACK_NAMES]
            sets = [_file(name) for name in _SET_NAMES]
            # A duplicate group: two files sharing one digest is what /s/dedupe groups on.
            shared = uuid.uuid4().hex + uuid.uuid4().hex
            dupes = [_file(f"<track-0{5 + i}>.mp3", sha=shared) for i in range(2)]
            session.add_all([*tracks, *sets, *dupes])
            await session.commit()

            # Metadata + analysis behind /s/metadata and /s/analyze.
            for index, file in enumerate(tracks):
                session.add(
                    FileMetadata(
                        id=uuid.uuid4(),
                        file_id=file.id,
                        artist="Synthetic Artist",
                        title=f"Synthetic Title {index + 1}",
                        album="Synthetic Album",
                        year=2024,
                        genre="Electronic",
                        track_number=index + 1,
                        duration=245.0,
                        bitrate=320000,
                    )
                )
                session.add(
                    AnalysisResult(
                        id=uuid.uuid4(),
                        file_id=file.id,
                        bpm=128.0 + index,
                        musical_key="Am",
                        mood="energetic",
                        style="techno",
                        fine_windows_analyzed=60,
                        fine_windows_total=60,
                        coarse_windows_analyzed=30,
                        coarse_windows_total=30,
                        analysis_completed_at=now,
                    )
                )

            # Pending proposals behind /s/propose and /s/rename (the Changes Review diff rows).
            pending = [
                RenameProposal(
                    id=uuid.uuid4(),
                    file_id=file.id,
                    proposed_filename=f"Synthetic Artist - Synthetic Album - {index + 1:02d} Synthetic Title.mp3",
                    proposed_path=f"<archive-mount-in-container>/Synthetic Artist/Synthetic Album/{index + 1:02d}.mp3",
                    confidence=0.94,
                    status="pending",
                )
                for index, file in enumerate(tracks)
            ]
            # One executed proposal + its execution log, so /s/audit and /s/apply have history.
            executed = RenameProposal(
                id=uuid.uuid4(),
                file_id=sets[0].id,
                proposed_filename="Artist - Event - <set-01> (2024).mp3",
                status="executed",
            )
            session.add_all([*pending, executed])
            await session.commit()
            session.add(
                ExecutionLog(
                    id=uuid.uuid4(),
                    proposal_id=executed.id,
                    operation="rename",
                    source_path=f"<archive-mount-in-container>/{_SET_NAMES[0]}",
                    destination_path="<archive-mount-in-container>/Artist - Event - <set-01> (2024).mp3",
                    sha256_verified=True,
                    status="success",
                )
            )

            # An approved tracklist with timestamped tracks: the cue-eligibility gate, so /s/cue and
            # /s/tracklist render real cards rather than their empty states.
            tracklist = Tracklist(
                id=uuid.uuid4(),
                external_id=f"ext-{uuid.uuid4().hex[:12]}",
                source_url="https://example.test/tracklist",
                file_id=sets[0].id,
                status="approved",
            )
            session.add(tracklist)
            await session.commit()
            version = TracklistVersion(id=uuid.uuid4(), tracklist_id=tracklist.id, version_number=1)
            session.add(version)
            await session.commit()
            session.add_all(
                [
                    TracklistTrack(
                        id=uuid.uuid4(),
                        version_id=version.id,
                        position=position,
                        title=f"Synthetic Title {position}",
                        artist="Synthetic Artist",
                        timestamp=f"0{position - 1}:00:00",
                    )
                    for position in (1, 2, 3)
                ]
            )
            tracklist.latest_version_id = version.id
            await session.commit()

            if with_failures:
                session.add(
                    FileMetadata(
                        id=uuid.uuid4(),
                        file_id=sets[1].id,
                        failed_at=now,
                        error_message="synthetic metadata failure (browser matrix)",
                    )
                )
                session.add(
                    AnalysisResult(
                        id=uuid.uuid4(),
                        file_id=sets[1].id,
                        failed_at=now,
                        error_message="synthetic analysis failure (browser matrix)",
                    )
                )
                await session.commit()
    finally:
        await engine.dispose()
