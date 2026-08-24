"""phaze-wbxi2: the exception-TYPE instrument for ``analysis_decoder``'s seven broad catches.

WHY THIS MODULE EXISTS. ``phaze-bk9el.29`` reviewed all seven ``except Exception`` sites in
:mod:`phaze.services.analysis_decoder`, left all seven broad, and recorded a *measured* reason
for doing so: every failure injection in that module's suite was a ``RuntimeError`` (11 sites)
or an ``OSError`` (1). Zero type variety. Narrowing ``@324``/``@329``/``@345`` to
``except RuntimeError`` would therefore have left the ENTIRE suite green while changing
production behaviour for every other exception type -- ADR-0012 rule 3's "verified against a
proxy that structurally cannot exhibit the failure", verbatim. The broad catches were never the
debt; the missing type variety was, because it is what blocks any future decision about them.

**This module narrows nothing and licenses nothing.** It is the instrument that would make such
a decision verifiable, and its own falsifiability is proved rather than asserted: see
``test_the_seven_analysed_sites_are_still_the_modules_only_broad_catches`` below, and the AC-3
transcript on the bead, where ``@345`` was temporarily narrowed to ``except RuntimeError`` and
this module went from green to red.

WHAT THESE INJECTIONS PROVE, AND WHAT THEY DO NOT (ADR-0012 rules 3 and 5). Injecting a
``MemoryError`` through a mocked seam does NOT prove that essentia raises ``MemoryError``; no
mock can prove anything about the C++ side. What it proves is the property the narrowing
question actually turns on: **each handler's degradation behaviour is TYPE-INDEPENDENT today.**
That is a claim about phaze's own control flow, the mocked seam is its real consumer, and it is
therefore discharged at the right seam. The complementary claim -- that the REAL network tears
down correctly -- belongs to the two D-09 guards in ``test_analysis_streaming_decode.py``, which
run real essentia over real audio, cannot be satisfied by a mock, and are deliberately untouched
by this bead. Read the two halves together; neither substitutes for the other.

---------------------------------------------------------------------------------------------
AC 1 -- PER-SITE REACHABLE-EXCEPTION ANALYSIS
---------------------------------------------------------------------------------------------
Line numbers are as of the bead; the structural test below keys on ENCLOSING FUNCTION instead,
so it does not rot when the file moves. "Not enumerable" is a real answer, and where it appears
it is the strongest available argument for keeping that catch broad PERMANENTLY.

``@160`` -- ``_malloc_trim``, around ``trim(0)``.
    NOT ENUMERABLE. ``trim`` is a ``ctypes`` foreign function pointer into glibc, resolved at
    import by ``ctypes.CDLL(None).malloc_trim``. ctypes converts a foreign-call failure into
    whatever its own machinery raises -- ``OSError`` for a segfault-class fault, ``ArgumentError``
    (a ``ValueError`` subclass) for a conversion failure, ``ValueError``/``TypeError`` for an
    argtypes mismatch -- and the set is not fixed by anything phaze owns. Reachable in practice:
    ``OSError``, ``ValueError``, ``TypeError``, ``AttributeError`` (a stale/ablated symbol),
    ``MemoryError``. This is a PURE OPTIMISATION and explicitly NOT a D-09 guard: CLAUDE.md is
    clear that ``malloc_trim`` cannot fix D-09's leak, because those pages are live-referenced
    rather than merely un-returned. A trim that fails must not fail an already-decoded chunk.

``@171`` -- ``_disconnect_network``, around reading ``algo.connections`` and flattening it.
    PARTIALLY ENUMERABLE, and narrower than it looks -- the guarded expression is
    ``getattr(algo, "connections", None) or {}``, and ``getattr`` with a default SWALLOWS
    ``AttributeError`` raised anywhere inside a property getter. So ``AttributeError`` is NOT
    reachable from the attribute access itself; it IS reachable from ``.items()`` on a truthy
    non-mapping (the shape ``test_an_unreadable_connections_map_is_survived_too`` pins).
    Also reachable: ``TypeError`` (``list(targets)`` over a non-iterable), ``KeyError`` and
    ``RuntimeError`` from a SWIG proxy's own accessor, ``MemoryError`` while materialising the
    two lists. D-09 LEAK GUARD (one of only two). It runs in a ``finally`` on the failure path,
    so raising here would REPLACE the decode's real exception and skip the retry rung.

``@177`` -- ``_disconnect_network``, around ``connector.disconnect(target)``.
    NOT ENUMERABLE. ``disconnect`` is a SWIG-bound C++ call; essentia surfaces
    ``EssentiaException`` as ``RuntimeError``, but the binding layer itself can raise
    ``TypeError``/``ValueError`` on a proxy shape it no longer recognises, ``SystemError`` when
    a C extension returns with an exception already set, and ``MemoryError``. D-09 LEAK GUARD
    (the other one). Per-EDGE rather than per-algorithm on purpose: one stuck edge must leak its
    own network and let the loop sever the rest, not abandon the sweep.

``@300`` -- ``_streaming_with_watchdog._watch``, around ``on_beat()``.
    NOT ENUMERABLE, AND UNOWNED. ``on_beat`` is a CALLER-SUPPLIED callback; this module does not
    own its raise surface and cannot enumerate it even in principle. In production it reaches
    ``AnalysisSignals.beat`` and from there an IPC/stdout channel, so ``OSError``,
    ``BrokenPipeError``, ``ValueError`` (closed file) and ``queue.Full`` are all live, and any
    future caller may add more. Narrowing here would convert a dead heartbeat into the
    ``phaze-1b39`` class: the liveness thread dies silently, D-08's 1800 s stall watchdog reads
    the silence as a hang, and SIGTERMs a HEALTHY multi-hour analysis. This is the site where
    "cannot be enumerated" is not a shrug but the argument.

``@324`` -- ``_try_streaming_ladder``, around the GATED streaming attempt.
``@329`` -- ``_try_streaming_ladder``, around the UNGATED streaming attempt.
    NOT ENUMERABLE. Both wrap ``essentia.run`` on a real streaming network -- one blocking C++
    call, plus the SWIG-bound construction and wiring of every algorithm in the fan-out.
    Reachable and demonstrated below: ``MemoryError`` (the deployed 4Gi cgroup makes this
    PLAUSIBLE, NOT EXOTIC, and it is the type a careless narrowing most likely reclassifies into
    a crash), ``OSError`` (the archive is a network mount; a read can fail mid-decode),
    ``RuntimeError`` (essentia's own exception translation), ``TypeError``/``AttributeError``/
    ``ValueError`` (binding drift on an essentia upgrade -- the exact hazard the retry rung's
    docstring already invokes when it says "a gate broken by some future essentia"), and
    ``SystemError``. These two rungs are what stand between a broken gate and a full re-run of
    the ``O(n_windows x duration)`` decode phaze-5lop deleted.

``@345`` -- ``_decode_per_window``, around ``easy_loader(...)()``.
    NOT ENUMERABLE, same reasons as ``@324``/``@329`` plus one more: this rung decodes ONE
    window of a possibly-corrupt file, so it meets malformed input directly. ``MemoryError``
    matters here most of all -- this is the LAST rung, and an escape takes the whole chunk with
    it, which is precisely the per-window isolation contract ``phaze-zibn``'s floor reads.

One catch in the module is deliberately NARROW and stays that way:
``_resolve_malloc_trim``'s ``except (AttributeError, OSError)``. Its reachable set genuinely IS
enumerable -- ``platform.libc_ver`` and a single ``CDLL(None)`` symbol lookup -- which is why it
could be narrowed and the seven could not. The structural test below pins that asymmetry.
"""

from __future__ import annotations

import ast
from collections import Counter
import logging
from pathlib import Path
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from phaze.services import analysis_decoder
import phaze.services.analysis as analysis_mod
from phaze.services.analysis import _COARSE_SAMPLE_RATE, _decode_windows


class _EssentiaBindingDrift(Exception):
    """A type this repository has never seen -- the stand-in for the one a future essentia raises.

    Every other member of ``_INJECTED_TYPES`` names a mechanism someone can argue about. This one
    exists so the suite cannot be satisfied by enumerating today's known types: it is unrelated to
    every builtin a narrowing might plausibly reach for, so any narrowing at all reclassifies it.
    """


def _drift() -> Exception:
    return _EssentiaBindingDrift("a future essentia raised something this module has never seen")


# The injected set, and the MECHANISM each one stands for. Ordered so `MemoryError` is first:
# under the deployed 4Gi cgroup it is plausible rather than exotic on the decode ladder, and it
# is the type most likely to be silently reclassified by a careless narrowing (phaze-wbxi2 AC 2).
_INJECTED_TYPES: list[tuple[str, Any]] = [
    ("MemoryError", lambda: MemoryError("essentia could not allocate the window buffer")),
    ("OSError", lambda: OSError("archive mount read failed mid-decode")),
    ("RuntimeError", lambda: RuntimeError("essentia translated an EssentiaException")),
    ("ValueError", lambda: ValueError("the bindings rejected a parameter value")),
    ("TypeError", lambda: TypeError("the bindings stopped accepting this keyword")),
    ("AttributeError", lambda: AttributeError("a SWIG proxy attribute was renamed")),
    ("KeyError", lambda: KeyError("phaze.window.0")),
    ("SystemError", lambda: SystemError("a C extension returned with an exception set")),
    ("_EssentiaBindingDrift", _drift),
]
_TYPE_IDS = [name for name, _ in _INJECTED_TYPES]
_TYPE_FACTORIES = [factory for _, factory in _INJECTED_TYPES]

_DECODER_SOURCE = Path(analysis_decoder.__file__)

# `_watch` beats every `heartbeat_interval_sec`; two beats prove the FIRST raise was swallowed and
# the loop kept running. One beat proves nothing -- a thread that dies on its first raise still
# produced one.
_BEATS_REQUIRED = 2
# Generous: this only elapses when the handler did NOT swallow, i.e. on the failing run AC 3 wants
# to be legible. The passing path costs `_BEATS_REQUIRED * 0.01 s`.
_WATCHDOG_TIMEOUT_SEC = 5.0
_TEST_HEARTBEAT_INTERVAL_SEC = 0.01
_WATCHDOG_THREAD_NAME = "phaze-decode-heartbeat"


# ---------------------------------------------------------------------------
# AC 1 / AC 5 -- the analysis above is pinned to the code, so it cannot rot silently
# ---------------------------------------------------------------------------


def _broad_catch_sites() -> Counter[str]:
    """Count ``except Exception:`` handlers per enclosing function in the decoder module."""
    tree = ast.parse(_DECODER_SOURCE.read_text(encoding="utf-8"))
    sites: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for handler in ast.walk(node):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            # `ast.walk` reaches nested functions too; attribute each handler to its OWN function.
            if _enclosing_function(tree, handler) is not node:
                continue
            if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                sites[node.name] += 1
    return sites


def _enclosing_function(tree: ast.Module, target: ast.ExceptHandler) -> ast.FunctionDef | None:
    """Return the innermost ``FunctionDef`` containing ``target``."""
    innermost: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(child is target for child in ast.walk(node)) and (innermost is None or node.lineno > innermost.lineno):
            innermost = node
    return innermost


def test_the_seven_analysed_sites_are_still_the_modules_only_broad_catches() -> None:
    """The module docstring's per-site analysis is pinned to the code it describes.

    Two jobs, and the second is the one that makes this bead's instrument honest:

    1. **AC 5, asserted rather than promised.** All seven catches stay broad. A narrowing that
       reaches this repo without a bead, a blast-radius statement and the operator's own sign-off
       fails here, by name, with the site it touched.
    2. **AC 1's analysis cannot rot silently.** A new broad catch, or one that moves to a
       different function, changes this map -- and the analysis above is then demonstrably
       incomplete rather than quietly stale.

    Deliberately keyed on the ENCLOSING FUNCTION, not on line numbers: the analysis is about
    what each site guards, and that survives the file being reformatted or re-ordered.
    """
    assert _broad_catch_sites() == Counter(
        {
            "_malloc_trim": 1,  # @160 -- the ctypes trim
            "_disconnect_network": 2,  # @171 connections map, @177 the disconnect call (D-09 guards)
            "_watch": 1,  # @300 -- the caller-supplied heartbeat callback
            "_try_streaming_ladder": 2,  # @324 gated, @329 ungated
            "_decode_per_window": 1,  # @345 -- the per-window EasyLoader
        }
    ), (
        "the broad-catch map changed. This bead's per-site analysis (see this module's docstring) is keyed to it, so a "
        "change here means either an unreviewed narrowing -- which needs its own bead, a blast-radius statement and the "
        "operator's own sign-off, per phaze-wbxi2 and epic phaze-bk9el -- or a new site the analysis does not cover."
    )


def _handler_type_names(handler: ast.ExceptHandler) -> list[str]:
    """The exception names one ``except`` clause catches; ``[]`` for a bare ``except:``."""
    if handler.type is None:
        return []
    if isinstance(handler.type, ast.Tuple):
        return sorted(elt.id for elt in handler.type.elts if isinstance(elt, ast.Name))
    return [handler.type.id] if isinstance(handler.type, ast.Name) else [ast.unparse(handler.type)]


def _narrow_catch_sites() -> list[tuple[str, list[str]]]:
    """Every handler in the decoder module that is NOT ``except Exception``, with its types."""
    tree = ast.parse(_DECODER_SOURCE.read_text(encoding="utf-8"))
    narrow: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for handler in ast.walk(node):
            if not isinstance(handler, ast.ExceptHandler) or _enclosing_function(tree, handler) is not node:
                continue
            names = _handler_type_names(handler)
            if names != ["Exception"]:
                narrow.append((node.name, names))
    return sorted(narrow)


def test_the_one_deliberately_narrow_catch_stays_narrow() -> None:
    """``_resolve_malloc_trim`` is the module's only ENUMERABLE catch, and stays the only one.

    It is the counter-example that gives the other seven their meaning: its reachable set really
    is ``platform.libc_ver`` plus one ``CDLL(None)`` symbol lookup, so it could be written narrow
    and was. Widening it to ``except Exception`` would erase the distinction this bead's analysis
    turns on -- that "broad" here is a measured property of the raise surface, not a house style.

    This is a COMPLETE INVENTORY of every non-``Exception`` handler in the module, at any nesting
    depth, not a check on one function -- so it is the second half of AC 5 and fails on ANY
    narrowing anywhere, including a bare ``except:``. It is deliberately redundant with
    ``test_the_seven_analysed_sites_are_still_the_modules_only_broad_catches``: that one notices a
    broad catch DISAPPEARING, this one notices what it turned into.
    """
    assert _narrow_catch_sites() == [("_resolve_malloc_trim", ["AttributeError", "OSError"])], (
        "a handler in analysis_decoder.py is no longer `except Exception` -- or a new narrow one appeared. Narrowing any of "
        "the seven is a behaviour question needing its own bead, a blast-radius statement and the operator's own sign-off "
        "(phaze-wbxi2, epic phaze-bk9el); it is not something this module's tests should have to discover after the fact."
    )


# ---------------------------------------------------------------------------
# AC 2 -- @324: a failing GATED attempt degrades to the ungated retry, for every type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_exc", _TYPE_FACTORIES, ids=_TYPE_IDS)
def test_the_gated_rung_retries_ungated_for_every_exception_type(make_exc: Any) -> None:
    """``@324``: whatever the gate raises, the chunk is retried UNGATED before anything degrades.

    The gate is a pure wall-clock optimisation, so this rung's whole job is to make a broken gate
    cost time and never correctness. If it escapes, the chunk skips straight past a decode that
    would have SUCCEEDED -- so a narrowing here does not merely change error handling, it loses
    audio that was recoverable.

    ``MemoryError`` is not decoration in this list. Under the deployed 4Gi cgroup an allocation
    failure inside the gated fan-out is an ordinary event, and the gated network is the LARGER of
    the two attempts (it carries an extra head ``Trimmer``): the ungated retry that follows is
    exactly the smaller thing worth trying next.
    """
    windows = [(0, 0.0, 5.0), (1, 5.0, 10.0)]
    buffers = {0: np.zeros(4, dtype=np.float32), 1: np.zeros(4, dtype=np.float32)}
    calls: list[float | None] = []
    beats: list[int] = []

    def _gate_only_fails(_path: str, _rate: int, _windows: Any, *, stop_at_sec: float | None = None) -> dict[int, Any]:
        calls.append(stop_at_sec)
        if stop_at_sec is not None:
            raise make_exc()
        return buffers

    mock_es = MagicMock()
    mock_es.EasyLoader.side_effect = AssertionError("the per-window fallback must not run: the ungated retry succeeded")
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=_gate_only_fails),
    ):
        decoded = _decode_windows(
            "/fake/audio.mp3",
            _COARSE_SAMPLE_RATE,
            windows,
            lambda *_a: None,
            stop_at_sec=10.0,
            on_beat=lambda: beats.append(len(beats)),
        )

    assert decoded == buffers, "the ungated retry decoded the chunk; its buffers must reach the caller"
    assert calls == [10.0, None], "the gated attempt must be followed by exactly one ungated retry, whatever type it raised"
    assert beats, "a failed gated attempt must still beat before the ungated retry begins"


# ---------------------------------------------------------------------------
# AC 2 -- @329: a failing UNGATED attempt degrades to the per-window loop, for every type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_exc", _TYPE_FACTORIES, ids=_TYPE_IDS)
def test_the_ungated_rung_falls_back_per_window_for_every_exception_type(make_exc: Any) -> None:
    """``@329``: whatever the streaming pass raises, the chunk degrades to the per-window decode.

    ``stop_at_sec`` is ``None`` here on purpose, so ``@324`` is never entered and this test can
    only be attributed to ``@329``.

    A single shared network cannot isolate one bad window from the rest of the file -- one raise
    takes the whole tier. This rung is what preserves ``phaze-zibn``'s failure contract across
    phaze-5lop's rewrite, so an escape here fails an entire tier of a file that the decode it
    replaced would have salvaged window by window.
    """
    windows = [(0, 0.0, 5.0), (1, 5.0, 10.0), (2, 10.0, 15.0)]

    def _easyloader(*, filename: str, sampleRate: int, startTime: float, endTime: float) -> Any:
        loader = MagicMock()
        loader.return_value = np.full(int((endTime - startTime) * sampleRate), 0.25, dtype=np.float32)
        return loader

    mock_es = MagicMock()
    mock_es.EasyLoader.side_effect = _easyloader
    skips: list[tuple[int, bool]] = []
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=make_exc()),
    ):
        decoded = _decode_windows("/fake/audio.mp3", _COARSE_SAMPLE_RATE, windows, lambda idx, _s, _e, exc: skips.append((idx, exc)))

    assert sorted(decoded) == [0, 1, 2], "the per-window fallback must decode every window the streaming pass lost"
    assert skips == [], "nothing is skipped when the fallback can decode every window"


# ---------------------------------------------------------------------------
# AC 2 -- @345: one undecodable window does not take the chunk, for every type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_exc", _TYPE_FACTORIES, ids=_TYPE_IDS)
def test_a_per_window_failure_is_isolated_for_every_exception_type(make_exc: Any) -> None:
    """``@345``: whatever ONE window raises, the other windows in the chunk still decode.

    This is the last rung. An escape here has nowhere left to degrade to, so it fails the chunk
    -- and on the fine tier a chunk is 60 windows of a multi-hour set. ``MemoryError`` is the
    sharpest case: the per-window loop is the LOW-memory path, entered precisely because the
    shared network already failed, so meeting an allocation failure here is the expected shape of
    a squeezed 4Gi pod rather than an exotic one.
    """
    skips: list[tuple[int, bool]] = []
    windows = [(0, 0.0, 5.0), (1, 5.0, 10.0), (2, 10.0, 15.0)]

    def _easyloader(*, filename: str, sampleRate: int, startTime: float, endTime: float) -> Any:
        if startTime == 5.0:
            raise make_exc()
        loader = MagicMock()
        loader.return_value = np.full(int((endTime - startTime) * sampleRate), 0.25, dtype=np.float32)
        return loader

    mock_es = MagicMock()
    mock_es.EasyLoader.side_effect = _easyloader
    with (
        patch.object(analysis_mod, "es", mock_es),
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=RuntimeError("network build failed")),
    ):
        decoded = _decode_windows("/fake/audio.mp3", _COARSE_SAMPLE_RATE, windows, lambda idx, _s, _e, exc: skips.append((idx, exc)))

    assert sorted(decoded) == [0, 2], "exactly the windows that could decode must be returned"
    assert skips == [(1, True)], "the undecodable window is skipped, with a live exception to log, whatever its type"


# ---------------------------------------------------------------------------
# AC 2 + AC 4 -- @300: the caller-supplied heartbeat callback, for every type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_exc", _TYPE_FACTORIES, ids=_TYPE_IDS)
def test_the_heartbeat_handler_keeps_beating_for_every_exception_type(make_exc: Any, caplog: pytest.LogCaptureFixture) -> None:
    """``@300``: a raising ``on_beat`` must not silently kill the liveness thread (AC 4).

    THIS IS THE ``phaze-1b39`` CLASS, from the other direction. ``essentia.run`` is one blocking
    C++ call, so a streaming rung emits nothing on its own; the watchdog thread is the only thing
    telling D-08 the analysis is alive. If a raise from the caller's callback killed that thread,
    the analysis would keep running perfectly while going SILENT -- and D-08's 1800 s stall
    watchdog would then SIGTERM a healthy multi-hour job. The handler's failure mode is a dead
    thread nobody notices, which is why the absence of a covering test FORBADE narrowing this
    site (phaze-bk9el.29) rather than excusing it.

    ``on_beat`` is caller-supplied, so this module does not own its raise surface and cannot
    enumerate it even in principle -- the strongest possible argument for keeping this catch
    broad permanently.

    Falsifiable by construction: ``_BEATS_REQUIRED`` is 2, not 1. A thread that dies on its first
    raise still produces one beat, so requiring two is what distinguishes "swallowed and kept
    looping" from "died quietly". Under a narrowing the second beat never arrives, the blocking
    decode's wait times out, and the assertion below names the site.
    """
    beats: list[int] = []
    enough = threading.Event()
    waited: dict[str, bool] = {}
    buffers = {0: np.zeros(4, dtype=np.float32)}

    def _on_beat() -> None:
        beats.append(len(beats))
        if len(beats) >= _BEATS_REQUIRED:
            enough.set()
        raise make_exc()

    def _blocking_streaming(_path: str, _rate: int, _windows: Any, **_kw: Any) -> dict[int, Any]:
        # Held open until the watchdog has beaten twice -- a handshake, not a sleep, so the
        # passing path costs two heartbeat intervals and nothing else.
        waited["beaten"] = enough.wait(timeout=_WATCHDOG_TIMEOUT_SEC)
        return buffers

    with (
        caplog.at_level(logging.WARNING, logger="phaze.services.analysis"),
        patch.object(analysis_mod, "_DECODE_HEARTBEAT_INTERVAL_SEC", _TEST_HEARTBEAT_INTERVAL_SEC),
        patch.object(analysis_mod, "_decode_windows_streaming", side_effect=_blocking_streaming),
    ):
        decoded = _decode_windows("/fake/audio.mp3", _COARSE_SAMPLE_RATE, [(0, 0.0, 5.0)], lambda *_a: None, on_beat=_on_beat)

    assert waited["beaten"], (
        f"the heartbeat thread stopped after {len(beats)} beat(s) when on_beat raised {make_exc()!r}. @300 must swallow it: "
        f"a dead liveness thread is the phaze-1b39 class -- D-08's stall watchdog reads the silence as a hang and SIGTERMs a "
        f"HEALTHY multi-hour analysis."
    )
    assert len(beats) >= _BEATS_REQUIRED, f"expected the loop to keep beating after the first raise; got {len(beats)}"
    assert decoded == buffers, "a failing heartbeat must not change what the decode returns"
    assert any("decode heartbeat callback failed" in record.message for record in caplog.records), (
        "the swallowed heartbeat failure must be LOGGED. A silently swallowed one is how a dead callback reaches production undetected."
    )


def test_the_heartbeat_thread_is_not_started_when_no_caller_wants_beats() -> None:
    """The watchdog is opt-in: ``on_beat`` left at its default must not spawn a thread at all.

    ``analysis.py`` decides this with ``watchdog_enabled=on_beat is not _noop``, so the identity
    check -- not truthiness -- is what gates it. Pinned here because the ``@300`` tests above are
    the only other place the thread is exercised, and a regression that started it unconditionally
    would make every non-analysis caller of ``_decode_windows`` pay for a thread it never reads.
    """
    buffers = {0: np.zeros(4, dtype=np.float32)}
    live_during_decode: list[str] = []

    def _recording_streaming(_path: str, _rate: int, _windows: Any, **_kw: Any) -> dict[int, Any]:
        # Sampled from INSIDE the decode: that is the only window in which the watchdog thread
        # would be alive, since `_streaming_with_watchdog` joins it before returning. Reading
        # `threading.active_count()` around the call instead would be both blind to that and
        # flaky, because unrelated pool threads come and go across a full-suite run.
        live_during_decode.extend(thread.name for thread in threading.enumerate())
        return buffers

    with patch.object(analysis_mod, "_decode_windows_streaming", side_effect=_recording_streaming):
        assert _decode_windows("/fake/audio.mp3", _COARSE_SAMPLE_RATE, [(0, 0.0, 5.0)], lambda *_a: None) == buffers

    assert live_during_decode, "the recording seam never ran, so this test asserted nothing"
    assert _WATCHDOG_THREAD_NAME not in live_during_decode, f"a {_WATCHDOG_THREAD_NAME!r} thread ran for a decode that was never asked to beat"


# ---------------------------------------------------------------------------
# AC 2 -- @171 / @177: the two D-09 leak guards, for every type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_exc", _TYPE_FACTORIES, ids=_TYPE_IDS)
def test_an_unreadable_connections_map_never_escapes_the_teardown(make_exc: Any) -> None:
    """``@171``: whatever reading one algorithm's ``connections`` raises, the sweep continues.

    D-09 leak guard. ``_disconnect_network`` runs from a ``finally`` on the FAILURE path, so a
    raise here would replace the decode's real exception with its own and the gated rung's
    ungated retry would never be reached -- turning a leak into both a lost retry and a
    misattributed failure.

    The assertion is that the loop CONTINUES, not merely that the call returns: one algorithm
    whose map cannot be read must not abandon the rest of the network, because every algorithm
    left unswept is ~5 MiB of C++ buffers retained for the life of the process (phaze-u1n7j).

    ``AttributeError`` is worth watching in this list: ``getattr(algo, "connections", None)``
    swallows it before the handler ever sees it, so that case is exercising the ``.items()`` call
    on ``"not a mapping"``-shaped values rather than the attribute access. Both reach @171.
    """
    exc = make_exc()

    class _UnreadableAlgo:
        @property
        def connections(self) -> Any:
            raise exc

    swept: list[Any] = []

    class _StillSweptConnector:
        name = "signal"

        def disconnect(self, target: Any) -> None:
            swept.append(target)

    class _HealthyAlgo:
        def __init__(self) -> None:
            self.connections = {_StillSweptConnector(): ["a-real-sink"]}

    # `getattr(..., None)` swallows AttributeError from the property, so that parametrisation
    # reaches @171 through `.items()` on a truthy non-mapping instead. Both shapes are real.
    unreadable: Any = _UnreadableAlgo() if not isinstance(exc, AttributeError) else type("_NotAMapping", (), {"connections": "not a mapping"})()

    analysis_mod._disconnect_network([unreadable, _HealthyAlgo()])  # must return normally

    assert swept == ["a-real-sink"], (
        f"an algorithm whose connections raised {exc!r} aborted the teardown sweep. Every algorithm left unswept retains "
        f"~5 MiB of essentia C++ buffers for the life of the process -- see D-09 in services/analysis.py."
    )


@pytest.mark.parametrize("make_exc", _TYPE_FACTORIES, ids=_TYPE_IDS)
def test_a_refusing_disconnect_never_escapes_the_teardown(make_exc: Any) -> None:
    """``@177``: whatever ONE edge's ``disconnect`` raises, the remaining edges are still severed.

    D-09 leak guard, per-EDGE by design. ``disconnect`` is a SWIG-bound C++ call and its raise
    surface is not enumerable from Python, so a stuck edge is allowed to leak its own network --
    it is not allowed to change control flow or to take the rest of the sweep with it.
    """
    exc = make_exc()
    severed: list[str] = []

    class _StuckConnector:
        name = "signal"

        def disconnect(self, _target: Any) -> None:
            raise exc

    class _WorkingConnector:
        name = "signal"

        def disconnect(self, target: str) -> None:
            severed.append(target)

    class _MixedAlgo:
        def __init__(self) -> None:
            self.connections = {_StuckConnector(): ["stuck-sink"], _WorkingConnector(): ["good-sink"]}

    analysis_mod._disconnect_network([_MixedAlgo()])  # must return normally

    assert severed == ["good-sink"], f"a {exc!r} on one edge must not stop the sweep severing the others (D-09)"


# ---------------------------------------------------------------------------
# AC 2 -- @160: the ctypes trim, for every type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_exc", _TYPE_FACTORIES, ids=_TYPE_IDS)
def test_the_trim_never_fails_a_chunk_for_any_exception_type(make_exc: Any) -> None:
    """``@160``: whatever the ctypes ``malloc_trim`` call raises, the already-decoded chunk survives.

    ``trim`` is a foreign function pointer, so its raise surface belongs to ctypes and glibc, not
    to phaze -- ``OSError``, ``ValueError`` (``ctypes.ArgumentError`` is one), ``TypeError``,
    ``AttributeError`` on an ablated symbol. NOT a D-09 guard: CLAUDE.md is explicit that
    ``malloc_trim`` cannot fix D-09's leak, because those pages are live-referenced rather than
    merely un-returned. This is a pure optimisation, and an optimisation must never fail a file.

    Previously carried ``# pragma: no cover`` at the site; the existing ``OSError`` injection had
    already made that stale, and this parametrisation makes it plainly wrong, so it was removed.
    """
    with patch.object(analysis_mod, "_MALLOC_TRIM", MagicMock(side_effect=make_exc())):
        analysis_mod._malloc_trim()  # must return normally


def test_a_trim_that_succeeds_is_just_a_call() -> None:
    """The other side of ``@160``: with a working symbol, the trim is called once with ``0``.

    Keeps the success arc of that ``try`` exercised on every platform. phaze runs the analyze job
    on glibc, but this suite also runs on macOS where ``_MALLOC_TRIM`` resolves to ``None`` and
    the real call is never reached -- so without this the only observed path through the ``try``
    would be the failing one.
    """
    trim = MagicMock(return_value=0)
    with patch.object(analysis_mod, "_MALLOC_TRIM", trim):
        analysis_mod._malloc_trim()
    trim.assert_called_once_with(0)
