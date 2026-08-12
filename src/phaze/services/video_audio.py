"""ffmpeg/ffprobe pre-analysis audio-track extraction (phaze-3ea41).

D-09 DECISION RECORD -- pre-analysis audio extraction, not container-aware essentia
-------------------------------------------------------------------------------------
Video containers (mkv, avi, mp4, ...) were already flowing into ``analyze_file`` unchanged
(phaze-p0l9 widened the analyzable set to MUSIC + VIDEO), relying on essentia's ffmpeg-backed
``MonoLoader``/``MetadataReader`` to demux *and* decode audio straight out of the container.
That is opaque about WHICH stream gets picked when a container carries several (subtitles,
commentary tracks, multiple camera angles' audio) and produces an essentia-internal error --
not a clean, storable ``error_message`` -- when a container has no audio stream at all
(concert B-roll, silent intro clips). This module makes stream selection and the no-audio
case EXPLICIT and cheap, ahead of essentia ever running:

    1. ``ffprobe`` lists the container's audio streams (never decodes PCM).
    2. No audio streams -> :class:`NoAudioTrackError`; any OTHER ffprobe/ffmpeg failure
       (corrupt container, missing binary, ...) -> :class:`AudioExtractionError`. BOTH are
       clean typed failures the callers (``tasks/functions.py``, ``job_runner.py``) map
       straight to a stored ``error_message`` and a TERMINAL outcome -- never a jam, and
       never a blind retry of a deterministically-doomed file (T-43-08).
    3. Multiple audio streams -> the stream ffprobe reports as the container's DEFAULT
       (``disposition.default == 1``) is selected, falling back to the first (lowest-index)
       stream when nothing is flagged default; every other track is logged at INFO so an
       operator can tell a multi-angle recording picked the expected one without re-running
       anything.
    4. ``ffmpeg -c:a copy`` demuxes ONLY that stream to a scratch file. ``analyze_file``
       then runs completely unchanged against the extracted audio path -- this module never
       imports ``phaze.services.analysis`` or essentia.

**Format scope: probe-based, any container -- operator decision, no extension whitelist
(phaze-3ea41).** An earlier draft of this module gated extraction on a static
``VIDEO_FILE_TYPES`` set derived from ``EXTENSION_MAP``/``FileCategory.VIDEO``, extracting
ONLY for a fixed list of recognized video extensions. The operator decision replacing that:
:func:`extract_audio_track` runs for EVERY file the callers hand it, regardless of
extension -- ``ffprobe`` is the sole authority on whether a given file has an audio stream at
all, not a maintained extension list. This is why :func:`extract_audio_track` takes no
file-type parameter and callers (``tasks/functions.py::process_file``,
``job_runner.py::run``) no longer branch on ``payload.file_type``/``audio_ext`` before
calling it. Two consequences worth being explicit about:

  * ``-c:a copy`` is a lossless stream copy (never a re-encode), so for an ALREADY-bare-audio
    file (mp3, flac, ...) this demux-and-remux round-trip produces bit-identical audio to what
    essentia would have decoded from the original -- "existing audio-file analysis is
    unchanged" is a claim about ANALYSIS OUTPUT, not about this module being skipped for
    audio-typed inputs.
  * Discovery/scan (``services/pipeline.py``'s enqueue-time gate, ``tasks/functions.py``'s own
    ``_ANALYZABLE_FILE_TYPES`` companion/unknown skip) KEEPS its broad, extension-based
    classification -- that gate answers "should this file be enqueued for analysis at all",
    a cheaper and necessarily coarser question than "does this specific file have an audio
    stream", which only ``ffprobe`` can answer authoritatively. The two gates are
    deliberately NOT unified: scan-time classification never opens the file; this module
    always does.

**Disk headroom (``-c:a copy``, never a decode-to-PCM/WAV intermediate).** A multi-hour
concert set decoded to raw PCM would be the multi-GiB-per-hour blowup ``services/analysis.py``
D-07's chunking exists to avoid for the ESSENTIA side; producing that same blowup one step
earlier, as the extraction intermediate, would just move the disk-pressure problem rather than
solve it. ``-c:a copy`` is a stream copy -- no re-encode, no PCM materialization -- so the
scratch file stays close to the SIZE OF THE ORIGINAL COMPRESSED AUDIO TRACK (typically tens to
a few hundred MB for a multi-hour set, and correspondingly tiny for an ordinary track),
regardless of how long the source runs. The Matroska audio container (``.mka``) is the output
wrapper because it accepts arbitrary audio codecs without a forced re-encode, so ``-c:a copy``
is always legal regardless of the source codec. Because extraction now runs unconditionally
(the format-scope decision above), this bound applies uniformly to every file in the
archive, not just recognized video containers -- an ordinary track's scratch file is a few MB
and gone within the same job attempt, so the aggregate scratch footprint at any instant is
still O(concurrent in-flight files), never O(archive size).

**Cloud lane vs local lane (both extract locally, no audio-push plumbing) -- operator
decision (phaze-3ea41).** ``ffmpeg`` is already installed in both the app/agent images AND
the burst-lane job-runner image (stack notes), and ``-c:a copy`` extraction is cheap (bounded
by disk I/O, not audio decode). Piping the ORIGINAL file through the existing push/pull cloud
pipeline unmodified and extracting on whichever side ends up holding the bytes -- rather than
inventing a THIRD artifact (the extracted audio) that would need its own S3 upload/download
leg through ``services/cloud_staging.py`` -- keeps this module a pure, lane-agnostic pre-step
in front of the SAME ``run_analysis_subprocess`` call every lane already makes. Multi-hour
concert videos are exactly what the cloud/burst lane exists to absorb, so this lane needs the
capability at least as much as the local one. Both ``tasks/functions.py::process_file``
(local/SAQ lane) and ``job_runner.py::run`` (cloud one-shot lane) call
:func:`extract_audio_track` on the file THEY ALREADY HAVE locally
(``original_path``/pushed ``scratch_path`` for the SAQ lane, the just-downloaded temp file for
the cloud lane) before handing the (possibly-rewritten) read path to the shared analysis
driver -- symmetric with how ``read_path = payload.scratch_path or payload.original_path``
already makes the analyzer path-agnostic between the two sources.

**Liveness during extraction (phaze-w55w1 discipline, extended).** ``ffmpeg -progress
pipe:1`` emits a periodic ``key=value`` progress block to stdout while it runs -- requested
ONLY when a caller supplies ``heartbeat_cb`` (a no-op caller gets a plain ``DEVNULL`` stdout,
no pipe, no pump); this module treats EVERY such block as a liveness tick and SPAWNS the
caller's ``heartbeat_cb`` (fire-and-forget, never awaited inline by the reading pump) at most
once per ``heartbeat_interval_sec`` (mirroring ``tasks/functions.py::_run_analysis_with_progress``'s
own throttle-and-spawn shape). Callers wire this to whatever THEY use to stay alive under
supervision -- the SAQ lane touches its own ``job.update()`` (the phaze-w55w1 outer net), so a
long extraction on a multi-hour container cannot let the SAQ job heartbeat deadline
(``analysis_job_heartbeat_sec``) expire before the analysis child itself even starts. Because
extraction runs BEFORE ``run_analysis_subprocess`` spawns the analysis child, the INNER stall
watchdog (``services/analysis_exec.py``) is not yet armed during extraction -- there is
nothing there to starve -- so only the OUTER (job-level) liveness signal needs feeding here,
and this module never invents a second inner watchdog for extraction itself. In practice
``-c:a copy`` is a demux, not a decode, so extraction is typically far faster than the file's
own duration and well inside ``analysis_stall_timeout_sec`` (1800s default) even for a
12-hour set -- the heartbeat is defense-in-depth against slow/loaded storage, not the primary
bound.

**Fire-and-forget, not inline (review correction, phaze-3ea41).** An earlier draft AWAITED
``heartbeat_cb`` directly inside the stdout-reading loop. A hung touch (e.g. a broker hiccup
that makes ``job.update()`` hang rather than fail fast) then stopped the pump from draining
``proc.stdout`` -- ffmpeg fills the OS pipe buffer writing further ``-progress`` lines nobody
is reading, blocks on the full pipe, and the whole extraction wedges with NO watchdog able to
save it (see the previous paragraph: the inner stall watchdog is not armed yet). Spawning the
touch as a tracked background task (mirroring ``_run_analysis_with_progress``'s ``_spawn``:
strong ref via a ``pending`` set + a done-callback that discards it) keeps the pump reading
regardless of how slow any single touch is. Deliberately asymmetric on exit: the SUCCESS path
does NOT drain ``pending`` before returning -- ffmpeg already finished, so blocking the return
on a straggling touch would just move the same hang one line later and wedge extraction's OWN
caller instead of merely the read loop, defeating the fix. The EXCEPTIONAL path DOES settle
(cancel + await) ``pending`` -- mirroring ``analysis_exec.py``'s own ``_settle`` -- because
there the caller is walking away entirely (a failure/cancellation), and an orphaned touch left
running would keep firing against a job nobody is watching any more.

This module MUST NOT import phaze.database, phaze.tasks.session, sqlalchemy, or essentia --
both ``tasks/functions.py`` (agent worker) and ``job_runner.py`` (Postgres-free one-shot pod)
import it, and both are subject to the same import-boundary guard as
``tests/shared/core/test_task_split.py`` enforces for their other imports.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
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


async def probe_audio_streams(file_path: str) -> list[dict[str, Any]]:
    """Return ffprobe's audio-stream list for ``file_path`` (possibly empty).

    Reads container/stream headers only -- never decodes PCM, mirroring
    ``services/analysis.py::_probe_duration_sec``'s ``MetadataReader`` discipline one layer
    up the stack. Each entry carries at least ``index`` (the file's absolute stream index,
    directly usable as ffmpeg's ``-map 0:<index>``), ``codec_name``, and a nested
    ``disposition`` dict whose ``default`` key is the track-selection signal (phaze-3ea41
    operator decision: prefer the container's DEFAULT-flagged stream).

    This is ALSO the sole authority for whether ``file_path`` is analyzable at all (the
    format-scope operator decision in this module's docstring) -- an empty return means "no
    audio here", regardless of what the file's extension claims.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "stream=index,codec_name,codec_type,channels,sample_rate:stream_disposition=default",
        "-select_streams",
        "a",
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


def _select_track(streams: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Pick the container's DEFAULT-flagged audio stream, falling back to the first.

    Track-selection operator decision (phaze-3ea41): prefer whichever stream ffprobe reports
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


async def extract_audio_track(
    file_path: str,
    *,
    file_id: str | None = None,
    scratch_dir: str | Path | None = None,
    heartbeat_cb: Callable[[], Awaitable[None]] | None = None,
    heartbeat_interval_sec: float = _DEFAULT_HEARTBEAT_INTERVAL_SEC,
) -> str:
    """Demux the container's DEFAULT-flagged audio stream (or the first, as fallback) to a
    scratch file.

    Returns the extracted-audio scratch path. Raises :class:`NoAudioTrackError` when the
    container has no audio stream, or :class:`AudioExtractionError` for any other
    ffprobe/ffmpeg failure. The caller owns cleanup of the returned path (this function
    never leaves a partial/empty file behind on its OWN failure paths, but the success path
    hands ownership of a real file to the caller) -- see ``tasks/functions.py::process_file``'s
    outer ``finally``, which deletes it on every terminal exit, success or failure alike.

    ``file_id`` is OPTIONAL and purely for log attribution (the "log the other streams'
    existence in the analysis record" operator decision) -- callers that have one (both real
    lanes do) pass it so a multi-track pick is attributable to the file it was made for in the
    structured log stream without threading a DB write through an essentia/DB-free module.
    """
    streams = await probe_audio_streams(file_path)
    if not streams:
        msg = f"no audio stream found in {file_path!r}"
        raise NoAudioTrackError(msg)

    selected, others = _select_track(streams)
    if others:
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

    # WHERE the extracted-audio scratch file lands (review correction, phaze-3ea41 -- naming
    # the exact mechanism here since a prior report described it only in prose elsewhere):
    # ``scratch_dir``, when the caller passes one, else ``tempfile.gettempdir()``. Both real
    # callers DO pass one explicitly (neither relies on this fallback in production):
    #   - tasks/functions.py::process_file passes ``cfg.cloud_scratch_dir`` (AgentSettings) --
    #     the SAME directory ``push_file`` already rsyncs large pushed containers into on a
    #     compute agent, so it is provisioned for exactly this file-size class. ``None`` only
    #     on an agent that never participates in the cloud push pipeline (pure local
    #     fileserver-only role), where tempfile.gettempdir() is the honest fallback -- such an
    #     agent has no OTHER configured large-file scratch location to fall back to instead.
    #   - job_runner.py::run passes ``tmp_path.parent`` explicitly (the SAME directory its own
    #     downloaded original already lives in) -- this pod has no separate configured scratch
    #     setting of its own (``cloud_scratch_dir`` names a DIFFERENT host's rsync landing
    #     zone, not this pod's ephemeral filesystem), so co-locating with the already-larger
    #     download is the only self-consistent choice, and it happens to equal
    #     tempfile.gettempdir() today since that is where tmp_path itself lands.
    # WITHIN dest_dir: a random ``uuid4().hex`` filename (never derived from file_path, which
    # could collide across concurrent extractions of files sharing a directory).
    #
    # CLEANUP GUARANTEE: this function deletes dest_path on every ONE OF ITS OWN failure exits
    # (ffmpeg nonzero exit / empty output below, and the exceptional-exit branch above) --
    # never hands a partial file to a caller. On the SUCCESS return, ownership transfers to the
    # caller: both ``tasks/functions.py::process_file`` and ``job_runner.py::run`` unlink the
    # returned path in their own outer ``finally`` on every terminal exit (success or later
    # failure), unconditionally and independent of the pushed-original scratch copy's
    # retry-preserving cleanup (see each lane's own comment on why that copy differs).
    dest_dir = Path(scratch_dir) if scratch_dir is not None else Path(tempfile.gettempdir())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{uuid.uuid4().hex}{_EXTRACTED_AUDIO_SUFFIX}"

    # Review correction (phaze-3ea41): request -progress pipe:1 -- and open the stdout PIPE at
    # all -- ONLY when a caller actually wants liveness ticks. With no consumer for it, the
    # extra pipe/pump is pure overhead, and ffmpeg writes nothing to stdout without -progress
    # (everything else already routes to stderr via -loglevel error -nostats).
    want_progress = heartbeat_cb is not None
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
        f"0:{selected['index']}",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "copy",
    ]
    if want_progress:
        argv.extend(("-progress", "pipe:1"))
    argv.append(str(dest_path))

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

    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    last_touch = time.monotonic()
    # Review correction (phaze-3ea41): heartbeat_cb is now FIRE-AND-FORGET (spawned, never
    # awaited inline by the pump), mirroring _run_analysis_with_progress's own _spawn pattern.
    # Awaiting it inline made a hung job.update() stop draining proc.stdout entirely; once
    # ffmpeg fills the OS pipe buffer writing -progress lines nobody is reading, ffmpeg itself
    # blocks and the whole extraction wedges with NO watchdog to save it (extraction runs
    # before run_analysis_subprocess arms the inner stall watchdog). Spawning keeps the pump
    # reading regardless of how slow/stuck any single touch is; ``pending`` holds a strong ref
    # so a spawned task is never GC'd mid-flight, and every touch swallows its own errors
    # (best-effort, matching the analysis-side heartbeat's contract).
    pending: set[asyncio.Task[None]] = set()

    async def _safe_heartbeat() -> None:
        # Genuinely can't happen -- _spawn_heartbeat is only ever called from _pump_stdout,
        # which itself returns immediately when want_progress (== heartbeat_cb is not None)
        # is False (bandit B101: no `assert` in production code; a silent no-op return is the
        # honest translation of an invariant this defensive, not a real runtime possibility).
        if heartbeat_cb is None:
            return
        with contextlib.suppress(Exception):
            await heartbeat_cb()

    def _spawn_heartbeat() -> None:
        task = asyncio.get_running_loop().create_task(_safe_heartbeat())
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def _pump_stdout() -> None:
        # -progress pipe:1 (D-08-style liveness, extended to extraction): treat any line as a
        # tick and throttle the caller's heartbeat_cb the same way the analysis side does.
        # A no-op when want_progress is False -- checked explicitly (not merely inferred from
        # proc.stdout is None) so this never depends on the real subprocess machinery's DEVNULL
        # behavior to keep heartbeat_cb unreachable when the caller asked for none.
        nonlocal last_touch
        if not want_progress or proc.stdout is None:
            return
        async for raw in proc.stdout:
            if not raw.strip():
                continue
            now = time.monotonic()
            if (now - last_touch) < heartbeat_interval_sec:
                continue
            last_touch = now
            _spawn_heartbeat()

    async def _pump_stderr() -> None:
        # Mirrors _pump_stdout's own guard above (bandit B101: no `assert` in production code).
        # proc.stderr is None only in the already-raised PIPE-guard branch further up, so this
        # never actually returns early in practice -- it is defense-in-depth, not a real path.
        if proc.stderr is None:
            return
        async for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            stderr_tail.append(line[:_STDERR_LINE_MAX])

    async def _settle_pending() -> None:
        """Cancel + await every still-pending heartbeat task (analysis_exec.py's ``_settle``
        discipline): a cancelled-but-unawaited task is still scheduled and would resume on a
        later loop iteration, touching a job nobody is waiting on any more."""
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        await asyncio.gather(_pump_stdout(), _pump_stderr())
        returncode = await proc.wait()
        # Deliberately NOT draining ``pending`` here (review correction, phaze-3ea41): a
        # touch is fire-and-forget precisely so a slow/hung ``heartbeat_cb`` cannot add ITS
        # OWN latency on top of a now-finished extraction -- ffmpeg is done, the caller gets
        # its result immediately, and any still-running touch keeps going in the background
        # (strong-refed by ``pending`` via each task's own done-callback closure, so it is
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
        await _settle_pending()
        dest_path.unlink(missing_ok=True)
        raise

    if returncode != 0 or not dest_path.exists() or dest_path.stat().st_size == 0:
        dest_path.unlink(missing_ok=True)
        # Review correction (phaze-3ea41): do NOT re-truncate the already-bounded joined tail
        # (<=20 lines * 500 chars) down to 500 chars -- that discarded everything but the
        # EARLIEST lines, exactly backwards: ffmpeg's real error is on the LAST line(s). The
        # per-line cap above already bounds total size sanely (~10 KiB ceiling); the join
        # itself needs no further truncation.
        detail = " | ".join(stderr_tail) or "no stderr output"
        msg = f"ffmpeg audio extraction failed (exit {returncode}) for {file_path!r}: {detail}"
        raise AudioExtractionError(msg)

    return str(dest_path)
