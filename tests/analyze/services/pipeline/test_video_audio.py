"""Tests for pre-analysis audio-track extraction (phaze-3ea41).

Every test mocks ``asyncio.create_subprocess_exec`` directly (the ``tests/analyze/core/
test_push_pipeline.py`` convention) rather than requiring a real ``ffmpeg``/``ffprobe`` binary,
so this module's control-flow (track selection, error mapping, heartbeat throttling, cleanup)
is exercised deterministically regardless of what is installed on the runner. A handful of
``skipif``-guarded tests at the bottom additionally exercise the REAL binaries end-to-end
(synthetic ``lavfi`` sources -- no fixture files, no local identifiers) for genuine black-box
confidence in the argv shape when ``ffmpeg``/``ffprobe`` are available.

Review correction (phaze-3ea41, round 2): the ten near-identical ``_fake_exec`` closures a
first draft repeated per-test are collapsed into the ``make_fake_exec`` factory fixture below.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
from typing import Any

import pytest

from phaze.services.video_audio import (
    AudioExtractionError,
    NoAudioTrackError,
    _select_track,
    extract_audio_track,
    probe_audio_streams,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCommunicateProc:
    """Stand-in for the ffprobe call, which uses ``communicate()`` (push.py convention)."""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return (self._stdout, self._stderr)

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


class _FakeStream:
    """Minimal async-iterable stand-in for ``proc.stdout``/``proc.stderr``."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStreamProc:
    """Stand-in for the ffmpeg extraction call, which streams stdout/stderr line-by-line."""

    def __init__(self, returncode: int, stdout_lines: list[bytes] | None = None, stderr_lines: list[bytes] | None = None) -> None:
        self._final_returncode = returncode
        self.returncode: int | None = None
        self.stdout = _FakeStream(stdout_lines or [])
        self.stderr = _FakeStream(stderr_lines or [])
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._final_returncode

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self._final_returncode


def _probe_json(streams: list[dict[str, Any]]) -> bytes:
    return json.dumps({"streams": streams}).encode()


def _make_fake_exec(procs: list[Any]) -> Any:
    """Return an async ``create_subprocess_exec`` replacement that yields ``procs`` in order.

    Used only by the ffprobe-only tests below (``probe_audio_streams``, the no-streams case),
    which never touch ffmpeg at all -- ``make_fake_exec`` (below) is for everything that does.
    """
    remaining = list(procs)

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> Any:
        return remaining.pop(0)

    return _fake_exec


@pytest.fixture
def make_fake_exec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Factory fixture for the ffprobe+ffmpeg subprocess double pair (review correction,
    phaze-3ea41: replaces ten near-identical per-test ``_fake_exec`` closures).

    Also patches ``tempfile.gettempdir`` to ``tmp_path`` so a caller that doesn't pass an
    explicit ``scratch_dir`` still lands extraction output somewhere real and cleanable.
    Returns a factory: ``make_fake_exec(streams, **ffmpeg_opts) -> extract_proc``. Every call
    the double receives (ffprobe AND ffmpeg alike, as ``(args, kwargs)`` tuples) is recorded on
    ``make_fake_exec.calls`` for argv-shape assertions.
    """

    def _factory(
        streams: list[dict[str, Any]],
        *,
        ffmpeg_returncode: int = 0,
        ffmpeg_stdout_lines: list[bytes] | None = None,
        ffmpeg_stderr_lines: list[bytes] | None = None,
        ffmpeg_proc: Any = None,
        write_output: bool = True,
        missing_ffmpeg: bool = False,
    ) -> Any:
        probe_proc = _FakeCommunicateProc(0, stdout=_probe_json(streams))
        extract_proc = (
            ffmpeg_proc
            if ffmpeg_proc is not None
            else _FakeStreamProc(ffmpeg_returncode, stdout_lines=ffmpeg_stdout_lines, stderr_lines=ffmpeg_stderr_lines)
        )

        async def _fake_exec(*args: Any, **kwargs: Any) -> Any:
            _factory.calls.append((args, kwargs))  # type: ignore[attr-defined]
            if args[0] == "ffmpeg":
                if missing_ffmpeg:
                    raise FileNotFoundError(2, "No such file or directory", "ffmpeg")
                if write_output:
                    Path(args[-1]).write_bytes(b"fake-mka-bytes")
                return extract_proc
            return probe_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return extract_proc

    _factory.calls = []  # type: ignore[attr-defined]
    monkeypatch.setattr("phaze.services.video_audio.tempfile.gettempdir", lambda: str(tmp_path))
    return _factory


def _ffmpeg_argv(make_fake_exec: Any) -> tuple[Any, ...]:
    """Pull the (single) recorded ffmpeg call's argv out of ``make_fake_exec.calls``."""
    args, _kwargs = next(c for c in make_fake_exec.calls if c[0][0] == "ffmpeg")
    return args


def _ffmpeg_kwargs(make_fake_exec: Any) -> dict[str, Any]:
    _args, kwargs = next(c for c in make_fake_exec.calls if c[0][0] == "ffmpeg")
    return kwargs


# ---------------------------------------------------------------------------
# _select_track (operator decision: prefer disposition.default, fallback to first)
# ---------------------------------------------------------------------------


def test_select_track_prefers_the_default_flagged_stream() -> None:
    streams = [
        {"index": 0, "codec_name": "aac", "disposition": {"default": 0}},
        {"index": 1, "codec_name": "ac3", "disposition": {"default": 1}},
        {"index": 2, "codec_name": "opus", "disposition": {"default": 0}},
    ]
    selected, others = _select_track(streams)
    assert selected["index"] == 1
    assert [s["index"] for s in others] == [0, 2]


def test_select_track_falls_back_to_first_when_nothing_is_default() -> None:
    streams = [
        {"index": 5, "codec_name": "aac", "disposition": {"default": 0}},
        {"index": 6, "codec_name": "ac3", "disposition": {"default": 0}},
    ]
    selected, others = _select_track(streams)
    assert selected["index"] == 5
    assert [s["index"] for s in others] == [6]


def test_select_track_falls_back_to_first_when_disposition_absent() -> None:
    """Older ffprobe/containers may omit disposition entirely -- must not raise."""
    streams = [{"index": 3, "codec_name": "mp3"}, {"index": 4, "codec_name": "flac"}]
    selected, others = _select_track(streams)
    assert selected["index"] == 3
    assert [s["index"] for s in others] == [4]


def test_select_track_single_stream_no_others() -> None:
    streams = [{"index": 0, "codec_name": "mp3", "disposition": {"default": 1}}]
    selected, others = _select_track(streams)
    assert selected["index"] == 0
    assert others == []


# ---------------------------------------------------------------------------
# probe_audio_streams
# ---------------------------------------------------------------------------


async def test_probe_audio_streams_returns_the_stream_list(monkeypatch: pytest.MonkeyPatch) -> None:
    streams = [{"index": 1, "codec_name": "aac", "codec_type": "audio"}]
    proc = _FakeCommunicateProc(0, stdout=_probe_json(streams))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _make_fake_exec([proc]))

    result = await probe_audio_streams("/video/concert.mkv")

    assert result == streams


async def test_probe_audio_streams_empty_when_no_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeCommunicateProc(0, stdout=_probe_json([]))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _make_fake_exec([proc]))

    assert await probe_audio_streams("/video/silent.mkv") == []


async def test_probe_audio_streams_nonzero_exit_raises_audio_extraction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeCommunicateProc(1, stderr=b"Invalid data found when processing input")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _make_fake_exec([proc]))

    with pytest.raises(AudioExtractionError, match="ffprobe failed"):
        await probe_audio_streams("/video/corrupt.mkv")


async def test_probe_audio_streams_non_json_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeCommunicateProc(0, stdout=b"not json")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _make_fake_exec([proc]))

    with pytest.raises(AudioExtractionError, match="non-JSON"):
        await probe_audio_streams("/video/weird.mkv")


async def test_probe_audio_streams_missing_binary_raises_audio_extraction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise_fnf(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory", "ffprobe")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_fnf)

    with pytest.raises(AudioExtractionError, match="ffprobe binary not found"):
        await probe_audio_streams("/video/concert.mkv")


# ---------------------------------------------------------------------------
# extract_audio_track — track selection
# ---------------------------------------------------------------------------


async def test_extract_audio_track_no_streams_raises_no_audio_track_error(monkeypatch: pytest.MonkeyPatch) -> None:
    probe_proc = _FakeCommunicateProc(0, stdout=_probe_json([]))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _make_fake_exec([probe_proc]))

    with pytest.raises(NoAudioTrackError, match="no audio stream"):
        await extract_audio_track("/video/broll.mkv")


async def test_extract_audio_track_falls_back_to_first_stream_when_none_default(make_fake_exec: Any) -> None:
    """Multiple audio streams, none flagged default -> the FIRST/lowest-index one is mapped
    via ``-map 0:<index>``."""
    streams = [{"index": 2, "codec_name": "aac"}, {"index": 3, "codec_name": "ac3"}]
    make_fake_exec(streams)

    result = await extract_audio_track("/video/multi_angle.mkv")

    assert Path(result).exists()
    argv = _ffmpeg_argv(make_fake_exec)
    assert argv[argv.index("-map") + 1] == "0:2"
    assert argv[argv.index("-c:a") + 1] == "copy"


async def test_extract_audio_track_prefers_default_flagged_stream_over_first(make_fake_exec: Any) -> None:
    """The container's DEFAULT-flagged stream wins even when it is NOT the first-listed one
    (operator decision, phaze-3ea41)."""
    streams = [
        {"index": 2, "codec_name": "aac", "disposition": {"default": 0}},
        {"index": 3, "codec_name": "ac3", "disposition": {"default": 1}},
    ]
    make_fake_exec(streams)

    result = await extract_audio_track("/video/multi_angle.mkv")

    assert Path(result).exists()
    argv = _ffmpeg_argv(make_fake_exec)
    assert argv[argv.index("-map") + 1] == "0:3"


async def test_extract_audio_track_logs_multi_track_with_file_id(make_fake_exec: Any) -> None:
    """The 'log the other streams' existence in the analysis record' operator decision --
    ``file_id`` rides the structured log line so a multi-track pick is attributable."""
    from structlog.testing import capture_logs

    streams = [
        {"index": 0, "codec_name": "aac", "disposition": {"default": 1}},
        {"index": 1, "codec_name": "ac3", "disposition": {"default": 0}},
    ]
    make_fake_exec(streams)

    with capture_logs() as logs:
        await extract_audio_track("/video/multi_angle.mkv", file_id="11111111-1111-1111-1111-111111111111")

    [event] = [entry for entry in logs if entry.get("event") == "video_audio_extraction_multi_track"]
    assert event["file_id"] == "11111111-1111-1111-1111-111111111111"
    assert event["selected_index"] == 0
    assert event["selected_is_default"] is True
    assert event["other_track_count"] == 1


async def test_extract_audio_track_uses_scratch_dir_when_given(make_fake_exec: Any, tmp_path: Path) -> None:
    make_fake_exec([{"index": 1, "codec_name": "aac"}])
    scratch = tmp_path / "extract-scratch"

    result = await extract_audio_track("/video/concert.mkv", scratch_dir=scratch)

    assert Path(result).parent == scratch
    assert scratch.is_dir()


# ---------------------------------------------------------------------------
# extract_audio_track — failure and cleanup
# ---------------------------------------------------------------------------


async def test_extract_audio_track_ffmpeg_nonzero_exit_raises_and_cleans_up(make_fake_exec: Any, tmp_path: Path) -> None:
    make_fake_exec([{"index": 1, "codec_name": "aac"}], ffmpeg_returncode=1, ffmpeg_stderr_lines=[b"Conversion failed!\n"])

    with pytest.raises(AudioExtractionError, match="Conversion failed"):
        await extract_audio_track("/video/broken.mkv")

    # The partial scratch file this function itself created is cleaned up on its OWN
    # failure path (never left behind for the caller to discover).
    assert not list(tmp_path.glob("*.mka"))


async def test_extract_audio_track_stderr_tail_keeps_the_last_lines_not_the_first(make_fake_exec: Any) -> None:
    """Review correction (phaze-3ea41): the joined tail must NOT be re-truncated to 500
    chars after already being bounded to <=20 lines * 500 chars -- that discarded the LAST
    lines, where ffmpeg's real error actually is. 25 lines pushes past the 20-line deque cap
    too, so this also proves the oldest lines are dropped (deque semantics), not the newest."""
    lines = [f"stage {i}: still converting\n".encode() for i in range(24)] + [b"Conversion failed: codec not supported\n"]
    make_fake_exec([{"index": 1, "codec_name": "aac"}], ffmpeg_returncode=1, ffmpeg_stderr_lines=lines)

    with pytest.raises(AudioExtractionError) as exc_info:
        await extract_audio_track("/video/broken.mkv")

    message = str(exc_info.value)
    # The real error (the LAST line) survives.
    assert "Conversion failed: codec not supported" in message
    # The deque(maxlen=20) dropped the earliest lines -- "stage 0" through "stage 3" (25 total
    # lines, cap 20, so the first 5 -- stage 0..4 -- are gone).
    assert "stage 0:" not in message
    assert "stage 23:" in message


async def test_extract_audio_track_empty_output_raises(make_fake_exec: Any) -> None:
    """returncode 0 but a zero-byte (or missing) output is still a failure -- a silently
    empty extraction must not masquerade as success."""
    make_fake_exec([{"index": 1, "codec_name": "aac"}], write_output=False)

    with pytest.raises(AudioExtractionError):
        await extract_audio_track("/video/empty_result.mkv")


async def test_extract_audio_track_missing_ffmpeg_binary_raises(make_fake_exec: Any) -> None:
    make_fake_exec([{"index": 1, "codec_name": "aac"}], missing_ffmpeg=True)

    with pytest.raises(AudioExtractionError, match="ffmpeg binary not found"):
        await extract_audio_track("/video/concert.mkv")


async def test_extract_audio_track_cancellation_kills_and_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No-orphan discipline (analysis_exec.py convention): cancelling the caller's task kills
    the child and removes any partial scratch file rather than leaking either."""
    streams = [{"index": 1, "codec_name": "aac"}]
    probe_proc = _FakeCommunicateProc(0, stdout=_probe_json(streams))

    class _NeverEndingStream:
        """Blocks forever (until cancelled) instead of ever yielding EOF -- models a wedged
        ffmpeg that has stopped emitting -progress lines but not exited."""

        def __aiter__(self) -> _NeverEndingStream:
            return self

        async def __anext__(self) -> bytes:
            await asyncio.sleep(3600)
            raise StopAsyncIteration  # pragma: no cover - unreachable, sleep is cancelled first

    class _HangingProc(_FakeStreamProc):
        def __init__(self) -> None:
            super().__init__(0)
            self.stdout = _NeverEndingStream()

    hanging = _HangingProc()

    async def _fake_exec(*args: Any, **_kwargs: Any) -> Any:
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"partial")
            return hanging
        return probe_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr("phaze.services.video_audio.tempfile.gettempdir", lambda: str(tmp_path))

    task = asyncio.ensure_future(extract_audio_track("/video/concert.mkv", heartbeat_cb=_noop_heartbeat))
    await asyncio.sleep(0)  # let it reach the (now-hanging) pump
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert hanging.killed
    assert not list(tmp_path.glob("*.mka"))


async def test_extract_audio_track_cancellation_settles_a_pending_heartbeat_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A heartbeat_cb still in-flight when the caller cancels must itself be cancelled and
    awaited (``_settle_pending``), not left running orphaned against a job nobody is
    watching -- the same ``_settle`` discipline ``analysis_exec.py`` uses."""
    streams = [{"index": 1, "codec_name": "aac"}]
    probe_proc = _FakeCommunicateProc(0, stdout=_probe_json(streams))

    class _OneLineThenHangStream:
        """Yields ONE progress line (enough to spawn a heartbeat), then hangs forever --
        keeps ``extract_audio_track`` itself genuinely mid-flight (suspended in the pump)
        at the moment of cancellation, with a heartbeat task already pending."""

        def __init__(self) -> None:
            self._yielded = False

        def __aiter__(self) -> _OneLineThenHangStream:
            return self

        async def __anext__(self) -> bytes:
            if not self._yielded:
                self._yielded = True
                return b"out_time_ms=1000\n"
            await asyncio.sleep(3600)
            raise StopAsyncIteration  # pragma: no cover - unreachable, cancelled first

    class _HangingProc(_FakeStreamProc):
        def __init__(self) -> None:
            super().__init__(0)
            self.stdout = _OneLineThenHangStream()

    hanging = _HangingProc()

    async def _fake_exec(*args: Any, **_kwargs: Any) -> Any:
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"partial")
            return hanging
        return probe_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr("phaze.services.video_audio.tempfile.gettempdir", lambda: str(tmp_path))

    started = asyncio.Event()
    cancelled = False

    async def _slow_heartbeat() -> None:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled = True
            raise

    task = asyncio.ensure_future(extract_audio_track("/video/concert.mkv", heartbeat_cb=_slow_heartbeat, heartbeat_interval_sec=0.0))
    await asyncio.wait_for(started.wait(), timeout=5.0)  # the spawned heartbeat task has started
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled, "the in-flight heartbeat task must be cancelled, not left orphaned"


async def _noop_heartbeat() -> None:
    return None


# ---------------------------------------------------------------------------
# extract_audio_track — no-progress-consumer optimization (review correction #8)
# ---------------------------------------------------------------------------


async def test_extract_audio_track_skips_progress_flag_and_stdout_pipe_without_heartbeat_cb(make_fake_exec: Any) -> None:
    """No ``heartbeat_cb`` -> no ``-progress pipe:1`` in argv AND stdout is NOT requested as a
    PIPE (review correction, phaze-3ea41: the flag/pipe/pump trio is pure overhead with no
    consumer)."""
    make_fake_exec([{"index": 1, "codec_name": "aac"}])

    await extract_audio_track("/video/concert.mkv")

    argv = _ffmpeg_argv(make_fake_exec)
    assert "-progress" not in argv
    assert "pipe:1" not in argv
    kwargs = _ffmpeg_kwargs(make_fake_exec)
    assert kwargs["stdout"] is asyncio.subprocess.DEVNULL


async def test_extract_audio_track_requests_progress_flag_and_pipe_with_heartbeat_cb(make_fake_exec: Any) -> None:
    """The inverse: a real ``heartbeat_cb`` DOES get ``-progress pipe:1`` + a real PIPE."""
    make_fake_exec([{"index": 1, "codec_name": "aac"}])

    await extract_audio_track("/video/concert.mkv", heartbeat_cb=_noop_heartbeat)

    argv = _ffmpeg_argv(make_fake_exec)
    assert "-progress" in argv
    assert argv[argv.index("-progress") + 1] == "pipe:1"
    kwargs = _ffmpeg_kwargs(make_fake_exec)
    assert kwargs["stdout"] is asyncio.subprocess.PIPE


# ---------------------------------------------------------------------------
# extract_audio_track — heartbeat throttling AND fire-and-forget (review correction #6)
# ---------------------------------------------------------------------------


async def test_extract_audio_track_invokes_heartbeat_cb_on_progress_lines(make_fake_exec: Any) -> None:
    make_fake_exec([{"index": 1, "codec_name": "aac"}], ffmpeg_stdout_lines=[b"out_time_ms=1000\n", b"out_time_ms=2000\n", b"progress=end\n"])

    calls = 0

    async def _heartbeat() -> None:
        nonlocal calls
        calls += 1

    await extract_audio_track("/video/concert.mkv", heartbeat_cb=_heartbeat, heartbeat_interval_sec=0.0)

    # heartbeat_interval_sec=0.0 means every progress line fires it (no throttling).
    assert calls == 3


async def test_extract_audio_track_heartbeat_throttled_to_one_call(make_fake_exec: Any) -> None:
    make_fake_exec([{"index": 1, "codec_name": "aac"}], ffmpeg_stdout_lines=[b"out_time_ms=1000\n", b"out_time_ms=2000\n", b"progress=end\n"])

    calls = 0

    async def _heartbeat() -> None:
        nonlocal calls
        calls += 1

    # A huge interval means only the FIRST line (last_touch initialized to "now") can pass the
    # throttle before the others land inside the same window.
    await extract_audio_track("/video/concert.mkv", heartbeat_cb=_heartbeat, heartbeat_interval_sec=3600.0)

    assert calls <= 1


async def test_extract_audio_track_heartbeat_error_is_swallowed(make_fake_exec: Any) -> None:
    """A raising heartbeat_cb must never fail the extraction (best-effort, phaze-w55w1 discipline)."""
    make_fake_exec([{"index": 1, "codec_name": "aac"}], ffmpeg_stdout_lines=[b"out_time_ms=1000\n"])

    async def _boom() -> None:
        raise RuntimeError("heartbeat sink is down")

    result = await extract_audio_track("/video/concert.mkv", heartbeat_cb=_boom, heartbeat_interval_sec=0.0)

    assert Path(result).exists()


async def test_extract_audio_track_no_heartbeat_cb_is_fine(make_fake_exec: Any) -> None:
    make_fake_exec([{"index": 1, "codec_name": "aac"}], ffmpeg_stdout_lines=[b"out_time_ms=1000\n"])

    result = await extract_audio_track("/video/concert.mkv")

    assert Path(result).exists()


async def test_extract_audio_track_hung_heartbeat_does_not_stall_the_pump(make_fake_exec: Any) -> None:
    """Review correction #6: a heartbeat_cb that never returns must NOT block the stdout pump
    from draining -progress lines -- fire-and-forget (spawned, not awaited inline) is what
    makes this possible. Bounded with wait_for: the OLD inline-await shape would have hung
    here (this test failing via TimeoutError is exactly the regression this guards)."""
    make_fake_exec(
        [{"index": 1, "codec_name": "aac"}],
        ffmpeg_stdout_lines=[b"out_time_ms=1000\n", b"out_time_ms=2000\n", b"out_time_ms=3000\n", b"progress=end\n"],
    )

    hang_forever = asyncio.Event()  # never set -> a heartbeat_cb awaiting it never returns

    async def _hung_heartbeat() -> None:
        await hang_forever.wait()

    result = await asyncio.wait_for(
        extract_audio_track("/video/concert.mkv", heartbeat_cb=_hung_heartbeat, heartbeat_interval_sec=0.0),
        timeout=5.0,
    )

    assert Path(result).exists()


# ---------------------------------------------------------------------------
# Real ffmpeg/ffprobe (skip-safe) — genuine argv/behavior confidence when available
# ---------------------------------------------------------------------------

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed on this runner")
async def test_real_extract_audio_track_from_synthetic_video(tmp_path: Path) -> None:
    """End-to-end against REAL ffmpeg/ffprobe: a synthetic (lavfi) video WITH an audio stream
    extracts cleanly and the result is itself decodable by ffprobe as an audio-only file."""
    src = tmp_path / "synthetic_with_audio.mkv"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=1:size=64x64:rate=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-shortest",
        str(src),
    )
    await proc.wait()
    assert src.exists()

    result = await extract_audio_track(str(src), scratch_dir=tmp_path)

    assert Path(result).exists()
    assert Path(result).stat().st_size > 0
    extracted_streams = await probe_audio_streams(result)
    assert len(extracted_streams) == 1


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed on this runner")
async def test_real_extract_audio_track_no_audio_stream_raises(tmp_path: Path) -> None:
    """A REAL video with no audio stream at all -- the acceptance-criteria case -- fails
    cleanly with NoAudioTrackError, never a hang."""
    src = tmp_path / "synthetic_silent.mkv"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=1:size=64x64:rate=1",
        "-c:v",
        "libx264",
        "-an",
        str(src),
    )
    await proc.wait()
    assert src.exists()

    with pytest.raises(NoAudioTrackError):
        await extract_audio_track(str(src), scratch_dir=tmp_path)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed on this runner")
async def test_real_extract_audio_track_prefers_default_flagged_stream(tmp_path: Path) -> None:
    """Real ffprobe/ffmpeg round-trip proving the disposition.default preference (operator
    decision) against genuine ffprobe JSON, not a hand-built fixture."""
    src = tmp_path / "synthetic_multi_track.mkv"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=1",
        "-map",
        "0:a",
        "-map",
        "1:a",
        "-c:a",
        "aac",
        "-disposition:a:0",
        "0",
        "-disposition:a:1",
        "default",
        str(src),
    )
    await proc.wait()
    assert src.exists()

    streams = await probe_audio_streams(str(src))
    assert len(streams) == 2
    selected, _others = _select_track(streams)
    assert selected["index"] == 1  # the SECOND stream is the one flagged default above
    assert selected["disposition"]["default"] == 1
