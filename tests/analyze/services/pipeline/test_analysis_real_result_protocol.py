"""A3 (phaze-qiwdk): a REAL ``analyze_file`` result crosses the REAL child protocol.

**The gap this closes.** ``tests/analyze/services/pipeline/test_analysis_exec.py`` already runs
the genuine ``python -m phaze.analysis_child`` subprocess against the genuine
``services.analysis_exec`` driver — the process boundary was never a proxy. What WAS a proxy is
the thing carried across it: every one of those tests hands the child
``tests.analyze._child_stubs._result``, a hand-built dict with one fine window, plain Python
floats, and a ``features`` dict of a single entry. ``_emit`` is ``json.dumps(obj)`` with **no**
``default=`` — deliberately strict, per its docstring — and ``_result`` is structurally
incapable of testing that strictness. It cannot exhibit a numpy scalar leaf, a non-finite
float, or a line large enough for a 64 KiB pipe to matter.

So these tests carry the real artifact (``tests/analyze/_real_result.py``, produced by real
essentia + the real 68-file model set over a real archive track) across the same real boundary,
plus three deliberately corrupted variants of it. The corrupted ones are not decoration: an
assertion that "the real result contains no numpy scalar" is worth nothing unless a numpy
scalar would actually have failed it, and
:func:`test_a_numpy_scalar_leaf_is_fatal_at_emit_which_is_what_makes_the_absence_claim_mean_anything`
is what supplies that.

D-07 (chunking), D-08 (progress-based liveness) and D-09 (streaming-network teardown) are
untouched here, and nothing in this module puts a wall clock on any lane — the multi-mebibyte
case is bounded by FIXTURE SIZE, which is the phaze-1b39 lesson applied to a test.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from phaze.analysis_child import _TARGET_ENV
from phaze.services.analysis_exec import AnalysisSubprocessError, run_analysis_subprocess
from tests.analyze._real_result import iter_leaves, real_analysis_result


if TYPE_CHECKING:
    from collections.abc import Iterator


_STUBS = "tests.analyze._child_stubs"
_REPO_ROOT = Path(__file__).resolve().parents[4]

# Every type a JSON leaf may legitimately be after `analyze_file` has coerced its essentia
# outputs. `bool` rides along as a subclass of `int` and is listed for the reader, not the check.
_JSON_LEAF_TYPES = (str, int, float, bool, type(None))

# The OS pipe buffer `_emit`'s line has to cross. 64 KiB on both Linux and macOS: a write past
# it BLOCKS until the reader drains, so a multi-mebibyte line is delivered in ~40+ refills that
# the parent's `_pump_stdout` has to reassemble into one `readline`. This is a different failure
# mechanism from serialization, which is why it gets its own case (bead AC 5).
_PIPE_BUFFER_BYTES = 64 * 1024

# What "multi-MB" has to clear for the framing case to be the thing it claims to be.
_MULTI_MEBIBYTE = 2 * 1024 * 1024


@pytest.fixture(autouse=True)
def _run_from_repo_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The child resolves ``tests.analyze._child_stubs`` via ``sys.path[0] == cwd`` under
    ``python -m``, so pin the driver's inherited cwd to the repo root."""
    monkeypatch.chdir(_REPO_ROOT)
    yield


def _point_child_at(monkeypatch: pytest.MonkeyPatch, stub: str) -> None:
    monkeypatch.setenv(_TARGET_ENV, f"{_STUBS}:{stub}")


def _protocol_line_bytes(result: dict[str, Any]) -> int:
    """The exact byte count ``_emit`` writes for this result, newline included."""
    return len(json.dumps({"type": "result", "result": result}).encode("utf-8")) + 1


# ---------------------------------------------------------------------------
# What the real artifact actually contains (bead AC 1 + AC 4)
# ---------------------------------------------------------------------------


def test_the_real_result_carries_no_numpy_scalar_and_no_non_finite_leaf() -> None:
    """MEASURED, stated as an assertion: every leaf is a JSON-native builtin and finite.

    This is the bead's AC-4 question answered in the affirmative direction — the real result
    contains NEITHER a numpy scalar NOR a NaN — and it is asserted rather than merely written
    down, so the day essentia's output shape changes the suite says so.

    The claim is backed by more than this one artifact: the same walk over five real archive
    tracks found 3 031 leaves, every one ``builtins.float`` / ``builtins.str`` / ``builtins.int``,
    with zero non-finite and zero NUL — and four deliberately degenerate real decodes (200 s and
    2 s of digital silence, a 3 s tone below the fine-window minimum, the synthetic parity clip)
    agree. Silence is the case most likely to drive a classifier to a non-finite output, and it
    did not.

    It holds by CONSTRUCTION, not by luck, and the construction is worth naming because it is
    one line thick: ``_predict_single`` returns a numpy array and ``np.mean`` a
    ``numpy.float64``, and the only thing standing between that and the wire is the explicit
    ``float(pred)`` / ``float(conf)`` coercion in ``_run_model_sets_over_windows``
    (``services/analysis.py``). ``FineWindow.bpm`` is likewise ``round(float(bpm), 1)`` and
    ``musical_key`` an f-string. Remove any one of those casts and this test fails; the
    companion test below proves that failure is real rather than hypothetical.
    """
    offenders = [
        (path, type(leaf).__module__ + "." + type(leaf).__qualname__)
        for path, leaf in iter_leaves(real_analysis_result())
        if not isinstance(leaf, _JSON_LEAF_TYPES)
    ]
    assert offenders == [], f"non-JSON-native leaves in the real analyze_file result: {offenders}"

    non_finite = [(path, leaf) for path, leaf in iter_leaves(real_analysis_result()) if isinstance(leaf, float) and not math.isfinite(leaf)]
    assert non_finite == [], f"non-finite leaves in the real analyze_file result: {non_finite}"

    nul_bearing = [path for path, leaf in iter_leaves(real_analysis_result()) if isinstance(leaf, str) and "\x00" in leaf]
    assert nul_bearing == [], f"NUL-bearing strings in the real analyze_file result: {nul_bearing}"


async def test_the_real_analyze_file_result_crosses_the_child_protocol_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real artifact goes in one side of the REAL boundary and comes out the other equal.

    Real components wired: ``phaze.analysis_child._emit`` (strict ``json.dumps``) in a real
    ``python -m`` subprocess, a real OS pipe, and ``analysis_exec._pump_stdout`` in the real
    driver. Nothing between them is stubbed except the analysis callable itself, which is what
    ``PHAZE_ANALYSIS_CHILD_TARGET`` exists for — and it returns the real captured result rather
    than a hand-built one, which is the whole point of the test.
    """
    _point_child_at(monkeypatch, "real_analyze")

    got = await run_analysis_subprocess("/f", "/m")

    assert got == real_analysis_result()


# ---------------------------------------------------------------------------
# The controls: what WOULD have broken, proving the claims above have teeth
# ---------------------------------------------------------------------------


async def test_a_numpy_scalar_leaf_is_fatal_at_emit_which_is_what_makes_the_absence_claim_mean_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ``numpy.float32`` in the result kills the child at ``json.dumps``.

    The cost is the whole analysis: this fires only AFTER ``analyze_file`` has returned, i.e.
    after every window of a possibly multi-hour file has been analyzed, and
    ``FAILURE_IS_TERMINAL[ANALYZE]`` means the file does not simply come back around. That is
    why the ``float()`` coercion upstream is load-bearing rather than tidy-minded, and why the
    absence assertion above is worth running.

    Note what does NOT happen: no partial line reaches the pipe. ``json.dumps`` builds the whole
    string before ``protocol.write`` is called, so the child's terminal ``error`` line is the
    only thing the parent sees on the protocol channel.
    """
    _point_child_at(monkeypatch, "numpy_leaf_analyze")

    with pytest.raises(AnalysisSubprocessError) as excinfo:
        await run_analysis_subprocess("/f", "/m")

    assert "TypeError" in str(excinfo.value)
    assert "not JSON serializable" in str(excinfo.value)
    assert excinfo.value.exit_code == 1


async def test_a_nan_leaf_crosses_emit_and_the_pump_silently_rather_than_failing_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``NaN`` is NOT caught at this seam — it is caught two hops later, and that is the finding.

    ``json.dumps`` defaults to ``allow_nan=True``, so ``_emit`` writes the bare ``NaN`` token
    (which is not legal JSON) and ``json.loads`` in ``_pump_stdout`` accepts it right back. The
    child protocol is therefore transparent to non-finite floats in BOTH directions, and the
    first thing that objects is ``AnalysisWindowPayload``'s ``_reject_pg_unsafe_json`` validator
    — client-side, in the worker, on the completed result.

    This test asserts the transparency deliberately rather than treating it as a defect to fix
    here. Making ``_emit`` strict about NaN would move the failure EARLIER, not remove it, and
    the same analysis would still be discarded; where the value should be caught (or coerced) is
    a product question about essentia outputs, not a protocol question. What the test does buy
    is that the behaviour is now pinned: if ``_emit`` ever gains ``allow_nan=False``, this test
    names the hop that changed and why it matters.
    """
    _point_child_at(monkeypatch, "nan_leaf_analyze")

    got = await run_analysis_subprocess("/f", "/m")

    coarse = next(w for w in got["windows"] if w["tier"] == "coarse")
    delivered = coarse["features"]["genre"]["predictions"][0]["confidence"]
    assert delivered != delivered, "expected the NaN to survive the round trip unchanged"


# ---------------------------------------------------------------------------
# Framing: a multi-mebibyte line through a 64 KiB pipe (bead AC 5)
# ---------------------------------------------------------------------------


async def test_a_multi_mebibyte_result_line_crosses_the_64_kib_pipe_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """One protocol line far larger than the pipe buffer is reassembled whole by the pump.

    A SEPARATE case from the serialization tests above on purpose. Serialization asks "can this
    value be encoded"; framing asks "can this many bytes cross a 64 KiB buffer as ONE line", and
    a fixture small enough to characterise leaf types (the real track's line is ~29 KB — under
    half a single pipe refill) is far too small to ask the second question at all. The two
    failure mechanisms are independent: `_emit` writing a value it cannot encode, versus the
    parent's `StreamReader` giving up on a line it cannot buffer.

    Scale is DERIVED from the measurement, not invented. phaze-qiwdk measured the real artifact
    at 115.0 B per fine window and 5 417.2 B per coarse window, and that figure is near-constant
    because the coarse feature dict has fixed shape (11 model sets x 3 variants x 2 classes, plus
    the genre head's top 10). So:

        24 h  ->  2 880 fine + 480 coarse  ->  2 958 820 B = 2.822 MiB  =  45.1 pipe refills
        6h08m ->    737 fine + 123 coarse  ->   756 494 B = 0.72 MiB  =  11.5 pipe refills

    The second row is the archive's longest real file TODAY, and it already exceeds the buffer
    elevenfold — so this failure mode is reachable now, not only at the ceiling. The first row is
    the product's stated concert-set ceiling and the point at which "multi-MB" becomes literally
    true, so it is the scale built here.

    Both bounds this exercises are real and neither is a wall clock: ``_STREAM_LIMIT`` (32 MiB,
    the parent's ``StreamReader`` line cap) and the 64 KiB pipe buffer. D-08 liveness is
    untouched — this test arms no ``stall_timeout`` at all.
    """
    _point_child_at(monkeypatch, "long_recording_analyze")

    got = await run_analysis_subprocess("/f", "/m")

    line_bytes = _protocol_line_bytes(got)
    assert line_bytes > _MULTI_MEBIBYTE, f"fixture is not multi-mebibyte: {line_bytes} bytes"
    assert line_bytes > 30 * _PIPE_BUFFER_BYTES, "fixture should span dozens of pipe refills"

    fine = [w for w in got["windows"] if w["tier"] == "fine"]
    coarse = [w for w in got["windows"] if w["tier"] == "coarse"]
    assert (len(fine), len(coarse)) == (2880, 480), "expected a 24-hour recording's exact window geometry"
    # The TAIL of the line is what a truncating pump loses, and a count assertion alone would not
    # notice: assert on the last window's own contents, not merely on how many arrived.
    assert coarse[-1]["window_index"] == 479
    assert coarse[-1]["features"]["genre"]["predictions"][0]["label"]
