"""phaze's OpenTelemetry seam -- OFF unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.

Start with ``docs/telemetry/exporter.md`` (how to point phaze at a collector) and
``docs/telemetry/metric-catalogue.md`` (what it emits, and each label's bound).

Four rules bind everything under this package:

1. **Instrumentation may never fail or stall the work it observes.** Every entry point
   here swallows; export is asynchronous with a bounded, dropping queue; teardown has a
   caller-bounded budget. A dropped metric is acceptable, a failed analyze job is not.
2. **A file id is never a metric label.** File, window and chunk identity are SPAN
   attributes. See ``catalogue.FORBIDDEN_LABEL_SUBSTRINGS`` and the guard test.
3. **Every metric is in the catalogue.** Instruments are built only in
   ``instruments.py``, only from ``catalogue.CATALOGUE``.
4. **phaze emits; it does not deploy.** The collector, Prometheus and Grafana belong to
   homelab. This repo ships the exporter, the contract, an example compose for local
   development, importable dashboards and adoptable alert rules -- and no production
   topology.
"""

from __future__ import annotations

from phaze.telemetry.bootstrap import configure_telemetry, shutdown_telemetry
from phaze.telemetry.context import child_environment, extract_from
from phaze.telemetry.instruments import add, record, set_gauge
from phaze.telemetry.tracing import span


__all__ = [
    "add",
    "child_environment",
    "configure_telemetry",
    "extract_from",
    "record",
    "set_gauge",
    "shutdown_telemetry",
    "span",
]
