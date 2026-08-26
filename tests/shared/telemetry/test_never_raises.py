"""The swallow branches -- the paths that make "instrumentation cannot break production" true.

These are the least glamorous lines in the package and the most load-bearing. Every one of
them is the difference between a dropped metric and a failed analyze job worth hours of burst
node time, and every one of them only executes when something has already gone wrong -- so
they are exactly the lines a happy-path test suite never reaches. The per-module coverage
floor is what surfaced that they were uncovered; the reason to cover them is that an
exception escaping any of them lands in a production pipeline.

The other half of this file is the small total-function edges: a status outside 100-599, a
tracestate alongside a traceparent, a version lookup that fails. Each is a branch written to
be total, and a branch written to be total and never exercised is a branch that is only
believed to be total.
"""

from __future__ import annotations

from typing import Any

import pytest

from phaze.telemetry import _env, bootstrap, context as telemetry_context, http as telemetry_http, instruments, saq as telemetry_saq, tracing


BLACK_HOLE = "http://192.0.2.1:4318"


class _Exploding:
    """An instrument whose every method raises -- what a broken SDK looks like from here."""

    def record(self, *_args: Any, **_kwargs: Any) -> None:
        msg = "instrument exploded"
        raise RuntimeError(msg)

    add = record
    set = record


@pytest.fixture
def exploding_instruments(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("phaze.analysis.tier.duration", "phaze.analysis.chunks", "phaze.pipeline.backlog"):
        monkeypatch.setitem(instruments._INSTRUMENTS, name, _Exploding())


def test_record_add_and_set_gauge_swallow_a_broken_instrument(exploding_instruments: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE contract, at its most literal: a broken instrument costs a metric, never the work."""
    monkeypatch.delenv(_env.STRICT_ENV, raising=False)
    instruments.record("phaze.analysis.tier.duration", 1.0, tier="fine")
    instruments.add("phaze.analysis.chunks", 1, tier="fine")
    instruments.set_gauge("phaze.pipeline.backlog", 1.0, backlog="awaiting_cloud")


@pytest.mark.parametrize(
    ("call", "name", "kwargs"),
    [
        (instruments.record, "phaze.analysis.tier.duration", {"tier": "fine"}),
        (instruments.add, "phaze.analysis.chunks", {"tier": "fine"}),
        (instruments.set_gauge, "phaze.pipeline.backlog", {"backlog": "awaiting_cloud"}),
    ],
)
def test_the_same_failure_RAISES_under_strict(call: Any, name: str, kwargs: dict[str, Any], exploding_instruments: None) -> None:
    """...and the test suite sets strict, so a broken instrument is a red test here rather
    than a silence nobody notices."""
    with pytest.raises(RuntimeError, match="instrument exploded"):
        call(name, 1.0, **kwargs)


def test_configure_returns_false_when_installation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An endpoint was configured and phaze could not honour it. The process keeps running.

    This is the branch that decides whether a bad collector URL is a logged warning or a
    worker that will not boot.
    """
    monkeypatch.setenv(_env.ENDPOINT_ENV, BLACK_HOLE)
    bootstrap._reset_for_tests()

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        msg = "SDK installation exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(bootstrap, "_install", _explode)
    assert bootstrap.configure_telemetry("api") is False
    assert bootstrap.shutdown_telemetry(50) is True


def test_shutdown_survives_providers_that_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """``shutdown_telemetry`` is called from a ``finally`` in the analysis child on every path.

    A teardown that raised there would replace a successful analysis's result -- hours of
    work -- with an exporter's stack trace.
    """

    class _BadProvider:
        def force_flush(self, _timeout: float) -> bool:
            msg = "flush exploded"
            raise RuntimeError(msg)

        def shutdown(self, **_kwargs: Any) -> None:
            msg = "shutdown exploded"
            raise RuntimeError(msg)

    monkeypatch.setattr(bootstrap, "_tracer_provider", _BadProvider())
    monkeypatch.setattr(bootstrap, "_meter_provider", _BadProvider())
    # Completes: the exceptions are caught inside the teardown thread, so the sequence runs.
    assert bootstrap.shutdown_telemetry(200) is True
    bootstrap._reset_for_tests()


def test_the_version_lookup_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs during bootstrap, before anything is installed. It may not be the thing that
    stops a process from starting."""
    import importlib.metadata

    def _explode(_name: str) -> str:
        msg = "no such distribution"
        raise RuntimeError(msg)

    monkeypatch.setattr(importlib.metadata, "version", _explode)
    assert bootstrap._version() == "unknown"


def test_the_deployment_environment_attribute_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_DEPLOYMENT_ENVIRONMENT", "home-server")
    assert bootstrap._resource_attributes("api", "phaze-api")["deployment.environment.name"] == "home-server"
    monkeypatch.delenv("PHAZE_DEPLOYMENT_ENVIRONMENT")
    assert "deployment.environment.name" not in bootstrap._resource_attributes("api", "phaze-api")


@pytest.mark.parametrize(("status", "expected"), [(None, "error"), (0, "error"), (99, "error"), (600, "error"), (100, "1xx"), (599, "5xx")])
def test_a_status_outside_the_http_range_folds_into_error(status: int | None, expected: str) -> None:
    """An ASGI app can send any integer. Anything outside 100-599 must land on ONE literal
    rather than minting a series per nonsense value."""
    assert telemetry_http._status_class(status) == expected


def test_the_mount_table_follows_a_lazily_included_router() -> None:
    """The fallback path for routes that set no ``scope["route"]``.

    Recursion through ``original_router`` is what this FastAPI version needs, and it is the
    exact shape whose absence made the first implementation recover 4 templates out of 36.
    """

    class _Endpoint:
        pass

    endpoint = _Endpoint()

    class _Leaf:
        path_format = "/leaf/{leaf_id}"
        routes = None
        original_router = None

        def __init__(self) -> None:
            self.endpoint = endpoint

    class _Router:
        routes = (_Leaf(),)

    class _Included:
        path_format = ""
        routes = None
        original_router = _Router()

    table = telemetry_http._mount_table([_Included()])
    assert table[id(endpoint)] == "/leaf/{leaf_id}"


def test_extract_carries_a_tracestate_alongside_the_traceparent() -> None:
    valid = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    extracted = telemetry_context.extract_from({telemetry_context.TRACEPARENT_ENV: valid, telemetry_context.TRACESTATE_ENV: "vendor=value"})
    assert extracted is not None


def test_a_propagator_that_raises_yields_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Total in both directions: the child starts a fresh trace rather than dying before it
    has done any work."""

    class _Explode:
        def extract(self, _carrier: dict[str, str]) -> None:
            msg = "propagator exploded"
            raise RuntimeError(msg)

    monkeypatch.setattr(telemetry_context, "_propagator", _Explode())
    assert telemetry_context.extract_from({telemetry_context.TRACEPARENT_ENV: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}) is None


def test_current_trace_id_is_none_without_a_span() -> None:
    assert tracing.current_trace_id() is None


def test_hooks_returns_the_pair_a_worker_settings_dict_needs() -> None:
    before, after = telemetry_saq.hooks()
    assert before is telemetry_saq.before_process
    assert after is telemetry_saq.after_process


def test_a_degraded_stage_activity_read_publishes_nothing(telemetry_sink: Any) -> None:
    """A read that FAILED must not be published as zeros.

    ``get_stage_activity_snapshot`` returns ``available=False`` with empty counts rather than
    raising, precisely so a failed ``saq_jobs`` read stays distinguishable from a measured
    empty queue. Publishing those zeros would turn "we could not tell" into "the queue is
    empty" on a dashboard -- the confusion that type exists to remove.

    This drives the REAL recorder, not a copy of its guard: the check lives with the
    publisher for exactly that reason.
    """
    from phaze.services.pipeline import StageActivitySnapshot
    from phaze.telemetry import pipeline as telemetry_pipeline

    telemetry_pipeline.record_stage_inflight(StageActivitySnapshot(counts={"analyze": {"queued": 0, "active": 0}}, available=False))
    assert telemetry_sink.attribute_sets("phaze.pipeline.stage.inflight") == [], "a degraded read published something"

    telemetry_pipeline.record_stage_inflight(StageActivitySnapshot(counts={"analyze": {"queued": 9, "active": 4}}, available=True))
    assert len(telemetry_sink.attribute_sets("phaze.pipeline.stage.inflight")) == 2


def test_the_sdk_kill_switch_is_honoured_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OTEL_SDK_DISABLED=true`` means OFF, and phaze says so rather than pretending.

    It is the SDK's own kill switch, and the SDK honours it by handing out NO-OP meters and
    tracers from providers that construct fine and accept every call. Installing providers
    anyway would have `configure_telemetry` return True and log `telemetry_on` while nothing
    was ever recorded -- an operator reading the boot log would believe telemetry was
    running.

    MEASURED: `bh work submit` sets this in its clean-checkout validation environment, which
    is how it was found -- 24 of this package's tests failed there and nowhere else.
    """
    monkeypatch.setenv(_env.ENDPOINT_ENV, BLACK_HOLE)
    monkeypatch.setenv(_env.SDK_DISABLED_ENV, "true")
    bootstrap._reset_for_tests()
    assert bootstrap.configure_telemetry("api") is False

    # The SDK's own spelling, matched exactly: anything but a case-insensitive "true" is not
    # the kill switch, and reading it more loosely would turn telemetry off by surprise.
    for value, expected in (("true", True), ("TRUE", True), ("  True  ", True), ("false", False), ("1", False), ("", False)):
        monkeypatch.setenv(_env.SDK_DISABLED_ENV, value)
        assert _env.sdk_disabled() is expected, f"{value!r} parsed as {not expected}"


def test_a_disabled_sdk_provider_really_does_record_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mechanism itself, pinned against the installed SDK rather than assumed.

    This is what makes the guard above load-bearing: a provider built under the kill switch
    is not an error, it is a silence. Verified here so an SDK upgrade that changed the
    behaviour would fail this test instead of quietly re-enabling a path phaze reports as off.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    monkeypatch.setenv(_env.SDK_DISABLED_ENV, "true")
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    provider.get_meter("probe").create_counter("probe.counter").add(1)
    data = reader.get_metrics_data()
    recorded = [
        metric.name
        for resource_metric in getattr(data, "resource_metrics", ()) or ()
        for scope_metric in resource_metric.scope_metrics
        for metric in scope_metric.metrics
    ]
    assert recorded == [], f"OTEL_SDK_DISABLED=true no longer silences the SDK; it recorded {recorded}"
    provider.shutdown()
