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

**OPERATOR DECISIONS OF RECORD (phaze-3ea41, 2026-08-12).** This block is the durable record
for this module: three of its design choices are the OPERATOR's and the rest are the
IMPLEMENTER's, and every other site in the tree that cites an operator decision for phaze-3ea41
points HERE rather than restating the question. All three were put in a single question at
2026-08-12T00:30:59Z and answered at 2026-08-12T00:31:36Z (Claude Code session 60b8bf47).
Recovered from that primary record on 2026-08-20 by phaze-d2hgv -- which is why ADR-0012 section
5, swept before the recovery, lists them as untraceable and why its section 7 R1 prescribes
stripping them. Read this block, not that prescription.

  1. **Which VIDEO containers the analyze lane accepts.** Question as put: *"phaze-3ea41: which video
     containers should the analyze lane accept?"* Answer, quoted: *"Probe-based, any
     container"*, chosen over *"Common concert formats"* and *"mkv + avi only"*. **Scope limit,
     and it is the whole of ADR-0012's Finding 1:** the question is about VIDEO containers, so
     the answer licenses deleting the video-extension whitelist and making ``ffprobe`` the
     authority on which containers are accepted. It does not license offering bare audio files
     to extraction as well. That was the implementer's generalization -- see "Format scope"
     below.
  2. **Where extraction runs.** Question as put: *"phaze-3ea41: where should audio extraction
     run?"* Answer, quoted: *"Both lanes"*. Carried by "Cloud lane vs local lane" below, and
     that is the WHOLE of it. The dispatch that accompanied the question left the adjacent
     disk-headroom question to the implementer in as many words -- *"Disk-headroom handling for
     long sets remains yours to design"* -- and liveness during extraction was never put to the
     operator at all. So "Disk headroom" and "Liveness during extraction" below are the
     implementer's decisions and must not be re-stamped as the operator's.
  3. **Which audio track gets analyzed.** Question as put: *"phaze-3ea41: when a container
     carries multiple audio tracks, which one gets analyzed?"* What the operator AUTHORED is the
     selection of the option LABELLED *"Default/first track"*. Beside that label stood a
     DESCRIPTION, written by the dispatcher and not by the operator, reading *"Take the
     container's default-flagged audio stream (falling back to the first), log the others'
     existence in the analysis record."* The two are cited separately below because they do not
     carry the same authority: default-then-first is what the operator selected BY NAME on
     2026-08-12, whereas
     logging the other streams is ACCOMPANYING CONTEXT that the operator accepted by selecting
     that option -- it was never put as its own question and the operator never wrote the
     sentence. Treat it as decided, and as the weaker of the two registers. Neither fixes the
     logging MECHANISM -- that it is a structured INFO line keyed by ``file_id`` rather than a
     row on the analysis record is the implementer's reading of "the analysis record", and is
     open to challenge as such.

**Format scope: probe-based container acceptance, no extension whitelist -- operator decision
2026-08-12 (phaze-3ea41, record 1 above).** An earlier draft of this module gated extraction on a
static ``VIDEO_FILE_TYPES`` set derived from ``EXTENSION_MAP``/``FileCategory.VIDEO``,
extracting ONLY for a fixed list of recognized video extensions. ``ffprobe`` replaced that
whitelist and is the sole authority on whether a given file has an audio stream at all, not a
maintained extension list. This is why :func:`extract_audio_track` takes no file-type parameter
and callers (``tasks/functions.py::process_file``, ``job_runner.py::run``) no longer branch on
``payload.file_type``/``audio_ext`` before calling it.

**NOT an operator decision: widening that from video containers to EVERY file.** The
operator decision of 2026-08-12 covered video containers (phaze-3ea41, record 1). phaze-3ea41
shipped ":func:`extract_audio_track` runs for EVERY file the callers hand it, regardless of
extension" -- remuxing bare audio through ffmpeg into ``.mka`` as well -- and this docstring
recorded the widening as the operator's. It was the IMPLEMENTER's: record 1 was asked and
answered about video containers only, and the two propositions were fused. The distinction is
not academic. The unconditional remux is what put a Matroska intermediate in front of the whole
archive and stopped the pipeline dead for 11.5 hours; D-10 below is the narrowing that removed
it, so the CODE no longer behaves this way. This paragraph exists so the widening cannot come
back wearing an authority it never had. Two consequences of probe-based scope worth being
explicit about:

  * ``-c:a copy`` is a lossless stream copy (never a re-encode), so where a remux DOES happen
    it produces bit-identical audio to what essentia would have decoded from the original --
    "existing audio-file analysis is unchanged" is a claim about ANALYSIS OUTPUT.
  * Discovery/scan (``services/pipeline.py``'s enqueue-time gate, ``tasks/functions.py``'s own
    ``_ANALYZABLE_FILE_TYPES`` companion/unknown skip) KEEPS its broad, extension-based
    classification -- that gate answers "should this file be enqueued for analysis at all",
    a cheaper and necessarily coarser question than "does this specific file have an audio
    stream", which only ``ffprobe`` can answer authoritatively. The two gates are
    deliberately NOT unified: scan-time classification never opens the file; this module
    always does.

**D-10 NARROWING (phaze-l832u): every file is still PROBED, but a file that is already plain
audio is no longer REMUXED.** phaze-3ea41's decision above was "extraction runs on every file";
2026.8.3 shipped it alongside phaze-w55w1's exhaustive analysis and the pair stopped the
pipeline dead for 11.5 hours -- the ``.mka`` intermediate is Matroska, and ``es.MetadataReader``
returns duration 0 for Matroska ON THE DEPLOYED PLATFORM, so every file produced zero natural
windows. (This was originally recorded as "MetadataReader is TagLib and TagLib cannot read
Matroska". The CONCLUSION is right and measured; that MECHANISM was never verified and is not
what the evidence shows -- see :func:`_probe_duration_sec`'s D-10 record in
``services/analysis_probe.py`` for what was actually measured, phaze-gppj2.)

The CORRECTNESS half of the fix is in ``services/analysis.py`` (D-10: probe duration
with ffprobe, which reads the ``.mka`` correctly -- gating the remux alone would have left
phaze-3ea41's actual feature, video containers, broken in exactly the same way, since a video's
extracted ``.mka`` hits the identical gap). This module carries the COST half, and only that:
:func:`_is_already_plain_audio` sends a single-audio-stream container straight to the analyzer
instead of copying it through ffmpeg first, removing a full remux of every audio file in an
11,428-file corpus from the hot path. ``ffprobe`` remains the sole authority -- the predicate
reads the container's real stream list, never the suffix or ``file_type``, so the extension
whitelist phaze-3ea41 removed stays removed. What changes for CALLERS is the return type: see
:class:`AudioSource`, which exists because a skip branch cannot express itself as a bare
"scratch path you own and delete" without deleting the operator's archive original.

**Disk headroom (``-c:a copy``, never a decode-to-PCM/WAV intermediate) -- the
IMPLEMENTER's decision, explicitly left to the developer (record 2 above).** A multi-hour
concert set decoded to raw PCM would be the multi-GiB-per-hour blowup ``services/analysis.py``
D-07's chunking exists to avoid for the ESSENTIA side; producing that same blowup one step
earlier, as the extraction intermediate, would just move the disk-pressure problem rather than
solve it. ``-c:a copy`` is a stream copy -- no re-encode, no PCM materialization -- so the
scratch file stays close to the SIZE OF THE ORIGINAL COMPRESSED AUDIO TRACK (typically tens to
a few hundred MB for a multi-hour set, and correspondingly tiny for an ordinary track),
regardless of how long the source runs. The Matroska audio container (``.mka``) is the output
wrapper because it accepts arbitrary audio codecs without a forced re-encode, so ``-c:a copy``
is always legal regardless of the source codec. This bound applies to every file that is
actually extracted -- any container that is not already plain audio (the D-10 narrowing above),
not just recognized video extensions -- and such a scratch file is gone within the same job
attempt, so the aggregate scratch footprint at any instant is O(concurrent in-flight files),
never O(archive size). A file that skips extraction contributes nothing to it at all.

**Cloud lane vs local lane (both extract locally, no audio-push plumbing) --
operator decision, answered "Both lanes" 2026-08-12 (phaze-3ea41; question as put in
record 2 above).** ``ffmpeg`` is already installed in both the app/agent images AND the burst-lane
job-runner image (stack notes), and ``-c:a copy`` extraction is cheap (bounded
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

**Liveness during extraction (phaze-w55w1 discipline, extended) -- the IMPLEMENTER's
decision; never put to the operator (record 2 above).** ``ffmpeg -progress
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
