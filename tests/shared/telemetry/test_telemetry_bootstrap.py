"""Configuration, and the contract that instrumentation cannot fail or stall the work.

phaze-m1drf.1 acceptance 5 and 7, and phaze-m1drf.2 acceptance 2 and 3, are all really
about ONE property: an operator's collector is a machine phaze does not own, and its
absence, slowness or misconfiguration must cost nothing but telemetry.

The endpoint used for the never-breaks tests is a BLACK HOLE -- a port on a
guaranteed-unroutable address (RFC 5737 TEST-NET-1, ``192.0.2.0/24``, which is reserved
for documentation and is not routed on any real network). A closed local port would be
the wrong instrument: it produces an immediate ECONNREFUSED, which is the EASY failure.
An unroutable address makes the exporter sit on a connect that will never complete, which
is the failure that could actually stall an analysis.
"""

from __future__ import annotations

import time

from opentelemetry import metrics, trace
import pytest

from phaze.telemetry import _env, bootstrap, tracing


#: RFC 5737 TEST-NET-1. Reserved for documentation, not routed -- a connect here hangs
#: rather than being refused, which is the failure mode worth testing against.
BLACK_HOLE = "http://192.0.2.1:4318"


@pytest.fixture(autouse=True)
def _clean_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (_env.ENDPOINT_ENV, _env.TRACES_ENDPOINT_ENV, _env.METRICS_ENDPOINT_ENV, _env.FLUSH_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    bootstrap._reset_for_tests()
    # The API's `set_tracer_provider` is one-way within a process, so a provider installed by
    # an EARLIER test file is still globally current here. Clearing the private globals is
    # the only way to give this file the no-provider state it is about -- otherwise these
    # tests pass or fail on collection order, which is the worst kind of red.
    trace._TRACER_PROVIDER = None
    metrics._internal._METER_PROVIDER = None
    tracing._reset_for_tests()


def test_telemetry_is_off_when_no_endpoint_is_configured() -> None:
    """The default, and the state 100% of production is in today (acceptance 5)."""
    assert bootstrap.configure_telemetry("api") is False
    # And the observable consequence: a span opened now is not recording, which is what
    # makes every unguarded instrumentation call site in the codebase free.
    with tracing.span("probe") as current:
        assert current.is_recording() is False
    assert trace.get_current_span().get_span_context().is_valid is False


def test_a_signal_specific_endpoint_alone_turns_telemetry_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator exporting only traces sets OTEL_EXPORTER_OTLP_TRACES_ENDPOINT and never
    the generic name. Reading only the generic one would leave them silently off."""
    monkeypatch.setenv(_env.TRACES_ENDPOINT_ENV, f"{BLACK_HOLE}/v1/traces")
    try:
        assert bootstrap.configure_telemetry("api") is True
    finally:
        bootstrap.shutdown_telemetry(50)


def test_configure_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three of the four roles can reach configuration by more than one path."""
    monkeypatch.setenv(_env.ENDPOINT_ENV, BLACK_HOLE)
    try:
        first = bootstrap.configure_telemetry("api")
        second = bootstrap.configure_telemetry("controller")
        assert first is True
        assert second is True
    finally:
        bootstrap.shutdown_telemetry(50)


def test_a_malformed_endpoint_leaves_the_process_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad endpoint is an operator typo, not a reason for the api to refuse to boot."""
    monkeypatch.setenv(_env.ENDPOINT_ENV, "not://a real:::endpoint")
    # Whatever the SDK makes of it, the ONLY acceptable outcomes are True or False --
    # never an exception out of configure_telemetry.
    assert bootstrap.configure_telemetry("api") in {True, False}
    bootstrap.shutdown_telemetry(50)


def test_export_defaults_are_installed_only_where_the_operator_left_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "12345")
    monkeypatch.delenv("OTEL_BSP_EXPORT_TIMEOUT", raising=False)
    _env.apply_export_defaults()
    import os

    assert os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] == "12345", "a tuned value must survive"
    assert os.environ["OTEL_BSP_EXPORT_TIMEOUT"] == "5000", "an unset knob gets phaze's bounded default"


def test_shutdown_against_a_black_hole_returns_inside_its_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE acceptance-7 mechanism, measured rather than asserted.

    This is what stops a k8s analyze Job from refusing to die for 30 s (the SDK's own
    shutdown budget) every time homelab is rebooting. The bound tested is generous
    relative to the 200 ms budget requested, because a loaded CI box adds real scheduling
    latency -- what is being refuted is 'it blocks for the SDK's 30 s default', not a
    millisecond-level claim.
    """
    monkeypatch.setenv(_env.ENDPOINT_ENV, BLACK_HOLE)
    assert bootstrap.configure_telemetry("analysis") is True

    started = time.perf_counter()
    bootstrap.shutdown_telemetry(200)
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0, f"shutdown took {elapsed:.2f}s against an unroutable endpoint"


def test_shutdown_with_no_provider_is_a_no_op() -> None:
    """Called from a ``finally`` in the analysis child on every path, including the one
    where configuration declined. It must not raise there."""
    assert bootstrap.shutdown_telemetry(50) is True


def test_the_flush_budget_is_configurable_and_falls_back_on_nonsense(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_env.FLUSH_TIMEOUT_ENV, "750")
    assert _env.flush_timeout_ms() == 750
    monkeypatch.setenv(_env.FLUSH_TIMEOUT_ENV, "not-a-number")
    assert _env.flush_timeout_ms() == _env.DEFAULT_FLUSH_TIMEOUT_MS
    monkeypatch.setenv(_env.FLUSH_TIMEOUT_ENV, "-1")
    assert _env.flush_timeout_ms() == _env.DEFAULT_FLUSH_TIMEOUT_MS


def test_the_metrics_resource_does_not_carry_a_per_pod_instance_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cardinality trap that lives OUTSIDE the label set.

    ``service.instance.id`` becomes the Prometheus ``instance`` label and multiplies EVERY
    series the service emits. The analysis role runs as a k8s Job whose pod name is unique
    per analyzed file, so defaulting this to the hostname would mint a fresh block of
    ~1,700 analysis series per file across an 11,428-file archive. It defaults to the
    service name; the pod identity goes on SPANS instead.
    """
    monkeypatch.setenv("HOSTNAME", "phaze-analyze-abc123-xyz")
    monkeypatch.delenv(bootstrap.INSTANCE_ENV, raising=False)

    metric_attributes = bootstrap._resource_attributes("analysis", "phaze-analysis")
    trace_attributes = bootstrap._trace_resource_attributes("analysis", "phaze-analysis")

    assert metric_attributes["service.instance.id"] == "phaze-analysis"
    assert "phaze-analyze-abc123-xyz" not in metric_attributes.values()
    assert trace_attributes["host.name"] == "phaze-analyze-abc123-xyz"


def test_an_operator_can_pin_the_instance_to_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Several hosts is the case the override exists for -- and a HOST is bounded."""
    monkeypatch.setenv(bootstrap.INSTANCE_ENV, "vox")
    assert bootstrap._resource_attributes("analysis", "phaze-analysis")["service.instance.id"] == "vox"
