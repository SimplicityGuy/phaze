"""phaze-nkf7n (seam B7): the tag-undo loop, assembled end to end from real parts.

Seam B7 of ``docs/spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md``. The undo anchor
``TagWriteLog.before_tags`` crosses five boundaries between being read off disk and being written
back to it::

    _extract_before_tags (agent, on disk)
      -> PhazeAgentClient.report_tag_write_before_snapshot  (HTTP, TagWriteBeforeSnapshotPayload)
      -> routers/agent_tag_writes.record_tag_write_before_snapshot  -> TagWriteLog.before_tags JSONB
      -> routers/tags.undo_tag_write -> enqueue_tag_write  -> SAQ PostgresQueue serializer
      -> tasks/tag_write.write_file_tags -> write_tags  (agent, on disk)

Every hop had a test, and no test had all of them. ``test_review_audit.py::test_tag_undo_reapplies_before_tags``
crosses Postgres and the undo route but HAND-SETS ``before_tags = {"artist": "Old Artist"}`` -- a
shape no real extractor emits -- and asserts the dispatched kwargs as a dict that never reaches a
file. The real-container tests cross disk but never JSONB. So a snapshot shape that survives in
process and is altered by the callback schema or the JSONB round trip -- a dropped ``None`` key
(phaze-52qd's delete mechanism), a multi-value genre collapsed to one value (phaze-z2u08), an MP4
``"N/total"`` track (phaze-2zl7) -- could reach production with every test green. Undo would then
restore something other than the original tags, and report COMPLETED doing it.

WHAT IS REAL HERE, AND THE ONE STAND-IN
=======================================

Real: the container (``ffmpeg``-generated, per format), the extractor, both agent HTTP calls
(a real ``PhazeAgentClient`` whose transport is the real router over ASGI, with real bearer auth),
the Postgres JSONB column, the ``/tags/{id}/undo`` route on the full app, ``enqueue_tag_write``, the
production SAQ ``PostgresQueue`` serializer (``tests/_queue_fakes.py`` round-trips every enqueue
through it -- phaze-9nz1g), ``write_file_tags`` and ``write_tags``. The kwargs handed to
``write_file_tags`` are exactly what the fake broker DESERIALIZED, never the producer's dict.

The stand-in is the broker's DELIVERY: the job is not picked up by a live SAQ worker, the test
passes the deserialized kwargs to the task itself. That hop is serialization plus a function call,
and serialization is the half with type-fidelity risk.

THE ORACLE IS NOT PHAZE'S OWN READER
====================================

Asserting through ``extract_tags`` -- the function ``_extract_before_tags`` is built on -- would be
the producer checking itself (ADR-0012 (verification fidelity and operator attribution), rule 3):
a normalization that loses the same detail on the way in and on the way out compares equal. Two
readers are used instead, neither of them phaze code:

* ``_raw_tags`` reads the container at the FRAME / COMMENT / ATOM level with bare mutagen, so a
  multi-value genre is a list, a track total survives, and an absent frame is absent.
* ``es.MetadataReader`` (TagLib), an independent implementation, already used by
  ``tests/review/services/test_tag_write_real_containers.py`` for the same reason.

Both are compared BEFORE the forward write against AFTER the undo, and each is also required to
SEE the forward write -- otherwise "restored" could be satisfied by a write that never landed.

The original tags are seeded by mutagen directly (not by phaze's writer, which is under test) and
deliberately include the four shapes that have each had their own undo bug: an ABSENT album
(must be deleted by the undo -- explicit ``None`` through JSONB), a multi-value genre, a full
release date, and a ``N/total`` track number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TCON, TDRC, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4
import pytest
from sqlalchemy import select

from phaze.config import AgentSettings
from phaze.database import get_session
from phaze.enums.tag_write import TagWriteStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteLog
from phaze.routers.agent_tag_writes import router as agent_tag_writes_router
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.hashing import compute_sha256
from phaze.services.tag_writer import enqueue_tag_write
from phaze.tasks.tag_write import write_file_tags
from tests._queue_fakes import install_fake_queues
from tests.review.services.test_tag_write_real_containers import _essentia_read, _make_container


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent
    from tests._queue_fakes import FakeTaskRouter


pytestmark = pytest.mark.integration

# The original on-disk state. ``album`` is ABSENT on purpose: the forward write adds it, so a
# faithful undo has to DELETE it, which only happens if the snapshot's explicit ``album: None``
# survives the callback schema and the JSONB column (phaze-52qd).
_ORIG_ARTIST = "Orig Artist"
_ORIG_TITLE = "Orig Title"
_ORIG_GENRES = ["House", "Techno"]
_ORIG_DATE = "2019-05-04"
_ORIG_TRACK, _ORIG_TOTAL = 3, 12

# A forward write touching every core field, including the absent one.
_FORWARD: dict[str, str | int | list[str] | None] = {
    "artist": "New Artist",
    "title": "New Title",
    "album": "New Album",
    "year": "2024",
    "genre": "Rock",
    "track_number": 7,
}

# The formats the undo loop is closed for: ID3, Vorbis and MP4 -- the three tag families whose
# snapshot shapes have each had their own undo bug. ASF is the fourth family ``write_tags``
# supports; it is out of scope here and covered for delete semantics by the real-container module.
_FORMATS = ("mp3", "flac", "m4a")

# Raw container keys per format, for the frame-level reader.
_ID3_FRAMES = ("TPE1", "TIT2", "TALB", "TDRC", "TCON", "TRCK")
_VORBIS_KEYS = ("artist", "title", "album", "date", "genre", "tracknumber")
_MP4_ATOMS = ("\xa9ART", "\xa9nam", "\xa9alb", "\xa9day", "\xa9gen", "trkn")


def _seed_original_tags(path: Path) -> None:
    """Write the ORIGINAL tags with bare mutagen -- never with phaze's writer, which is under test."""
    ext = path.suffix.lstrip(".")
    if ext == "mp3":
        id3 = ID3()
        id3.add(TPE1(encoding=3, text=[_ORIG_ARTIST]))
        id3.add(TIT2(encoding=3, text=[_ORIG_TITLE]))
        id3.add(TCON(encoding=3, text=list(_ORIG_GENRES)))
        id3.add(TDRC(encoding=3, text=[_ORIG_DATE]))
        id3.add(TRCK(encoding=3, text=[f"{_ORIG_TRACK}/{_ORIG_TOTAL}"]))
        id3.save(str(path))
    elif ext == "flac":
        flac = FLAC(str(path))
        flac["artist"] = [_ORIG_ARTIST]
        flac["title"] = [_ORIG_TITLE]
        flac["genre"] = list(_ORIG_GENRES)
        flac["date"] = [_ORIG_DATE]
        flac["tracknumber"] = [f"{_ORIG_TRACK}/{_ORIG_TOTAL}"]
        flac.save()
    else:
        mp4 = MP4(str(path))
        mp4["\xa9ART"] = [_ORIG_ARTIST]
        mp4["\xa9nam"] = [_ORIG_TITLE]
        mp4["\xa9gen"] = list(_ORIG_GENRES)
        mp4["\xa9day"] = [_ORIG_DATE]
        mp4["trkn"] = [(_ORIG_TRACK, _ORIG_TOTAL)]
        mp4.save()


def _raw_tags(path: Path) -> dict[str, list[str]]:
    """Read the core tags at the frame / comment / atom level with bare mutagen.

    Deliberately NOT ``phaze.services.metadata.extract_tags``: that is the reader the snapshot is
    built on, so it would share any normalization the snapshot loses. Values are stringified so the
    three containers compare the same way; a key that is absent on disk is absent here.
    """
    audio = mutagen.File(str(path))
    assert audio is not None
    assert audio.tags is not None
    ext = path.suffix.lstrip(".")
    if ext == "mp3":
        return {frame: [str(t) for t in audio.tags[frame].text] for frame in _ID3_FRAMES if frame in audio.tags}
    if ext == "flac":
        return {key: [str(v) for v in audio.tags[key]] for key in _VORBIS_KEYS if key in audio.tags}
    return {atom: [str(v) for v in audio.tags[atom]] for atom in _MP4_ATOMS if atom in audio.tags}


def _agent_settings(scan_roots: list[str]) -> Any:
    """A stand-in AgentSettings carrying only ``scan_roots`` (``spec=`` because the task gates on isinstance)."""
    cfg = MagicMock(spec=AgentSettings)
    cfg.scan_roots = scan_roots
    return cfg


def _make_agent_client(session: AsyncSession, token: str) -> PhazeAgentClient:
    """A REAL ``PhazeAgentClient`` whose transport is the REAL callback router over ASGI, real auth."""
    app = FastAPI(title="tag-undo-loop-seam", version="test")
    app.include_router(agent_tag_writes_router)
    app.dependency_overrides[get_session] = lambda: session
    return PhazeAgentClient(
        base_url="http://test",
        token=token,
        _client=AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}),
    )


async def _applied_file(session: AsyncSession, agent_id: str, path: Path) -> FileRecord:
    """Persist the ingested, APPLIED FileRecord the tag routes require (an executed proposal exists)."""
    record = FileRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        sha256_hash=compute_sha256(path),
        original_path=str(path),
        original_filename=path.name,
        current_path=str(path),
        file_type=path.suffix.lstrip("."),
        file_size=path.stat().st_size,
    )
    session.add(record)
    await session.flush()
    session.add(FileMetadata(id=uuid.uuid4(), file_id=record.id, artist=_ORIG_ARTIST, title=_ORIG_TITLE))
    session.add(RenameProposal(id=uuid.uuid4(), file_id=record.id, proposed_filename=path.name, status=ProposalStatus.EXECUTED.value))
    await session.commit()
    await session.refresh(record)
    return record


async def _deliver_write_file_tags(router: FakeTaskRouter, agent: PhazeAgentClient, scan_root: Path) -> dict[str, Any]:
    """Hand the ONE captured ``write_file_tags`` job to the real task, as a worker would.

    The kwargs are what ``FakeQueue`` got back from the production ``PostgresQueue.deserialize`` --
    the worker-visible payload, not the dict the producer passed in.
    """
    jobs = [kwargs for _queue, task, kwargs in router.captures if task == "write_file_tags"]
    assert len(jobs) == 1, f"expected exactly one write_file_tags dispatch, got {len(jobs)}"
    router.captures.clear()
    with patch("phaze.tasks.tag_write.get_settings", return_value=_agent_settings([str(scan_root)])):
        return await write_file_tags({"api_client": agent}, **jobs[0])


async def _log_rows(session: AsyncSession, file_id: uuid.UUID) -> dict[str, tuple[str, dict[str, Any]]]:
    """``{source: (status, before_tags)}`` per audit row, read back out of Postgres.

    Keyed by ``source`` rather than ordered by ``written_at``: that column defaults to ``now()``,
    which is the TRANSACTION start time, so two rows written under one test transaction can tie.
    A column SELECT rather than ORM entities, so the values come from the database and not from
    the identity map of objects this session already holds.
    """
    stmt = select(TagWriteLog.source, TagWriteLog.status, TagWriteLog.before_tags).where(TagWriteLog.file_id == file_id)
    rows = (await session.execute(stmt)).all()
    by_source = {row.source: (row.status, row.before_tags) for row in rows}
    assert len(by_source) == len(rows), f"more than one audit row per source: {rows}"
    return by_source


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", _FORMATS)
async def test_undo_restores_the_original_tags_on_disk(
    ext: str,
    client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    tmp_path: Path,
) -> None:
    """disk -> real extractor -> callback -> JSONB -> undo route -> write_file_tags -> disk.

    No patch on ``_extract_before_tags`` and no hand-set ``before_tags``: the only way the undo
    can restore the original is if the snapshot the agent read survived every hop intact.
    """
    agent_row, token = seed_test_agent
    path = _make_container(tmp_path, ext)
    _seed_original_tags(path)
    original_raw = _raw_tags(path)
    original_es = _essentia_read(path)
    # Fixture sanity: the shapes this test exists for are really on disk before anything runs.
    assert len(next(v for k, v in original_raw.items() if k in {"TCON", "genre", "\xa9gen"})) == 2, original_raw
    assert not {"TALB", "album", "\xa9alb"} & original_raw.keys(), original_raw

    record = await _applied_file(session, agent_row.id, path)
    _controller_queue, router = install_fake_queues(client)
    agent = _make_agent_client(session, token)

    # 1. The forward write, through the real producer and the real agent task.
    forward_log = await enqueue_tag_write(session, router, record, dict(_FORWARD), source="metadata")
    assert forward_log.status == TagWriteStatus.QUEUED.value
    forward = await _deliver_write_file_tags(router, agent, tmp_path)
    assert forward["status"] == str(TagWriteStatus.COMPLETED)

    # The write LANDED, as both independent readers see it -- without this, "restored" below could
    # be satisfied by a forward write that never changed anything.
    written_raw = _raw_tags(path)
    assert written_raw != original_raw
    assert {"TALB", "album", "\xa9alb"} & written_raw.keys(), "the forward write did not add the album"
    assert _essentia_read(path) != original_es

    # The snapshot as Postgres holds it: produced by the real extractor, never hand-set. The
    # explicit None for the absent album is the delete instruction the undo depends on.
    [(status, snapshot)] = (await _log_rows(session, record.id)).values()
    assert status == TagWriteStatus.COMPLETED.value
    assert "album" in snapshot, f"the absent album's explicit None was dropped before JSONB: {snapshot}"
    assert snapshot["album"] is None
    assert snapshot["genre"] == _ORIG_GENRES

    # 2. The undo, through the real route on the full app.
    undo = await client.post(f"/tags/{record.id}/undo")
    assert undo.status_code == 200, undo.text
    reverted = await _deliver_write_file_tags(router, agent, tmp_path)
    assert reverted["status"] == str(TagWriteStatus.COMPLETED), await _log_rows(session, record.id)

    # 3. The file is back where it started, per both readers that are not phaze.
    assert _raw_tags(path) == original_raw
    assert _essentia_read(path) == original_es

    rows = await _log_rows(session, record.id)
    assert {source: status for source, (status, _before) in rows.items()} == {
        "metadata": TagWriteStatus.COMPLETED.value,
        "undo": TagWriteStatus.COMPLETED.value,
    }
