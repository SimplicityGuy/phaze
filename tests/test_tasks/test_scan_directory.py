"""Tests for the scan_directory SAQ task (Phase 27 Plan 04, D-11..D-13).

Covers:
- Extension filter (only MUSIC + VIDEO; UNKNOWN and COMPANION dropped -- matches
  watcher's _EXTRACTABLE so manual-scan ingestion population == watcher ingestion population).
- Exact chunking at AgentSettings.scan_chunk_size (default 500).
- Per-chunk PATCH(processed_files=...) calls with monotonic counts.
- Terminal PATCH(status='completed', total_files=N, processed_files=N).
- Terminal PATCH(status='failed', error_message=...) when scan_path is missing.
- Mid-walk OSError per file -> warning + continue (mirrors services/ingestion.py:65).
- NFC normalization on original_path / original_filename / current_path (Pitfall 3).
- agent_id and id fields are NEVER stamped by the agent (AUTH-01 invariant).
- scan_directory registered in agent_worker.settings.functions (Task 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import unicodedata
from unittest.mock import AsyncMock, MagicMock
import uuid

from pydantic import ValidationError
import pytest


def _make_ctx(api_client: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with api_client mock.

    upsert_files and patch_scan_batch are AsyncMocks that record every call.
    """
    if api_client is None:
        api_client = AsyncMock()
        api_client.upsert_files = AsyncMock(return_value=MagicMock(upserted=0, inserted=0, enqueued=0))
        api_client.patch_scan_batch = AsyncMock(return_value=MagicMock())
    return {"api_client": api_client}


def _make_payload_kwargs(scan_path: str, batch_id: uuid.UUID | None = None, agent_id: str = "test-agent") -> dict[str, Any]:
    return {
        "scan_path": scan_path,
        "batch_id": str(batch_id or uuid.uuid4()),
        "agent_id": agent_id,
    }


def _touch(p: Path, size: int = 1) -> None:
    """Create an empty/sized file. Default 1-byte to make file_size deterministic."""
    p.write_bytes(b"x" * size)


async def test_scan_directory_walks_known_extensions(tmp_path: Path) -> None:
    """Only MUSIC + VIDEO categories are posted; UNKNOWN and COMPANION extensions dropped."""
    from phaze.tasks.scan import scan_directory

    _touch(tmp_path / "a.mp3")
    _touch(tmp_path / "b.flac")
    _touch(tmp_path / "c.unknownext")  # UNKNOWN -- must be filtered
    _touch(tmp_path / "d.mp4")

    ctx = _make_ctx()
    result = await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    assert result["status"] == "completed"
    assert result["files_posted"] == 3

    # Single chunk because 3 < default 500.
    assert ctx["api_client"].upsert_files.await_count == 1
    chunk = ctx["api_client"].upsert_files.await_args.args[0]
    file_types = {r.file_type for r in chunk.files}
    assert file_types == {"mp3", "flac", "mp4"}


def test_scan_directory_extractable_set_is_music_and_video_only() -> None:
    """CR-01 regression: _EXTRACTABLE must be exactly {MUSIC, VIDEO}.

    Asserting the explicit set (not just behaviour) pins down the chosen filter so
    that future schema widening of FileCategory (e.g., a new SUBTITLE category)
    cannot silently change scan_directory's ingestion population without breaking
    this test. The watcher uses the same frozenset (see
    ``agent_watcher/observer.py``) and the auto-enqueue gate uses the same one
    (``routers/agent_files.py``); the three sets MUST stay in lockstep.
    """
    from phaze.constants import FileCategory
    from phaze.tasks.scan import _EXTRACTABLE

    assert frozenset({FileCategory.MUSIC, FileCategory.VIDEO}) == _EXTRACTABLE


async def test_scan_directory_drops_companion_files(tmp_path: Path) -> None:
    """CR-01 regression: COMPANION extensions (.cue/.nfo/.txt/.jpg/.png/.m3u/...) are NOT posted.

    The watcher drops these (see ``agent_watcher/observer.py``); scan_directory
    must match so the LIVE-sentinel batch row population is identical to what
    the operator-triggered scan produces. Otherwise a manual scan would insert
    FileRecord rows for companion siblings that the watcher never discovers,
    creating divergent ingestion sets between the two paths.
    """
    from phaze.tasks.scan import scan_directory

    # One file from each COMPANION extension surveyed by EXTENSION_MAP. Plus a
    # MUSIC file so the walk produces at least one upsert chunk to inspect.
    _touch(tmp_path / "cover.jpg")
    _touch(tmp_path / "art.jpeg")
    _touch(tmp_path / "art.png")
    _touch(tmp_path / "art.gif")
    _touch(tmp_path / "playlist.m3u")
    _touch(tmp_path / "playlist.m3u8")
    _touch(tmp_path / "playlist.pls")
    _touch(tmp_path / "info.nfo")
    _touch(tmp_path / "notes.txt")
    _touch(tmp_path / "tracks.cue")
    _touch(tmp_path / "checksum.sfv")
    _touch(tmp_path / "checksum.md5")
    _touch(tmp_path / "song.mp3")

    ctx = _make_ctx()
    result = await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    assert result["status"] == "completed"
    # Only song.mp3 survives the MUSIC+VIDEO filter.
    assert result["files_posted"] == 1
    chunk = ctx["api_client"].upsert_files.await_args.args[0]
    assert len(chunk.files) == 1
    assert chunk.files[0].original_filename == "song.mp3"


async def test_scan_directory_chunks_at_500(tmp_path: Path) -> None:
    """1001 known-ext files -> exactly 3 chunks: 500, 500, 1."""
    from phaze.tasks.scan import scan_directory

    for i in range(1001):
        _touch(tmp_path / f"f{i:04d}.mp3")

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    assert ctx["api_client"].upsert_files.await_count == 3
    sizes = [len(call.args[0].files) for call in ctx["api_client"].upsert_files.await_args_list]
    assert sizes == [500, 500, 1]


async def test_scan_directory_patches_progress_after_each_chunk(tmp_path: Path) -> None:
    """1500 files -> per-chunk PATCH calls with monotonic processed_files (500, 1000, 1500), plus terminal PATCH."""
    from phaze.tasks.scan import scan_directory

    for i in range(1500):
        _touch(tmp_path / f"f{i:04d}.mp3")

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    # Each of 3 chunks triggers one PATCH(processed_files=N); then terminal PATCH(status='completed').
    patch_calls = ctx["api_client"].patch_scan_batch.await_args_list
    assert len(patch_calls) == 4  # 3 per-chunk + 1 terminal

    processed_seq = []
    for call in patch_calls:
        body = call.args[1]
        if body.processed_files is not None:
            processed_seq.append(body.processed_files)
    # The first three PATCHes should be 500, 1000, 1500 (per-chunk).
    # The terminal PATCH carries processed_files=1500 too.
    assert processed_seq[:3] == [500, 1000, 1500]
    # Monotonic non-decreasing across ALL patches.
    assert processed_seq == sorted(processed_seq)


async def test_scan_directory_patches_final_status_completed(tmp_path: Path) -> None:
    """Clean walk -> final PATCH carries status='completed' + total_files=N + processed_files=N."""
    from phaze.tasks.scan import scan_directory

    _touch(tmp_path / "a.mp3")
    _touch(tmp_path / "b.flac")

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    final_call = ctx["api_client"].patch_scan_batch.await_args_list[-1]
    body = final_call.args[1]
    assert body.status == "completed"
    assert body.total_files == 2
    assert body.processed_files == 2


async def test_scan_directory_patches_final_status_failed_on_missing_path(tmp_path: Path) -> None:
    """Non-existent scan_path -> PATCH(status='failed', error_message=...) and ZERO upsert_files calls."""
    from phaze.tasks.scan import scan_directory

    missing = tmp_path / "does-not-exist"

    ctx = _make_ctx()
    result = await scan_directory(ctx, **_make_payload_kwargs(str(missing)))

    assert result["status"] == "failed"
    assert ctx["api_client"].upsert_files.await_count == 0
    assert ctx["api_client"].patch_scan_batch.await_count == 1
    body = ctx["api_client"].patch_scan_batch.await_args.args[1]
    assert body.status == "failed"
    assert body.error_message is not None
    assert "does not exist" in body.error_message


async def test_scan_directory_skips_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError on one file -> warning + continue; walk completes with files-minus-one count."""
    from phaze.services import hashing as hashing_module
    from phaze.tasks import scan as scan_module
    from phaze.tasks.scan import scan_directory

    _touch(tmp_path / "good1.mp3")
    bad = tmp_path / "bad.mp3"
    _touch(bad)
    _touch(tmp_path / "good2.mp3")

    real_compute = hashing_module.compute_sha256

    def fake_compute(path: Path) -> str:
        if path.name == "bad.mp3":
            raise OSError("simulated unreadable file")
        return real_compute(path)

    monkeypatch.setattr(scan_module, "compute_sha256", fake_compute)

    ctx = _make_ctx()
    result = await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    assert result["status"] == "completed"
    assert result["files_posted"] == 2  # bad.mp3 skipped


async def test_scan_directory_nfc_normalizes_paths(tmp_path: Path) -> None:
    """File with NFD-form combining character in name -> posted record paths are NFC-normalized (Pitfall 3)."""
    from phaze.tasks.scan import scan_directory

    # NFD ("eu0301") vs NFC ("é") -- create the file with the NFD form on disk if filesystem
    # supports it; assert the posted record is NFC-normalized regardless of disk form.
    nfd_filename = "café.mp3"  # "café" in NFD form
    nfc_filename = unicodedata.normalize("NFC", nfd_filename)
    assert nfd_filename != nfc_filename  # sanity: the two byte sequences differ

    # Write the file. On macOS/HFS+ the filesystem may auto-normalize to NFD; that's fine
    # for this test -- we assert the posted strings are NFC regardless of what's on disk.
    (tmp_path / nfd_filename).write_bytes(b"x")

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    chunk = ctx["api_client"].upsert_files.await_args.args[0]
    assert len(chunk.files) == 1
    record = chunk.files[0]
    assert unicodedata.is_normalized("NFC", record.original_path)
    assert unicodedata.is_normalized("NFC", record.original_filename)
    assert unicodedata.is_normalized("NFC", record.current_path)


async def test_scan_directory_omits_agent_id_and_id_from_record_dict(tmp_path: Path) -> None:
    """Posted FileUpsertRecord must NOT carry agent_id or id fields (AUTH-01 invariant).

    The schema has extra='forbid' so these would 422 anyway; this test proves the
    agent record-building path doesn't even try to stamp them.
    """
    from phaze.tasks.scan import scan_directory

    _touch(tmp_path / "a.mp3")

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    chunk = ctx["api_client"].upsert_files.await_args.args[0]
    record_dict = chunk.files[0].model_dump()
    assert "agent_id" not in record_dict
    assert "id" not in record_dict


async def test_scan_directory_chunk_carries_batch_id(tmp_path: Path) -> None:
    """Each upsert_files call posts a FileUpsertChunk whose batch_id equals the payload batch_id (D-09)."""
    from phaze.tasks.scan import scan_directory

    _touch(tmp_path / "a.mp3")
    bid = uuid.uuid4()

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path), batch_id=bid))

    chunk = ctx["api_client"].upsert_files.await_args.args[0]
    assert chunk.batch_id == bid


async def test_scan_directory_does_not_follow_symlinks(tmp_path: Path) -> None:
    """os.walk(followlinks=False) -- a symlinked directory is NOT traversed (Pitfall 4 mitigation)."""
    from phaze.tasks.scan import scan_directory

    real_root = tmp_path / "real"
    real_root.mkdir()
    _touch(real_root / "inside.mp3")

    # external_target lives OUTSIDE tmp_path's scanned tree and contains a file
    # we should NOT pick up if the symlink is correctly skipped.
    external_target = tmp_path / "external_target"
    external_target.mkdir()
    _touch(external_target / "should_not_appear.mp3")

    # Symlink inside real_root that points at the external target.
    (real_root / "linked").symlink_to(external_target, target_is_directory=True)

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(real_root)))

    chunk = ctx["api_client"].upsert_files.await_args.args[0]
    filenames = {Path(r.original_path).name for r in chunk.files}
    assert "inside.mp3" in filenames
    assert "should_not_appear.mp3" not in filenames


async def test_scan_directory_rejects_extra_kwargs(tmp_path: Path) -> None:
    """ScanDirectoryPayload has extra='forbid' -- unknown kwargs raise ValidationError."""
    from phaze.tasks.scan import scan_directory

    bad = _make_payload_kwargs(str(tmp_path))
    bad["bogus"] = "x"

    ctx = _make_ctx()
    with pytest.raises(ValidationError):
        await scan_directory(ctx, **bad)
    # No HTTP traffic happens when validation fails.
    assert ctx["api_client"].upsert_files.await_count == 0
    assert ctx["api_client"].patch_scan_batch.await_count == 0


def test_scan_directory_registered_in_agent_worker_settings() -> None:
    """Task 2: scan_directory must be reachable via SAQ task-name resolution on agent_worker.

    The settings dict is built at module-import time; the function-list registration is
    what AgentTaskRouter.enqueue_for_agent(task_name="scan_directory", ...) resolves by name.
    Subprocess-isolated import is NOT required here -- the function-object identity check
    is enough; the import-boundary invariant (no phaze.database / phaze.models / sqlalchemy)
    is enforced separately by tests/test_task_split.py.

    This test needs the same env that tests/test_task_split.py uses for the subprocess
    invocation, because phaze.tasks.agent_worker raises at module-import time if
    PHAZE_AGENT_QUEUE is unset (Phase 26 D-16 module-import-time guard).
    """
    import os

    os.environ.setdefault("PHAZE_ROLE", "agent")
    os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
    os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    os.environ.setdefault("PHAZE_AGENT_QUEUE", "phaze-agent-test-agent")
    os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")  # noqa: S108  # validator only checks non-empty list

    from phaze.tasks.agent_worker import settings as agent_settings

    func_names = {f.__name__ for f in agent_settings["functions"]}
    assert "scan_directory" in func_names, f"scan_directory not registered: got {func_names}"
