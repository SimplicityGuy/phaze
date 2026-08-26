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

Every ``except Exception`` in this module is BROAD BY REVIEW, not by omission (phaze-bk9el.29)
-------------------------------------------------------------------------------------------
All seven were reviewed individually and all seven were kept broad; each carries an inline
comment naming the specific failure its breadth prevents and the test that exercises it.  Two
things about that review are worth having here rather than only at the call sites:

* **Only two of the seven are D-09 leak guards** -- the pair inside the teardown ladder,
  :func:`_readable_edges_of` and :func:`_sever_algorithm_edges`.  (Both sat inside
  :func:`_disconnect_network` until phaze-48ghg.5 named the teardown's three tiers; the guards
  themselves are untouched, and the tier they each defend is now in their names.)  The other five
  prevent different, individually named failures (an optimisation failing an already-decoded
  chunk; a dead liveness thread; the decode ladder refusing to degrade -- one rung each; a single
  window taking a whole chunk with it).  Labelling all seven "D-09" would be tidier and wrong,
  and a wrong attribution here reads as authoritative to whoever audits this next.
* **Narrowing any of them is currently unverifiable, and that is measured rather than asserted.**
  Every failure this module's suite injects is a ``RuntimeError`` (eleven sites) or an ``OSError``
  (one).  A narrowing to those types would therefore leave the entire suite green while changing
  production behaviour for every other exception type -- ADR-0012 rule 3's "verified against a
  proxy that structurally cannot exhibit the failure", exactly.  Closing that gap means widening
  the injected type set first; it is not a comment-only change and is not this bead's scope.

The teardown reads as three tiers, one function each (phaze-48ghg.5)
--------------------------------------------------------------------
:func:`_disconnect_network` used to carry this file's largest single health finding
(nested_complexity, nests 4, cognitive 16, deduction 0.938 -- nearly twice the entire seven-site
error_handling bucket) because all three tiers of the sweep were compounded into one body.  The
defensiveness was never the problem and is unchanged; the *unnamed* nesting was.  It is now:

Three nested scopes, largest first, each function handling exactly one of them.  Each also hosts
the guard whose blast radius is the scope BELOW it, which is the property that makes a failure
cost one unit and never the sweep:

* :func:`_disconnect_network` -- the whole NETWORK.  Walks ``algos`` front to back, which is the
  reverse-construction order :meth:`_PartialNetwork.connected` hands it, so the branches are
  severed first and the shared head of the fan-out (gate, then loader) last.  That order is what
  D-09 documents, and it is now the only thing this function does.
* :func:`_sever_algorithm_edges` -- one ALGORITHM's edges.  Hosts the per-EDGE guard, so one edge
  essentia refuses to sever leaks its own network and its siblings are still swept.
* :func:`_readable_edges_of` -- one algorithm's EDGE LIST.  Hosts the per-ALGORITHM guard, so an
  unreadable ``connections`` map costs exactly that algorithm's network and the walk continues.

Both D-09 guards moved with the tier they defend; neither was narrowed, broadened, relabelled or
merged, and both remain guarded by the two tests CLAUDE.md records as unsatisfiable by a mocked
essentia (``test_repeated_gated_chunk_decodes_do_not_grow_peak_rss`` and
``test_the_chunk_decode_leaves_no_connected_network_behind``).
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

    phaze-bk9el.3 built this at the top of :func:`_decode_windows` and
        :func:`_decode_windows_streaming` and threaded it inward, leaving both of those with the
        historical ``(file_path, sample_rate, windows, ...)`` argument list because
        ``services/analysis.py`` was out of that bead's scope.  phaze-48ghg.5 finished the job: both
        now TAKE a ``DecodeTarget``, and the two thin wrappers in ``services/analysis.py`` -- which
        are the module's only src callers, and which keep their own historical signature because the
        suite calls THEM positionally -- build it.  The bundle is now enforced across the seam
        rather than only behind it.

        The parameters left OUT of this type are independent on purpose, not overlooked:
        ``stop_at_sec`` is a per-attempt gate value that changes across retries of the SAME
        target (gated, then ungated); ``on_skip``/``on_beat``/``watchdog_enabled`` are how the decode
        reports back rather than what it decodes, and are bundled separately as
        :class:`DecodeSignals`; and ``runtime``/``DecodeRuntime`` are dependency bundles already
        grouped by their own dataclasses below.
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
class _ChunkFanOut:
    """The single streaming graph one chunk decode fans out, plus what it takes to extend it.

    Primitive-obsession cleanup (phaze-48ghg.5).  Three things that only ever travel together
    and are only ever meaningful together: the essentia constructors each branch is built from,
    the ``Pool`` every branch sinks into, and the eager registry that keeps a HALF-built network
    reachable from the decode's ``finally`` (D-09).  Threading them as three parameters made it
    possible to register an algorithm onto one decode's network while wiring it into another
    decode's ``Pool`` -- a mistake with no type error and a D-09 leak as its symptom.  Passing
    the fan-out makes it unrepresentable.

    ``register`` is delegated rather than reached through so the D-09 ordering rule reads at the
    call site: an algorithm joins the network the moment it is constructed and BEFORE any edge is
    wired from or to it.
    """

    runtime: StreamingRuntime
    pool: Any
    network: _PartialNetwork

    def register(self, algo: Any) -> Any:
        """Record ``algo`` on the partial network BEFORE any edge is wired to it (D-09)."""
        return self.network.register(algo)


@dataclass(frozen=True)
class DecodeRuntime:
    """Fallback, teardown, logging, and liveness dependencies for a decode ladder."""

    streaming_decode: Callable[..., dict[int, Any]]
    easy_loader: Callable[..., Any]
    release_decode_network: Callable[[], None]
    logger: logging.Logger
    heartbeat_interval_sec: float


@dataclass(frozen=True)
class DecodeSignals:
    """What one chunk decode reports back to its caller, and whether liveness is live.

    Primitive-obsession cleanup (phaze-48ghg.5), and the counterpart to :class:`DecodeTarget`:
    that type says WHAT is being decoded, this one says how the decode reports what it did.
    ``on_skip`` and ``on_beat`` are the decode's only two outbound channels -- windows lost, and
    "still working" -- and ``watchdog_enabled`` is not a third concern but a property of the
    second: it says whether ``on_beat`` is a real callback worth running a watchdog thread for,
    or the caller's total no-op.  ``services/analysis.py`` derives it as ``on_beat is not _noop``
    at the seam, which is the one place that can know.

    They are deliberately NOT folded into :class:`DecodeTarget`.  That type has to stay exactly
    what is identical across a retry -- the gated rung and the ungated rung decode the same
    target and differ only in ``stop_at_sec`` -- and reporting is orthogonal to it: the same
    windows of the same file can be decoded by a caller that wants beats and by one that does
    not.  ``stop_at_sec`` stays out of BOTH types for the same reason, being the one thing a
    retry changes.
    """

    on_skip: SkipCallback
    on_beat: BeatCallback
    watchdog_enabled: bool


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
    # phaze-bk9el.29: BROAD BY REVIEW, and explicitly NOT a D-09 leak guard -- the honest label matters.
    # CLAUDE.md records that ``malloc_trim`` CANNOT fix D-09's leak (those pages were live-referenced, not
    # merely un-returned); this only hands back pages the teardown already freed.  What the breadth buys is
    # that a pure optimisation cannot fail a file: ``_release_decode_network`` runs at the tail of EVERY
    # ``_decode_windows``, so an escape here would abort a chunk that had already decoded successfully.
    # Nothing narrower is meaningful -- ``trim`` is a ctypes foreign-function pointer, so its raise surface
    # is whatever the C library and ctypes between them produce.  Exercised (but pragma-excluded, so it
    # shows as neither covered nor missed) by ``test_malloc_trim_never_raises``, which injects OSError.
    except Exception:  # defensive; an optimisation must never fail an already-decoded chunk -- NOT a D-09 guard (bk9el.29)
        # phaze-wbxi2 removed a `# pragma: no cover` from this line. It was already stale -- `test_malloc_trim_never_raises`
        # injected an OSError here -- and it is now exercised for nine exception types by
        # `test_the_trim_never_fails_a_chunk_for_any_exception_type`. An excluded line whose behaviour IS asserted reports a
        # gap that does not exist, which is the same class of untrue claim this bead exists to close.
        logger.debug("malloc_trim(0) failed; continuing", exc_info=True)


def _readable_edges_of(algo: Any, logger: logging.Logger) -> list[tuple[Any, Any]]:
    """One algorithm's EDGE LIST, and the per-ALGORITHM guard: what it will admit to, or nothing.

    An algorithm whose ``connections`` map cannot be read yields an EMPTY edge list, which is
    exactly the "leak this one network and carry on" outcome the guard below has always produced
    -- the caller then sweeps nothing for it and moves to the next algorithm.  Returning the empty
    list rather than a failure flag is what makes this a complete guard instead of a classifier
    the caller has to remember to act on.
    """
    try:
        connections = list((getattr(algo, "connections", None) or {}).items())
        return [(connector, target) for connector, targets in connections for target in list(targets)]
    # phaze-bk9el.29: BROAD BY REVIEW -- a D-09 leak guard, and the load-bearing one.  Yielding no edges
    # costs exactly ONE algorithm's network; an escape would abort the whole sweep and leave every
    # REMAINING algorithm's C++ edges and implicit ``PoolStorage`` sink allocated, which is the
    # duration-linear +0.31 GiB-per-fine-chunk growth that OOMKilled every file past ~3 h against the
    # 4 GiB limit.  This runs inside ``_decode_windows_streaming``'s ``finally``, so an escape would also
    # REPLACE the decode's real exception and the ungated retry rung would never be reached.
    # Covered by ``test_an_unreadable_connections_map_is_survived_too``.
    except Exception:
        logger.warning("could not read a streaming algorithm's connections; its network will leak", exc_info=True)
        return []


def _sever_algorithm_edges(algo: Any, logger: logging.Logger) -> None:
    """One ALGORITHM's edges, severed one independent edge at a time, and the per-EDGE guard."""
    for connector, target in _readable_edges_of(algo, logger):
        try:
            connector.disconnect(target)
        # phaze-bk9el.29: BROAD BY REVIEW -- the same D-09 guard, applied per EDGE rather than per
        # algorithm.  One edge essentia refuses to sever is allowed to leak its own network; it is not
        # allowed to stop the siblings from being severed, which is what keeps teardown's coverage
        # independent of WHICH edge sticks.
        # Covered by ``test_a_stuck_disconnect_never_masks_the_callers_exception``.
        except Exception:
            logger.warning("failed to disconnect a streaming edge; this chunk's network will leak", exc_info=True)


def _disconnect_network(algos: Sequence[Any], logger: logging.Logger) -> None:
    """The whole NETWORK: sever every discoverable Essentia graph edge, in teardown order.

    ``algos`` is consumed FRONT TO BACK and that order is load-bearing, not incidental: the only
    caller passes :meth:`_PartialNetwork.connected`, which is reverse construction order, so the
    window branches are severed first and the shared head of the fan-out -- gate, then loader --
    LAST.  That is the order D-09 documents.  Nothing here reorders, filters or batches ``algos``;
    the per-algorithm and per-edge defences live one scope down each, where the thing they defend
    is.  A PARTIAL network is therefore tolerated without a check here -- an entry that never
    finished being wired, or is not an algorithm at all, simply yields no edges one scope down
    (``test_disconnecting_a_partially_built_network_is_inert``).
    """
    for algo in algos:
        _sever_algorithm_edges(algo, logger)


def _release_decode_network(*, collect: Callable[[], int], trim: Callable[[], None]) -> None:
    """Collect disconnected proxy cycles, then trim the pages they released."""
    collect()
    trim()


def _chunked(windows: list[Window], size: int) -> list[list[Window]]:
    """Split ordered windows into consecutive, bounded chunks."""
    return [windows[i : i + size] for i in range(0, len(windows), size)]


def _stream_source(loader: Any, sample_rate: int, stop_at_sec: float | None, fanout: _ChunkFanOut) -> Any:
    """Return the fan-out source, registering the optional early-stop gate before wiring it."""
    if stop_at_sec is None:
        return loader.audio
    gate = fanout.register(fanout.runtime.trimmer(sampleRate=sample_rate, startTime=0.0, endTime=stop_at_sec + _CHUNK_GATE_MARGIN_SEC))
    loader.audio >> gate.signal
    return gate.signal


def _build_window_branches(source: Any, target: DecodeTarget, fanout: _ChunkFanOut) -> None:
    """Connect one isolated Scale/Trimmer branch per requested window.

    Each algorithm is registered on the fan-out's network before it is wired, so a raise anywhere
    in this loop still leaves every branch built so far -- including the half-wired one -- visible
    to the caller's teardown.  Nothing is returned: the fan-out IS the accumulator.
    """
    for idx, start, end in target.windows:
        scale = fanout.register(fanout.runtime.scale(factor=1.0))
        trimmer = fanout.register(fanout.runtime.trimmer(sampleRate=target.sample_rate, startTime=start, endTime=end))
        source >> scale.signal
        scale.signal >> trimmer.signal
        trimmer.signal >> (fanout.pool, f"{_SINK_KEY_PREFIX}{idx}")


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
    target: DecodeTarget,
    *,
    runtime: StreamingRuntime,
    stop_at_sec: float | None = None,
) -> dict[int, Any]:
    """Decode every requested window of ``target`` in one streaming fan-out pass.

    The identity ``Scale`` on every branch prevents an early Trimmer from stopping sibling
    windows.  A separate head Trimmer supplies the optional chunk gate.  Every graph edge is
    disconnected in ``finally`` because dropping Python proxies does not release Essentia's
    C++ buffers or the implicit ``PoolStorage`` sink.

    The build writes into the fan-out's :class:`_PartialNetwork` as it goes rather than returning
    a finished list, so a raise part-way through the fan-out -- the ordinary failure path, not an
    exotic one -- still hands the ``finally`` everything that was constructed (D-09, phaze-u1n7j).
    """
    pool = runtime.pool()
    network = _PartialNetwork()
    fanout = _ChunkFanOut(runtime, pool, network)
    loader = fanout.register(runtime.mono_loader(filename=target.file_path, sampleRate=target.sample_rate))
    try:
        source = _stream_source(loader, target.sample_rate, stop_at_sec, fanout)
        _build_window_branches(source, target, fanout)
        runtime.run(loader)
        return _extract_window_buffers(pool, target.windows)
    finally:
        runtime.disconnect_network(network.connected())
        network.clear()
        del loader, pool, fanout


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
    signals: DecodeSignals,
    runtime: DecodeRuntime,
) -> dict[int, Any]:
    """Run one blocking streaming attempt with optional periodic liveness beats."""
    if not signals.watchdog_enabled:
        return _call_streaming(target, stop_at_sec, runtime)

    stopped = threading.Event()

    def _watch() -> None:
        while not stopped.wait(runtime.heartbeat_interval_sec):
            try:
                signals.on_beat()
            # phaze-bk9el.29: BROAD BY REVIEW, and NOT a D-09 leak guard -- it prevents the phaze-1b39 class
            # instead.  ``on_beat`` is caller-supplied (-> ``AnalysisSignals.beat`` -> an injected
            # ``heartbeat_cb`` doing IPC), so this module does not own its raise surface and cannot enumerate
            # it on behalf of every future caller.  An escape kills this daemon thread SILENTLY: the beats
            # stop, and D-08's stall watchdog (``analysis_stall_timeout_sec``, 1800 s of silence) then
            # SIGTERMs an analysis that is running perfectly well -- on a multi-hour set, hours in.
            # THIS HANDLER IS THE ONLY UNCOVERED CODE IN THIS MODULE (measured, phaze-bk9el.29: the two
            # lines below are the module's sole coverage misses).  Per CLAUDE.md rule 4 that absence
            # FORBIDS narrowing it rather than excusing it -- there is no test T to prove a narrowed set
            # still degrades, and "no test exists" is the finding, not a reason to write a weaker sentence.
            except Exception:
                runtime.logger.warning("decode heartbeat callback failed; continuing", exc_info=True)

    watchdog = threading.Thread(target=_watch, name="phaze-decode-heartbeat", daemon=True)
    watchdog.start()
    try:
        return _call_streaming(target, stop_at_sec, runtime)
    finally:
        stopped.set()
        watchdog.join()


def _try_gated_rung(target: DecodeTarget, stop_at_sec: float, signals: DecodeSignals, runtime: DecodeRuntime) -> dict[int, Any] | None:
    """The GATED rung: decode the chunk behind its early-stop gate, or ``None`` to retry ungated.

    ``None`` is this rung declining, not an error: the gate is a pure wall-clock optimisation, so
    a gate that misbehaves must cost the caller the ungated retry below and never the chunk.  The
    beat before returning is what stops that retry starting from an already-old heartbeat.

    A ``None`` that did NOT come from the handler, and why the fix is not a raise (phaze-48ghg.5)
    ------------------------------------------------------------------------------------------
    ``stop_at_sec`` here is ``float``, not ``float | None`` -- "there is no gate to try" is decided
    by the caller and never enters this function -- so inside it ``None`` means one thing: the
    attempt raised.  The one other way ``None`` can reach the caller's check is
    ``runtime.streaming_decode`` *returning* it, violating its ``Callable[..., dict[int, Any]]``
    type.  That degrades silently, it has done so since this ladder was written (before the split
    the ``return`` sat inside the ``try`` and passed a ``None`` straight out to
    :func:`_decode_windows`, which fell through to :func:`_decode_per_window`), and mypy is what
    currently closes the path -- nothing in the suite or in production has produced it.

    **If it ever becomes reachable, treat a non-mapping return as a failed rung and log it; do NOT
    raise.**  A raise here does not stay here: it propagates out of :func:`_try_streaming_ladder`
    and :func:`_decode_windows` and fails the whole chunk, past the per-window fallback that exists
    to salvage exactly a broken streaming path -- contradicting this rung's own contract that ANY
    failure of the gated pass degrades rather than failing the file.
    """
    try:
        return _streaming_with_watchdog(target, stop_at_sec, signals, runtime)
    # phaze-bk9el.29: BROAD BY REVIEW -- the ladder's entire contract is that ANY failure of the gated
    # pass degrades to the ungated one rather than failing the file, and "any" is the substance of it:
    # the raise crosses real essentia plus two ``finally`` blocks (this module's teardown and the
    # watchdog join), and under the 4 GiB cgroup this is precisely where a MemoryError is plausible
    # rather than exotic.  NARROWING IS UNVERIFIABLE HERE, which is the decisive reason and not a
    # stylistic one: every failure the suite injects at this rung is a RuntimeError, so narrowing to
    # RuntimeError would leave all of those tests green while changing production behaviour for every
    # other type -- ADR-0012 rule 3's proxy-that-cannot-exhibit-the-failure, verbatim.
    # Covered by ``test_a_failing_gate_retries_ungated_before_the_per_window_fallback`` and
    # ``test_a_failing_gated_attempt_beats_before_the_ungated_retry``.
    except Exception:
        runtime.logger.warning("gated streaming decode failed at %d Hz; retrying ungated", target.sample_rate, exc_info=True)
        signals.on_beat()
        return None


def _try_ungated_rung(target: DecodeTarget, signals: DecodeSignals, runtime: DecodeRuntime) -> dict[int, Any] | None:
    """The LAST streaming rung: decode from byte 0, or ``None`` to degrade to the per-window loader.

    There is no third streaming attempt.  ``None`` here means the fan-out is out of options and
    the caller must fall back to :func:`_decode_per_window`, which is slower by the whole factor
    phaze-5lop removed but isolates failures window by window.
    """
    try:
        return _streaming_with_watchdog(target, None, signals, runtime)
    # phaze-bk9el.29: BROAD BY REVIEW -- the last streaming rung, and the same reasoning as the gated rung
    # above: a streaming failure of any kind must degrade to the per-window loader instead of taking the
    # file, and the suite injects only RuntimeError here too, so a narrowing could not be verified by it.
    # Covered by ``test_a_network_failure_falls_back_to_the_per_window_decode`` and
    # ``test_the_per_window_fallback_beats_once_per_window``.
    except Exception:
        runtime.logger.warning("streaming decode pass failed at %d Hz; falling back to per-window EasyLoader", target.sample_rate, exc_info=True)
        return None


def _try_streaming_ladder(target: DecodeTarget, stop_at_sec: float | None, signals: DecodeSignals, runtime: DecodeRuntime) -> dict[int, Any] | None:
    """Attempt gated then ungated streaming, returning ``None`` for full fallback.

    The ladder itself is now only the ORDER of its rungs -- gated first when there is a gate to
    try, ungated always -- with each rung owning its own degradation.  ``None`` propagates
    unchanged through both: a rung that declines hands the decision straight to the next one, and
    the last rung's ``None`` is the caller's signal to leave streaming behind entirely.
    """
    if stop_at_sec is not None:
        gated = _try_gated_rung(target, stop_at_sec, signals, runtime)
        if gated is not None:
            return gated
    return _try_ungated_rung(target, signals, runtime)


def _decode_per_window(target: DecodeTarget, signals: DecodeSignals, runtime: DecodeRuntime) -> dict[int, Any]:
    """Decode windows independently, preserving per-window failure isolation.

    Deliberately NOT decomposed further (phaze-48ghg.5).  The remaining ``for`` around a ``try``
    is the minimum shape per-window isolation can have, and a ``_decode_one_window`` helper would
    have to signal "skipped" to its caller: through ``None``, which this loop cannot distinguish
    from a loader that legitimately returned ``None``, or through a sentinel, which is more
    machinery than the two lines it would remove.  The parameter count came down instead.
    """
    decoded: dict[int, Any] = {}
    for idx, start, end in target.windows:
        try:
            decoded[idx] = runtime.easy_loader(filename=target.file_path, sampleRate=target.sample_rate, startTime=start, endTime=end)()
        # phaze-bk9el.29: BROAD BY REVIEW -- the module docstring's "isolate failures per window on that
        # final fallback", and this is the LAST rung: there is nothing below it to degrade to.  Narrowing
        # would let one undecodable window abort the whole chunk instead of being skipped via ``on_skip``,
        # converting a partial result into no result.  Same unverifiable-narrowing problem as the ladder.
        # Covered by ``test_a_network_failure_falls_back_to_the_per_window_decode`` and
        # ``test_the_per_window_fallback_beats_once_per_window``.
        except Exception:
            signals.on_skip(idx, start, end, True)
        signals.on_beat()
    return decoded


def _report_missing_windows(decoded: dict[int, Any], windows: Sequence[Window], on_skip: SkipCallback) -> None:
    """Report streaming sinks that produced no audio without inventing a traceback."""
    for idx, start, end in windows:
        if idx not in decoded:
            on_skip(idx, start, end, False)


def _decode_windows(
    target: DecodeTarget,
    signals: DecodeSignals,
    *,
    runtime: DecodeRuntime,
    stop_at_sec: float | None = None,
) -> dict[int, Any]:
    """Run the gated -> ungated -> per-window decode ladder for one chunk."""
    decoded = _try_streaming_ladder(target, stop_at_sec, signals, runtime)
    if decoded is None:
        decoded = _decode_per_window(target, signals, runtime)
    else:
        _report_missing_windows(decoded, target.windows, signals.on_skip)
    runtime.release_decode_network()
    return decoded
