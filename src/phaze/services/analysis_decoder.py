"""Internal chunk-decoder protocol for :mod:`phaze.services.analysis`.

This module owns the risky native-resource boundary of audio analysis.  Its contract is
deliberately narrower than the tier orchestration that consumes it:

* decode every requested window in one streaming fan-out when possible;
* retry a failed gated pass without the gate before degrading to ``EasyLoader``;
* isolate failures per window on that final fallback;
* disconnect every Essentia graph edge, collect proxy cycles, and trim freed pages; and
* emit liveness while a blocking streaming pass or per-window fallback is running.

Essentia dependencies are supplied by ``analysis`` at the compatibility seam.  Besides making
the boundary explicit, that preserves the load-bearing environment-before-TensorFlow import
order and the existing test/instrumentation patches on ``phaze.services.analysis``.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
import platform
import threading
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Sequence
    import logging


Window = tuple[int, float, float]
SkipCallback = Callable[[int, float, float, bool], None]
BeatCallback = Callable[[], None]

_SINK_KEY_PREFIX = "phaze.window."
_CHUNK_GATE_MARGIN_SEC = 1.0


@dataclass(frozen=True)
class StreamingRuntime:
    """Essentia constructors and runner used by one streaming decode."""

    pool: Callable[[], Any]
    mono_loader: Callable[..., Any]
    scale: Callable[..., Any]
    trimmer: Callable[..., Any]
    run: Callable[[Any], None]
    disconnect_network: Callable[[Sequence[Any]], None]


@dataclass(frozen=True)
class DecodeRuntime:
    """Fallback, teardown, logging, and liveness dependencies for a decode ladder."""

    streaming_decode: Callable[..., dict[int, Any]]
    easy_loader: Callable[..., Any]
    release_decode_network: Callable[[], None]
    logger: logging.Logger
    heartbeat_interval_sec: float


def _resolve_malloc_trim(
    *,
    platform_module: Any = platform,
    ctypes_module: Any = ctypes,
) -> Callable[[int], int] | None:
    """Resolve glibc's ``malloc_trim`` once, or ``None`` on other C libraries."""
    try:
        if platform_module.libc_ver()[0] != "glibc":
            return None
        trim: Callable[[int], int] = ctypes_module.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return None
    trim.argtypes = [ctypes_module.c_size_t]  # type: ignore[attr-defined]
    trim.restype = ctypes_module.c_int  # type: ignore[attr-defined]
    return trim


_MALLOC_TRIM: Callable[[int], int] | None = _resolve_malloc_trim()


def _malloc_trim(trim: Callable[[int], int] | None, logger: logging.Logger) -> None:
    """Return freed decode pages to glibc without ever failing an analysis."""
    if trim is None:
        return
    try:
        trim(0)
    except Exception:  # pragma: no cover -- defensive; an optimisation cannot fail a file
        logger.debug("malloc_trim(0) failed; continuing", exc_info=True)


def _disconnect_network(algos: Sequence[Any], logger: logging.Logger) -> None:
    """Sever all discoverable Essentia graph edges, tolerating partial networks."""
    for algo in algos:
        try:
            edges = [
                (connector, target) for connector, targets in list((getattr(algo, "connections", None) or {}).items()) for target in list(targets)
            ]
        except Exception:
            logger.warning("could not read a streaming algorithm's connections; its network will leak", exc_info=True)
            continue
        for connector, target in edges:
            try:
                connector.disconnect(target)
            except Exception:
                logger.warning("failed to disconnect a streaming edge; this chunk's network will leak", exc_info=True)


def _release_decode_network(*, collect: Callable[[], int], trim: Callable[[], None]) -> None:
    """Collect disconnected proxy cycles, then trim the pages they released."""
    collect()
    trim()


def _chunked(windows: list[Window], size: int) -> list[list[Window]]:
    """Split ordered windows into consecutive, bounded chunks."""
    return [windows[i : i + size] for i in range(0, len(windows), size)]


def _stream_source(loader: Any, sample_rate: int, stop_at_sec: float | None, runtime: StreamingRuntime) -> tuple[Any, Any]:
    """Return the fan-out source and optional early-stop gate."""
    if stop_at_sec is None:
        return loader.audio, None
    gate = runtime.trimmer(sampleRate=sample_rate, startTime=0.0, endTime=stop_at_sec + _CHUNK_GATE_MARGIN_SEC)
    loader.audio >> gate.signal
    return gate.signal, gate


def _build_window_branches(
    source: Any,
    sample_rate: int,
    windows: Sequence[Window],
    pool: Any,
    runtime: StreamingRuntime,
) -> list[tuple[Any, Any]]:
    """Connect one isolated Scale/Trimmer branch per requested window."""
    branches: list[tuple[Any, Any]] = []
    for idx, start, end in windows:
        scale = runtime.scale(factor=1.0)
        trimmer = runtime.trimmer(sampleRate=sample_rate, startTime=start, endTime=end)
        source >> scale.signal
        scale.signal >> trimmer.signal
        trimmer.signal >> (pool, f"{_SINK_KEY_PREFIX}{idx}")
        branches.append((scale, trimmer))
    return branches


def _extract_window_buffers(pool: Any, windows: Sequence[Window]) -> dict[int, Any]:
    """Copy produced buffers out of the Pool and remove each Pool copy immediately."""
    produced = set(pool.descriptorNames())
    decoded: dict[int, Any] = {}
    for idx, _start, _end in windows:
        key = f"{_SINK_KEY_PREFIX}{idx}"
        if key not in produced:
            continue
        buf = pool[key]
        pool.remove(key)
        if len(buf) > 0:
            decoded[idx] = buf
    return decoded


def _decode_windows_streaming(
    file_path: str,
    sample_rate: int,
    windows: Sequence[Window],
    *,
    runtime: StreamingRuntime,
    stop_at_sec: float | None = None,
) -> dict[int, Any]:
    """Decode every requested window in one streaming fan-out pass.

    The identity ``Scale`` on every branch prevents an early Trimmer from stopping sibling
    windows.  A separate head Trimmer supplies the optional chunk gate.  Every graph edge is
    disconnected in ``finally`` because dropping Python proxies does not release Essentia's
    C++ buffers or the implicit ``PoolStorage`` sink.
    """
    pool = runtime.pool()
    loader = runtime.mono_loader(filename=file_path, sampleRate=sample_rate)
    branches: list[tuple[Any, Any]] = []
    gate: Any = None
    try:
        source, gate = _stream_source(loader, sample_rate, stop_at_sec, runtime)
        branches = _build_window_branches(source, sample_rate, windows, pool, runtime)
        runtime.run(loader)
        return _extract_window_buffers(pool, windows)
    finally:
        runtime.disconnect_network([*(algo for branch in branches for algo in branch), gate, loader])
        branches.clear()
        del loader, pool, gate


def _call_streaming(
    file_path: str,
    sample_rate: int,
    windows: Sequence[Window],
    stop_at_sec: float | None,
    runtime: DecodeRuntime,
) -> dict[int, Any]:
    """Call the streaming seam while preserving its established ungated call shape."""
    if stop_at_sec is None:
        return runtime.streaming_decode(file_path, sample_rate, windows)
    return runtime.streaming_decode(file_path, sample_rate, windows, stop_at_sec=stop_at_sec)


def _streaming_with_watchdog(
    file_path: str,
    sample_rate: int,
    windows: Sequence[Window],
    stop_at_sec: float | None,
    on_beat: BeatCallback,
    runtime: DecodeRuntime,
    *,
    watchdog_enabled: bool,
) -> dict[int, Any]:
    """Run one blocking streaming attempt with optional periodic liveness beats."""
    if not watchdog_enabled:
        return _call_streaming(file_path, sample_rate, windows, stop_at_sec, runtime)

    stopped = threading.Event()

    def _watch() -> None:
        while not stopped.wait(runtime.heartbeat_interval_sec):
            try:
                on_beat()
            except Exception:
                runtime.logger.warning("decode heartbeat callback failed; continuing", exc_info=True)

    watchdog = threading.Thread(target=_watch, name="phaze-decode-heartbeat", daemon=True)
    watchdog.start()
    try:
        return _call_streaming(file_path, sample_rate, windows, stop_at_sec, runtime)
    finally:
        stopped.set()
        watchdog.join()


def _try_streaming_ladder(
    file_path: str,
    sample_rate: int,
    windows: Sequence[Window],
    stop_at_sec: float | None,
    on_beat: BeatCallback,
    runtime: DecodeRuntime,
    *,
    watchdog_enabled: bool,
) -> dict[int, Any] | None:
    """Attempt gated then ungated streaming, returning ``None`` for full fallback."""
    if stop_at_sec is not None:
        try:
            return _streaming_with_watchdog(file_path, sample_rate, windows, stop_at_sec, on_beat, runtime, watchdog_enabled=watchdog_enabled)
        except Exception:
            runtime.logger.warning("gated streaming decode failed at %d Hz; retrying ungated", sample_rate, exc_info=True)
            on_beat()
    try:
        return _streaming_with_watchdog(file_path, sample_rate, windows, None, on_beat, runtime, watchdog_enabled=watchdog_enabled)
    except Exception:
        runtime.logger.warning("streaming decode pass failed at %d Hz; falling back to per-window EasyLoader", sample_rate, exc_info=True)
        return None


def _decode_per_window(
    file_path: str,
    sample_rate: int,
    windows: Sequence[Window],
    on_skip: SkipCallback,
    on_beat: BeatCallback,
    runtime: DecodeRuntime,
) -> dict[int, Any]:
    """Decode windows independently, preserving per-window failure isolation."""
    decoded: dict[int, Any] = {}
    for idx, start, end in windows:
        try:
            decoded[idx] = runtime.easy_loader(filename=file_path, sampleRate=sample_rate, startTime=start, endTime=end)()
        except Exception:
            on_skip(idx, start, end, True)
        on_beat()
    return decoded


def _report_missing_windows(decoded: dict[int, Any], windows: Sequence[Window], on_skip: SkipCallback) -> None:
    """Report streaming sinks that produced no audio without inventing a traceback."""
    for idx, start, end in windows:
        if idx not in decoded:
            on_skip(idx, start, end, False)


def _decode_windows(
    file_path: str,
    sample_rate: int,
    windows: Sequence[Window],
    on_skip: SkipCallback,
    *,
    runtime: DecodeRuntime,
    stop_at_sec: float | None = None,
    on_beat: BeatCallback,
    watchdog_enabled: bool,
) -> dict[int, Any]:
    """Run the gated -> ungated -> per-window decode ladder for one chunk."""
    decoded = _try_streaming_ladder(
        file_path,
        sample_rate,
        windows,
        stop_at_sec,
        on_beat,
        runtime,
        watchdog_enabled=watchdog_enabled,
    )
    if decoded is None:
        decoded = _decode_per_window(file_path, sample_rate, windows, on_skip, on_beat, runtime)
    else:
        _report_missing_windows(decoded, windows, on_skip)
    runtime.release_decode_network()
    return decoded
