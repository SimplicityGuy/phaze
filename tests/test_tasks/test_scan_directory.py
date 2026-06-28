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
from typing import TYPE_CHECKING, Any
import unicodedata
from unittest.mock import AsyncMock, MagicMock
import uuid

from pydantic import ValidationError
import pytest


if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """WR-04: pin PHAZE_ROLE=agent and clear get_settings()'s lru_cache.

    ``_resolve_chunk_size`` reads ``get_settings()`` (an ``@lru_cache(maxsize=1)``
    function). Under ``PHAZE_ROLE=control`` (the default in most test envs) the
    helper falls through to ``_DEFAULT_SCAN_CHUNK_SIZE`` -- which means the
    production ``AgentSettings.scan_chunk_size`` code path was never exercised
    by chunking tests. Worse: an earlier test that set ``PHAZE_ROLE=agent`` via
    monkeypatch could leave the lru_cache holding an AgentSettings instance
    even after monkeypatch teardown, so test ordering silently shifted which
    branch ran. Force the agent branch + clear the cache so every test gets a
    fresh AgentSettings populated from the test env.
    """
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test:8000")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-TOKEN-1234567890ab")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/tmp")  # noqa: S108 -- validator only checks non-empty list

    from phaze.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


def test_resolve_chunk_size_falls_back_when_not_agent_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coverage gap fill (Codecov PR #59): scan.py:82.

    When get_settings() returns ControlSettings (PHAZE_ROLE=control) rather than
    AgentSettings, _resolve_chunk_size MUST return the documented default of
    500 rather than crashing trying to read .scan_chunk_size off a control
    settings object. This branch is defensive: scan_directory is registered
    only on the agent worker today, but module-level imports across both
    roles are still possible (test_task_split.py runs under PHAZE_ROLE=control,
    for example).
    """
    from phaze.config import ControlSettings
    from phaze.tasks import scan as scan_mod

    monkeypatch.setenv("PHAZE_ROLE", "control")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://app.test:8000")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-control")
    fake_cfg = ControlSettings()
    monkeypatch.setattr(scan_mod, "get_settings", lambda: fake_cfg)

    assert scan_mod._resolve_chunk_size() == scan_mod._DEFAULT_SCAN_CHUNK_SIZE


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


async def test_scan_directory_honors_agent_settings_chunk_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-04 regression: PHAZE_SCAN_CHUNK_SIZE override is read from AgentSettings.

    Previously the chunking tests ran under PHAZE_ROLE=control and exercised
    ``_DEFAULT_SCAN_CHUNK_SIZE = 500`` -- the production code path that reads
    ``AgentSettings.scan_chunk_size`` from ``PHAZE_SCAN_CHUNK_SIZE`` was never
    tested. Override the env var to 3 and assert the chunks split at 3, not 500.
    """
    from phaze.config import get_settings
    from phaze.tasks.scan import scan_directory

    monkeypatch.setenv("PHAZE_SCAN_CHUNK_SIZE", "3")
    # Invalidate the lru_cache so the override is observed by _resolve_chunk_size.
    get_settings.cache_clear()

    # Seven files -> three chunks of 3, 3, 1.
    for i in range(7):
        _touch(tmp_path / f"f{i:04d}.mp3")

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    assert ctx["api_client"].upsert_files.await_count == 3
    sizes = [len(call.args[0].files) for call in ctx["api_client"].upsert_files.await_args_list]
    assert sizes == [3, 3, 1]


async def test_scan_directory_patches_progress_after_each_chunk(tmp_path: Path) -> None:
    """1500 files -> per-chunk PATCH calls with monotonic processed_files (500, 1000, 1500), plus terminal PATCH."""
    from phaze.tasks.scan import scan_directory

    for i in range(1500):
        _touch(tmp_path / f"f{i:04d}.mp3")

    ctx = _make_ctx()
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    # One up-front PATCH(total_files=precount); then each of 3 chunks triggers one
    # PATCH(processed_files=N); then terminal PATCH(status='completed').
    patch_calls = ctx["api_client"].patch_scan_batch.await_args_list
    assert len(patch_calls) == 5  # 1 precount + 3 per-chunk + 1 terminal

    # The first PATCH is the pre-count denominator (total_files set, processed_files unset).
    first_body = patch_calls[0].args[1]
    assert first_body.total_files == 1500
    assert first_body.processed_files is None

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


async def test_scan_directory_patches_total_files_precount_before_first_upsert(tmp_path: Path) -> None:
    """Pre-count denominator: a PATCH(total_files=N) is sent BEFORE the first upsert_files.

    The Recent Scans "processed / total" widget needs a real denominator during a
    RUNNING scan. The pre-count walk (no stat, no hashing) populates total_files up
    front; this test pins both the value (count of ingestible files) and the ordering
    (it lands before any file persistence handshake).
    """
    from phaze.tasks.scan import scan_directory

    for i in range(3):
        _touch(tmp_path / f"f{i:04d}.mp3")
    _touch(tmp_path / "ignored.txt")  # COMPANION -- excluded from the precount

    manager = MagicMock()
    api = AsyncMock()
    api.upsert_files = AsyncMock(return_value=MagicMock(upserted=0, inserted=0, enqueued=0))
    api.patch_scan_batch = AsyncMock(return_value=MagicMock())
    manager.attach_mock(api.upsert_files, "upsert_files")
    manager.attach_mock(api.patch_scan_batch, "patch_scan_batch")

    ctx = _make_ctx(api_client=api)
    await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    # The first PATCH carries the pre-count: 3 ingestible files, txt excluded.
    first_patch = api.patch_scan_batch.await_args_list[0].args[1]
    assert first_patch.total_files == 3
    assert first_patch.processed_files is None

    # Ordering: the first recorded call is a patch_scan_batch, and it precedes the
    # first upsert_files call.
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names[0] == "patch_scan_batch"
    assert call_names.index("patch_scan_batch") < call_names.index("upsert_files")


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


# ---------------------------------------------------------------------------
# Coverage gap fills (Codecov PR #59): scan.py:212-225 — D-12 controller-down path
# ---------------------------------------------------------------------------


async def test_scan_directory_aborts_with_failed_patch_on_server_error(tmp_path: Path) -> None:
    """5xx after retries (D-12) aborts the walk and surfaces a `failed` terminal PATCH
    with the controller error message (scan.py:212-222).

    The outer SAQ retry policy handles the broader recovery; this test pins the
    in-task abort contract: walk stops, a final patch_scan_batch(status='failed')
    is attempted, and the return shape is `{"status": "failed", "reason":
    "controller_5xx"}`.
    """
    from phaze.services.agent_client import AgentApiServerError
    from phaze.tasks.scan import scan_directory

    _touch(tmp_path / "a.mp3")
    _touch(tmp_path / "b.flac")

    api = AsyncMock()
    api.upsert_files = AsyncMock(side_effect=AgentApiServerError("POST /files -> 503 after retries"))
    # patch_scan_batch must SUCCEED on the terminal-failed call so we cover the
    # outer except + inner try/success path (not the inner except).
    api.patch_scan_batch = AsyncMock(return_value=MagicMock())
    ctx = _make_ctx(api_client=api)

    payload = _make_payload_kwargs(str(tmp_path))
    result = await scan_directory(ctx, **payload)

    assert result["status"] == "failed"
    assert result["reason"] == "controller_5xx"
    # `files_posted` tracks files scanned-into-batch, not files persisted server-side
    # (the 5xx interrupts the persistence handshake but the local counter already
    # advanced). We staged 2 files; the failure surfaces both.
    assert result["files_posted"] == 2

    # Two PATCHes total: the up-front pre-count denominator (total_files), then the
    # terminal failed-PATCH carrying the controller-error message. The last call is the
    # failed one.
    assert api.patch_scan_batch.await_count == 2
    precount_patch = api.patch_scan_batch.await_args_list[0].args[1]
    assert precount_patch.total_files == 2
    assert precount_patch.status is None
    failed_patch = api.patch_scan_batch.await_args.args[1]
    assert failed_patch.status == "failed"
    assert failed_patch.error_message is not None
    assert "Controller error" in failed_patch.error_message


async def test_scan_directory_terminal_failed_patch_also_fails(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """If the terminal failed-PATCH ALSO raises AgentApiServerError, scan_directory
    still returns the documented failure envelope (scan.py:223-225) and does NOT
    re-raise. The outer SAQ retry policy is the next line of defense.
    """
    import logging as _logging

    from phaze.services.agent_client import AgentApiServerError
    from phaze.tasks.scan import scan_directory

    _touch(tmp_path / "a.mp3")

    api = AsyncMock()
    api.upsert_files = AsyncMock(side_effect=AgentApiServerError("POST /files -> 503"))
    api.patch_scan_batch = AsyncMock(side_effect=AgentApiServerError("PATCH /scan-batches -> 503"))
    ctx = _make_ctx(api_client=api)

    payload = _make_payload_kwargs(str(tmp_path))
    with caplog.at_level(_logging.ERROR, logger="phaze.tasks.scan"):
        result = await scan_directory(ctx, **payload)

    # `files_posted` reflects the local scan counter at the moment of failure
    # (one .mp3 file was staged before upsert_files raised); the documented
    # return-shape contract is the three keys, not the counter value.
    assert result == {"status": "failed", "files_posted": 1, "reason": "controller_5xx"}
    # The "terminal failed-PATCH also failed" message MUST surface so operators
    # see both failures in the log when triaging.
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "terminal failed-PATCH also failed" in text, f"missing inner-except log: {text!r}"


# ---------------------------------------------------------------------------
# Incident 260608: zero-access / partial-access walks (scan.py onerror handler)
# ---------------------------------------------------------------------------


async def test_scan_directory_root_unreadable_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Walk root raises PermissionError (onerror) AND total==0 -> terminal failed PATCH.

    This is the exact incident failure mode: the agent container user could not
    read the media tree, os.walk swallowed the PermissionError, and the scan
    reported completed/0-files -- indistinguishable from a genuinely empty dir.
    The onerror handler must surface this as status='failed' with a
    permission-pointing message and reason='walk_permission_errors'.
    """
    from phaze.tasks import scan as scan_module
    from phaze.tasks.scan import scan_directory

    scan_path = str(tmp_path)

    def fake_walk(path: object, followlinks: bool = False, onerror: object = None) -> object:
        exc = PermissionError(f"[Errno 13] Permission denied: '{scan_path}'")
        exc.filename = scan_path
        if onerror is not None:
            onerror(exc)  # type: ignore[operator]
        return
        yield  # pragma: no cover -- makes fake_walk a generator that yields nothing

    monkeypatch.setattr(scan_module.os, "walk", fake_walk)

    ctx = _make_ctx()
    result = await scan_directory(ctx, **_make_payload_kwargs(scan_path))

    assert result == {"status": "failed", "files_posted": 0, "reason": "walk_permission_errors"}
    # No files were ever posted.
    assert ctx["api_client"].upsert_files.await_count == 0
    # The terminal PATCH carried status='failed' with a permission-pointing message.
    body = ctx["api_client"].patch_scan_batch.await_args.args[1]
    assert body.status == "failed"
    assert body.error_message is not None
    assert scan_path in body.error_message
    assert "ownership" in body.error_message.lower() or "permission" in body.error_message.lower()


async def test_scan_directory_partial_access_still_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Some subdirs error via onerror but >=1 file found -> completed + single warning.

    A partial-access scan (one unreadable subdir, but readable files elsewhere)
    must still complete successfully and log exactly one summarizing warning so
    the operator knows directories were skipped without flooding the log.
    """
    import logging as _logging

    from phaze.tasks import scan as scan_module
    from phaze.tasks.scan import scan_directory

    _touch(tmp_path / "good.mp3")

    def fake_walk(path: object, followlinks: bool = False, onerror: object = None) -> object:
        exc = PermissionError("[Errno 13] Permission denied: '/blocked/subdir'")
        exc.filename = "/blocked/subdir"
        if onerror is not None:
            onerror(exc)  # type: ignore[operator]
        yield (str(tmp_path), [], ["good.mp3"])

    monkeypatch.setattr(scan_module.os, "walk", fake_walk)

    ctx = _make_ctx()
    with caplog.at_level(_logging.WARNING, logger="phaze.tasks.scan"):
        result = await scan_directory(ctx, **_make_payload_kwargs(str(tmp_path)))

    assert result["status"] == "completed"
    assert result["files_posted"] == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "partial access" in text, f"missing partial-access warning: {text!r}"


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

    # Entries are either a bare function (name == __name__) or a (name, func) tuple registered
    # under an explicit SAQ name (e.g. ("s3_upload", upload_file_s3), Phase 53).
    func_names = {(f[0] if isinstance(f, tuple) else f.__name__) for f in agent_settings["functions"]}
    assert "scan_directory" in func_names, f"scan_directory not registered: got {func_names}"
