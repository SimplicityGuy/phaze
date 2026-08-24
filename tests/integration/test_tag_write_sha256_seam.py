"""phaze-2zeu0: the tag-write -> stored-sha256 seam, driven to its REAL consumers.

A tag write rewrites the audio file's bytes (mutagen's ``audio.save()``), so the file's sha256
changes. ``FileRecord.sha256_hash`` is written exactly once, at ingest (``tasks/scan.py:218``), and
was never refreshed -- so every consumer that verifies bytes against that column failed
PERMANENTLY once a file had been tag-written, with no retry able to clear it.

WHY THESE TESTS LIVE HERE AND LOOK LIKE THIS
============================================

There is no seam TEST because there was no seam CODE. Producer (the tag write, on the agent) and
consumers (the execution pre-copy verify; the cloud lane's integrity gate) were written in
different phases and shared only a database column, so there is no call site to grep for and no
natural home for a test. That is what let this ship. These tests build the missing seam explicitly:
a real file on disk, the real write, the real callback, the real database column, handed to the
real consumer.

ADR-0012 RULE 3 IS THE WHOLE POINT OF THE SHAPE. "Verify with the artifact's real consumer, not
with the tool that produced it." Every pre-existing tag test re-reads the written file with
mutagen -- the producer's own tooling -- which round-trips perfectly and therefore CANNOT exhibit
this defect at any fidelity. So nothing below re-reads tags. Each test hands the written file, plus
the digest the database is holding, to the consumer's OWN function:

  * ``tasks.execution._verify_hash_or_raise``  -- the pre-copy verify, ``execution.py``'s
    ``if not already_moved:`` branch. Not a replay-only path: it is what a FRESH execution runs
    before it moves the file.
  * ``job_runner._verify_integrity_step``      -- the cloud lane's integrity gate, which
    ``sys.exit(EXIT_INTEGRITY)`` == 11, fail-fast, no retry.

THE REPRO IS ONE STEP SHORTER THAN THE BEAD ORIGINALLY DESCRIBED. The bead framed the execution
consumer as "renamed, then tag-written, then renamed AGAIN". It does not need the first rename, and
including it would test the wrong thing: ``FileRecord.original_path`` is never updated after a move
(execution updates ``current_path``), so a second execution resolves a path that no longer exists
and dies in ``_sha256_of_file`` with ``FileNotFoundError`` before any hash is ever compared -- a
DIFFERENT defect, filed as phaze-shzdj. The path exercised here is INGEST -> TAG-WRITE -> FIRST
EXECUTION, which is both the ordinary product order (the tags workspace and the propose/execute
pipeline are independent) and the one that fails for the reason this bead is about.

MEASURED, and the reason no "safe subset" argument survives: a write that did not change the file's
SIZE at all -- 5208 bytes before and after, the ID3 area having had padding to spare -- still
changed the digest. ``write_and_verify_sync``'s docstring speaks of mutagen rewriting the file "when
the tag area must grow", which invites the wrong inference that only growth-triggering writes are
affected. ``test_a_tag_write_that_does_not_change_the_file_size_still_changes_the_digest`` pins that
down so the inference cannot be re-derived later.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mutagen.mp3 import MP3
import pytest
from sqlalchemy import select

from phaze.database import get_session
from phaze.enums.tag_write import TagWriteStatus
from phaze.job_runner import _verify_integrity_step
from phaze.models.file import FileRecord
from phaze.models.tag_write_log import TagWriteLog
from phaze.routers.agent_tag_writes import router as agent_tag_writes_router
from phaze.services.hashing import compute_sha256
from phaze.tasks.execution import _verify_hash_or_raise
from phaze.tasks.tag_write import write_file_tags


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# A REAL audio file. Real MPEG1 Layer3 frames, parsed and rewritten by real mutagen -- the same
# ``_make_mp3`` shape tests/review/services/test_tag_writer.py and tests/review/tasks/test_tag_write.py
# already use. A synthetic byte blob would not survive `mutagen.File()` and so could not exercise
# `audio.save()`, which is the operation whose side effect this whole bead is about.
# ---------------------------------------------------------------------------


def _make_mp3(path: Path) -> Path:
    """Create a minimal valid MP3 with multiple MPEG frames + an ID3 container."""
    header = struct.pack(">I", 0xFFFB9000)  # MPEG1 Layer3 128kbps 44100Hz stereo
    frame_size = 417  # 144 * 128000 / 44100
    frame = header + b"\x00" * (frame_size - 4)
    path.write_bytes(frame * 10)
    audio = MP3(str(path))
    audio.add_tags()
    audio.save()
    return path


def _agent_settings(scan_roots: list[str]) -> Any:
    """A stand-in AgentSettings carrying only what the task reads (mirrors test_tag_write.py).

    ``spec=`` matters: the task gates on ``isinstance(cfg, AgentSettings)``.
    """
    from phaze.config import AgentSettings  # import-local, mirrors test_tag_write.py

    cfg = MagicMock(spec=AgentSettings)
    cfg.scan_roots = scan_roots
    return cfg


def _make_client(session: AsyncSession, token: str) -> AsyncClient:
    """The real callback router on a bare app, with the real token auth dep (test_agent_tag_writes.py pattern)."""
    app = FastAPI(title="smoke", version="test")
    app.include_router(agent_tag_writes_router)
    app.dependency_overrides[get_session] = lambda: session
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"})


async def _ingest(session: AsyncSession, agent_id: str, path: Path) -> FileRecord:
    """Persist the FileRecord ingest would write, hashing the file the way ``tasks/scan.py:218`` does."""
    record = FileRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        sha256_hash=compute_sha256(path),
        original_path=str(path),
        original_filename=path.name,
        current_path=str(path),
        file_type="mp3",
        file_size=path.stat().st_size,
    )
    session.add(record)
    await session.commit()
    return record


async def _queued_log(session: AsyncSession, file_id: uuid.UUID, after_tags: dict[str, Any]) -> TagWriteLog:
    """The ``queued`` audit row ``enqueue_tag_write`` creates before handing the job to the agent."""
    log = TagWriteLog(
        id=uuid.uuid4(),
        file_id=file_id,
        before_tags={},
        after_tags=after_tags,
        source="proposal",
        status=TagWriteStatus.QUEUED.value,
    )
    session.add(log)
    await session.commit()
    return log


async def _run_real_tag_write(
    session: AsyncSession,
    token: str,
    record: FileRecord,
    log: TagWriteLog,
    tags: dict[str, Any],
    path: Path,
) -> None:
    """Drive the REAL agent task, with its callback wired to the REAL control-plane endpoint.

    The only stand-in is the HTTP client object: ``PhazeAgentClient`` is replaced by an ``AsyncMock``
    whose ``patch_tag_write`` forwards the task's OWN payload to the real router over ASGI. So the
    payload the agent builds, its validation, the endpoint's transaction and the column write are
    all genuine -- what is elided is the socket, not any logic on either side of the seam.
    """
    async with _make_client(session, token) as client:

        async def _forward(log_id: uuid.UUID, payload: Any) -> Any:
            response = await client.patch(f"/api/internal/agent/tag-writes/{log_id}", json=payload.model_dump(mode="json"))
            assert response.status_code == 200, response.text
            return response.json()

        api = AsyncMock()
        api.patch_tag_write.side_effect = _forward
        with patch("phaze.tasks.tag_write.get_settings", return_value=_agent_settings([str(path.parent)])):
            await write_file_tags(
                {"api_client": api},
                log_id=str(log.id),
                file_id=str(record.id),
                agent_id=record.agent_id,
                file_path=str(path),
                tags=tags,
            )


async def _stored_hash(session: AsyncSession, file_id: uuid.UUID) -> str:
    """Read the digest back OUT of the database -- the column every consumer is served from.

    A column-only SELECT, deliberately: it goes to the database rather than through the ORM
    identity map, so it cannot hand back a cached pre-write attribute off the `FileRecord` these
    tests still hold. The callback's UPDATE ran on this same session, so the read sees it.
    """
    return (await session.execute(select(FileRecord.sha256_hash).where(FileRecord.id == file_id))).scalar_one()


class TestTagWriteRefreshesTheStoredDigest:
    """The producer half: after a real tag write, does the column describe the file on disk?"""

    @pytest.mark.asyncio
    async def test_a_tag_write_that_does_not_change_the_file_size_still_changes_the_digest(self, tmp_path: Path) -> None:
        """The premise, pinned: byte-length is NOT a proxy for byte-identity.

        This is the measurement that refutes the tempting "only growth-triggering writes are
        affected" reading of ``write_and_verify_sync``'s docstring, and with it Design option (c)
        ("avoid the rewrite so the hash stays valid") even in the easy in-place case. If this test
        ever fails because mutagen stopped rewriting in place, the bug is not gone -- re-measure
        before concluding anything.
        """
        from phaze.services.tag_write_disk import write_and_verify_sync

        path = _make_mp3(tmp_path / "<track-01>.mp3")
        before_digest, before_size = compute_sha256(path), path.stat().st_size

        status, _discrepancies, error, _before_tags = write_and_verify_sync(str(path), {"artist": "A", "title": "T"})

        assert status == TagWriteStatus.COMPLETED, error
        assert path.stat().st_size == before_size, "fixture no longer exercises the same-size case"
        assert compute_sha256(path) != before_digest

    @pytest.mark.asyncio
    async def test_the_real_task_and_callback_refresh_the_stored_hash(
        self,
        session: AsyncSession,
        seed_test_agent: tuple[Any, str],
        tmp_path: Path,
    ) -> None:
        """End to end across the seam: real write, real callback, real column.

        Pre-fix this asserted the stale INGEST digest; the write's effect on the column was nil.
        """
        agent, token = seed_test_agent
        path = _make_mp3(tmp_path / "<track-01>.mp3")
        record = await _ingest(session, agent.id, path)
        file_id, ingest_digest = record.id, record.sha256_hash
        log = await _queued_log(session, file_id, {"artist": "New Artist"})

        await _run_real_tag_write(session, token, record, log, {"artist": "New Artist"}, path)

        stored = await _stored_hash(session, file_id)
        assert stored != ingest_digest, "the tag write left the stored digest stale"
        assert stored == compute_sha256(path), "the stored digest does not describe the file on disk"


class TestConsumerOneOrdinaryExecution:
    """CONSUMER 1 -- ``tasks/execution.py``'s pre-copy verify, reached by a FRESH execution."""

    @pytest.mark.asyncio
    async def test_the_real_pre_copy_verify_accepts_a_tag_written_file(
        self,
        session: AsyncSession,
        seed_test_agent: tuple[Any, str],
        tmp_path: Path,
    ) -> None:
        """INGEST -> TAG-WRITE -> FIRST EXECUTION, through ``_verify_hash_or_raise`` itself.

        THIS IS THE TEST THAT MUST FAIL AGAINST THE PRE-FIX CODE, and against it it raised
        ``ValueError: sha256 mismatch for <path>: expected <ingest>, got <post-write>`` -- the exact
        message an operator would have seen, permanently, on the file's next execution.

        The digest handed to the consumer is read back out of the DATABASE, not recomputed here.
        That matters: recomputing it locally would compare the file against itself and pass no
        matter how stale the column is, which is precisely the mistake that let this ship.
        """
        agent, token = seed_test_agent
        path = _make_mp3(tmp_path / "<track-01>.mp3")
        record = await _ingest(session, agent.id, path)
        file_id = record.id
        log = await _queued_log(session, file_id, {"artist": "New Artist"})

        await _run_real_tag_write(session, token, record, log, {"artist": "New Artist"}, path)

        # Exactly what services/execution_dispatch.py ships into ExecuteBatchProposalItem.sha256_hash.
        await _verify_hash_or_raise(path, await _stored_hash(session, file_id), label=str(path))

    @pytest.mark.asyncio
    async def test_the_stale_digest_is_what_the_verify_rejects(self, tmp_path: Path) -> None:
        """The negative control: the consumer still REFUSES a genuinely wrong file.

        Without this, the test above could pass for the wrong reason -- a verify that accepted
        everything would satisfy it just as well. Here the ingest digest is deliberately kept and
        the verify must reject it, proving the gate is still armed and that what changed is the
        column's freshness, not the strictness of the check.
        """
        from phaze.services.tag_write_disk import write_and_verify_sync

        path = _make_mp3(tmp_path / "<track-01>.mp3")
        stale = compute_sha256(path)
        write_and_verify_sync(str(path), {"artist": "New Artist"})

        with pytest.raises(ValueError, match="sha256 mismatch"):
            await _verify_hash_or_raise(path, stale, label=str(path))


class TestConsumerTwoCloudLaneIntegrityGate:
    """CONSUMER 2 -- ``job_runner._verify_integrity_step``, which exits 11 with no retry."""

    @pytest.mark.asyncio
    async def test_the_real_integrity_gate_accepts_a_tag_written_file(
        self,
        session: AsyncSession,
        seed_test_agent: tuple[Any, str],
        tmp_path: Path,
    ) -> None:
        """``expected_sha256`` -> ``_verify_integrity_step`` for a tag-written file.

        ``routers/agent_files.py`` serves ``expected_sha256`` straight from
        ``FileRecord.sha256_hash``, so the digest read back from the database IS what the pod is
        handed. Pre-fix this exited 11 (``EXIT_INTEGRITY``) -- fail-fast, no retry, so the file's
        re-analysis was unrecoverable rather than merely delayed.
        """
        agent, token = seed_test_agent
        path = _make_mp3(tmp_path / "<track-01>.mp3")
        record = await _ingest(session, agent.id, path)
        file_id = record.id
        log = await _queued_log(session, file_id, {"artist": "New Artist"})

        await _run_real_tag_write(session, token, record, log, {"artist": "New Artist"}, path)

        await _verify_integrity_step(path, await _stored_hash(session, file_id), str(file_id))

    @pytest.mark.asyncio
    async def test_the_gate_still_exits_11_on_a_genuinely_stale_digest(self, tmp_path: Path) -> None:
        """Negative control for the cloud lane -- the integrity gate is unweakened.

        The fix refreshes what the column HOLDS; it must not soften what the pod CHECKS. A
        corrupt/partial transfer must still be caught.
        """
        from phaze.services.tag_write_disk import write_and_verify_sync

        path = _make_mp3(tmp_path / "<track-01>.mp3")
        stale = compute_sha256(path)
        write_and_verify_sync(str(path), {"artist": "New Artist"})

        with pytest.raises(SystemExit) as excinfo:
            await _verify_integrity_step(path, stale, "fid-stale")
        assert excinfo.value.code == 11


class TestWhichStatusesRefreshTheDigest:
    """IMPLEMENTER'S DECISION (not the operator's) -- see ``_write_verify_and_rehash``'s docstring.

    Bead phaze-2zeu0. On 2026-08-22 the operator chose the fix MECHANISM, with the option label
    "Re-hash on the agent, PATCH it back"; the durable record is that bead. The STATUS COVERAGE
    asserted below was never put to them and was decided by the implementer, because bytes land
    BEFORE ``write_and_verify_sync`` classifies the outcome: a fix wired only to ``COMPLETED``
    would leave ``DISCREPANCY`` and ``VERIFY_FAILED`` -- both of which follow a landed write --
    carrying stale digests.
    """

    @pytest.mark.asyncio
    async def test_a_discrepancy_write_still_refreshes_the_digest(
        self,
        session: AsyncSession,
        seed_test_agent: tuple[Any, str],
        tmp_path: Path,
    ) -> None:
        """DISCREPANCY means the write landed with the WRONG values -- the bytes still moved.

        Driven by writing a genre list the verifier compares strictly, so the real
        ``write_and_verify_sync`` classifies it without any mocking of the classification itself.
        """
        agent, token = seed_test_agent
        path = _make_mp3(tmp_path / "<track-01>.mp3")
        record = await _ingest(session, agent.id, path)
        file_id, ingest_digest = record.id, record.sha256_hash
        log = await _queued_log(session, file_id, {"artist": "New Artist"})

        # `verify_write` re-reads through `extract_tags`, whose normalized `title` is the written
        # text; a trailing-whitespace value round-trips as stripped, so the compare reports a
        # discrepancy while the write itself lands.
        await _run_real_tag_write(session, token, record, log, {"title": "Trailing "}, path)

        refreshed = await session.get(TagWriteLog, log.id)
        assert refreshed is not None
        assert refreshed.status in {TagWriteStatus.DISCREPANCY.value, TagWriteStatus.COMPLETED.value}
        stored = await _stored_hash(session, file_id)
        assert stored != ingest_digest
        assert stored == compute_sha256(path)

    @pytest.mark.asyncio
    async def test_a_refused_path_reports_no_hash_and_leaves_the_column_alone(
        self,
        session: AsyncSession,
        seed_test_agent: tuple[Any, str],
        tmp_path: Path,
    ) -> None:
        """The containment refusal returns BEFORE any disk I/O, so there is nothing observed.

        ``None`` must mean "not observed", never "unchanged" and never "clear it": the file is
        outside this agent's ``scan_roots``, which is exactly the file it must not read. The stored
        digest has to survive untouched -- and it must not be nulled, since the column is
        ``nullable=False`` and a NULL would additionally collapse every such file into one bogus
        duplicate group under ``services/dedup.py``'s ``GROUP BY sha256_hash``.
        """
        agent, token = seed_test_agent
        outside = tmp_path / "outside"
        outside.mkdir()
        path = _make_mp3(outside / "<track-01>.mp3")
        record = await _ingest(session, agent.id, path)
        file_id, ingest_digest = record.id, record.sha256_hash
        log = await _queued_log(session, file_id, {"artist": "New Artist"})

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        async with _make_client(session, token) as client:

            async def _forward(log_id: uuid.UUID, payload: Any) -> Any:
                response = await client.patch(f"/api/internal/agent/tag-writes/{log_id}", json=payload.model_dump(mode="json"))
                assert response.status_code == 200, response.text
                return response.json()

            api = AsyncMock()
            api.patch_tag_write.side_effect = _forward
            with patch("phaze.tasks.tag_write.get_settings", return_value=_agent_settings([str(allowed)])):
                result = await write_file_tags(
                    {"api_client": api},
                    log_id=str(log.id),
                    file_id=str(record.id),
                    agent_id=record.agent_id,
                    file_path=str(path),
                    tags={"artist": "New Artist"},
                )

        assert result["status"] == str(TagWriteStatus.FAILED)
        assert await _stored_hash(session, file_id) == ingest_digest
