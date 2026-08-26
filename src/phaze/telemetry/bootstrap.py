"""Install (or decline to install) the OpenTelemetry SDK for one phaze process.

**The whole module is a no-op unless an OTLP endpoint is configured.** That is
phaze-m1drf.1 acceptance 5, and it is what makes the instrumentation scattered through
``services/analysis.py``, the HTTP middleware and the SAQ hooks free by default: with no
provider installed, ``get_tracer`` / ``get_meter`` resolve to the API's no-op
implementations.

**Instrumentation must never be able to fail or stall the work it observes**
(phaze-m1drf.1 acceptance 7, phaze-m1drf.2 acceptance 3). Four things deliver that, and
each one is a deliberate departure from an SDK default:

1. **Every entry point in this module swallows.** ``configure_telemetry`` catches
   ``Exception`` and returns ``False``; a malformed endpoint, an unreadable certificate
   or an import error leaves the process running with telemetry off.
2. **Export is asynchronous and bounded.** ``BatchSpanProcessor`` and
   ``PeriodicExportingMetricReader`` each own a background thread with a BOUNDED queue.
   A full queue DROPS. A dropped span is acceptable; a blocked analysis is not.
3. **The SDK's ``atexit`` hooks are disabled** (``shutdown_on_exit=False``). Left on,
   process exit calls ``shutdown()``, which performs a final export with the SDK's own
   30 s budget -- on a black-holed endpoint that is 30 s of a k8s Job refusing to die
   after its work is finished. :func:`shutdown_telemetry` replaces it with a flush the
   caller bounds (``PHAZE_TELEMETRY_FLUSH_TIMEOUT_MS``, default 3,000 ms).
4. **HTTP/protobuf, not gRPC.** ``requests``-based export has one connect+read timeout
   per attempt and no channel state to unwind; the gRPC channel keeps reconnection
   backoff across a shutdown call.

The endpoint is NOT validated for reachability at startup, deliberately. A reachability
probe would be a startup dependency on a machine phaze does not own -- exactly the
coupling this design exists to avoid -- and it would still say nothing about whether the
collector is up ten minutes later.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace

from phaze.telemetry import _env
from phaze.telemetry.catalogue import CATALOGUE


if TYPE_CHECKING:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.view import View
    from opentelemetry.sdk.trace import TracerProvider

log = logging.getLogger(__name__)

#: Set once telemetry has been configured (or declined) for this process. Configuration
#: is idempotent because three of the four roles can reach it by more than one path --
#: the api through the FastAPI lifespan, a SAQ worker through its startup hook, and the
#: analysis child through its ``main``.
_lock = threading.Lock()
_configured = False
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None

#: Roles, and the ``service.name`` each reports. Bounded on purpose: ``service.name``
#: becomes the Prometheus ``job`` label.
SERVICE_NAMES: dict[str, str] = {
    "api": "phaze-api",
    "controller": "phaze-controller",
    "agent": "phaze-agent",
    "analysis": "phaze-analysis",
    "watcher": "phaze-watcher",
}

#: Overrides ``service.instance.id``. READ THE DOCSTRING BEFORE SETTING THIS PER POD.
INSTANCE_ENV = "PHAZE_TELEMETRY_INSTANCE"


def _resource_attributes(role: str, service_name: str) -> dict[str, str]:
    """Resource attributes for the METRICS provider -- bounded, and bounded on purpose.

    ``service.instance.id`` becomes the Prometheus ``instance`` label, and it multiplies
    EVERY series this service emits. The analysis role emits ~1,700 series (see
    ``catalogue.total_series``) and runs as a k8s Job whose pod name is unique per file,
    so defaulting this to the hostname would mint a fresh ~1,700-series block per analyzed
    file -- 11,428 files x 1,700 = a shared Prometheus phaze does not own, destroyed by a
    default nobody chose.

    So the default is the SERVICE NAME itself: one instance per role, and every analyze
    pod's data lands on it. An operator running phaze on several hosts sets
    ``PHAZE_TELEMETRY_INSTANCE`` to the HOST (``host-prod`` / ``vox``), never to the pod.
    Pod identity is not lost -- it is carried on SPANS, where it belongs (see
    :func:`_trace_resource_attributes`).
    """
    attributes = {
        "service.name": service_name,
        "service.namespace": "phaze",
        "service.version": _version(),
        "service.instance.id": os.environ.get(INSTANCE_ENV, "").strip() or service_name,
        "phaze.role": role,
    }
    environment = os.environ.get("PHAZE_DEPLOYMENT_ENVIRONMENT", "").strip()
    if environment:
        attributes["deployment.environment.name"] = environment
    return attributes


def _trace_resource_attributes(role: str, service_name: str) -> dict[str, str]:
    """Metrics resource PLUS the per-process identity that would be poison on a metric.

    A trace is stored per-span and aged out; a metric label is stored forever, per series.
    So the pod name, the node and the pid go here and nowhere else.
    """
    attributes = dict(_resource_attributes(role, service_name))
    attributes["process.pid"] = str(os.getpid())
    for env_name, attribute in (
        ("HOSTNAME", "host.name"),
        ("PHAZE_POD_NAME", "k8s.pod.name"),
        ("PHAZE_NODE_NAME", "k8s.node.name"),
        ("PHAZE_BACKEND_ID", "phaze.backend.id"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            attributes[attribute] = value
    return attributes


def _version() -> str:
    """phaze's own version, or ``unknown`` -- never an exception during bootstrap."""
    try:
        from importlib.metadata import version  # noqa: PLC0415  # deferred: only needed when telemetry is on

        return version("phaze")
    except Exception:
        return "unknown"


def _views() -> list[View]:
    """One View per catalogued histogram, carrying its measured bucket ladder.

    The OTel metrics API has no per-instrument bucket argument by design -- aggregation is
    a provider concern -- so this is the only place the ladders in
    :mod:`phaze.telemetry.catalogue` can be attached. A histogram with no View gets the
    SDK default ladder (5 ms .. 10 s), which is useless at both ends of this workload.
    """
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View  # noqa: PLC0415  # SDK import, telemetry-on path only

    return [
        View(
            instrument_name=spec.name,
            aggregation=ExplicitBucketHistogramAggregation(boundaries=spec.buckets),
        )
        for spec in CATALOGUE
        if spec.kind == "histogram" and spec.buckets
    ]


def configure_telemetry(role: str, *, service_name: str | None = None) -> bool:
    """Install the SDK for ``role``; return True when telemetry is now ON.

    Idempotent and thread-safe: the second call returns the first call's verdict without
    touching the providers. Returns False when no OTLP endpoint is configured (the
    default) and when configuration itself failed -- the caller cannot tell those apart
    and must not care, because both mean "carry on with telemetry off".
    """
    global _configured, _tracer_provider, _meter_provider  # process-wide singletons by design
    with _lock:
        if _configured:
            return _tracer_provider is not None
        _configured = True
        if not _env.endpoint_configured():
            log.debug("telemetry_off_no_endpoint")
            return False
        if _env.sdk_disabled():
            # The SDK's own kill switch. Installing providers anyway would "succeed" and then
            # record nothing, which is the worst of both: an operator reading the boot log
            # would believe telemetry is on. Say so instead.
            log.info("telemetry_off_sdk_disabled role=%s (%s=true)", role, _env.SDK_DISABLED_ENV)
            return False
        try:
            _env.apply_export_defaults()
            resolved = service_name or SERVICE_NAMES.get(role, f"phaze-{role}")
            _tracer_provider, _meter_provider = _install(role, resolved)
        except Exception:
            # An endpoint was configured and we could not honour it. Log and carry on --
            # this is the acceptance-7 contract at its most literal.
            log.warning("telemetry_configuration_failed", exc_info=True)
            _tracer_provider = None
            _meter_provider = None
            return False
        log.info("telemetry_on role=%s endpoint=%s", role, os.environ.get(_env.ENDPOINT_ENV, "<signal-specific>"))
        return True


def _install(role: str, service_name: str) -> tuple[TracerProvider, MeterProvider]:
    """Build and register both providers. Raises on failure; the caller swallows."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter  # noqa: PLC0415  # SDK imports, telemetry-on path only
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415
    from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader  # noqa: PLC0415
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    from phaze.telemetry import instruments  # noqa: PLC0415  # circular at module scope: instruments imports the API, not this

    tracer_provider = TracerProvider(
        resource=Resource.create(_trace_resource_attributes(role, service_name)),
        # See this module's docstring, point 3. THE default here is True.
        shutdown_on_exit=False,
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=Resource.create(_resource_attributes(role, service_name)),
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        views=_views(),
        shutdown_on_exit=False,
    )
    metrics.set_meter_provider(meter_provider)
    # The proxy meter created at import forwards to whatever provider is installed later,
    # but its instruments were built before the Views existed. Rebuild so every histogram
    # picks up its ladder.
    instruments._reset_for_tests()
    return tracer_provider, meter_provider


def shutdown_telemetry(timeout_ms: int | None = None) -> bool:
    """Flush and tear down, within a bounded budget. Returns True if teardown RAN to completion.

    **This is where a short-lived producer's final data is either delivered or lost**
    (phaze-m1drf.2 §2). An analyze Job accumulates counters for hours and then exits; there
    is no scrape to catch the tail, so the final push is the only delivery. The budget is
    ``PHAZE_TELEMETRY_FLUSH_TIMEOUT_MS`` (default 3,000 ms) and it is a CEILING on the delay
    a dead collector can add to process exit, not a guarantee of delivery. A False return
    means teardown was ABANDONED at the deadline and whatever was still queued is gone.

    **A True return does NOT prove delivery, and this is deliberately not dressed up as if
    it did.** ``force_flush`` reports that the SDK's queue drained, not that the collector
    accepted anything: measured against a listener that accepts and then stalls for 30 s,
    both providers' ``force_flush`` returned True while nothing had left the process, because
    the periodic worker had already taken the batch out of the queue and was sitting on a
    failing export. The SDK exposes no delivery signal, so this function does not invent one
    -- what it guarantees is the BOUND. Whether homelab received anything is a question for
    homelab's collector, and ``docs/telemetry/exporter.md`` §4 says so.

    **THE BUDGET IS ENFORCED BY A JOINED DAEMON THREAD, and it has to be.** Passing a
    timeout to the providers is NOT sufficient, and this was measured rather than reasoned
    about: ``TracerProvider.shutdown()`` takes no timeout argument at all and
    ``MeterProvider.shutdown()`` defaults to **30,000 ms**, so a first implementation that
    only bounded ``force_flush`` took **40.3 seconds** against a black-holed collector while
    asking for 3. For a k8s analyze Job that is 40 s of a pod refusing to die and holding a
    Kueue slot behind it, once per file.

    Abandoning the teardown when the budget expires is safe **because both SDK worker
    threads are daemons** -- verified against the installed SDK, not assumed:
    ``_shared_internal.BatchProcessor`` and ``PeriodicExportingMetricReader`` each create
    their worker with ``daemon=True``, so the interpreter will not wait for one at exit.
    ``tests/shared/telemetry/test_telemetry_never_breaks_analysis.py`` measures the bound
    end to end after a real analysis.

    Never raises. A telemetry teardown that raised out of a ``finally`` would replace a
    successful analysis's result with an exporter's stack trace.
    """
    global _tracer_provider, _meter_provider  # process-wide singletons by design
    budget = _env.flush_timeout_ms() if timeout_ms is None else timeout_ms
    providers = [provider for provider in (_tracer_provider, _meter_provider) if provider is not None]
    _tracer_provider = None
    _meter_provider = None
    if not providers:
        return True

    completed = threading.Event()

    def _teardown() -> None:
        # Deferred, like every other SDK import in this module: by the time this runs there
        # IS an SDK provider, but importing the SDK at module scope would put it in the
        # analysis child's import graph whether or not telemetry is on.
        from opentelemetry.sdk.metrics import MeterProvider as SDKMeterProvider  # noqa: PLC0415

        for provider in providers:
            try:
                provider.force_flush(budget)
            except Exception:
                log.debug("telemetry_flush_failed", exc_info=True)
        for provider in providers:
            try:
                # MeterProvider takes a budget; TracerProvider takes none. Both are inside
                # the joined thread, so the caller is bounded either way.
                if isinstance(provider, SDKMeterProvider):
                    provider.shutdown(timeout_millis=budget)
                else:
                    provider.shutdown()
            except Exception:
                log.debug("telemetry_shutdown_failed", exc_info=True)
        completed.set()

    worker = threading.Thread(target=_teardown, name="phaze-telemetry-shutdown", daemon=True)
    worker.start()
    # +1 s of slack: the budget is what each provider is ASKED for, and asking two of them
    # for it sequentially costs slightly more than one budget. The point is a small constant,
    # not the SDK's 30 s.
    worker.join((budget + 1000) / 1000)
    return completed.is_set()


def _reset_for_tests() -> None:
    """Forget that configuration happened, without flushing. Tests only."""
    global _configured, _tracer_provider, _meter_provider  # test seam
    with _lock:
        _configured = False
        _tracer_provider = None
        _meter_provider = None
