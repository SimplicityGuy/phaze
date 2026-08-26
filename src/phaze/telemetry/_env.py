"""Environment resolution for the telemetry seam -- stdlib only, no phaze imports.

Deliberately does NOT read ``phaze.config``. Two reasons, both load-bearing:

* the exec'd analysis child (``phaze.analysis_child``) must be able to configure
  telemetry before it imports anything essentia- or settings-bound, and
  ``phaze.config`` validates a whole role's settings block on import;
* ``OTEL_*`` is the SDK's own configuration surface, so an operator wiring phaze
  into an existing collector sets the SAME variables they set for every other
  service rather than learning a phaze-specific spelling (phaze-m1drf.2 §5).

The one phaze-specific variable is ``PHAZE_TELEMETRY_STRICT`` (see
``instruments.py``): a TEST-only knob that turns an attribute-set violation into
an exception instead of a dropped label. It is never set in production.
"""

from __future__ import annotations

import os


# The single switch. Telemetry is OFF unless this is set to a non-empty value
# (phaze-m1drf.1 acceptance 5). "OFF" means no SDK provider is installed at all,
# so every ``get_tracer`` / ``get_meter`` call in the codebase resolves to the
# API's built-in no-op -- see docs/telemetry/overhead.md for what that costs.
ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
TRACES_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
METRICS_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"

# Bounded-by-default export knobs. Every one of these has an SDK default that is
# LONGER than what an analysis pod can afford to spend at exit (phaze-m1drf.2 §2),
# so phaze lowers them and lets the operator raise them back.
#
# **THE UNITS ARE NOT UNIFORM, AND THIS WAS MEASURED RATHER THAN ASSUMED.** In
# opentelemetry-python 1.44.0 `OTEL_EXPORTER_OTLP_TIMEOUT` is in **SECONDS** (the exporter
# reads it straight into `requests`' `timeout=`, default 10), while every `OTEL_BSP_*` and
# `OTEL_METRIC_EXPORT_*` knob beside it is in **MILLISECONDS**. The OTel specification says
# milliseconds for all of them, so a value carried over from the spec -- or from another
# language's SDK -- is wrong by 1000x in the dangerous direction. Setting it to "5000"
# meaning 5 s gave a 5,000-SECOND (83-minute) per-batch deadline, and a black-holed
# collector then held process exit open for the length of it. ADR-0016
# (docs/design/0016-transferred-model-verification.md) is the rule this instance belongs
# to, and `tests/shared/telemetry/test_export_timeout_units.py` pins it against the
# installed SDK so an upgrade that unifies the units fails loudly instead of silently
# re-introducing the 1000x.
_DEFAULTS: dict[str, str] = {
    # SECONDS. 10 is the SDK default; 5 bounds one whole export attempt -- the exporter's
    # own retry loop is deadlined at `time() + timeout`, so this bounds the retries too.
    "OTEL_EXPORTER_OTLP_TIMEOUT": "5",
    # Span batching. A queue that is full DROPS, which is the correct failure
    # mode here: a dropped span is acceptable, a blocked analysis is not.
    "OTEL_BSP_MAX_QUEUE_SIZE": "2048",
    "OTEL_BSP_SCHEDULE_DELAY": "5000",  # milliseconds
    "OTEL_BSP_EXPORT_TIMEOUT": "5000",  # milliseconds (SDK default 30000)
    "OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "512",
    # Metric export cadence. 15 s rather than the SDK's 60 s because an analyze
    # Job's whole lifetime is a few scrape intervals' worth of interesting
    # transitions and 60 s loses the shape of a chunk.
    "OTEL_METRIC_EXPORT_INTERVAL": "15000",  # milliseconds
    "OTEL_METRIC_EXPORT_TIMEOUT": "5000",  # milliseconds
}

# How long ``shutdown_telemetry`` may spend flushing before it gives up and lets
# the process exit anyway. This is the number phaze-m1drf.2 §2 quantifies the
# loss against: whatever has not left the queue when this expires is lost.
FLUSH_TIMEOUT_ENV = "PHAZE_TELEMETRY_FLUSH_TIMEOUT_MS"
DEFAULT_FLUSH_TIMEOUT_MS = 3000

STRICT_ENV = "PHAZE_TELEMETRY_STRICT"


def endpoint_configured(environ: dict[str, str] | None = None) -> bool:
    """True when ANY OTLP endpoint variable carries a non-empty value.

    The signal-specific variables count: an operator who exports only traces sets
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` and never the generic one, and reading
    only the generic name would leave that operator with telemetry silently off.
    """
    env = os.environ if environ is None else environ
    return any(env.get(name, "").strip() for name in (ENDPOINT_ENV, TRACES_ENDPOINT_ENV, METRICS_ENDPOINT_ENV))


def apply_export_defaults(environ: dict[str, str] | None = None) -> None:
    """Install phaze's bounded export defaults for any knob the operator left unset.

    ``setdefault``, never assignment: an operator who has tuned a value keeps it.
    """
    env = os.environ if environ is None else environ
    for name, value in _DEFAULTS.items():
        if not env.get(name, "").strip():
            env[name] = value


def flush_timeout_ms(environ: dict[str, str] | None = None) -> int:
    """Resolve the shutdown flush budget, falling back on an unparseable value."""
    env = os.environ if environ is None else environ
    raw = env.get(FLUSH_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_FLUSH_TIMEOUT_MS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_FLUSH_TIMEOUT_MS
    return parsed if parsed > 0 else DEFAULT_FLUSH_TIMEOUT_MS


def strict(environ: dict[str, str] | None = None) -> bool:
    """True when the attribute-set guard should RAISE rather than drop (tests only)."""
    env = os.environ if environ is None else environ
    return bool(env.get(STRICT_ENV, "").strip())
