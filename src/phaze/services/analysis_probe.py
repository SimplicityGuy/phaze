"""D-10 duration probe: read a file's audio duration via ``ffprobe``, never by decoding.

Extracted VERBATIM from ``services/analysis.py`` (phaze-bk9el.15) -- the probe, its JSON
reader and its fatal error type. No essentia import and no dependency on the analysis
pipeline; nothing here touches D-07, D-08 or D-09.

``services/analysis.py`` re-exports :class:`AnalysisProbeError` and
:func:`_probe_duration_sec`, so ``analyze_file`` still resolves the probe through that
module's globals and ``patch("phaze.services.analysis._probe_duration_sec", ...)`` -- the
patch target 20+ tests use -- keeps working unchanged. ``subprocess`` is patched at
``phaze.services.analysis_probe.subprocess.run``, i.e. here, where the call now lives.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404  # ffprobe duration probe (D-10); fixed argv, no shell
from typing import Any


# Bound on the ffprobe stderr excerpt carried in an AnalysisProbeError message. The error text
# is stored (truncated again by the callers' own _ERROR_DETAIL_MAX) and read by an operator --
# enough for ffprobe's actual complaint, never an unbounded string.
_PROBE_STDERR_MAX = 500


class AnalysisProbeError(RuntimeError):
    """``ffprobe`` could not read a usable duration for the file (phaze-l832u, D-10).

    Raised by :func:`_probe_duration_sec` when ffprobe cannot be run, exits nonzero, emits
    unparseable output, or reports no positive duration for any audio stream or for the
    container. FATAL by design and NOT caught anywhere below :func:`analyze_file`: this probe is
    the reason a mis-suffixed or undecodable download fails loudly with a stored
    ``error_message`` instead of recording a NULL-everything "success". Distinct from
    :class:`AnalysisDecodeError` (headers readable, every WINDOW undecodable) so the stored
    failure text says which of the two happened without re-running anything.
    """


def _probe_duration_sec(file_path: str) -> float:
    """Return total audio duration in seconds WITHOUT materializing PCM, via ``ffprobe``.

    D-10 DECISION RECORD -- ffprobe, not ``es.MetadataReader`` (phaze-l832u)
    ----------------------------------------------------------------------
    This probe used to read ``es.MetadataReader`` output index 8. MetadataReader is TagLib,
    and **TagLib cannot read a duration out of Matroska**: it returns ``0``. That was harmless
    while every file reaching :func:`analyze_file` was the archive's own mp3/m4a/ogg, and fatal
    the moment phaze-3ea41 put an unconditional pre-analysis remux to ``.mka`` in front of it --
    ``total_sec == 0`` makes :func:`_iter_windows` yield nothing, both ``*_total`` counts land on
    0, and the zero-window guard fails the file after "analyzing" it in ~4 s. Measured inside the
    deployed 2026.8.3 agent image, on one file, three ways::

        original .mp3   MetadataReader duration = 90
        remuxed  .mka   MetadataReader duration = 0
        remuxed  .mka   ffprobe        duration = 90.044000

    Only the metadata probe was ever broken -- essentia DECODES the ``.mka`` correctly (the same
    measurement got 2/2 windows out of it). So the fix is the probe, not the container: ffprobe
    reads a duration out of every container the pipeline can produce, including the ``.mka``
    extraction intermediate a VIDEO container still legitimately goes through (which is why
    merely gating the remux would have left phaze-3ea41's actual feature broken in exactly the
    same way -- see the epic's design note).

    ffprobe is the sanctioned tool here (never the abandoned ``ffmpeg-python``) and is already
    how ``services/video_audio.py::probe_container_streams`` talks to the same binary; this is the
    established invocation shape, not a third style. Like MetadataReader it reads container and
    stream headers only -- it never decodes PCM.

    The longest AUDIO STREAM's own ``duration`` is preferred, with the container-level
    ``format.duration`` as the fallback -- see :func:`_duration_from_ffprobe_payload` for why
    that order and not the other. A failure here stays FATAL and propagates as
    :class:`AnalysisProbeError` -- this function is the reason a mis-suffixed or undecodable
    download fails loudly instead of recording a NULL-everything success.

    Deliberately UNBOUNDED (no ``timeout=``), matching the MetadataReader call it replaces and
    ``video_audio.py``'s own ffprobe: a header read is not a wall clock to bound (D-01 /
    phaze-1b39), and a genuinely wedged probe is already covered by the analysis child's
    progress-based stall watchdog (D-08) in the lane above.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration:stream=duration",
        "-select_streams",
        "a",
        file_path,
    ]
    try:
        # Fixed argv, never a shell, no interpolation (services/analysis_sizing.py convention).
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603  # nosec B603
    except OSError as exc:
        msg = f"ffprobe could not be run to probe the duration of {file_path!r}: {exc}"
        raise AnalysisProbeError(msg) from exc
    if proc.returncode != 0:
        msg = f"ffprobe failed (exit {proc.returncode}) probing the duration of {file_path!r}: {proc.stderr.strip()[:_PROBE_STDERR_MAX]}"
        raise AnalysisProbeError(msg)
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        msg = f"ffprobe produced non-JSON output probing the duration of {file_path!r}"
        raise AnalysisProbeError(msg) from exc
    duration = _duration_from_ffprobe_payload(payload)
    if duration is None:
        msg = f"ffprobe reported no readable audio duration for {file_path!r}"
        raise AnalysisProbeError(msg)
    return duration


def _duration_from_ffprobe_payload(payload: Any) -> float | None:
    """Pull a duration (seconds) out of ffprobe's JSON, or ``None`` when it carries none.

    The caller's argv selects AUDIO streams only, so ``streams[].duration`` is the length of
    the material :func:`analyze_file` will actually window -- preferred over the container-level
    ``format.duration``, which on a container carrying anything besides that audio (a video
    track running past the end of the audio, a trailing chapter/attachment) describes the
    CONTAINER's span rather than the audio's, and would hand ``_iter_windows`` windows that lie
    past the last sample. ``format.duration`` is the fallback because Matroska -- the very
    container the extraction step produces -- routinely records its duration at container level
    and reports ``N/A`` per stream, which is precisely the case that must not come back 0.

    ffprobe writes the literal ``"N/A"`` for an unknown duration: any entry that fails to parse,
    or is not strictly positive, is skipped rather than allowed to become a silent 0.0 (the
    exact shape this whole function exists to stop returning). ``None`` means every candidate
    was unusable, which the caller turns into a hard failure.
    """

    def _longest(values: list[Any]) -> float | None:
        best: float | None = None
        for raw in values:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and (best is None or value > best):
                best = value
        return best

    if not isinstance(payload, dict):
        return None
    streams = payload.get("streams")
    stream_durations = [s.get("duration") for s in streams if isinstance(s, dict)] if isinstance(streams, list) else []
    from_streams = _longest(stream_durations)
    if from_streams is not None:
        return from_streams
    fmt = payload.get("format")
    return _longest([fmt.get("duration")]) if isinstance(fmt, dict) else None
