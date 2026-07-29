"""Tests for the tag writer service."""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from mutagen.mp3 import MP3
import pytest

from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.tag_write_log import TagWriteStatus


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession
from phaze.services.tag_write_disk import write_and_verify_sync
from phaze.services.tag_writer import (
    _extract_before_tags,
    _write_mp4,
    _write_vorbis,
    enqueue_tag_write,
    verify_write,
    write_tags,
)


async def _add_proposal(session: AsyncSession, file_id: uuid.UUID, status: str) -> None:
    """Insert one ``RenameProposal`` with ``status`` for ``file_id`` and commit."""
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename="Renamed Set.mp3",
            proposed_path=None,
            confidence=0.95,
            status=status,
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Fixtures: minimal valid audio files
# ---------------------------------------------------------------------------


def _make_mp3(path: Path) -> Path:
    """Create a minimal valid MP3 file with multiple MPEG frames + ID3 tags."""
    # MPEG1 Layer3 128kbps 44100Hz stereo, no padding, no CRC
    header = struct.pack(">I", 0xFFFB9000)
    frame_size = 417  # 144 * 128000 / 44100 = 417 bytes
    frame = header + b"\x00" * (frame_size - 4)
    # Write 10 frames so mutagen can sync properly
    path.write_bytes(frame * 10)
    # Add ID3 tags via mutagen
    audio = MP3(str(path))
    audio.add_tags()
    audio.save()
    return path


@pytest.fixture
def mp3_file(tmp_path: Path) -> Path:
    """Create a temporary MP3 file."""
    return _make_mp3(tmp_path / "test.mp3")


class TestWriteTags:
    """Tests for write_tags function."""

    def test_write_id3_tags_to_mp3(self, mp3_file: Path) -> None:
        """Write ID3 tags to an MP3 file and read them back."""
        tags = {
            "artist": "Test Artist",
            "title": "Test Title",
            "album": "Test Album",
            "year": "2024",
            "genre": "Electronic",
            "track_number": "3",
        }
        write_tags(str(mp3_file), tags)

        audio = MP3(str(mp3_file))
        assert audio.tags is not None
        assert str(audio.tags["TPE1"]) == "Test Artist"
        assert str(audio.tags["TIT2"]) == "Test Title"
        assert str(audio.tags["TALB"]) == "Test Album"

    def test_write_tags_handles_none_audio(self, tmp_path: Path) -> None:
        """write_tags raises ValueError for non-audio files."""
        bad_file = tmp_path / "not_audio.txt"
        bad_file.write_text("not audio")
        with pytest.raises(ValueError, match="not a recognized audio"):
            write_tags(str(bad_file), {"artist": "Test"})

    def test_write_tags_adds_tags_when_none(self, mp3_file: Path) -> None:
        """write_tags handles files with no existing tags by adding them."""
        # Remove existing tags
        audio = MP3(str(mp3_file))
        audio.delete()
        audio.save()

        # Now write tags
        write_tags(str(mp3_file), {"artist": "New Artist"})

        # Verify
        audio = MP3(str(mp3_file))
        assert audio.tags is not None
        assert str(audio.tags["TPE1"]) == "New Artist"

    def test_write_tags_none_leaves_absent_field_absent(self, mp3_file: Path) -> None:
        """A None value for an already-absent field is a harmless no-op (nothing to delete)."""
        write_tags(str(mp3_file), {"artist": "Test", "title": None})

        audio = MP3(str(mp3_file))
        assert "TPE1" in audio.tags
        assert "TIT2" not in audio.tags

    def test_write_tags_none_deletes_existing_id3_frame(self, mp3_file: Path) -> None:
        """phaze-52qd: a None value DELETES an existing ID3 frame (the undo delete path)."""
        write_tags(str(mp3_file), {"artist": "Sven Vath", "album": "Coachella 2024"})
        audio = MP3(str(mp3_file))
        assert "TPE1" in audio.tags
        assert "TALB" in audio.tags

        # Now re-apply a snapshot that marks both fields absent -- they must be removed.
        write_tags(str(mp3_file), {"artist": None, "album": None})
        audio = MP3(str(mp3_file))
        assert "TPE1" not in audio.tags
        assert "TALB" not in audio.tags


class TestWriteVorbisFormat:
    """Tests for Vorbis format writing via mock (OGG/FLAC/OPUS)."""

    def test_write_vorbis_keys(self) -> None:
        """Vorbis writer sets correct keys with list-wrapped values."""
        audio = MagicMock()
        _write_vorbis(audio, {"artist": "Vorbis Artist", "title": "Vorbis Title", "year": "2024"})

        audio.__setitem__.assert_any_call("artist", ["Vorbis Artist"])
        audio.__setitem__.assert_any_call("title", ["Vorbis Title"])
        audio.__setitem__.assert_any_call("date", ["2024"])

    def test_write_vorbis_skips_none(self) -> None:
        """Vorbis writer skips None values."""
        audio = MagicMock()
        _write_vorbis(audio, {"artist": "Test", "title": None})

        audio.__setitem__.assert_called_once_with("artist", ["Test"])

    def test_write_vorbis_track_number(self) -> None:
        """Vorbis writer maps track_number to 'tracknumber'."""
        audio = MagicMock()
        _write_vorbis(audio, {"track_number": "7"})

        audio.__setitem__.assert_called_once_with("tracknumber", ["7"])


class TestWriteMP4Format:
    """Tests for MP4/M4A format writing via mock."""

    def test_write_mp4_keys(self) -> None:
        """MP4 writer sets correct atom keys."""
        audio = MagicMock()
        _write_mp4(audio, {"artist": "MP4 Artist", "album": "MP4 Album"})

        audio.__setitem__.assert_any_call("\xa9ART", ["MP4 Artist"])
        audio.__setitem__.assert_any_call("\xa9alb", ["MP4 Album"])

    def test_write_mp4_track_number_tuple(self) -> None:
        """MP4 writer uses [(track_number, 0)] tuple format for trkn."""
        audio = MagicMock()
        _write_mp4(audio, {"track_number": "5"})

        audio.__setitem__.assert_called_once_with("trkn", [(5, 0)])

    def test_write_mp4_skips_none(self) -> None:
        """MP4 writer skips None values."""
        audio = MagicMock()
        _write_mp4(audio, {"artist": "Test", "title": None})

        audio.__setitem__.assert_called_once_with("\xa9ART", ["Test"])


class TestVerifyWrite:
    """Tests for verify_write function."""

    def test_perfect_write_returns_empty(self, mp3_file: Path) -> None:
        """verify_write returns empty dict when written tags match."""
        tags = {"artist": "Test Artist", "title": "Test Title"}
        write_tags(str(mp3_file), tags)
        discrepancies = verify_write(str(mp3_file), tags)
        assert discrepancies == {}

    def test_discrepancy_detected(self, mp3_file: Path) -> None:
        """verify_write detects mismatched tags."""
        write_tags(str(mp3_file), {"artist": "Actual Artist"})
        discrepancies = verify_write(str(mp3_file), {"artist": "Expected Artist"})
        assert "artist" in discrepancies
        assert discrepancies["artist"]["expected"] == "Expected Artist"
        assert discrepancies["artist"]["actual"] == "Actual Artist"

    def test_verify_none_expected_passes_when_field_absent(self, mp3_file: Path) -> None:
        """An expected None for an absent field is NOT a discrepancy (a deletion that held)."""
        write_tags(str(mp3_file), {"artist": "Test"})
        discrepancies = verify_write(str(mp3_file), {"artist": "Test", "title": None})
        assert discrepancies == {}

    def test_verify_none_expected_flags_surviving_field(self, mp3_file: Path) -> None:
        """phaze-52qd: an expected None is a discrepancy when the field is still on disk.

        This is what makes an undo that FAILED to delete an added tag report a real discrepancy
        instead of a false 'completed' reversal.
        """
        write_tags(str(mp3_file), {"artist": "Should Be Deleted"})
        discrepancies = verify_write(str(mp3_file), {"artist": None})
        assert "artist" in discrepancies
        assert discrepancies["artist"]["expected"] is None
        assert discrepancies["artist"]["actual"] == "Should Be Deleted"


class TestExtractBeforeTags:
    """phaze-52qd: the before/undo snapshot must span every core field, marking absent tags None."""

    def test_records_absent_fields_as_none(self, mp3_file: Path) -> None:
        """A previously-untagged file yields an all-None snapshot -- not an empty dict.

        Pre-fix this returned {} (only non-None fields), so undo had nothing to delete and the
        tags a write ADDED survived the 'revert'.
        """
        snapshot = _extract_before_tags(str(mp3_file))
        assert snapshot == {
            "artist": None,
            "title": None,
            "album": None,
            "year": None,
            "genre": None,
            "track_number": None,
        }

    def test_records_present_and_absent_together(self, mp3_file: Path) -> None:
        """Present fields keep their values; absent fields are explicit None."""
        write_tags(str(mp3_file), {"artist": "Present Artist"})
        snapshot = _extract_before_tags(str(mp3_file))
        assert snapshot["artist"] == "Present Artist"
        assert snapshot["album"] is None
        assert set(snapshot) == {"artist", "title", "album", "year", "genre", "track_number"}


class TestUndoDeletesAddedTags:
    """phaze-52qd end-to-end: reverting a write that ADDED tags removes them from disk."""

    def test_undo_snapshot_removes_added_tags(self, mp3_file: Path) -> None:
        """Write artist+album into an untagged file, then re-apply the before snapshot to revert.

        The before snapshot (all-None for the untagged file) must delete both added frames and the
        reversal must verify COMPLETED, not silently leave the tags on disk.

        phaze-6bkk: exercised through ``write_and_verify_sync`` -- the on-disk sequence the AGENT
        runs. It used to go through ``execute_tag_write``, but that function no longer touches a
        file: under DIST-01 the api container has no media mount, so the write was moved to the
        owning agent's meta lane. The undo SEMANTICS under test (a None-valued snapshot DELETES the
        frame a write added) are unchanged and still live here.
        """
        # Untagged file -> capture the true before snapshot (all None).
        before = _extract_before_tags(str(mp3_file))

        status, _disc, _err, _before = write_and_verify_sync(str(mp3_file), {"artist": "Sven Vath", "album": "Coachella 2024"})
        assert status == TagWriteStatus.COMPLETED
        audio = MP3(str(mp3_file))
        assert "TPE1" in audio.tags
        assert "TALB" in audio.tags

        # Undo re-applies the captured before snapshot.
        undo_status, _disc2, _err2, _before2 = write_and_verify_sync(str(mp3_file), before)

        assert undo_status == TagWriteStatus.COMPLETED
        audio = MP3(str(mp3_file))
        assert "TPE1" not in audio.tags
        assert "TALB" not in audio.tags

    def test_verify_raises_on_unreadable_file(self, tmp_path: Path) -> None:
        """phaze-vq3g: an unreadable/absent file on re-read raises TagReadError, not a false discrepancy.

        Pre-fix, verify_write re-read via the SWALLOWING ``extract_tags``, so an I/O failure produced
        an all-field ``actual=None`` discrepancy indistinguishable from a genuinely-wrong write.
        """
        from phaze.services.metadata import TagReadError

        missing = tmp_path / "gone.mp3"
        with pytest.raises(TagReadError):
            verify_write(str(missing), {"artist": "Written Value"})

    def test_verify_absent_tags_is_a_real_discrepancy_not_a_read_failure(self, mp3_file: Path) -> None:
        """phaze-vq3g: a file that opens cleanly but LACKS the tag is a real discrepancy (actual=None).

        This is the case that must stay a discrepancy -- the distinction the fix draws is between a
        re-read that FAILED (raises) and tags that are genuinely absent (readable, reported None).
        """
        # Write nothing; the freshly-tagged (empty) file is fully readable but has no artist frame.
        discrepancies = verify_write(str(mp3_file), {"artist": "Expected"})
        assert "artist" in discrepancies
        assert discrepancies["artist"]["actual"] is None


class TestEnqueueTagWrite:
    """phaze-6bkk: ``enqueue_tag_write`` creates the audit row and DISPATCHES -- it writes nothing.

    The old ``execute_tag_write`` performed the mutagen write inline in the api process. That could
    never work in the documented production topology: DIST-01 gives the api container no media
    mount, so ``current_path`` (a file-SERVER path) did not exist there and every write failed
    ``[Errno 2]`` behind a toast blaming file permissions. The disk half now lives in
    ``phaze.services.tag_write_disk`` / ``phaze.tasks.tag_write`` and is tested there; what remains
    here is the control-plane contract: guard, ``queued`` audit row, lane routing, and the
    dispatch-failure path.

    READ-05 / D-01: the guard gates on ``await is_applied(session, file_record.id)`` -- a real DB
    ``EXISTS`` over ``proposals.status == 'executed'`` -- NOT on ``file_record.state``.
    """

    def _make_file_record(self, current_path: str = "/data/music/<set-01>.mp3", agent_id: str = "fileserver-01") -> MagicMock:
        """Mock FileRecord for the dispatch cases (the applied guard is patched separately)."""
        fr = MagicMock()
        fr.current_path = current_path
        fr.agent_id = agent_id
        fr.id = uuid.uuid4()
        return fr

    # ------------------------------------------------------------------------------------------------
    # SC#2 guard behavior (real DB rows, mutation-checked) -- the load-bearing behavior-revival test.
    # ------------------------------------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_applied_file_passes_guard(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """SC#2: an actually-applied file (executed proposal, ``state != 'executed'``) PASSES the guard.

        Mutation check: reverting the guard to ``file_record.state != EXECUTED`` makes this fixture
        (applied-ness via proposals.status) RAISE and this test go RED.
        """
        file = await make_file()
        await _add_proposal(session, file.id, ProposalStatus.EXECUTED.value)
        router = MagicMock()
        router.enqueue_for_file = AsyncMock()

        log_entry = await enqueue_tag_write(session, router, file, {"artist": "New Artist"}, "tracklist")

        # The guard admitted the file and the dispatch path ran to completion.
        assert log_entry.status == TagWriteStatus.QUEUED
        assert log_entry.file_id == file.id
        router.enqueue_for_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_applied_file_raises(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A file with no executed proposal (only a failed one) RAISES ``ValueError`` matching 'executed'."""
        file = await make_file()
        await _add_proposal(session, file.id, ProposalStatus.FAILED.value)
        router = MagicMock()
        router.enqueue_for_file = AsyncMock()

        with pytest.raises(ValueError, match="executed"):
            await enqueue_tag_write(session, router, file, {"artist": "Test"}, "tracklist")

        router.enqueue_for_file.assert_not_awaited()

    # ------------------------------------------------------------------------------------------------
    # Dispatch contract -- the guard is explicitly admitted so routing is exercised in isolation.
    # ------------------------------------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_creates_queued_tag_write_log(self) -> None:
        """The audit row is created up front in ``queued``, with an EMPTY before-tags snapshot.

        ``before_tags`` can only be read where the file is, so it stays empty until the agent's
        callback fills it -- the row never claims a pre-write state the api did not observe.
        """
        fr = self._make_file_record()
        session = AsyncMock()
        router = MagicMock()
        router.enqueue_for_file = AsyncMock()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "New Artist"}, "tracklist")

        assert log_entry.status == TagWriteStatus.QUEUED
        assert log_entry.source == "tracklist"
        assert log_entry.after_tags == {"artist": "New Artist"}
        assert log_entry.before_tags == {}
        assert log_entry.error_message is None
        session.add.assert_called_once()
        session.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_routes_to_the_owning_agent_with_current_path(self) -> None:
        """phaze-c9w9 affinity + the D-24 ``current_path`` exception, both asserted on the payload.

        The write MUST go to ``file_record.agent_id`` -- the agent that reported the file -- and MUST
        carry ``current_path``, because a tag write is only offered for an APPLIED file whose
        ``original_path`` no longer names anything on disk.
        """
        fr = self._make_file_record(current_path="/data/music/<set-02>.mp3", agent_id="fileserver-02")
        session = AsyncMock()
        router = MagicMock()
        router.enqueue_for_file = AsyncMock()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "Test"}, "tracklist")

        kwargs = router.enqueue_for_file.await_args.kwargs
        assert kwargs["task_name"] == "write_file_tags"
        assert kwargs["file_record"] is fr
        payload = kwargs["payload"]
        assert payload.file_path == "/data/music/<set-02>.mp3"
        assert payload.agent_id == "fileserver-02"
        assert payload.tags == {"artist": "Test"}
        # The pre-minted log id is what makes the agent's callback retry-stable.
        assert payload.log_id == log_entry.id

    @pytest.mark.asyncio
    async def test_write_file_tags_routes_to_the_meta_lane(self) -> None:
        """The dispatched task name resolves to the ``meta`` lane -- the rw-mounted worker."""
        from phaze.services.enqueue_router import lane_for_task

        assert lane_for_task("write_file_tags") == "meta"

    @pytest.mark.asyncio
    async def test_enqueue_failure_downgrades_the_row_to_failed(self) -> None:
        """A broker/enqueue failure leaves a FAILED row with an actionable message, not a stuck ``queued``.

        A ``queued`` row no agent will ever answer for would hold the file out of every terminal
        count forever with nothing on screen explaining why.
        """
        fr = self._make_file_record(agent_id="fileserver-09")
        session = AsyncMock()
        router = MagicMock()
        router.enqueue_for_file = AsyncMock(side_effect=RuntimeError("broker unreachable"))

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "Test"}, "manual_edit")

        assert log_entry.status == TagWriteStatus.FAILED
        assert log_entry.error_message is not None
        assert "fileserver-09" in log_entry.error_message
        assert "broker unreachable" in log_entry.error_message

    @pytest.mark.asyncio
    async def test_does_not_touch_the_filesystem(self, tmp_path: Path) -> None:
        """The DIST-01 regression guard: a nonexistent path is NOT an error on the control plane.

        Pre-fix this exact call did ``mutagen.File("/data/music/...")`` inside the api container and
        recorded FAILED for every file forever. Post-fix the api only records intent and hands off,
        so a path it cannot see is irrelevant to it.
        """
        fr = self._make_file_record(current_path=str(tmp_path / "does-not-exist.mp3"))
        session = AsyncMock()
        router = MagicMock()
        router.enqueue_for_file = AsyncMock()

        with patch("phaze.services.tag_writer.is_applied", AsyncMock(return_value=True)):
            log_entry = await enqueue_tag_write(session, router, fr, {"artist": "Test"}, "tracklist")

        assert log_entry.status == TagWriteStatus.QUEUED
        assert log_entry.error_message is None
