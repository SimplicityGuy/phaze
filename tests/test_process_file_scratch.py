"""Tests for the process_file ephemeral-scratch read-path (CLOUDPIPE-03 / -04, Plan 50-04).

The compute agent reads a pushed file from a scratch dir, sha256-verifies it OFF the event
loop before analysis, and removes the scratch copy in a ``finally`` on every exit path. The
producer (:func:`enqueue_process_file`) can pin the scratch path + expected sha256 for a cloud
file while leaving the bulk local path byte-identical.

Owned by Plan 50-04 (disjoint from ``tests/test_push_pipeline.py`` so the two Wave-2 plans
never write the same file in parallel).

Selectors:
  * ``-k sha256``   → off-loop verify: match analyzes the scratch copy; mismatch deletes it,
                      reports a clean push-mismatch, and does NOT analyze (re-pushable).
  * ``-k cleanup``  → scratch deleted in a ``finally`` on success AND every terminal failure.
  * ``-k enqueue``  → producer threads expected_sha256/scratch_path; omitted → both None.
  * ``-k scratch``  → byte-identical local path + scratch-aware payload threading.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pebble import ProcessExpired
import pytest

from phaze.schemas.agent_tasks import ProcessFilePayload
from phaze.services.analysis_enqueue import enqueue_process_file, process_file_job_key
from phaze.services.hashing import compute_sha256
from phaze.tasks.functions import process_file
from tests._queue_fakes import FakeQueue


if TYPE_CHECKING:
    from pathlib import Path


# Minimal analyze_file return matching analysis.py's contract (aggregates only; no windows key).
MOCK_ANALYSIS: dict[str, Any] = {
    "bpm": 128.0,
    "musical_key": "C minor",
    "features": {},
}


@pytest.fixture(autouse=True)
def _patch_agent_settings() -> Any:
    """Supply AgentSettings-shaped config so ``process_file`` clears its agent-role guard."""
    from phaze.config import AgentSettings

    stub = MagicMock(spec=AgentSettings)
    stub.analysis_inner_timeout_sec = 6600
    stub.analysis_fine_cap = 60
    stub.analysis_coarse_cap = 30
    with patch("phaze.tasks.functions.get_settings", return_value=stub) as m:
        yield m


def _ctx(api: AsyncMock, job: Any | None = None) -> dict[str, Any]:
    ctx: dict[str, Any] = {"process_pool": MagicMock(), "api_client": api}
    if job is not None:
        ctx["job"] = job
    return ctx


def _api() -> AsyncMock:
    api = AsyncMock()
    api.put_analysis = AsyncMock(return_value=MagicMock())
    api.report_analysis_failed = AsyncMock(return_value=MagicMock())
    api.report_push_mismatch = AsyncMock(return_value=MagicMock())
    return api


def _kwargs(
    *,
    file_id: uuid.UUID | None = None,
    original_path: str = "/music/track.mp3",
    scratch_path: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": original_path,
        "file_type": "mp3",
        "agent_id": "test-agent",
        "models_path": "/models",
    }
    if scratch_path is not None:
        kw["scratch_path"] = scratch_path
    if expected_sha256 is not None:
        kw["expected_sha256"] = expected_sha256
    return kw


def _write_scratch(tmp_path: Path, content: bytes = b"pushed-audio-bytes") -> Path:
    scratch = tmp_path / "deadbeef-cafe.mp3"
    scratch.write_bytes(content)
    return scratch


# ---------------------------------------------------------------------------
# CLOUDPIPE-03 -- off-event-loop sha256 verify before analysis
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_sha256_match_analyzes_the_scratch_copy(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """scratch_path + matching expected_sha256 → analyze the SCRATCH path, then clean it up."""
    scratch = _write_scratch(tmp_path)
    good = compute_sha256(scratch)
    mock_pool.return_value = MOCK_ANALYSIS
    api = _api()

    result = await process_file(
        _ctx(api),
        **_kwargs(scratch_path=str(scratch), expected_sha256=good),
    )

    assert result["status"] == "analyzed"
    mock_pool.assert_awaited_once()
    # read_path is the 3rd positional arg to run_in_process_pool -> the scratch copy, NOT original_path.
    assert mock_pool.await_args.args[2] == str(scratch)
    api.put_analysis.assert_awaited_once()
    api.report_push_mismatch.assert_not_awaited()
    # finally cleanup on the success path.
    assert not scratch.exists()


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_sha256_mismatch_deletes_scratch_and_reports(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """CLOUDPIPE-03: a sha256 mismatch deletes the scratch copy, reports a clean push-mismatch,
    runs NO analysis, and returns a push_mismatch result (re-pushable, no partial persisted)."""
    scratch = _write_scratch(tmp_path)
    file_id = uuid.uuid4()
    api = _api()

    result = await process_file(
        _ctx(api),
        **_kwargs(file_id=file_id, scratch_path=str(scratch), expected_sha256="0" * 64),
    )

    assert result == {"file_id": str(file_id), "status": "push_mismatch"}
    mock_pool.assert_not_awaited()  # corrupt transfer is NEVER analyzed (T-50-corrupt)
    api.put_analysis.assert_not_awaited()
    api.report_push_mismatch.assert_awaited_once_with(file_id)
    assert not scratch.exists()  # deleted (T-50-scratch-dos)


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_sha256_verify_skipped_without_expected_hash(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """scratch_path set but expected_sha256 None → no verify gate; analyze the scratch copy anyway."""
    scratch = _write_scratch(tmp_path)
    mock_pool.return_value = MOCK_ANALYSIS
    api = _api()

    result = await process_file(_ctx(api), **_kwargs(scratch_path=str(scratch)))

    assert result["status"] == "analyzed"
    assert mock_pool.await_args.args[2] == str(scratch)
    api.report_push_mismatch.assert_not_awaited()
    assert not scratch.exists()


# ---------------------------------------------------------------------------
# CLOUDPIPE-04 -- finally cleanup on EVERY exit path
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_scratch_cleanup_finally_on_all_exit_paths(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """CLOUDPIPE-04: the scratch copy is removed in a ``finally`` on the SUCCESS path."""
    scratch = _write_scratch(tmp_path)
    mock_pool.return_value = MOCK_ANALYSIS
    api = _api()

    result = await process_file(_ctx(api), **_kwargs(scratch_path=str(scratch)))

    assert result["status"] == "analyzed"
    assert not scratch.exists()


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_scratch_cleanup_on_timeout(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """Terminal TimeoutError → scratch still deleted by the ``finally`` (no DoS)."""
    scratch = _write_scratch(tmp_path)
    mock_pool.side_effect = TimeoutError("inner pebble timeout")
    api = _api()

    result = await process_file(_ctx(api), **_kwargs(scratch_path=str(scratch)))

    assert result["status"] == "analysis_failed"
    api.report_analysis_failed.assert_awaited_once()
    assert not scratch.exists()


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_scratch_cleanup_on_process_expired(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """Terminal ProcessExpired (essentia crash) → scratch still deleted by the ``finally``."""
    scratch = _write_scratch(tmp_path)
    mock_pool.side_effect = ProcessExpired("child died", code=1)
    api = _api()

    result = await process_file(_ctx(api), **_kwargs(scratch_path=str(scratch)))

    assert result["status"] == "analysis_failed"
    assert not scratch.exists()


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_scratch_cleanup_on_generic_error(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """A re-raised generic error still runs the ``finally`` cleanup before propagating."""
    scratch = _write_scratch(tmp_path)
    mock_pool.side_effect = RuntimeError("boom")
    api = _api()
    ctx = _ctx(api, job=MagicMock(retryable=False))

    with pytest.raises(RuntimeError, match="boom"):
        await process_file(ctx, **_kwargs(scratch_path=str(scratch)))

    api.report_analysis_failed.assert_awaited_once()
    assert not scratch.exists()


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_scratch_cleanup_on_mismatch_return(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """The mismatch early-return path also leaves no scratch file behind."""
    scratch = _write_scratch(tmp_path)
    api = _api()

    await process_file(_ctx(api), **_kwargs(scratch_path=str(scratch), expected_sha256="f" * 64))

    assert not scratch.exists()


# ---------------------------------------------------------------------------
# CR-01 -- a RETRYABLE failure must KEEP the scratch copy for the in-place SAQ retry
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_scratch_survives_retryable_failure(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """CR-01: a retryable analysis error must NOT delete the scratch copy.

    SAQ retries process_file IN PLACE (it does not re-run push_file), so the pushed copy has to
    survive for the retry to re-verify + analyze. The prior blanket ``finally`` unlink stranded the
    file in PUSHED forever and permanently consumed a bounded cloud-window slot.
    """
    scratch = _write_scratch(tmp_path)
    good = compute_sha256(scratch)
    mock_pool.side_effect = RuntimeError("transient pool error")
    api = _api()
    ctx = _ctx(api, job=MagicMock(retryable=True))

    with pytest.raises(RuntimeError, match="transient pool error"):
        await process_file(ctx, **_kwargs(scratch_path=str(scratch), expected_sha256=good))

    # The scratch copy SURVIVES so the in-place SAQ retry can re-verify and analyze it.
    assert scratch.exists()
    # No terminal report on a retryable attempt -- SAQ still has its retry budget.
    api.report_analysis_failed.assert_not_awaited()


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_scratch_survives_retryable_put_analysis_failure(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """CR-01: a retryable failure in the put_analysis callback (OUTSIDE the pool try) also keeps the copy.

    The put_analysis 5xx was the second stranding trap -- it sits outside the inner pool ``try`` so
    the generic handler had to move to an OUTER ``except`` to cover it.
    """
    scratch = _write_scratch(tmp_path)
    good = compute_sha256(scratch)
    mock_pool.return_value = MOCK_ANALYSIS
    api = _api()
    api.put_analysis = AsyncMock(side_effect=RuntimeError("put_analysis 5xx after retries"))
    ctx = _ctx(api, job=MagicMock(retryable=True))

    with pytest.raises(RuntimeError, match="put_analysis 5xx"):
        await process_file(ctx, **_kwargs(scratch_path=str(scratch), expected_sha256=good))

    assert scratch.exists()  # kept for the retry
    api.report_analysis_failed.assert_not_awaited()


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_scratch_cleaned_up_on_terminal_non_retryable_failure(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
) -> None:
    """CR-01: once the attempt is TERMINAL (not retryable) the copy is reclaimed and the failure reported.

    This is the counterpart to ``test_scratch_survives_retryable_failure`` -- the disk-bound
    cleanup guarantee (T-50-scratch-dos) still holds on the final attempt.
    """
    scratch = _write_scratch(tmp_path)
    good = compute_sha256(scratch)
    mock_pool.side_effect = RuntimeError("boom")
    api = _api()
    ctx = _ctx(api, job=MagicMock(retryable=False))

    with pytest.raises(RuntimeError, match="boom"):
        await process_file(ctx, **_kwargs(scratch_path=str(scratch), expected_sha256=good))

    assert not scratch.exists()  # terminal -> disk reclaimed
    api.report_analysis_failed.assert_awaited_once()


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_missing_scratch_at_sha_gate_routes_to_mismatch(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CR-01 defense: a missing scratch file at the sha256 gate reports a re-pushable mismatch.

    It must NEVER escape as an uncaught FileNotFoundError (which would strand the file in PUSHED
    with no callback), and it emits a scratch-dir-skew diagnostic (T-50-scratch-skew) naming the
    path so a persistent miss is diagnosable instead of an endless silent re-push.
    """
    missing = tmp_path / "never-written.mp3"
    file_id = uuid.uuid4()
    api = _api()

    with caplog.at_level("WARNING"):
        result = await process_file(_ctx(api), **_kwargs(file_id=file_id, scratch_path=str(missing), expected_sha256="a" * 64))

    assert result == {"file_id": str(file_id), "status": "push_mismatch"}
    api.report_push_mismatch.assert_awaited_once_with(file_id)
    mock_pool.assert_not_awaited()
    assert "PHAZE_CLOUD_SCRATCH_DIR" in caplog.text and str(missing) in caplog.text


# ---------------------------------------------------------------------------
# Local path is byte-identical when neither scratch field is set
# ---------------------------------------------------------------------------


@patch("phaze.tasks.functions._load_analyze_file", return_value=MagicMock())
@patch("phaze.tasks.functions.run_in_process_pool", new_callable=AsyncMock)
async def test_local_file_without_scratch_is_byte_identical(
    mock_pool: AsyncMock,
    _mock_loader: MagicMock,
) -> None:
    """No scratch_path/expected_sha256 → analyze original_path, no verify, no unlink branch taken."""
    mock_pool.return_value = MOCK_ANALYSIS
    api = _api()

    result = await process_file(_ctx(api), **_kwargs(original_path="/music/local.mp3"))

    assert result["status"] == "analyzed"
    # read_path == original_path (no scratch swap).
    assert mock_pool.await_args.args[2] == "/music/local.mp3"
    api.report_push_mismatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Producer threading (Task 2): enqueue_process_file pins scratch_path + expected_sha256
# ---------------------------------------------------------------------------


def _fake_file(file_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(id=file_id, original_path=f"/music/{file_id.hex}.mp3", file_type="mp3")


async def test_enqueue_threads_expected_sha256_and_scratch_path() -> None:
    """enqueue_process_file(..., expected_sha256=X, scratch_path=Y) builds a payload carrying X and Y;
    the deterministic key and timeout/retries policy are unchanged by the new kwargs."""
    queue = FakeQueue("phaze-agent-compute")
    fid = uuid.uuid4()
    file = _fake_file(fid)

    await enqueue_process_file(
        queue,
        file,
        "compute",
        "/models/pb",
        expected_sha256="a" * 64,
        scratch_path="/scratch/" + fid.hex + ".mp3",
    )

    _, payload = queue.captured[0]
    assert payload["expected_sha256"] == "a" * 64
    assert payload["scratch_path"] == "/scratch/" + fid.hex + ".mp3"
    validated = ProcessFilePayload.model_validate(payload)
    assert validated.expected_sha256 == "a" * 64
    assert validated.scratch_path == "/scratch/" + fid.hex + ".mp3"
    # Single funnel: deterministic key + policy preserved regardless of the new kwargs.
    assert queue.captured_policy[0]["key"] == process_file_job_key(fid)
    assert queue.captured_policy[0]["timeout"] == 7200
    assert queue.captured_policy[0]["retries"] == 2


async def test_enqueue_scratch_fields_default_none_for_bulk_local_producer() -> None:
    """Omitting the new kwargs → both fields serialize None (bulk local producer byte-identical)."""
    queue = FakeQueue("phaze-agent-compute")
    fid = uuid.uuid4()
    file = _fake_file(fid)

    await enqueue_process_file(queue, file, "compute", "/models/pb")

    _, payload = queue.captured[0]
    assert payload["expected_sha256"] is None
    assert payload["scratch_path"] is None
    assert queue.captured_policy[0]["key"] == process_file_job_key(fid)
    assert queue.captured_policy[0]["timeout"] == 7200
    assert queue.captured_policy[0]["retries"] == 2
