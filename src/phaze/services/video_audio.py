"""Probe and, when needed, extract one audio stream before analysis (phaze-3ea41).

``ffprobe`` is the authority on stream availability. No audio stream raises
:class:`NoAudioTrackError`; other probe/extraction failures raise
:class:`AudioExtractionError`, so callers can persist terminal failures without retrying a
deterministically unusable file. Multiple streams select the default-flagged stream, falling
back to the lowest index, and ``ffmpeg -c:a copy`` writes an ``.mka`` scratch file without
decoding or re-encoding.

Operator decision, 2026-08-12, ``phaze-3ea41``: asked "which video containers should the
analyze lane accept?", the operator selected "Probe-based, any container". This removes a
video-extension whitelist; it does not authorize remuxing plain audio. Operator decision,
2026-08-12, ``phaze-3ea41``: asked "where should audio extraction run?", the operator selected
"Both lanes". Operator decision, 2026-08-12, ``phaze-3ea41``: asked which track to analyze when
several exist, the operator selected "Default/first track"; logging the other streams was part
of that option's description, not a separately authored answer. The recovered evidence and
exact incident measurements are in
``docs/design/0012-verification-fidelity-and-operator-attribution.md``.

Plain audio is still probed but bypasses remuxing. Extracted intermediates remain compressed,
so scratch use is O(concurrent extracted files), not O(archive size); D-07's chunk-bounded PCM
contract remains in ``docs/design/0007-windowed-analysis.md``. Extraction runs before the
analysis child's D-08 stall watchdog, so progress lines feed only the caller's outer heartbeat.
Heartbeat callbacks are spawned rather than awaited inline, keeping the ffmpeg progress pipe
drained even when a callback stalls. D-09 keeps the module lane-agnostic and database-free.

This module must not import ``phaze.database``, ``phaze.tasks.session``, SQLAlchemy, or Essentia:
both the agent worker and the Postgres-free one-shot job runner import it.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING, Any
import uuid

import structlog

from phaze.services.analysis_exec import _STDERR_LINE_MAX, _STDERR_TAIL_LINES


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


logger = structlog.get_logger(__name__)


# Matroska audio: accepts arbitrary audio codecs without forcing a re-encode, so `-c:a copy`
# (the disk-headroom decision above) is always a legal mux target regardless of source codec.
_EXTRACTED_AUDIO_SUFFIX = ".mka"

# How often (seconds) an in-progress extraction may invoke heartbeat_cb. Independent of the
# analysis-side cadence constant in tasks/functions.py -- this module has no AgentSettings
# dependency by design (see the import-boundary docstring above) -- but callers are expected to
# pass a value derived the same way (a fraction of their own outer liveness deadline).
_DEFAULT_HEARTBEAT_INTERVAL_SEC = 5.0

# Bounded stderr capture for diagnosis. Reuses services/analysis_exec.py's OWN constants
# (review correction, phaze-3ea41) rather than re-declaring the same two numbers a second
# time -- both modules bound a subprocess's trailing stderr the same way for the same reason
# (diagnosis without an unbounded string), so one definition is the source of truth.


@dataclass(frozen=True, slots=True)
class AudioSource:
    """What :func:`extract_audio_track` produced: what to ANALYZE, and what to DELETE.

    **This type exists to make ownership impossible to get wrong (phaze-l832u).** The old
    return was a bare ``str`` under the contract "a scratch path the caller owns and deletes",
    and both lanes unlink it in an outer ``finally``. That contract is safe only while the
    function ALWAYS creates a new file. The moment a skip branch exists, returning the input
    path under it would make the caller delete its own input -- and on the local lane
    (``tasks/functions.py::process_file`` with no pushed copy) the input is the operator's REAL
    ARCHIVE FILE, not a staged copy. A bare string cannot express the difference; two named
    fields can:

    * :attr:`analysis_path` -- hand THIS to the analyzer. It may be the input path.
    * :attr:`cleanup_path` -- delete THIS, and only this, in your ``finally``. ``None`` means
      nothing was created and there is nothing to delete. It is NEVER the input path.

    Callers must not reconstruct one from the other. The correct shape is literally::

        source = await extract_audio_track(read_path, ...)
        cleanup = source.cleanup_path       # may be None -- unlink guarded on that
        analysis_path = source.analysis_path
    """

    analysis_path: str
    cleanup_path: str | None


class NoAudioTrackError(RuntimeError):
    """The container has no audio stream at all -- a clean, deterministic, TERMINAL failure.

    Callers report this straight to a stored ``error_message`` and do NOT retry (retrying
    ffprobe against the same bytes will report the same absence every time) -- see
    ``tasks/functions.py::process_file``'s dedicated ``except NoAudioTrackError`` branch,
    which mirrors the existing ``TimeoutError``/``AnalysisSubprocessError`` "no blind re-run
    of a deterministically-doomed file" handling (T-43-08).
    """


class AudioExtractionError(RuntimeError):
    """``ffprobe``/``ffmpeg`` itself failed for a reason OTHER than "no audio track"
    (corrupt/truncated container, missing binary, disk full writing the scratch file, ...).

    NOT a subclass of :class:`NoAudioTrackError` (the two are diagnosed differently and the
    stored ``error`` text should say which), but callers give it the SAME TERMINAL treatment
    (review correction, phaze-3ea41): the dominant real-world cause is a corrupt/truncated
    container, and re-running ffprobe/ffmpeg against the SAME bytes reproduces the SAME
    failure -- exactly the "no blind re-run of a deterministically-doomed file" reasoning
    (T-43-08) the codebase already applies to ``AnalysisSubprocessError`` (an essentia child
    crash). Both ``tasks/functions.py::process_file`` and ``job_runner.py::run`` report a
    stored ``error_message`` immediately and do not fall through to their generic
    retryable-aware handler for this error.
    """


async def probe_container_streams(file_path: str) -> list[dict[str, Any]]:
    """Return ffprobe's stream list for ``file_path`` -- EVERY stream, not just the audio ones.

    Reads container/stream headers only -- never decodes PCM, mirroring
    ``services/analysis.py::_probe_duration_sec``'s own ffprobe discipline one layer up the
    stack. Each entry carries at least ``index`` (the file's absolute stream index, directly
    usable as ffmpeg's ``-map 0:<index>``), ``codec_name``, ``codec_type``, and a nested
    ``disposition`` dict whose ``default`` key is the track-selection signal (phaze-3ea41
    operator decision, answered "Default/first track" 2026-08-12: prefer the container's
    DEFAULT-flagged stream -- record 3 of this module's docstring) and whose
    ``attached_pic`` key marks a cover-art "video" stream (phaze-l832u: what separates an mp3
    with embedded artwork from a real video container).

    This is ALSO the sole authority for whether ``file_path`` is analyzable at all (the
    probe-based format scope in this module's docstring) and for whether it needs
    extracting at all (:func:`_is_already_plain_audio`) -- regardless of what the file's
    extension claims. **phaze-l832u widened this from ``-select_streams a`` to the whole
    container**: "does this file have audio" can be answered from the audio streams alone, but
    "is this file ALREADY just that audio" cannot -- it is a statement about what else is in
    there. One probe answers both; do not split it back into two ffprobe invocations.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "stream=index,codec_name,codec_type,channels,sample_rate:stream_disposition=default,attached_pic",
        file_path,
    ]
    try:
        # Fixed list argv, never a shell (push.py / analysis_exec.py convention: S603/B603-clean).
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as exc:
        msg = "ffprobe binary not found on PATH"
        raise AudioExtractionError(msg) from exc
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = f"ffprobe failed (exit {proc.returncode}) probing {file_path!r}: {stderr.decode('utf-8', errors='replace')[:_STDERR_LINE_MAX]}"
        raise AudioExtractionError(msg)
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError as exc:
        msg = f"ffprobe produced non-JSON output probing {file_path!r}"
        raise AudioExtractionError(msg) from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    return streams if isinstance(streams, list) else []


def _audio_streams(streams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``codec_type == "audio"`` entries of a :func:`probe_container_streams` result."""
    return [s for s in streams if s.get("codec_type") == "audio"]


def _is_already_plain_audio(streams: list[dict[str, Any]]) -> bool:
    """True when the container is ALREADY nothing but one audio track (phaze-l832u).

    The remux-skip predicate for decision 2 of the phaze-l832u epic: a file that is already a
    single-audio-stream container is analyzed DIRECTLY, instead of being copied through ffmpeg
    into a ``.mka`` first. That removes a full remux of every one of the ~11,428 audio files in
    the corpus from the hot path -- correctness is decision 1's job (the ffprobe duration probe,
    ``services/analysis.py`` D-10), this is cost.

    ffprobe remains the SOLE authority (phaze-3ea41's format scope is narrowed here, not
    reverted -- and what D-10 narrows is the implementer's every-file REMUX, not the operator's
    probe-based container acceptance; this module's docstring keeps the two apart): the
    predicate reads what the container actually holds, never the suffix or
    ``FileRecord.file_type``. Two shapes are deliberately still EXTRACTED:

    * **More than one audio stream** -- which track essentia would pick is exactly the opacity
      this module exists to remove; :func:`_select_track` must make that choice explicitly.
    * **Any non-audio stream that is not cover art** -- a real video track, subtitles, chapters,
      timed data. That is phaze-3ea41's actual feature and it keeps working unchanged.

    An ``attached_pic`` "video" stream (embedded album artwork, ubiquitous in mp3/m4a) is NOT a
    video track: it is a single still frame in the tag payload, and essentia decoded such files
    directly for the whole life of the project before phaze-3ea41 -- every completed analysis in
    the corpus came through that path. Treating artwork as "this is a video container" would
    put the remux back in front of essentially the entire archive and give the skip nothing to
    do.
    """
    audio = _audio_streams(streams)
    if len(audio) != 1:
        return False
    for stream in streams:
        if stream.get("codec_type") == "audio":
            continue
        if (stream.get("disposition") or {}).get("attached_pic") == 1:
            continue
        return False
    return True


def _select_track(streams: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Pick the container's DEFAULT-flagged audio stream, falling back to the first.

    Track-selection operator decision (phaze-3ea41), answered "Default/first track"
    2026-08-12 -- the question as put, and the label/description split it turns on, are in
    record 3 of this module's docstring: prefer whichever stream ffprobe reports
    with ``disposition.default == 1`` -- the container's own author-declared primary track --
    over blindly taking the lowest ffprobe index. Falls back to the first-listed stream when
    NONE carries the default flag (common: many encoders never set it). Returns
    ``(selected, others)`` with ``others`` in original probe order for logging.
    """
    for i, stream in enumerate(streams):
        disposition = stream.get("disposition")
        if isinstance(disposition, dict) and disposition.get("default") == 1:
            return stream, [*streams[:i], *streams[i + 1 :]]
    return streams[0], streams[1:]


def _log_multi_track_selection(file_path: str, file_id: str | None, selected: dict[str, Any], others: list[dict[str, Any]]) -> None:
    """Log which track was picked, and which others existed, for a multi-audio-stream container.

    Split out of :func:`extract_audio_track` (phaze-bk9el.3, CCN cleanup) -- pure logging, no
    branch on ``others`` itself; the caller only calls this when ``others`` is non-empty.
    """
    logger.info(
        "video_audio_extraction_multi_track",
        file=file_path,
        file_id=file_id,
        selected_index=selected.get("index"),
        selected_codec=selected.get("codec_name"),
        selected_is_default=bool((selected.get("disposition") or {}).get("default")),
        other_track_count=len(others),
        other_tracks=[{"index": s.get("index"), "codec_name": s.get("codec_name")} for s in others],
    )


def _resolve_dest_path(scratch_dir: str | Path | None) -> Path:
    """Where the extracted-audio scratch file lands (review correction, phaze-3ea41 -- naming
    the exact mechanism here since a prior report described it only in prose elsewhere):
    ``scratch_dir``, when the caller passes one, else ``tempfile.gettempdir()``. Both real
    callers DO pass one explicitly (neither relies on this fallback in production):

    * ``tasks/functions.py::process_file`` passes ``cfg.cloud_scratch_dir`` (AgentSettings) --
      the SAME directory ``push_file`` already rsyncs large pushed containers into on a
      compute agent, so it is provisioned for exactly this file-size class. ``None`` only on
      an agent that never participates in the cloud push pipeline (pure local fileserver-only
      role), where ``tempfile.gettempdir()`` is the honest fallback -- such an agent has no
      OTHER configured large-file scratch location to fall back to instead.
    * ``job_runner.py::run`` passes ``tmp_path.parent`` explicitly (the SAME directory its own
      downloaded original already lives in) -- this pod has no separate configured scratch
      setting of its own (``cloud_scratch_dir`` names a DIFFERENT host's rsync landing zone,
      not this pod's ephemeral filesystem), so co-locating with the already-larger download is
      the only self-consistent choice, and it happens to equal ``tempfile.gettempdir()`` today
      since that is where ``tmp_path`` itself lands.

    WITHIN the resolved directory: a random ``uuid4().hex`` filename (never derived from
    ``file_path``, which could collide across concurrent extractions of files sharing a
    directory).
    """
    dest_dir = Path(scratch_dir) if scratch_dir is not None else Path(tempfile.gettempdir())
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / f"{uuid.uuid4().hex}{_EXTRACTED_AUDIO_SUFFIX}"


def _build_ffmpeg_argv(file_path: str, selected_index: Any, dest_path: Path, *, want_progress: bool) -> list[str]:
    """The ``-c:a copy`` extraction argv (disk-headroom decision, this module's docstring).

    Review correction (phaze-3ea41): ``-progress pipe:1`` is requested ONLY when
    ``want_progress`` holds -- with no consumer for it, the extra pipe/pump is pure overhead,
    and ffmpeg writes nothing to stdout without ``-progress`` (everything else already routes
    to stderr via ``-loglevel error -nostats``).
    """
    argv = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-nostats",
        "-i",
        file_path,
        "-map",
        f"0:{selected_index}",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "copy",
    ]
    if want_progress:
        argv.extend(("-progress", "pipe:1"))
    argv.append(str(dest_path))
    return argv


async def _spawn_ffmpeg(argv: list[str], *, want_progress: bool) -> asyncio.subprocess.Process:
    """Start the extraction ffmpeg with the pipes the pump loops need, or raise cleanly.

    Raises :class:`AudioExtractionError` for a missing binary or for a spawn that somehow
    yields ``None`` pipes despite requesting them (defensive; not a real runtime path -- see
    the guard's own comment below).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE if want_progress else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        msg = "ffmpeg binary not found on PATH"
        raise AudioExtractionError(msg) from exc

    if proc.stderr is None or (want_progress and proc.stdout is None):  # pragma: no cover - PIPEs requested above
        proc.kill()
        msg = "ffmpeg extraction spawned without stdout/stderr pipes"
        raise AudioExtractionError(msg)
    return proc


def _check_ffmpeg_result(returncode: int | None, dest_path: Path, stderr_tail: deque[str], file_path: str) -> None:
    """Raise :class:`AudioExtractionError` for a failed or empty extraction, cleaning up first.

    Review correction (phaze-3ea41): do NOT re-truncate the already-bounded joined tail
    (<=20 lines * 500 chars) down to 500 chars -- that discarded everything but the EARLIEST
    lines, exactly backwards: ffmpeg's real error is on the LAST line(s). The per-line cap
    already bounds total size sanely (~10 KiB ceiling); the join itself needs no further
    truncation.
    """
    if returncode == 0 and dest_path.exists() and dest_path.stat().st_size > 0:
        return
    dest_path.unlink(missing_ok=True)
    detail = " | ".join(stderr_tail) or "no stderr output"
    msg = f"ffmpeg audio extraction failed (exit {returncode}) for {file_path!r}: {detail}"
    raise AudioExtractionError(msg)


@dataclass
class _ExtractionPump:
    """Mutable state shared by one extraction's stdout/stderr readers and heartbeat spawner.

    Pulled out of :func:`extract_audio_track`'s own closures (phaze-bk9el.3, CCN cleanup) so
    the pump loops are ordinary module-level functions instead of nested ones -- the behaviour
    is unchanged, this just gives the shared, mutating state (``pending``, ``stderr_tail``,
    ``last_touch``) one home instead of five closures over the same locals.
    """

    proc: asyncio.subprocess.Process
    heartbeat_cb: Callable[[], Awaitable[None]] | None
    heartbeat_interval_sec: float
    want_progress: bool
    pending: set[asyncio.Task[None]] = field(default_factory=set)
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINES))
    last_touch: float = field(default_factory=time.monotonic)


async def _safe_heartbeat(heartbeat_cb: Callable[[], Awaitable[None]] | None) -> None:
    # Genuinely can't happen -- only ever called from _spawn_heartbeat, which is only ever
    # called from _pump_stdout, which itself returns immediately when want_progress
    # (== heartbeat_cb is not None) is False (bandit B101: no `assert` in production code; a
    # silent no-op return is the honest translation of an invariant this defensive, not a real
    # runtime possibility).
    if heartbeat_cb is None:
        return
    with contextlib.suppress(Exception):
        await heartbeat_cb()


def _spawn_heartbeat(pump: _ExtractionPump) -> None:
    task = asyncio.get_running_loop().create_task(_safe_heartbeat(pump.heartbeat_cb))
    pump.pending.add(task)
    task.add_done_callback(pump.pending.discard)


async def _pump_stdout(pump: _ExtractionPump) -> None:
    # -progress pipe:1 (D-08-style liveness, extended to extraction): treat any line as a
    # tick and throttle the caller's heartbeat_cb the same way the analysis side does.
    # A no-op when want_progress is False -- checked explicitly (not merely inferred from
    # proc.stdout is None) so this never depends on the real subprocess machinery's DEVNULL
    # behavior to keep heartbeat_cb unreachable when the caller asked for none.
    if not pump.want_progress or pump.proc.stdout is None:
        return
    async for raw in pump.proc.stdout:
        if not raw.strip():
            continue
        now = time.monotonic()
        if (now - pump.last_touch) < pump.heartbeat_interval_sec:
            continue
        pump.last_touch = now
        _spawn_heartbeat(pump)


async def _pump_stderr(pump: _ExtractionPump) -> None:
    # Mirrors _pump_stdout's own guard above (bandit B101: no `assert` in production code).
    # proc.stderr is None only in the already-raised PIPE-guard branch (_spawn_ffmpeg), so
    # this never actually returns early in practice -- it is defense-in-depth, not a real path.
    if pump.proc.stderr is None:
        return
    async for raw in pump.proc.stderr:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        pump.stderr_tail.append(line[:_STDERR_LINE_MAX])


async def _settle_pending(pump: _ExtractionPump) -> None:
    """Cancel + await every still-pending heartbeat task (analysis_exec.py's ``_settle``
    discipline): a cancelled-but-unawaited task is still scheduled and would resume on a
    later loop iteration, touching a job nobody is waiting on any more."""
    for task in pump.pending:
        if not task.done():
            task.cancel()
    if pump.pending:
        await asyncio.gather(*pump.pending, return_exceptions=True)


async def extract_audio_track(
    file_path: str,
    *,
    file_id: str | None = None,
    scratch_dir: str | Path | None = None,
    heartbeat_cb: Callable[[], Awaitable[None]] | None = None,
    heartbeat_interval_sec: float = _DEFAULT_HEARTBEAT_INTERVAL_SEC,
) -> AudioSource:
    """Demux the container's DEFAULT-flagged audio stream (or the first, as fallback) to a
    scratch file -- unless the file is ALREADY just that audio, in which case nothing is copied.

    Returns an :class:`AudioSource`; read its docstring before touching either field. Raises
    :class:`NoAudioTrackError` when the container has no audio stream, or
    :class:`AudioExtractionError` for any other ffprobe/ffmpeg failure.

    **The skip branch (phaze-l832u, epic decision 2).** When :func:`_is_already_plain_audio`
    holds, this returns ``AudioSource(analysis_path=file_path, cleanup_path=None)``: the
    analyzer reads the ORIGINAL file and the caller has nothing to delete. ``cleanup_path`` is
    ``None`` -- NOT the input path -- because both lanes unlink ``cleanup_path`` in an outer
    ``finally`` and on the local lane the input is the operator's REAL ARCHIVE FILE. Returning
    the input path here under the old delete-me contract would delete the archive original;
    this is the single highest-risk line in the change and the reason the return type is a pair
    of named fields rather than a string.

    On the EXTRACTION branch ``cleanup_path`` is the newly created scratch file and equals
    ``analysis_path``. This function never leaves a partial/empty file behind on its own failure
    paths (it unlinks its own output before raising), but on a successful return ownership of
    that scratch file transfers to the caller -- see ``tasks/functions.py::process_file``'s and
    ``job_runner.py::run``'s outer ``finally``, which delete it on every terminal exit, success
    or failure alike.

    ``file_id`` is OPTIONAL and purely for log attribution. "Log the others' existence in the
    analysis record" carries the operator decision of 2026-08-12 (phaze-3ea41) -- but in its
    weaker register: it is dispatcher-written ACCOMPANYING CONTEXT in the DESCRIPTION beside the
    option label the operator actually chose, "Default/first track". Record 3 of this module's
    docstring keeps the two apart rather than flattening them. That the log
    is a structured INFO line keyed by ``file_id``, rather than a row on the analysis record, is
    the implementer's reading of "the analysis record" -- callers that have one (both real
    lanes do) pass it so a multi-track pick is attributable to the file it was made for in the
    structured log stream without threading a DB write through an essentia/DB-free module.
    """
    container = await probe_container_streams(file_path)
    streams = _audio_streams(container)
    if not streams:
        msg = f"no audio stream found in {file_path!r}"
        raise NoAudioTrackError(msg)

    if _is_already_plain_audio(container):
        # phaze-l832u decision 2: nothing to demux and nothing to disambiguate -- analyze the
        # file where it lies. cleanup_path is None precisely so the caller's unconditional
        # unlink cannot reach this path (see AudioSource's docstring).
        logger.info(
            "video_audio_extraction_skipped_plain_audio",
            file=file_path,
            file_id=file_id,
            codec=streams[0].get("codec_name"),
        )
        return AudioSource(analysis_path=file_path, cleanup_path=None)

    selected, others = _select_track(streams)
    if others:
        _log_multi_track_selection(file_path, file_id, selected, others)

    # CLEANUP GUARANTEE: this function deletes dest_path on every ONE OF ITS OWN failure exits
    # (ffmpeg nonzero exit / empty output, and the exceptional-exit branch below) -- never
    # hands a partial file to a caller. On the SUCCESS return, ownership transfers to the
    # caller: both ``tasks/functions.py::process_file`` and ``job_runner.py::run`` unlink the
    # returned path in their own outer ``finally`` on every terminal exit (success or later
    # failure), unconditionally and independent of the pushed-original scratch copy's
    # retry-preserving cleanup (see each lane's own comment on why that copy differs).
    dest_path = _resolve_dest_path(scratch_dir)
    want_progress = heartbeat_cb is not None
    argv = _build_ffmpeg_argv(file_path, selected["index"], dest_path, want_progress=want_progress)
    proc = await _spawn_ffmpeg(argv, want_progress=want_progress)

    # Review correction (phaze-3ea41): heartbeat_cb is FIRE-AND-FORGET (spawned, never awaited
    # inline by the pump), mirroring _run_analysis_with_progress's own _spawn pattern. Awaiting
    # it inline made a hung job.update() stop draining proc.stdout entirely; once ffmpeg fills
    # the OS pipe buffer writing -progress lines nobody is reading, ffmpeg itself blocks and the
    # whole extraction wedges with NO watchdog to save it (extraction runs before
    # run_analysis_subprocess arms the inner stall watchdog). Spawning keeps the pump reading
    # regardless of how slow/stuck any single touch is; ``pump.pending`` holds a strong ref so a
    # spawned task is never GC'd mid-flight, and every touch swallows its own errors
    # (best-effort, matching the analysis-side heartbeat's contract).
    pump = _ExtractionPump(proc=proc, heartbeat_cb=heartbeat_cb, heartbeat_interval_sec=heartbeat_interval_sec, want_progress=want_progress)

    try:
        await asyncio.gather(_pump_stdout(pump), _pump_stderr(pump))
        returncode = await proc.wait()
        # Deliberately NOT draining ``pump.pending`` here (review correction, phaze-3ea41): a
        # touch is fire-and-forget precisely so a slow/hung ``heartbeat_cb`` cannot add ITS
        # OWN latency on top of a now-finished extraction -- ffmpeg is done, the caller gets
        # its result immediately, and any still-running touch keeps going in the background
        # (strong-refed by ``pump.pending`` via each task's own done-callback closure, so it is
        # never GC'd mid-flight even though this function has already returned). Blocking the
        # return on a drain here would silently reintroduce the exact hang this fix removes,
        # just moved one line later -- a heartbeat_cb with no bound of its own would then wedge
        # extraction's OWN caller instead of merely the read loop.
    except BaseException:
        # No-orphan discipline (analysis_exec.py convention): kill+reap on any exceptional exit
        # (cancellation included) so a stalled ffmpeg never survives its caller's interest.
        if proc.returncode is None:
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        await _settle_pending(pump)
        dest_path.unlink(missing_ok=True)
        raise

    _check_ffmpeg_result(returncode, dest_path, pump.stderr_tail, file_path)

    # Extraction branch: analysis_path IS the scratch file, so cleanup_path is the same path --
    # the caller deletes exactly what this call created, and never its input.
    return AudioSource(analysis_path=str(dest_path), cleanup_path=str(dest_path))
