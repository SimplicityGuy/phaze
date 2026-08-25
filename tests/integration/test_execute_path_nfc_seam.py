"""phaze-sy8z3: the execute path -> ``FileRecord.current_path`` NFC seam, driven to a REAL consumer.

Every writer of a path field in this repo NFC-normalizes it, and the code says so at each site:
``tasks/scan.py:222-224`` ("Pitfall 3: NFC-normalize EVERY path field"), ``agent_watcher/poster.py``
(the watcher's own producer), ``routers/agent_files.py:111`` ("RESEARCH Pitfall 7: NFC-normalize
defensively") and ``routers/scan.py``. The ingest path is covered TWICE -- once at the producer and
once, defensively, at the HTTP receiver.

The EXECUTE path is the one writer that was covered neither time. ``tasks/execution.py``'s
``_report_success`` patches the post-move location with ``ProposalStatePatch(current_path=str(proposed))``
and ``routers/agent_proposals.py`` assigned ``body.current_path`` verbatim -- under a comment that
already claimed "(Pitfall 3)". ``proposed`` is built from ``item.proposed_filename``, which arrives
from LLM JSON via ``sanitize_pg_text``, and ``sanitize_pg_text`` strips NULs and lone surrogates
without touching normalization form.

WHY A SEAM TEST, AND WHY IT LIVES HERE
======================================

Same shape as ``test_tag_write_sha256_seam.py``: producer (the agent's execute task) and consumers
(the CUE writer, the tag writer, the file views) share only a database column, so there is no call
site to grep and no natural home. These tests build the seam explicitly -- the real writer, the real
wire schema, the real HTTP route, the real database column, handed to the real consumer.

ADR-0012 RULE 3 DECIDES THE SECOND TEST'S CONSUMER. "Verify with the artifact's real consumer, not
with the tool that produced it." Re-reading ``current_path`` through the execute path that wrote it
round-trips perfectly and CANNOT exhibit this defect at any fidelity.

The consumer chosen is the CUE writer (``routers.cue._eligible_cue_text`` ->
``services.cue_generator.generate_cue_content``), and the choice is deliberate over the tag writer:

  * The CUE ``FILE "<name>" MP3`` line is a PURE TEXT artifact. The mismatch it exhibits is
    visible in bytes, on every platform, with no filesystem involved.
  * A tag-write consumer resolves ``current_path`` against a real filesystem -- and macOS APFS is
    normalization-INSENSITIVE, so an NFD path happily opens an NFC file there. A test asserting
    "the open fails" would pass on Linux CI and silently prove nothing on a developer's Mac. That
    is a proxy that structurally cannot exhibit the failure on half the platforms that run it,
    which is the exact ADR-0012 trap these rules exist to close.

The two tests also chain in the product's own order rather than in a contrived one: the execute run
in the first test is what flips the proposal to EXECUTED, which is what makes ``is_applied`` true,
which is what makes the tracklist CUE-eligible in the second.

POPULATION (ADR-0012 rule 4 -- measured, not adjectival). Measured against the live catalog on
2026-08-24 and recorded on the bead: 0 of 11,428 ``files.current_path`` rows are non-NFC, 60 of
11,428 (0.52%) carry non-ASCII at all, and ``proposals`` has never held a row
(``n_tup_ins = 0`` with ``pg_stat_database.stats_reset`` NULL). So this fix changes the path for a
population that is currently EMPTY and cannot regress an existing row -- and it closes the seam
before the first execute run meets one of those 60 paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
import unicodedata
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from phaze.database import get_session
from phaze.enums.execution import ExecutionStatus
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.routers import agent_proposals
from phaze.routers.cue import _eligible_cue_text
from phaze.schemas.agent_execution import ExecutionLogCreate
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.agent_client import PhazeAgentClient
from phaze.tasks.execution import _report_success, _ReportContext


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


# An INVENTED filename in the naming format the proposer emits -- no archive identifier. Both forms
# are derived from one literal so the pair cannot drift, and the guard below fails the test rather
# than letting a degenerate fixture (NFC == NFD) pass vacuously.
_FILENAME_NFC = unicodedata.normalize("NFC", "Artist Ångström - Live Set 01 (2024).mp3")
_FILENAME_NFD = unicodedata.normalize("NFD", _FILENAME_NFC)

# The seeded agent's scan_root (tests/conftest.py::seed_test_agent).
_SCAN_ROOT = "/test/music"


def _assert_fixture_is_actually_non_nfc() -> None:
    """The fixture guard. Without it, an ASCII-only filename would make both tests pass trivially."""
    assert _FILENAME_NFD != _FILENAME_NFC, "fixture is degenerate: the NFD and NFC forms are identical"
    assert not unicodedata.is_normalized("NFC", _FILENAME_NFD), "fixture NFD form is already NFC-normalized"
    assert unicodedata.is_normalized("NFC", _FILENAME_NFC), "fixture NFC form is not NFC-normalized"


def _make_agent_client(session: AsyncSession, token: str) -> PhazeAgentClient:
    """A REAL ``PhazeAgentClient`` whose transport is the REAL router over ASGI.

    Not a mock and not respx: the body is serialized by the real ``ProposalStatePatch``, parsed by
    the real FastAPI route, and committed by the real handler against the real database. The
    ``_client`` injection point is the one the class documents for test transport substitution.
    """
    app = FastAPI(title="execute-path-nfc-seam", version="test")
    app.include_router(agent_proposals.router)
    app.dependency_overrides[get_session] = lambda: session
    return PhazeAgentClient(
        base_url="http://test",
        token=token,
        _client=AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ),
    )


async def _seed_file_and_approved_proposal(session: AsyncSession, agent_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed the pre-execution state: a file at its ingest location plus an APPROVED rename proposal.

    ``current_path`` starts NFC because ingest normalizes it -- the point of the test is what the
    EXECUTE path leaves behind, so the starting row must be in the form ingest actually produces.
    """
    file_id = uuid.uuid4()
    ingest_path = f"{_SCAN_ROOT}/incoming/set-01.mp3"
    session.add(
        FileRecord(
            id=file_id,
            sha256_hash="0" * 64,
            original_path=ingest_path,
            original_filename="set-01.mp3",
            current_path=ingest_path,
            file_type="mp3",
            file_size=50_000_000,
            agent_id=agent_id,
        )
    )
    proposal_id = uuid.uuid4()
    session.add(
        RenameProposal(
            id=proposal_id,
            file_id=file_id,
            # The non-NFC emission, exactly as it reaches the executor: LLM JSON -> sanitize_pg_text
            # (which does not touch normalization form) -> RenameProposal.proposed_filename.
            proposed_filename=_FILENAME_NFD,
            proposed_path="performances/sets",
            confidence=0.9,
            status=ProposalStatus.APPROVED,
        )
    )
    await session.commit()
    return file_id, proposal_id


async def _run_execute_path_report(
    session: AsyncSession,
    agent: Agent,
    token: str,
    file_id: uuid.UUID,
    proposal_id: uuid.UUID,
) -> Path:
    """Drive ``tasks.execution._report_success`` -- the execute path's own writer -- unmodified.

    Returns the destination ``Path`` the executor would have moved to, built by the same
    ``owning_root / proposed_path / proposed_filename`` join ``_resolve_destination`` performs.

    ``is_last=False`` keeps the sub-batch completion token out of play (phaze-j7u8 re-raises that
    one deliberately), and ``start_logged=True`` skips the write-ahead audit POST. The
    execution-log PATCH and the progress POST still fire for real and 404 into their existing
    swallow paths -- the same degradation production takes when those calls fail, and irrelevant
    to the column under test.
    """
    proposed = Path(_SCAN_ROOT) / "performances/sets" / _FILENAME_NFD
    item = ExecuteBatchProposalItem(
        proposal_id=proposal_id,
        file_id=file_id,
        original_path=f"{_SCAN_ROOT}/incoming/set-01.mp3",
        proposed_path="performances/sets",
        proposed_filename=_FILENAME_NFD,
    )
    api = _make_agent_client(session, token)
    try:
        await _report_success(
            _ReportContext(
                api=api,
                item=item,
                execution_log_id=uuid.uuid4(),
                progress_request_id=uuid.uuid4(),
                payload=ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id=agent.id, proposals=[item]),
                is_last=False,
                start_log=ExecutionLogCreate(
                    id=uuid.uuid4(),
                    proposal_id=proposal_id,
                    operation="move",
                    source_path=f"{_SCAN_ROOT}/incoming/set-01.mp3",
                    destination_path=str(proposed),
                    sha256_verified=True,
                    status=ExecutionStatus.IN_PROGRESS,
                ),
            ),
            proposed,
            start_logged=True,
            sha_verified=True,
        )
    finally:
        await api.close()
    return proposed


async def _persisted_current_path(session: AsyncSession, file_id: uuid.UUID) -> str:
    """Read the column back from the database, not from any in-session identity map."""
    await session.commit()
    session.expire_all()
    record = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    return record.current_path


async def test_the_execute_path_persists_current_path_in_the_ingest_writers_normalization_form(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """A non-NFC ``proposed_filename`` must not reach ``FileRecord.current_path`` un-normalized.

    The assertion is against the form the INGEST writers produce (``tasks/scan.py:222-224``), not
    against a hard-coded literal, so the two writers cannot drift apart without this failing.
    """
    _assert_fixture_is_actually_non_nfc()
    agent, token = seed_test_agent
    file_id, proposal_id = await _seed_file_and_approved_proposal(session, agent.id)

    proposed = await _run_execute_path_report(session, agent, token, file_id, proposal_id)

    persisted = await _persisted_current_path(session, file_id)

    # The seam actually ran: the execute path reached the column at all.
    assert persisted != f"{_SCAN_ROOT}/incoming/set-01.mp3", "the execute path never wrote current_path"

    # The invariant. `unicodedata.normalize("NFC", ...)` is literally what tasks/scan.py applies.
    assert unicodedata.is_normalized("NFC", persisted), (
        f"execute path persisted a non-NFC current_path: {persisted!r} "
        f"(the ingest writers would have stored {unicodedata.normalize('NFC', persisted)!r})"
    )
    assert persisted == unicodedata.normalize("NFC", str(proposed))
    assert Path(persisted).name == _FILENAME_NFC


async def test_the_persisted_current_path_reaches_the_cue_writer_in_the_ingest_form(
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """ADR-0012 rule 3: hand the persisted row to the CUE writer, a consumer that did not write it.

    ``routers.cue._eligible_cue_text`` copies ``Path(file_record.current_path).name`` straight into
    the CUE ``FILE "<name>"`` line. The archive's own filenames are NFC (measured: 0 of 11,428 rows
    non-NFC), so a CUE emitting the NFD spelling names a file that does not exist under that byte
    sequence on any normalization-sensitive filesystem -- which is what the app container and the
    archive mount run on. Nothing here re-reads the value through the execute path that wrote it.
    """
    _assert_fixture_is_actually_non_nfc()
    agent, token = seed_test_agent
    file_id, proposal_id = await _seed_file_and_approved_proposal(session, agent.id)

    # The same execute run under test above: it is also what flips the proposal to EXECUTED, which
    # is what `is_applied` requires for the tracklist to be CUE-eligible at all.
    await _run_execute_path_report(session, agent, token, file_id, proposal_id)

    # Both ids are bound BEFORE the commit below. `session.commit()` expires every instance, so
    # reading `tracklist.id` afterwards would trigger a synchronous refresh outside the greenlet
    # and raise MissingGreenlet -- a fixture failure that looks nothing like the defect under test.
    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()
    tracklist = Tracklist(
        id=tracklist_id,
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        source_url=f"https://www.1001tracklists.com/tracklist/{uuid.uuid4().hex[:6]}",
        file_id=file_id,
        match_confidence=95,
        artist="Artist Ångström",
        event="Live Set 01",
        latest_version_id=version_id,
        source="1001tracklists",
        status="approved",
    )
    session.add(tracklist)
    session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
    await session.flush()
    for position in (1, 2):
        session.add(
            TracklistTrack(
                id=uuid.uuid4(),
                version_id=version_id,
                position=position,
                artist=f"Track Artist {position}",
                title=f"Track Title {position}",
                timestamp=f"0:{position * 10}:00",
            )
        )
    await session.commit()
    session.expire_all()

    file_record = (await session.execute(select(FileRecord).where(FileRecord.id == file_id))).scalar_one()
    tracklist_row = (await session.execute(select(Tracklist).where(Tracklist.id == tracklist_id))).scalar_one()

    cue_text = await _eligible_cue_text(session, tracklist_row, file_record)

    assert cue_text is not None, "the tracklist was not CUE-eligible -- the seam was never reached"
    assert f'FILE "{_FILENAME_NFC}" MP3' in cue_text, (
        "the CUE writer named the audio file in a normalization form the archive does not use; "
        f"emitted FILE line: {next((line for line in cue_text.splitlines() if line.startswith('FILE ')), '<none>')!r}"
    )
    assert _FILENAME_NFD not in cue_text
