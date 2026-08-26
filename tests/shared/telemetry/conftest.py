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
import pytest

from phaze.telemetry import _env, bootstrap, instruments, tracing


if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def strict_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make an undeclared metric attribute a test failure rather than a dropped label."""
    monkeypatch.setenv(_env.STRICT_ENV, "1")


def _install_providers() -> tuple[InMemoryMetricReader, InMemorySpanExporter, MeterProvider, TracerProvider]:
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader], views=bootstrap._views(), shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider(shutdown_on_exit=False)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The API refuses a SECOND set_*_provider in a process, logging a warning and keeping
    # the first -- so the private globals are set directly. That is the only way to give
    # each test a clean provider, and it is confined to this fixture.
    metrics._internal._METER_PROVIDER = meter_provider
    # `_PROXY_METER_PROVIDER` is deliberately left alone: it is what `get_meter_provider`
    # falls back to once `_METER_PROVIDER` is cleared again in teardown, and nulling it
    # turns that fallback into an AttributeError on the next `get_meter`.
    trace._TRACER_PROVIDER = tracer_provider
    instruments._reset_for_tests()
    tracing._reset_for_tests()
    return reader, exporter, meter_provider, tracer_provider


@pytest.fixture
def telemetry_sink() -> Iterator[TelemetrySink]:
    """A live SDK wired to memory. Yields a reader for both signals."""
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
