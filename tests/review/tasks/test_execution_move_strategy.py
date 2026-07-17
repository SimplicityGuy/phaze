"""Regression tests for the execute_approved_batch move strategy (bead phaze-uciu.5).

The executor must never load a whole file into RAM: the core use case is
multi-GB concert videos and ``execute_approved_batch`` runs on the 'meta' lane
(concurrency 2, no memory pin), so a whole-file read would MemoryError or get
the worker OOM-killed. The move therefore:

* prefers ``os.replace`` (atomic, O(1), constant memory) when source and
  destination share a filesystem -- the move IS the delete; and
* falls back to a bounded ``shutil.copyfileobj`` stream + fsync + unlink across
  a filesystem boundary.

These tests cover BOTH branches plus the bounded-memory guarantee (the fallback
must not call ``Path.read_bytes``) and the helper units.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
import uuid

from phaze.config import AgentSettings
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
import phaze.tasks.execution as execmod
from phaze.tasks.execution import _same_filesystem, _streamed_copy, execute_approved_batch


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_api_client_mock() -> AsyncMock:
    api = AsyncMock()
    api.post_execution_log = AsyncMock(return_value=MagicMock(execution_log_id=uuid.uuid4()))
    api.patch_execution_log = AsyncMock(return_value=None)
    api.patch_proposal_state = AsyncMock(return_value=None)
    api.post_exec_batch_progress = AsyncMock(return_value=None)
    return api


def _patch_settings(monkeypatch: pytest.MonkeyPatch, scan_roots: list[str]) -> None:
    fake_cfg = MagicMock(spec=AgentSettings)
    fake_cfg.scan_roots = scan_roots
    monkeypatch.setattr("phaze.tasks.execution.get_settings", lambda: fake_cfg)


def _item(orig: Path, proposed_path: str, filename: str) -> ExecuteBatchProposalItem:
    return ExecuteBatchProposalItem(
        proposal_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        original_path=str(orig),
        proposed_path=proposed_path,
        proposed_filename=filename,
    )


async def test_same_filesystem_move_uses_os_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same-fs move goes through os.replace (atomic) and never streams/read_bytes."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "orig" / "concert.mp4"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"a" * (3 * 1024 * 1024)
    orig.write_bytes(content)

    from pathlib import Path as _Path

    calls = {"replace": 0, "stream": 0}
    real_replace = _Path.replace

    def spy_replace(self: _Path, target: object) -> object:
        calls["replace"] += 1
        return real_replace(self, target)

    monkeypatch.setattr(_Path, "replace", spy_replace)
    monkeypatch.setattr(execmod, "_streamed_copy", lambda _s, _d: calls.__setitem__("stream", calls["stream"] + 1))

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "moved", "concert.mp4")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert calls["replace"] == 1
    assert calls["stream"] == 0
    dest = tmp_path / "moved" / "concert.mp4"
    assert dest.exists()
    with dest.open("rb") as fh:
        assert fh.read() == content
    assert not orig.exists()


async def test_cross_filesystem_move_streams_with_bounded_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-fs move streams in bounded chunks and never slurps the whole file into RAM.

    We force the fallback branch and make ``Path.read_bytes`` fail loudly: if the
    move path ever reads the whole file into memory the test aborts.
    """
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()
    orig = tmp_path / "orig" / "huge.mkv"
    orig.parent.mkdir(parents=True, exist_ok=True)
    content = b"z" * (5 * 1024 * 1024)
    orig.write_bytes(content)

    from pathlib import Path as _Path

    monkeypatch.setattr(execmod, "_same_filesystem", lambda _s, _d: False)

    def _forbid_read_bytes(_self: object) -> bytes:
        msg = "read_bytes() slurps the whole file -- the streamed move must not call it"
        raise AssertionError(msg)

    monkeypatch.setattr(_Path, "read_bytes", _forbid_read_bytes)

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "out", "huge.mkv")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    dest = tmp_path / "out" / "huge.mkv"
    assert dest.exists()
    with dest.open("rb") as fh:
        assert fh.read() == content
    # Cross-filesystem copy leaves the original in place, then unlinks it.
    assert not orig.exists()


async def test_move_refuses_to_clobber_existing_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A destination that already exists is never overwritten: the proposal fails, both files survive (phaze-yu2e)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    orig = tmp_path / "orig" / "set.mp3"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(b"ORIGINAL-BYTES")

    dest = tmp_path / "moved" / "Artist - Track.mp3"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"PREEXISTING-DO-NOT-DESTROY")

    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "moved", "Artist - Track.mp3")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed_with_errors"
    assert result["error_count"] == 1
    # Neither the source nor the pre-existing destination was destroyed.
    assert orig.read_bytes() == b"ORIGINAL-BYTES"
    assert dest.read_bytes() == b"PREEXISTING-DO-NOT-DESTROY"
    # Reported as a failure at the 'copy' step.
    assert api.patch_proposal_state.await_args.args[1].proposal_state == "failed"
    assert api.patch_execution_log.await_args.args[1].error_message.startswith("copy:")


async def test_in_place_rename_to_same_file_is_not_a_clobber(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename whose destination resolves to the file itself is a no-op, not a refused clobber (phaze-yu2e)."""
    _patch_settings(monkeypatch, [str(tmp_path)])
    api = _make_api_client_mock()

    orig = tmp_path / "set.mp3"
    orig.write_bytes(b"KEEP-ME")

    # Empty proposed_path == rename in place; same filename -> destination IS the original file.
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id="a", proposals=[_item(orig, "", "set.mp3")])
    result = await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))

    assert result["status"] == "completed"
    assert result["error_count"] == 0
    assert orig.read_bytes() == b"KEEP-ME"


def test_is_same_file_true_for_same_path(tmp_path: Path) -> None:
    from phaze.tasks.execution import _is_same_file

    f = tmp_path / "x.mp3"
    f.write_bytes(b"data")
    assert _is_same_file(f, f) is True


def test_is_same_file_false_for_distinct_files(tmp_path: Path) -> None:
    from phaze.tasks.execution import _is_same_file

    a = tmp_path / "a"
    a.write_bytes(b"1")
    b = tmp_path / "b"
    b.write_bytes(b"2")
    assert _is_same_file(a, b) is False


def test_is_same_file_false_when_destination_missing(tmp_path: Path) -> None:
    from phaze.tasks.execution import _is_same_file

    a = tmp_path / "a"
    a.write_bytes(b"1")
    assert _is_same_file(a, tmp_path / "nope") is False


def test_same_filesystem_helper_true_within_one_tree(tmp_path: Path) -> None:
    """Two paths under the same tmp dir share a filesystem."""
    a = tmp_path / "a"
    a.write_bytes(b"x")
    assert _same_filesystem(a, tmp_path) is True


def test_streamed_copy_preserves_content_and_mtime(tmp_path: Path) -> None:
    """The streamed copy reproduces bytes exactly and preserves mtime (copystat)."""
    src = tmp_path / "src.bin"
    payload = b"q" * (2 * 1024 * 1024 + 7)  # non-chunk-aligned size
    src.write_bytes(payload)
    import os as _os

    _os.utime(src, (1_600_000_000, 1_600_000_000))
    dst = tmp_path / "nested" / "dst.bin"
    dst.parent.mkdir(parents=True, exist_ok=True)

    _streamed_copy(src, dst)

    with dst.open("rb") as fh:
        assert fh.read() == payload
    assert dst.stat().st_mtime == src.stat().st_mtime
