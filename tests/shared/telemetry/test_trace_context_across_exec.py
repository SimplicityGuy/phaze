"""phaze-m1drf.1 acceptance 4: one file's analysis is ONE trace, across the exec boundary.

``services/analysis_exec.py`` EXECs ``python -m phaze.analysis_child`` because essentia's
C++ holds the GIL of the process it runs in. Without propagation the parent's span and the
child's spans are two unrelated traces and the whole question the epic exists to answer --
where did this file's hours go -- cannot be asked of one trace.

**ADR-0012 rule 3 is what shapes this file.** The claim is about a REAL exec'd child, so
the test EXECs a real child process: it spawns ``phaze.analysis_child`` through the real
``run_analysis_subprocess``, with the real environment plumbing, and reads the trace id the
child observed out of the child's own protocol output. Asserting that
``child_environment()`` contains a ``TRACEPARENT`` would prove that phaze can format a
header -- not that the child receives it, parses it, and lands in the parent's trace.

The analysis target is stubbed (``PHAZE_ANALYSIS_CHILD_TARGET``, the module's existing
test seam) because what is under test is the PROCESS BOUNDARY, not essentia. That
narrowing is deliberate and stated: the real-essentia claims are discharged by
``test_analysis_instrumentation_real_essentia.py``, which runs the real pipeline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
import pytest

from phaze.telemetry import context as telemetry_context
from phaze.telemetry.tracing import span


if TYPE_CHECKING:
    from tests.shared.telemetry.conftest import TelemetrySink


def test_no_active_span_leaves_the_environment_untouched() -> None:
    """A telemetry-off parent must not hand the child a traceparent of all zeroes.

    The all-zero context is what a no-op tracer produces, and it LOOKS like a valid remote
    parent to an extractor -- the child would attach to an invalid parent and its own trace
    would be unlinked from anything. So an invalid span injects nothing.
    """
    assert trace.get_current_span().get_span_context().is_valid is False
    assert telemetry_context.inject_into({}) == {}


def test_child_environment_is_a_copy(telemetry_sink: TelemetrySink) -> None:
    """Mutating ``os.environ`` to pass a traceparent would leak this span's context into
    every other subprocess this worker ever spawns, for the rest of the process's life."""
    before = dict(os.environ)
    with span("parent"):
        child_env = telemetry_context.child_environment()
    assert telemetry_context.TRACEPARENT_ENV in child_env
    assert telemetry_context.TRACEPARENT_ENV not in os.environ
    assert dict(os.environ) == before


def test_a_malformed_traceparent_yields_no_context() -> None:
    """Total in both directions: garbage in the environment starts a new trace rather than
    raising inside the child before it has done any work."""
    assert telemetry_context.extract_from({telemetry_context.TRACEPARENT_ENV: "not-a-traceparent"}) is None
    assert telemetry_context.extract_from({}) is None
    assert telemetry_context.extract_from({telemetry_context.TRACEPARENT_ENV: "   "}) is None


def test_a_raising_propagator_degrades_to_an_unchanged_environment(telemetry_sink: TelemetrySink, monkeypatch: pytest.MonkeyPatch) -> None:
    """Total in both directions means the INJECT side too, and the stakes are higher there:
    ``child_environment`` runs in ``run_analysis_subprocess`` before the spawn, inside a
    ``try`` that handles only ``FileNotFoundError``. A propagator that raises must cost the
    trace link, never the analysis -- the child simply starts its own trace."""

    def _explode(carrier: object, *args: object, **kwargs: object) -> None:
        msg = "propagator fault"
        raise RuntimeError(msg)

    monkeypatch.setattr(telemetry_context._propagator, "inject", _explode)
    with span("parent"):
        child_env = telemetry_context.child_environment()
    assert telemetry_context.TRACEPARENT_ENV not in child_env
    assert child_env  # still a full copy of the parent's environment, not an empty dict


def test_round_trip_preserves_the_trace_id(telemetry_sink: TelemetrySink) -> None:
    with span("parent") as parent:
        parent_trace_id = parent.get_span_context().trace_id
        carrier = telemetry_context.inject_into({})

    extracted = telemetry_context.extract_from(carrier)
    assert extracted is not None
    assert trace.get_current_span(extracted).get_span_context().trace_id == parent_trace_id


@pytest.mark.asyncio
async def test_the_real_execd_child_lands_in_the_parents_trace(telemetry_sink: TelemetrySink, tmp_path: Any) -> None:
    """THE acceptance-4 test. A real subprocess, the real spawn path, the real environment.

    The stub target reports the trace id its own process observed, through the existing
    protocol channel. Equality with the parent's trace id is the whole claim: two
    processes, one trace.
    """
    from phaze.services.analysis_exec import run_analysis_subprocess  # after the sink is installed

    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"not really audio; the stub target never opens it")

    os.environ["PHAZE_ANALYSIS_CHILD_TARGET"] = "tests.shared.telemetry.trace_probe_target:report_trace"
    os.environ.setdefault("PYTHONPATH", str(tmp_path))
    # The child is a FRESH interpreter, so it needs the repo root on its path to import
    # the probe target from tests/.
    repo_root = str(Path(__file__).resolve().parents[3])
    os.environ["PYTHONPATH"] = repo_root + os.pathsep + os.environ.get("PYTHONPATH", "")
    try:
        with span("analysis.parent") as parent:
            expected_trace_id = format(parent.get_span_context().trace_id, "032x")
            result = await run_analysis_subprocess(str(audio), str(tmp_path), stall_timeout=120.0)
    finally:
        os.environ.pop("PHAZE_ANALYSIS_CHILD_TARGET", None)

    assert result["pid"] != os.getpid(), "the target must have run in a DIFFERENT process"
    assert result["executable"] == sys.executable
    assert result["traceparent_seen"], "the child saw no TRACEPARENT in its environment"
    assert result["child_trace_id"] == expected_trace_id, (
        f"the child ran in trace {result['child_trace_id']} but the parent is {expected_trace_id}; the exec boundary broke the trace"
    )
    # And the parent-side span exists, so the trace has both halves rather than only the child's.
    assert "analysis.subprocess" in telemetry_sink.span_names()


def test_the_probe_target_is_json_serializable() -> None:
    """The child emits its report through the protocol channel, whose ``json.dumps`` is
    strict (no ``default=``) -- so a non-serializable report is a loud child error, not a
    silently mangled assertion."""
    from tests.shared.telemetry.trace_probe_target import report_trace

    json.dumps(report_trace("ignored", "ignored"))
