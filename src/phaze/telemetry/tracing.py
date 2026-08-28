"""Span and timing primitives -- the shapes the instrumented call sites actually use.

Three primitives, and the split between them is the cardinality rule in code form:

* :func:`span` opens a span. Span attributes are FREE-FORM: file id, window index and
  chunk index belong here, because a span is stored per-occurrence and aged out.
* :func:`timed_metric` times a block into a catalogued histogram and opens no span. This
  is what the hot loops use -- 34 models x 30 windows per coarse chunk is 1,020
  observations, which is a fine number of histogram records and an absurd number of spans.
* :func:`timed` does both, for the coarser phases where a span is worth having.

**Everything here is written to be cheap when telemetry is off.** ``get_tracer`` resolves
to the API's no-op, whose ``start_as_current_span`` returns an already-built context
manager and whose span drops every attribute. What remains is a ``perf_counter`` pair and
a dict build. ``docs/telemetry/overhead.md`` measures what that costs on a real run rather
than asserting it is nothing.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from phaze.telemetry.instruments import record


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from opentelemetry.context import Context

_tracer = trace.get_tracer("phaze")


@contextlib.contextmanager
def span(name: str, attributes: dict[str, Any] | None = None, *, context: Context | None = None) -> Iterator[trace.Span]:
    """Open a span named ``name`` with free-form attributes.

    ``attributes`` is a positional DICT rather than ``**kwargs`` for two reasons. The
    keys are dotted OTel attribute names (``phaze.analysis.chunk_index``), which are not
    valid Python identifiers; and a ``**kwargs`` span whose sibling ``timed`` takes
    ``**labels`` invites passing a chunk index to the one that turns it into a metric
    label. Two different shapes, so the mistake does not typecheck.

    ``context`` accepts a remote parent -- the analysis child passes the context it
    extracted from ``TRACEPARENT`` so its work joins the parent's trace rather than
    starting a second one.

    An exception escaping the block is recorded on the span and re-raised UNCHANGED. This
    seam never converts an error into a return value; the caller's own failure handling is
    what decides what an error means.
    """
    with _tracer.start_as_current_span(name, context=context) as current:
        if attributes and current.is_recording():
            current.set_attributes({key: value for key, value in attributes.items() if value is not None})
        try:
            yield current
        except Exception as exc:
            if current.is_recording():
                current.record_exception(exc)
                current.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}"))
            raise


@contextlib.contextmanager
def timed_metric(metric: str, **labels: Any) -> Iterator[None]:
    """Time the block into ``metric``. No span; the cheapest primitive.

    The observation is recorded in a ``finally``, so a raising block is still measured --
    a model that fails after 40 s of inference cost those 40 s, and dropping the
    observation would make the failure look free.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        record(metric, time.perf_counter() - started, **labels)


@contextlib.contextmanager
def timed(metric: str, span_name: str, *, attributes: dict[str, Any] | None = None, **labels: Any) -> Iterator[trace.Span]:
    """Open a span AND time the block into ``metric``.

    ``attributes`` are the span's (free-form); ``labels`` are the metric's (catalogued and
    bounded). Keeping them in two different parameters is what stops a chunk index that is
    perfectly good on a span from being passed through to a metric by habit.
    """
    started = time.perf_counter()
    with span(span_name, attributes) as current:
        try:
            yield current
        finally:
            record(metric, time.perf_counter() - started, **labels)


def current_trace_id() -> str | None:
    """Hex trace id of the active span, for correlating a log line to a trace."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def _reset_for_tests(factory: Callable[[], trace.Tracer] | None = None) -> None:
    """Rebind the module tracer. Tests only.

    ``factory`` binds DIRECTLY to a tracer the caller created, rather than to whatever
    ``get_tracer`` resolves out of process-global state. That matters because
    ``trace.set_tracer_provider`` is ONE-WAY -- the first provider installed in a process
    wins -- and the API's proxy tracer caches the first real tracer it resolves to. A fixture
    that depends on winning that race against ~8,000 other tests passes on some orderings and
    fails on others. Mirrors ``instruments._reset_for_tests``.
    """
    global _tracer  # test seam, mirrors instruments._reset_for_tests
    _tracer = trace.get_tracer("phaze") if factory is None else factory()
