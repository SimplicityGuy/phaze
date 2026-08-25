"""phaze-pqib3 (seam C5): the CUE ``FILE`` line vs. the directory the ``.cue`` is written into.

**The two halves of CUE generation disagreed about which path they meant, and no test could
see it because each half was tested against its own view of the world.**

``routers/cue.py::generate_cue`` renders ``FILE "<basename>"`` from the UNRESOLVED
``FileRecord.current_path``; ``tasks/cue_write.py::_write_sync`` resolves that same path
(``resolve_and_check_containment``) and writes the ``.cue`` beside the RESOLVED file. For an entry
whose ``current_path`` is a symlink into a differently-named target, the sheet therefore lands in
the target's directory naming the symlink's basename -- a file that is not beside it.

That is ADR-0012 rule 3 at one remove. The artifact is a CUE sheet; its real consumer is a parser
or player that opens the ``.cue`` and resolves ``FILE`` **relative to the .cue's own directory**.
Both existing modules validate it against the PRODUCER's view instead:

* ``tests/review/routers/test_cue.py`` asserts the rendered text and the enqueue, and never writes;
* ``tests/review/tasks/test_cue_write.py`` writes, and its fixtures pass a real path whose
  ``realpath`` is itself -- so producer and writer can never disagree about the basename.

This module is written so neither mock can satisfy it: it drives the REAL ``POST
/cue/{id}/generate``, hands the REAL enqueued payload to the REAL ``write_cue_sheet`` task, then
performs the CONSUMER's operation -- read the ``FILE`` line out of the bytes on disk and resolve it
from the directory the ``.cue`` actually occupies.

**What fails without the fix.** ``test_file_line_names_a_sibling_of_the_written_cue`` fails at
HEAD 705287e4: the sheet is written to the archive directory while its ``FILE`` line names the
symlink's basename. Recorded on the bead, per the ``phaze-l832u.3`` precedent.

**The invariant this pins**, and the reason the other two cases are here: a symlink is only a
problem when it moves the basename. A farm entry that keeps the target's name, and a plain
non-symlinked file, must stay green -- so a fix cannot discharge this module by making every
``FILE`` line something other than a name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.tasks.cue_write import write_cue_sheet
from tests._queue_fakes import install_fake_queues


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_generatable_tracklist(session: AsyncSession, current_path: str) -> Tracklist:
    """An approved tracklist on an APPLIED file with timestamped tracks -- the generate gate's happy path.

    Mirrors ``tests/review/routers/test_cue.py::_create_approved_tracklist_with_file`` in the parts
    ``generate_cue`` gates on (READ-05/D-01: applied means an ``executed`` proposal exists, never
    ``files.state``), kept local so this module's fixtures cannot drift when that one's do.
    """
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id="test-fileserver",
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path="/music/ingest/<set-01>.mp3",
            original_filename="<set-01>.mp3",
            current_path=current_path,
            file_type="mp3",
            file_size=50_000_000,
        )
    )
    await session.flush()
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename="<set-01>.mp3",
            status=ProposalStatus.EXECUTED,
        )
    )

    tracklist_id = uuid.uuid4()
    version_id = uuid.uuid4()
    tracklist = Tracklist(
        id=tracklist_id,
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        source_url=f"https://www.1001tracklists.com/tracklist/{uuid.uuid4().hex[:6]}",
        file_id=file_id,
        match_confidence=95,
        artist="DJ Shadow",
        event="Coachella 2024",
        latest_version_id=version_id,
        source="1001tracklists",
        status="approved",
    )
    session.add(tracklist)
    session.add(TracklistVersion(id=version_id, tracklist_id=tracklist_id, version_number=1))
    await session.flush()
    for i in range(1, 4):
        session.add(
            TracklistTrack(
                id=uuid.uuid4(),
                version_id=version_id,
                position=i,
                artist=f"Track Artist {i}",
                title=f"Track Title {i}",
                timestamp=f"0:{i * 10}:00",
            )
        )
    await session.commit()
    return tracklist


def _agent_settings(scan_roots: list[str]) -> Any:
    """A stand-in ``AgentSettings`` carrying only ``scan_roots``.

    ``spec=`` matters: ``write_cue_sheet`` gates on ``isinstance(cfg, AgentSettings)``, and only a
    spec'd mock satisfies that (same shape as ``tests/review/tasks/test_cue_write.py``).
    """
    from phaze.config import AgentSettings

    cfg = MagicMock(spec=AgentSettings)
    cfg.scan_roots = scan_roots
    return cfg


async def _generate_then_write(client: AsyncClient, tracklist: Tracklist, scan_root: Path) -> None:
    """Drive the REAL router, then the REAL agent task, over the payload the router really enqueued.

    No hand-built payload: whatever ``generate_cue`` put on the wire (round-tripped through the
    production SAQ serializer by ``FakeQueue``, phaze-9nz1g) is exactly what the writer receives.
    """
    _controller_queue, router = install_fake_queues(client)
    response = await client.post(f"/cue/{tracklist.id}/generate")
    assert response.status_code == 200, response.text

    assert len(router.captures) == 1, f"expected one enqueue, got {router.captures}"
    _queue_name, task_name, kwargs = router.captures[0]
    assert task_name == "write_cue_sheet"

    with patch("phaze.tasks.cue_write.get_settings", return_value=_agent_settings([str(scan_root)])):
        await write_cue_sheet({}, **kwargs)


def _assert_file_line_resolves_beside_the_cue(scan_root: Path) -> None:
    """The CONSUMER's operation: read ``FILE`` off disk and resolve it from the .cue's own directory.

    A CUE sheet's ``FILE`` is a bare name, and every parser resolves it relative to the sheet, not
    relative to whatever the producer happened to be looking at. The sheet is located by sweeping
    the tree rather than by assuming a directory -- assuming one would re-encode the very producer
    view this module exists to distrust.
    """
    cues = sorted(scan_root.rglob("*.cue"))
    assert len(cues) == 1, f"expected exactly one written .cue, found {[c.name for c in cues]}"
    written = cues[0]

    file_lines = [line for line in written.read_text(encoding="utf-8-sig").splitlines() if line.startswith("FILE ")]
    assert len(file_lines) == 1, f"expected exactly one FILE directive, got {file_lines}"
    named = file_lines[0].split('"')[1]

    sibling = written.parent / named
    assert sibling.exists(), (
        f"the written CUE names FILE {named!r}, which does not exist beside it: "
        f"the sheet is in {written.parent.name}/ and that directory holds "
        f"{sorted(p.name for p in written.parent.iterdir())}"
    )


@pytest.mark.asyncio
async def test_file_line_names_a_sibling_of_the_written_cue(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """SEAM C5: a symlinked ``current_path`` whose target carries a DIFFERENT name.

    The realistic archive shape behind this: a symlink farm whose entries are named for the event
    while the canonical archive names the file something else. ``current_path`` is the farm entry;
    the resolve in ``_write_sync`` lands the sheet in the archive.

    RED at HEAD 705287e4 -- the sheet lands beside ``<set-02>.mp3`` naming ``<set-01>.mp3``.
    """
    archive = tmp_path / "archive"
    archive.mkdir()
    farm = tmp_path / "farm"
    farm.mkdir()

    real_audio = archive / "<set-02>.mp3"
    real_audio.write_text("fake audio")
    link = farm / "<set-01>.mp3"
    link.symlink_to(real_audio)

    tracklist = await _seed_generatable_tracklist(session, str(link))
    await _generate_then_write(client, tracklist, tmp_path)
    _assert_file_line_resolves_beside_the_cue(tmp_path)


@pytest.mark.asyncio
async def test_file_line_resolves_for_a_name_preserving_symlink(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """A symlink that keeps the target's basename is already correct, and must STAY correct.

    Green at HEAD: the two halves disagree about the DIRECTORY here but not about the NAME, and the
    sheet lands beside a real file of exactly that name. Present so a fix cannot satisfy this module
    by degrading the ``FILE`` line into something that is not a plain sibling name.
    """
    archive = tmp_path / "archive"
    archive.mkdir()
    farm = tmp_path / "farm"
    farm.mkdir()

    real_audio = archive / "<set-01>.mp3"
    real_audio.write_text("fake audio")
    link = farm / "<set-01>.mp3"
    link.symlink_to(real_audio)

    tracklist = await _seed_generatable_tracklist(session, str(link))
    await _generate_then_write(client, tracklist, tmp_path)
    _assert_file_line_resolves_beside_the_cue(tmp_path)


@pytest.mark.asyncio
async def test_file_line_resolves_for_a_plain_unsymlinked_file(client: AsyncClient, session: AsyncSession, tmp_path: Path) -> None:
    """The overwhelming majority of the corpus: ``current_path`` IS its own realpath.

    The control case. It is green at HEAD and its job is to stay green -- the blast-radius guard for
    every file the seam does not touch.
    """
    archive = tmp_path / "archive"
    archive.mkdir()
    real_audio = archive / "<set-01>.mp3"
    real_audio.write_text("fake audio")

    tracklist = await _seed_generatable_tracklist(session, str(real_audio))
    await _generate_then_write(client, tracklist, tmp_path)
    _assert_file_line_resolves_beside_the_cue(tmp_path)
