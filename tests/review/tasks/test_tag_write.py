"""phaze-6bkk: the AGENT-side tag write -- on-disk mechanics + the result callback.

These are the write-mechanics cases that used to live in
``tests/review/services/test_tag_writer.py::TestExecuteTagWrite``. They moved with the code: the
mutagen write no longer happens in the api process (DIST-01 -- no media mount there), it happens on
the owning agent's ``meta`` lane. What is asserted is unchanged in substance (COMPLETED /
DISCREPANCY / VERIFY_FAILED / FAILED classification, before-tags capture on every path) plus the new
contract that the outcome is reported back over HTTP.

phaze-2xg8n added the containment check (below, ``_agent_settings`` / the ``with patch(...
get_settings...)`` wrapper on every case) and phaze-anrw4 added the separate pre-write snapshot
report (``report_tag_write_before_snapshot``, asserted in ``TestPreWriteSnapshot``).
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
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


def _agent_settings(scan_roots: list[str]) -> Any:
    """A stand-in AgentSettings carrying only what the task reads (mirrors test_cue_write.py).

    ``spec=`` matters: the task gates on ``isinstance(cfg, AgentSettings)`` (a control-role config
    has no ``scan_roots``), and only a spec'd mock satisfies that.
    """
    from phaze.config import AgentSettings

    cfg = MagicMock(spec=AgentSettings)
    cfg.scan_roots = scan_roots
    return cfg


def _patch_scan_roots(scan_roots: list[str]) -> Any:
    return patch("phaze.tasks.tag_write.get_settings", return_value=_agent_settings(scan_roots))


class TestWriteFileTagsTask:
    @pytest.mark.asyncio
    async def test_writes_tags_and_reports_completed(self, mp3_file: Path) -> None:
        """The happy path: tags land on disk and a COMPLETED result is PATCHed back."""
        ctx, api = _ctx()
        log_id = uuid.uuid4()

        with _patch_scan_roots([str(mp3_file.parent)]):
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

        with _patch_scan_roots([str(mp3_file.parent)]):
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

        with _patch_scan_roots([str(tmp_path)]):
            result = await write_file_tags(ctx, **_kwargs(str(tmp_path / "gone.mp3"), {"artist": "Test"}))

        assert result["status"] == TagWriteStatus.FAILED
        payload = api.patch_tag_write.await_args.args[1]
        assert payload.status == TagWriteStatus.FAILED
        assert payload.error_message

    @pytest.mark.asyncio
    async def test_discrepancy_is_reported_with_the_field_detail(self, mp3_file: Path) -> None:
        """A verify mismatch reports DISCREPANCY plus the per-field expected/actual map."""
        ctx, api = _ctx()

        with (
            _patch_scan_roots([str(mp3_file.parent)]),
            patch("phaze.services.tag_write_disk.verify_write", return_value={"artist": {"expected": "A", "actual": "B"}}),
        ):
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
            _patch_scan_roots([str(mp3_file.parent)]),
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

        with _patch_scan_roots([str(mp3_file.parent)]), pytest.raises(RuntimeError, match="hub unreachable"):
            await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "Test"}))

    @pytest.mark.asyncio
    async def test_disk_work_runs_off_the_event_loop(self, mp3_file: Path) -> None:
        """phaze-qfxv discipline survives the move: the disk work runs off the event loop.

        phaze-anrw4 split the offload in two (the pre-write snapshot extraction, then
        ``write_and_verify_sync``) -- both calls must still go through ``asyncio.to_thread``, never
        directly on the loop. The agent's event loop also runs the Phase-46 liveness heartbeat, so an
        on-loop mutagen stall against a slow mount risks a false DEAD classification and
        duplicate-work re-enqueue.
        """
        ctx, _api = _ctx()

        with (
            _patch_scan_roots([str(mp3_file.parent)]),
            patch("phaze.tasks.tag_write.asyncio.to_thread", new_callable=AsyncMock) as to_thread,
        ):
            to_thread.side_effect = [{}, (TagWriteStatus.COMPLETED, None, None, {})]
            await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "Test"}))

        assert to_thread.await_count == 2
        assert to_thread.await_args_list[0].args[0].__name__ == "_extract_before_tags"
        assert to_thread.await_args_list[1].args[0].__name__ == "write_and_verify_sync"


class TestContainment:
    """phaze-2xg8n: ``file_path`` is agent-supplied (``FileRecord.current_path``) -- refuse anything
    that resolves outside this agent's own configured ``scan_roots`` BEFORE any disk I/O, exactly
    the gate the move/execution path already applies (T-26-11-S1)."""

    @pytest.mark.asyncio
    async def test_path_outside_scan_roots_is_refused_without_writing(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        roots = tmp_path / "roots"
        roots.mkdir()
        target = _make_mp3(outside / "<track-01>.mp3")
        original_bytes = target.read_bytes()

        ctx, api = _ctx()

        with _patch_scan_roots([str(roots)]):
            result = await write_file_tags(ctx, **_kwargs(str(target), {"artist": "New Artist"}))

        assert result["status"] == TagWriteStatus.FAILED
        assert target.read_bytes() == original_bytes, "a refused write must never touch the file"

        payload = api.patch_tag_write.await_args.args[1]
        assert payload.status == TagWriteStatus.FAILED
        assert "escapes all scan_roots" in (payload.error_message or "")

        # The refusal is a terminal audit report, never a raise -- a retry of the SAME bad path
        # will never resolve differently, so this must not burn the SAQ retry budget.
        api.report_tag_write_before_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_scan_roots_refuses_everything(self, mp3_file: Path) -> None:
        """An empty root list matches nothing -- 'refuse', never 'allow' (services/containment.py).

        This is also the DEFAULT in a plain unmocked ``get_settings()`` (role=control has no
        ``scan_roots`` at all), so the gate fails closed if this task is ever wired without an
        AgentSettings role.
        """
        ctx, api = _ctx()

        with _patch_scan_roots([]):
            result = await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "Test"}))

        assert result["status"] == TagWriteStatus.FAILED
        payload = api.patch_tag_write.await_args.args[1]
        assert "escapes all scan_roots" in (payload.error_message or "")


class TestPreWriteSnapshot:
    """phaze-anrw4: the pre-write snapshot is reported in its OWN call, BEFORE the mutating write."""

    @pytest.mark.asyncio
    async def test_before_snapshot_is_reported_before_the_write_lands(self, mp3_file: Path) -> None:
        ctx, api = _ctx()
        log_id = uuid.uuid4()

        with _patch_scan_roots([str(mp3_file.parent)]):
            await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "New Artist"}, log_id))

        api.report_tag_write_before_snapshot.assert_awaited_once()
        called_log_id, snapshot_payload = api.report_tag_write_before_snapshot.await_args.args
        assert called_log_id == log_id
        assert set(snapshot_payload.before_tags) == {"artist", "title", "album", "year", "genre", "track_number"}
        assert snapshot_payload.before_tags["artist"] is None  # captured before the write

    @pytest.mark.asyncio
    async def test_snapshot_report_failure_does_not_abort_the_write(self, mp3_file: Path) -> None:
        """Best-effort: the terminal callback's own ``before_tags`` is the single-attempt fallback."""
        ctx, api = _ctx()
        api.report_tag_write_before_snapshot.side_effect = RuntimeError("hub unreachable")

        with _patch_scan_roots([str(mp3_file.parent)]):
            result = await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "New Artist"}))

        assert result["status"] == TagWriteStatus.COMPLETED
        assert MP3(str(mp3_file)).tags["TPE1"].text == ["New Artist"]
        api.patch_tag_write.assert_awaited_once()
        payload = api.patch_tag_write.await_args.args[1]
        assert payload.before_tags["artist"] is None

    @pytest.mark.asyncio
    async def test_snapshot_reported_before_write_and_verify(self, mp3_file: Path) -> None:
        """Ordering: the snapshot call must complete before the mutating disk work starts."""
        ctx, api = _ctx()
        calls: list[str] = []

        async def _record_snapshot(*_args: Any, **_kwargs: Any) -> None:
            calls.append("snapshot")

        api.report_tag_write_before_snapshot.side_effect = _record_snapshot

        with (
            _patch_scan_roots([str(mp3_file.parent)]),
            patch("phaze.services.tag_write_disk.write_tags", side_effect=lambda *_a, **_k: calls.append("write")),
        ):
            await write_file_tags(ctx, **_kwargs(str(mp3_file), {"artist": "New Artist"}))

        assert calls[0] == "snapshot"
        assert calls[1] == "write"
