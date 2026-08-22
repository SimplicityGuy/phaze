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
from dataclasses import dataclass, field
import platform
import threading
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Sequence
    import logging


Window = tuple[int, float, float]
SkipCallback = Callable[[int, float, float, bool], None]
BeatCallback = Callable[[], None]

# phaze-5lop: the streaming decode's sink-key namespace inside its per-tier essentia
# ``Pool``. Prefixed and numbered by ORIGINAL window index (never renumbered), so a
# chunk's window set maps back to whole-file window indices without a side table.
_SINK_KEY_PREFIX = "phaze.window."

# Slack added to a chunk decode's early-stop gate (see `_decode_windows_streaming`). The gate
# exists to stop the non-seeking loader once a chunk's last window has been read; a second of
# over-read costs nothing and removes any chance that the stop lands a token early and truncates
# that last window.
_CHUNK_GATE_MARGIN_SEC = 1.0


@dataclass
class _PartialNetwork:
    """Every streaming algorithm built so far, reachable even if the build raises mid-way.

    Registration is EAGER: an algorithm joins the list the moment it is constructed and
    BEFORE any edge is wired from or to it.  That ordering is the whole point of the class
    and is load-bearing (D-09, phaze-u1n7j).  Construction and wiring both run under exactly
    the memory pressure this module exists to survive, so window ``k``'s constructor -- or
    either of its ``>>`` calls -- can raise with windows ``0..k-1`` already connected.  If the
    caller's ``finally`` cannot see those algorithms, dropping their Python proxies leaves
    essentia's C++ edges and the implicit ``PoolStorage`` sink allocated, which is the
    duration-linear peak-RSS growth that OOMKilled the pod.

    Teardown reads the list in reverse construction order, so the branches are severed first
    and the shared head of the fan-out (gate, then loader) last -- the order D-09 documents.
    Teardown was measured flat in either direction, so this is for readability.
    """

    algos: list[Any] = field(default_factory=list)

    def register(self, algo: Any) -> Any:
        """Record ``algo`` as connectable, returning it so callers can wire it in one step."""
        self.algos.append(algo)
        return algo

    def connected(self) -> list[Any]:
        """Everything built so far, newest first -- safe to call from a ``finally``."""
        return list(reversed(self.algos))

    def clear(self) -> None:
        """Drop the proxy references once the edges are severed."""
        self.algos.clear()


@dataclass(frozen=True)
class DecodeTarget:
    """The file, sample rate, and window set one decode call targets.

    Primitive-obsession cleanup (phaze-bk9el.3): ``file_path``, ``sample_rate`` and
    ``windows`` travel together through every rung of the decode ladder below --
    "decode which windows, of which file, at what rate" is one question, not three
    independent parameters that happen to share an argument list. Bundling them stops a
    future call site from swapping two of the three (e.g. passing one call's ``windows``
    against another call's ``sample_rate``) without a type error.

    :func:`_decode_windows` and :func:`_decode_windows_streaming` keep their historical
    ``(file_path, sample_rate, windows, ...)`` signature -- ``services/analysis.py`` calls
    both positionally and is out of this bead's scope -- but build one ``DecodeTarget`` at
    the top and thread it through every internal helper instead.

    The parameters left OUT of this type are independent on purpose, not overlooked:
    ``stop_at_sec`` is a per-attempt gate value that changes across retries of the SAME
    target (gated, then ungated); ``on_skip``/``on_beat`` are callbacks, not decode
    identity; ``watchdog_enabled`` toggles a concern (liveness) orthogonal to what is being
    decoded; and ``runtime``/``DecodeRuntime`` are dependency bundles already grouped by
    their own dataclasses above.
    """

    file_path: str
    sample_rate: int
    windows: Sequence[Window]


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
    except Exception:  # defensive; an optimisation must never fail an already-decoded chunk -- NOT a D-09 guard (bk9el.29)
        # phaze-wbxi2 removed a `# pragma: no cover` from this line. It was already stale -- `test_malloc_trim_never_raises`
        # injected an OSError here -- and it is now exercised for nine exception types by
        # `test_the_trim_never_fails_a_chunk_for_any_exception_type`. An excluded line whose behaviour IS asserted reports a
        # gap that does not exist, which is the same class of untrue claim this bead exists to close.
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


def _stream_source(loader: Any, sample_rate: int, stop_at_sec: float | None, runtime: StreamingRuntime, network: _PartialNetwork) -> Any:
    """Return the fan-out source, registering the optional early-stop gate before wiring it."""
    if stop_at_sec is None:
        return loader.audio
    gate = network.register(runtime.trimmer(sampleRate=sample_rate, startTime=0.0, endTime=stop_at_sec + _CHUNK_GATE_MARGIN_SEC))
    loader.audio >> gate.signal
    return gate.signal


def _build_window_branches(
    source: Any,
    target: DecodeTarget,
    pool: Any,
    runtime: StreamingRuntime,
    network: _PartialNetwork,
) -> None:
    """Connect one isolated Scale/Trimmer branch per requested window.

    Each algorithm is registered on ``network`` before it is wired, so a raise anywhere in this
    loop still leaves every branch built so far -- including the half-wired one -- visible to
    the caller's teardown.  Nothing is returned: the network IS the accumulator.
    """
    for idx, start, end in target.windows:
        scale = network.register(runtime.scale(factor=1.0))
        trimmer = network.register(runtime.trimmer(sampleRate=target.sample_rate, startTime=start, endTime=end))
        source >> scale.signal
        scale.signal >> trimmer.signal
        trimmer.signal >> (pool, f"{_SINK_KEY_PREFIX}{idx}")


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

    The build writes into :class:`_PartialNetwork` as it goes rather than returning a finished
    list, so a raise part-way through the fan-out -- the ordinary failure path, not an exotic
    one -- still hands the ``finally`` everything that was constructed (D-09, phaze-u1n7j).
    """
    target = DecodeTarget(file_path, sample_rate, windows)
    pool = runtime.pool()
    network = _PartialNetwork()
    loader = network.register(runtime.mono_loader(filename=target.file_path, sampleRate=target.sample_rate))
    try:
        source = _stream_source(loader, target.sample_rate, stop_at_sec, runtime, network)
        _build_window_branches(source, target, pool, runtime, network)
        runtime.run(loader)
        return _extract_window_buffers(pool, target.windows)
    finally:
        runtime.disconnect_network(network.connected())
        network.clear()
        del loader, pool


def _call_streaming(
    target: DecodeTarget,
    stop_at_sec: float | None,
    runtime: DecodeRuntime,
) -> dict[int, Any]:
    """Call the streaming seam while preserving its established ungated call shape."""
    if stop_at_sec is None:
        return runtime.streaming_decode(target.file_path, target.sample_rate, target.windows)
    return runtime.streaming_decode(target.file_path, target.sample_rate, target.windows, stop_at_sec=stop_at_sec)


def _streaming_with_watchdog(
    target: DecodeTarget,
    stop_at_sec: float | None,
    on_beat: BeatCallback,
    runtime: DecodeRuntime,
    *,
    watchdog_enabled: bool,
) -> dict[int, Any]:
    """Run one blocking streaming attempt with optional periodic liveness beats."""
    if not watchdog_enabled:
        return _call_streaming(target, stop_at_sec, runtime)

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
        return _call_streaming(target, stop_at_sec, runtime)
    finally:
        stopped.set()
        watchdog.join()


def _try_streaming_ladder(
    target: DecodeTarget,
    stop_at_sec: float | None,
    on_beat: BeatCallback,
    runtime: DecodeRuntime,
    *,
    watchdog_enabled: bool,
) -> dict[int, Any] | None:
    """Attempt gated then ungated streaming, returning ``None`` for full fallback."""
    if stop_at_sec is not None:
        try:
            return _streaming_with_watchdog(target, stop_at_sec, on_beat, runtime, watchdog_enabled=watchdog_enabled)
        except Exception:
            runtime.logger.warning("gated streaming decode failed at %d Hz; retrying ungated", target.sample_rate, exc_info=True)
            on_beat()
    try:
        return _streaming_with_watchdog(target, None, on_beat, runtime, watchdog_enabled=watchdog_enabled)
    except Exception:
        runtime.logger.warning("streaming decode pass failed at %d Hz; falling back to per-window EasyLoader", target.sample_rate, exc_info=True)
        return None


def _decode_per_window(
    target: DecodeTarget,
    on_skip: SkipCallback,
    on_beat: BeatCallback,
    runtime: DecodeRuntime,
) -> dict[int, Any]:
    """Decode windows independently, preserving per-window failure isolation."""
    decoded: dict[int, Any] = {}
    for idx, start, end in target.windows:
        try:
            decoded[idx] = runtime.easy_loader(filename=target.file_path, sampleRate=target.sample_rate, startTime=start, endTime=end)()
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
    target = DecodeTarget(file_path, sample_rate, windows)
    decoded = _try_streaming_ladder(
        target,
        stop_at_sec,
        on_beat,
        runtime,
        watchdog_enabled=watchdog_enabled,
    )
    if decoded is None:
        decoded = _decode_per_window(target, on_skip, on_beat, runtime)
    else:
        _report_missing_windows(decoded, target.windows, on_skip)
    runtime.release_decode_network()
    return decoded
