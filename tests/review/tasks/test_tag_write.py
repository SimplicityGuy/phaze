"""phaze-6bkk: the AGENT-side tag write -- on-disk mechanics + the result callback.

These are the write-mechanics cases that used to live in
``tests/review/services/test_tag_writer.py::TestExecuteTagWrite``. They moved with the code: the
mutagen write no longer happens in the api process (DIST-01 -- no media mount there), it happens on
the owning agent's ``meta`` lane. What is asserted is unchanged in substance (COMPLETED /
DISCREPANCY / VERIFY_FAILED / FAILED classification, before-tags capture on every path) plus the new
contract that the outcome is reported back over HTTP.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
import uuid

from mutagen.mp3 import MP3
import pytest

from phaze.enums.tag_write import TagWriteStatus
from phaze.tasks.tag_write import write_file_tags


if TYPE_CHECKING:
    from pathlib import Path


def _make_mp3(path: Path) -> Path:
    """Create a minimal valid MP3 file with multiple MPEG frames + ID3 tags."""
    header = struct.pack(">I", 0xFFFB9000)
    frame_size = 417  # 144 * 128000 / 44100
    frame = header + b"\x00" * (frame_size - 4)
    path.write_bytes(frame * 10)
    audio = MP3(str(path))
    audio.add_tags()
    audio.save()
    return path


@pytest.fixture
def mp3_file(tmp_path: Path) -> Path:
    """A temporary MP3 file standing in for a file on the agent's media mount."""
    return _make_mp3(tmp_path / "<track-01>.mp3")


def _ctx() -> tuple[dict[str, Any], AsyncMock]:
    """SAQ ctx carrying a mock PhazeAgentClient, plus the client for assertions."""
    api = AsyncMock()
    return {"api_client": api}, api


def _kwargs(path: str, tags: dict[str, Any], log_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "log_id": str(log_id or uuid.uuid4()),
        "file_id": str(uuid.uuid4()),
        "agent_id": "fileserver-01",
        "file_path": path,
        "tags": tags,
    }


class TestWriteFileTagsTask:
    @pytest.mark.asyncio
    async def test_writes_tags_and_reports_completed(self, mp3_file: Path) -> None:
        """The happy path: tags land on disk and a COMPLETED result is PATCHed back."""
        ctx, api = _ctx()
        log_id = uuid.uuid4()

        result = await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "New Artist"}, log_id))

        assert result["status"] == TagWriteStatus.COMPLETED
        assert MP3(str(mp3_file)).tags["TPE1"].text == ["New Artist"]

        api.patch_tag_write.assert_awaited_once()
        called_log_id, payload = api.patch_tag_write.await_args.args
        assert called_log_id == log_id
        assert payload.status == TagWriteStatus.COMPLETED
        assert payload.error_message is None

    @pytest.mark.asyncio
    async def test_reports_the_before_tags_snapshot(self, mp3_file: Path) -> None:
        """phaze-52qd: the COMPLETE pre-write snapshot rides the callback.

        It can only be captured here -- the control plane cannot read the file -- and it is what an
        undo re-applies, so a missing/partial snapshot silently breaks undo.
        """
        ctx, api = _ctx()

        await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "New Artist"}))

        payload = api.patch_tag_write.await_args.args[1]
        assert set(payload.before_tags) == {"artist", "title", "album", "year", "genre", "track_number"}
        assert payload.before_tags["artist"] is None  # the fixture file starts untagged

    @pytest.mark.asyncio
    async def test_missing_file_reports_failed_without_raising(self, tmp_path: Path) -> None:
        """A failed WRITE is not a failed JOB -- it is a terminal audit status the operator retries.

        Only the callback may fail the job; a mutagen error is classified and reported.
        """
        ctx, api = _ctx()

        result = await write_file_tags(ctx, **_kwargs(str(tmp_path / "gone.mp3"), {"artist": "Test"}))

        assert result["status"] == TagWriteStatus.FAILED
        payload = api.patch_tag_write.await_args.args[1]
        assert payload.status == TagWriteStatus.FAILED
        assert payload.error_message

    @pytest.mark.asyncio
    async def test_discrepancy_is_reported_with_the_field_detail(self, mp3_file: Path) -> None:
        """A verify mismatch reports DISCREPANCY plus the per-field expected/actual map."""
        ctx, api = _ctx()

        with patch("phaze.services.tag_write_disk.verify_write", return_value={"artist": {"expected": "A", "actual": "B"}}):
            result = await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "A"}))

        assert result["status"] == TagWriteStatus.DISCREPANCY
        payload = api.patch_tag_write.await_args.args[1]
        assert payload.discrepancies == {"artist": {"expected": "A", "actual": "B"}}

    @pytest.mark.asyncio
    async def test_verify_read_failure_reports_verify_failed_not_discrepancy(self, mp3_file: Path) -> None:
        """phaze-vq3g: a LANDED write whose verify re-read fails is VERIFY_FAILED, with no fake discrepancy."""
        from phaze.services.metadata import TagReadError

        ctx, api = _ctx()

        with (
            patch("phaze.services.tag_write_disk.write_tags"),
            patch("phaze.services.tag_write_disk.verify_write", side_effect=TagReadError("mount hiccup on re-read")),
        ):
            result = await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "A"}))

        assert result["status"] == TagWriteStatus.VERIFY_FAILED
        payload = api.patch_tag_write.await_args.args[1]
        assert payload.discrepancies is None
        assert "verify failed" in (payload.error_message or "")

    @pytest.mark.asyncio
    async def test_callback_failure_raises_so_saq_retries(self, mp3_file: Path) -> None:
        """If the control plane never hears the outcome, the audit row is stranded in ``queued``.

        That is the one failure this task must NOT swallow -- it takes SAQ's retry instead.
        """
        ctx, api = _ctx()
        api.patch_tag_write.side_effect = RuntimeError("hub unreachable")

        with pytest.raises(RuntimeError, match="hub unreachable"):
            await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "Test"}))

    @pytest.mark.asyncio
    async def test_disk_work_runs_off_the_event_loop(self, mp3_file: Path) -> None:
        """phaze-qfxv discipline survives the move: the whole blocking sequence is ONE to_thread offload.

        The agent's event loop also runs the Phase-46 liveness heartbeat, so an on-loop mutagen stall
        against a slow mount risks a false DEAD classification and duplicate-work re-enqueue.
        """
        ctx, _api = _ctx()

        with patch("phaze.tasks.tag_write.asyncio.to_thread", new_callable=AsyncMock) as to_thread:
            to_thread.return_value = (TagWriteStatus.COMPLETED, None, None, {})
            await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "Test"}))

        to_thread.assert_awaited_once()
        assert to_thread.await_args.args[0].__name__ == "write_and_verify_sync"
