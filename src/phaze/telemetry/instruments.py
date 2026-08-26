"""The metric instruments, built FROM :mod:`phaze.telemetry.catalogue` and nothing else.

Every ``create_counter`` / ``create_histogram`` / ``create_gauge`` call in phaze lives in
this module. That is enforced by
``tests/shared/telemetry/test_metric_catalogue.py::test_no_instrument_is_created_outside_this_module``
so a metric cannot be added without a catalogue entry, and therefore cannot be added
without a stated cardinality bound (phaze-m1drf.3 acceptance 4).

**Nothing here needs telemetry to be configured.** ``opentelemetry.metrics.get_meter``
returns a proxy that resolves to the API's no-op meter while no ``MeterProvider`` is
installed, and a no-op instrument's ``record`` / ``add`` is a bound method that returns
immediately. So every call site in the codebase is written unconditionally, with no
``if telemetry_enabled:`` guard to forget -- see ``docs/telemetry/overhead.md`` for what
that costs when the endpoint is unset.

The recording helpers below take **keyword** attributes and check them against the
catalogue. In production a mismatch drops the offending key and keeps the observation
(instrumentation must never raise into a production path); under
``PHAZE_TELEMETRY_STRICT=1`` -- which the test suite sets -- it raises, so the mismatch
is a red test rather than a metric nobody notices is wrong.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics

from phaze.telemetry import _env
from phaze.telemetry.catalogue import BY_NAME, CATALOGUE


if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

#: One meter for the whole application. The instrumentation SCOPE is phaze, not the
#: individual module -- a per-module scope would put the module name into the scope
#: dimension, which the OTLP -> Prometheus translation can surface as a label.
_METER_NAME = "phaze"

_meter = metrics.get_meter(_METER_NAME)

# Bucket boundaries reach the SDK through Views (see bootstrap.py), not through the
# instrument, because the OTel metrics API deliberately has no per-instrument bucket
# argument -- aggregation is a provider-side concern. An instrument created here before
# any provider exists still gets its View: the proxy meter re-creates every instrument
# against the real meter when the provider is installed.


def _instrument(name: str) -> Any:
    spec = BY_NAME[name]
    if spec.kind == "counter":
        return _meter.create_counter(name, unit=spec.unit, description=spec.description)
    if spec.kind == "histogram":
        # Bucket boundaries are NOT passed here even though `create_histogram` now takes an
        # advisory. An advisory is exactly that -- a hint a provider may ignore -- while the
        # View installed in bootstrap.py is binding. One mechanism, not two that can disagree.
        return _meter.create_histogram(name, unit=spec.unit, description=spec.description)
    if spec.kind == "updowncounter":
        return _meter.create_up_down_counter(name, unit=spec.unit, description=spec.description)
    return _meter.create_gauge(name, unit=spec.unit, description=spec.description)


_INSTRUMENTS: dict[str, Any] = {spec.name: _instrument(spec.name) for spec in CATALOGUE}


def _checked_attributes(name: str, attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop (or, under strict, reject) any attribute the catalogue does not declare.

    This is the runtime half of the cardinality guard. The static half -- the catalogue
    test -- cannot see an attribute assembled from a variable at a call site, and that is
    precisely the shape that puts a file id into a label.
    """
    allowed = BY_NAME[name].label_names
    unknown = set(attributes) - allowed
    if not unknown:
        return attributes
    msg = f"metric {name!r} received undeclared attribute(s) {sorted(unknown)}; declared: {sorted(allowed)}"
    if _env.strict():
        raise ValueError(msg)
    log.warning("telemetry_undeclared_attribute", extra={"detail": msg})
    return {key: value for key, value in attributes.items() if key in allowed}


def record(name: str, value: float, **attributes: Any) -> None:
    """Record one histogram observation. Never raises in production."""
    try:
        _INSTRUMENTS[name].record(value, _checked_attributes(name, attributes))
    except Exception:
        if _env.strict():
            raise
        log.debug("telemetry_record_failed", exc_info=True)


def add(name: str, value: float = 1, **attributes: Any) -> None:
    """Add to one counter / up-down counter. Never raises in production."""
    try:
        _INSTRUMENTS[name].add(value, _checked_attributes(name, attributes))
    except Exception:
        if _env.strict():
            raise
        log.debug("telemetry_add_failed", exc_info=True)


def set_gauge(name: str, value: float, **attributes: Any) -> None:
    """Set one synchronous gauge. Never raises in production."""
    try:
        _INSTRUMENTS[name].set(value, _checked_attributes(name, attributes))
    except Exception:
        if _env.strict():
            raise
        log.debug("telemetry_gauge_failed", exc_info=True)


def instrument_names() -> frozenset[str]:
    """Every instrument this module built -- the catalogue test's other half."""
    return frozenset(_INSTRUMENTS)


def _reset_for_tests(factory: Callable[[], Any] | None = None) -> None:
    """Rebind the meter and rebuild every instrument against the CURRENT provider.

    Tests install a real ``MeterProvider`` with an in-memory reader AFTER this module has
    been imported. The API's proxy meter does forward to a provider installed later, but a
    test that installs a SECOND provider (the next test) would keep the first one's
    instruments, so the reader would see nothing. Rebuilding is the reliable seam.

    **Pass ``factory``.** ``metrics.set_meter_provider`` is ONE-WAY -- the first provider
    installed in a process wins -- so a fixture that resolves its meter through the global
    provider is depending on winning that race against ~8,000 other tests, and will pass on
    some orderings and fail on others. A factory binds the instruments to the caller's own
    meter and takes global state out of it entirely.
    """
    global _meter  # module-level rebind is the point of this seam
    _meter = metrics.get_meter(_METER_NAME) if factory is None else factory()
    _INSTRUMENTS.clear()
    _INSTRUMENTS.update({spec.name: _instrument(spec.name) for spec in CATALOGUE})
