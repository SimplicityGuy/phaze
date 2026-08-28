"""Carry one trace across the exec'd-analysis process boundary.

``services/analysis_exec.py`` EXECs ``python -m phaze.analysis_child`` because essentia's
C++ holds the GIL of whatever process it runs in. Without this module the parent's
``analysis.file`` span and the child's spans are two unrelated traces, and the question
the epic exists to answer -- *where did this file's 7,119 seconds go* -- cannot be asked
of one trace.

**THE MECHANISM IS W3C Trace Context IN THE CHILD'S ENVIRONMENT.** The parent serializes
its active span context into ``TRACEPARENT`` (and ``TRACESTATE`` when non-empty) using the
same ``TraceContextTextMapPropagator`` that writes the HTTP header of the same name; the
child reads them back before it imports essentia and starts its root span inside that
context. There is no phaze-specific encoding: the value is a standard traceparent, so a
collector, a Jaeger UI or any other OTel process reads it without knowing about phaze.

Environment was chosen over the two alternatives on purpose:

* **argv** -- the child's argv is built by ``_build_argv`` and is a stable, tested
  contract; adding a flag would change the child's CLI for every caller, and a traceparent
  in argv is visible in ``ps`` output to every process on the node.
* **the JSON protocol channel** -- it flows the WRONG WAY. The child writes protocol lines
  to the parent; there is no parent-to-child direction to put a traceparent in, and adding
  one would mean the child could not start a span until after its first read.

``asyncio.create_subprocess_exec`` inherits the parent's environment when no ``env`` is
passed, so the parent must pass ``env=child_environment()`` explicitly -- inheriting is
what it did before, and inheriting an unchanged ``os.environ`` is exactly what leaves the
traceparent out.

Both directions are total: an absent, empty or malformed ``TRACEPARENT`` yields an empty
context and the child simply starts a new trace. A telemetry seam is not a place to raise.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


if TYPE_CHECKING:
    from opentelemetry.context import Context

#: The conventional spelling. Deliberately the same name the W3C header carries, so an
#: operator reading a pod's environment recognizes it without a phaze-specific glossary.
TRACEPARENT_ENV = "TRACEPARENT"
TRACESTATE_ENV = "TRACESTATE"

_propagator = TraceContextTextMapPropagator()


def inject_into(environ: dict[str, str]) -> dict[str, str]:
    """Write the CURRENT span context into ``environ`` and return it.

    A no-op when no span is active or the active span is invalid (which is what a no-op
    tracer produces), so a telemetry-off parent hands the child an unchanged environment
    rather than a ``traceparent`` of all zeroes -- the child would otherwise treat that as
    a valid-looking remote parent and drop its own trace on the floor.

    Guarded like :func:`extract_from`, and for a sharper reason: this runs inside
    ``run_analysis_subprocess`` BEFORE the child is spawned, where the enclosing ``try``
    handles only ``FileNotFoundError``. An unguarded propagator fault here would fail every
    analysis spawn on the worker -- the child degrading to its own fresh trace is the
    correct outcome, per the module rule that both directions are total.
    """
    try:
        if not trace.get_current_span().get_span_context().is_valid:
            return environ
        carrier: dict[str, str] = {}
        _propagator.inject(carrier)
        for key, value in carrier.items():
            if value:
                environ[key.upper()] = value
    except Exception:
        return environ
    return environ


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """A full environment for the analysis child: this process's, plus the traceparent.

    Returns a COPY. Mutating ``os.environ`` to pass a traceparent would leak the
    parent's span context into every other subprocess this process ever spawns, and into
    any library that reads the environment.
    """
    return inject_into(dict(os.environ if base is None else base))


def extract_from(environ: dict[str, str] | None = None) -> Context | None:
    """Read a remote parent context out of the environment, or None when there is none.

    Returns ``None`` rather than an empty ``Context`` for the absent case so the caller
    can pass the result straight to ``start_as_current_span(context=...)``: ``None`` there
    means "use the ambient context", which for a freshly exec'd child is the root.
    """
    env = os.environ if environ is None else environ
    traceparent = env.get(TRACEPARENT_ENV, "").strip()
    if not traceparent:
        return None
    carrier = {"traceparent": traceparent}
    tracestate = env.get(TRACESTATE_ENV, "").strip()
    if tracestate:
        carrier["tracestate"] = tracestate
    try:
        context = _propagator.extract(carrier)
    except Exception:
        return None
    span_context = trace.get_current_span(context).get_span_context()
    return context if span_context.is_valid else None
