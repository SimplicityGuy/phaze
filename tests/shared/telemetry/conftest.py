"""In-memory OTel providers, so a test can read exactly what phaze emitted.

Every fixture here installs a REAL SDK provider with an in-memory exporter -- not a mock
of phaze's own seam. The distinction matters for the same reason ADR-0012 rule 3 does: a
mock of ``instruments.record`` would prove phaze called its own function, while an
in-memory ``MetricReader`` proves the observation survived instrument creation, the View's
bucket ladder and the attribute set -- which is where the interesting mistakes are.

``PHAZE_TELEMETRY_STRICT`` is set for the whole package (see :func:`strict_telemetry`), so
an undeclared metric attribute RAISES here instead of being dropped with a warning as it
is in production.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once
import pytest

from phaze.telemetry import _env, bootstrap, instruments, tracing


if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def strict_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make an undeclared metric attribute a test failure rather than a dropped label.

    Also clears ``OTEL_SDK_DISABLED``, and that is the important half. It is the SDK's own
    kill switch: with it set to ``true`` a provider constructs fine, accepts every call and
    hands out NO-OP meters and tracers. These tests exercise phaze's instrumentation and must
    not inherit the ambient environment's preference about whether telemetry runs.

    MEASURED, and it cost two 23-minute validation runs: `bh work submit` sets
    ``OTEL_SDK_DISABLED=true`` in its clean-checkout validation environment -- a perfectly
    reasonable thing for a tool with its own telemetry to do. Twenty-four of this package's
    tests failed there, deterministically, and passed everywhere else, including in a bare
    `git worktree` provisioned from scratch. Read off the live process with
    ``ps eww -p $(pgrep -f bin/pytest)``.
    """
    monkeypatch.setenv(_env.STRICT_ENV, "1")
    monkeypatch.delenv(_env.SDK_DISABLED_ENV, raising=False)


def reset_otel_globals() -> None:
    """Clear the PROCESS-GLOBAL OpenTelemetry providers.

    Any test that calls ``configure_telemetry`` more than once in a process needs this, and
    needing it is the point rather than an inconvenience. ``set_tracer_provider`` and
    ``set_meter_provider`` are ONE-WAY: the second call is a silent no-op. In production a
    process configures telemetry exactly once, so a test that configures twice is simulating
    something that cannot happen and must reset the state it is pretending to start from --
    otherwise it is asserting against a provider from a previous test.

    ``bootstrap._require_provider_took`` is what turned this from an invisible assumption into
    a visible requirement: before that guard, a second `configure_telemetry` returned True and
    quietly emitted into the FIRST test's provider.

    **THE `Once` IS THE MECHANISM, NOT THE GLOBAL** -- and getting that wrong the first time
    is instructive. ``set_tracer_provider`` does not check whether ``_TRACER_PROVIDER`` is
    already set; it calls ``_TRACER_PROVIDER_SET_ONCE.do_once(...)``. So clearing only the
    global leaves the spent ``Once`` in place and NO later provider can be installed at all --
    ``get_tracer_provider()`` then returns the PROXY, not the provider you assigned. And the
    converse: assigning the global without firing the ``Once`` does not simulate "somebody got
    there first", because the next real ``set_tracer_provider`` fires the unspent ``Once`` and
    overwrites you. Both halves have to be reset together.
    """
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    metrics._internal._METER_PROVIDER = None
    metrics._internal._METER_PROVIDER_SET_ONCE = Once()


def _install_providers() -> tuple[InMemoryMetricReader, InMemorySpanExporter, MeterProvider, TracerProvider]:
    """Give this test its OWN providers, and bind phaze's seams to them EXPLICITLY.

    **The explicit binding is the whole point, and it was bought with a red run.** Both
    ``trace.set_tracer_provider`` and ``metrics.set_meter_provider`` are ONE-WAY: the first
    provider installed in a process wins and later calls are ignored with a log line. Writing
    the private globals gets around that, but it is still a RACE -- the API's proxy tracer and
    proxy meter CACHE whatever they first resolved to, so once anything else in an
    8,000-test suite has installed a provider, seams rebuilt "against the current provider"
    can still be holding the old one's.

    That produced exactly one red run out of six: ``bh work submit``'s clean-checkout
    validation failed 21 of this package's own tests, while five full-suite runs at other
    pytest-randomly seeds passed. The signature was unmistakable -- every test using
    ``telemetry_sink`` failed, and the single test that would pass VACUOUSLY against an empty
    sink was the only one of them that did not. Re-running until green would have been the
    wrong move; binding explicitly removes the race instead of re-rolling it.
    """
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader], views=bootstrap._views(), shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

    # The global state is still set, because production code paths OUTSIDE phaze.telemetry
    # read it (the HTTP middleware's spans, for one). Nothing below DEPENDS on this write
    # having won, which is the difference from the version that flaked.
    #
    # `_PROXY_METER_PROVIDER` is deliberately left alone: it is what `get_meter_provider`
    # falls back to once `_METER_PROVIDER` is cleared again in teardown, and nulling it turns
    # that fallback into an AttributeError on the next `get_meter`.
    metrics._internal._METER_PROVIDER = meter_provider
    trace._TRACER_PROVIDER = tracer_provider

    # ...and phaze's own seams are bound to THESE providers by hand. This is what makes the
    # fixture order-independent rather than merely usually-right.
    instruments._reset_for_tests(factory=lambda: meter_provider.get_meter("phaze"))
    tracing._reset_for_tests(factory=lambda: tracer_provider.get_tracer("phaze"))
    return reader, exporter, meter_provider, tracer_provider


def _prove_the_binding_takes() -> None:
    """Bind a THROWAWAY provider pair the same way, and prove an observation reaches it.

    Run before the test's own providers are installed, and on providers that are then
    discarded -- so the probe cannot pollute the sink the test reads. An
    ``InMemoryMetricReader`` reports CUMULATIVE state, so a probe recorded into the test's own
    provider would still be visible to it afterwards, and a test asserting an exact count
    would be silently off by one.

    What this checks is the BINDING MECHANISM on the identical code path, immediately before
    it is used for real. It cannot catch a failure that afflicts only the second call; that is
    the honest limit of it, and it is still the difference between one loud line and
    twenty-one confusing ones.

    A sink that silently records nothing is the worst possible failure shape for this
    package. It does not raise; it turns every assertion in every test that uses it into a
    plain "expected X, got nothing", and it turns the one test that asserts an ABSENCE
    (``test_no_analysis_metric_carries_an_identifier``) green. Twenty-one failures and one
    misleading pass, with no line pointing at the fixture.

    That is exactly what one clean-checkout run produced, and working back from the symptom
    cost far more than this probe does. So the fixture now proves itself with a throwaway
    observation and a throwaway span, and fails LOUDLY and in one place if the wiring is
    wrong -- whatever the cause turns out to be.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    instruments._reset_for_tests(factory=lambda: provider.get_meter("phaze"))
    tracing._reset_for_tests(factory=lambda: tracer_provider.get_tracer("phaze"))

    instruments.add("phaze.analysis.chunks", 1, tier="fine")
    with tracing.span("telemetry_sink.probe"):
        pass

    data = reader.get_metrics_data()
    recorded = [
        metric.name
        for resource_metric in getattr(data, "resource_metrics", ()) or ()
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    ]
    if "phaze.analysis.chunks" not in recorded:
        msg = (
            "the telemetry_sink fixture is not wired: a probe observation did not reach its own "
            f"InMemoryMetricReader (saw {recorded}). phaze.telemetry.instruments is bound to some "
            "other meter -- do not debug the test that reported this, debug the binding."
        )
        raise AssertionError(msg)
    if not exporter.get_finished_spans():
        msg = "the telemetry_sink fixture is not wired: a probe span did not reach its own InMemorySpanExporter."
        raise AssertionError(msg)

    provider.shutdown()
    tracer_provider.shutdown()


@pytest.fixture
def telemetry_sink() -> Iterator[TelemetrySink]:
    """A live SDK wired to memory. Yields a reader for both signals."""
    # Forget any configuration another test left behind BEFORE installing this one's
    # providers: `configure_telemetry` is idempotent by design, so a leaked `_configured`
    # would make a later `configure_telemetry` in a test silently no-op.
    bootstrap._reset_for_tests()
    _prove_the_binding_takes()
    reader, exporter, meter_provider, tracer_provider = _install_providers()
    try:
        yield TelemetrySink(reader, exporter)
    finally:
        meter_provider.shutdown()
        tracer_provider.shutdown()
        metrics._internal._METER_PROVIDER = None
        trace._TRACER_PROVIDER = None
        bootstrap._reset_for_tests()
        instruments._reset_for_tests()
        tracing._reset_for_tests()


class TelemetrySink:
    """Reads back what phaze emitted, in the shape assertions want."""

    def __init__(self, reader: InMemoryMetricReader, exporter: InMemorySpanExporter) -> None:
        self._reader = reader
        self._exporter = exporter

    def points(self, metric_name: str) -> list[Any]:
        """Every data point recorded for ``metric_name``, across all attribute sets."""
        data = self._reader.get_metrics_data()
        found: list[Any] = []
        for resource_metric in getattr(data, "resource_metrics", ()) or ():
            for scope_metric in resource_metric.scope_metrics:
                for metric in scope_metric.metrics:
                    if metric.name == metric_name:
                        found.extend(metric.data.data_points)
        return found

    def metric_names(self) -> set[str]:
        data = self._reader.get_metrics_data()
        return {
            metric.name
            for resource_metric in getattr(data, "resource_metrics", ()) or ()
            for scope_metric in resource_metric.scope_metrics
            for metric in scope_metric.metrics
        }

    def attribute_sets(self, metric_name: str) -> list[dict[str, Any]]:
        return [dict(point.attributes or {}) for point in self.points(metric_name)]

    def total(self, metric_name: str) -> float:
        """Sum of every point -- ``value`` for a counter/gauge, ``sum`` for a histogram."""
        return float(sum(getattr(point, "value", None) if hasattr(point, "value") else point.sum for point in self.points(metric_name)))

    def count(self, metric_name: str) -> int:
        """Number of OBSERVATIONS recorded into a histogram, summed over attribute sets."""
        return int(sum(point.count for point in self.points(metric_name)))

    def spans(self) -> list[Any]:
        return list(self._exporter.get_finished_spans())

    def span_names(self) -> list[str]:
        return [span.name for span in self.spans()]
