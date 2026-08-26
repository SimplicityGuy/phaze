"""Pin the export-timeout UNITS against the installed SDK (ADR-0016).

``docs/design/0016-transferred-model-verification.md`` names the failure this file exists
for: *a belief carried in from a neighbouring system presents as something you already know
rather than as something you are claiming, so verification never fires.*

The OpenTelemetry SPECIFICATION says ``OTEL_EXPORTER_OTLP_TIMEOUT`` is in **milliseconds**.
opentelemetry-python 1.44.0 reads it in **SECONDS** -- straight into ``requests``'
``timeout=``, defaulting to 10 -- while every ``OTEL_BSP_*`` and ``OTEL_METRIC_EXPORT_*``
knob beside it really is in milliseconds. A value carried over from the spec is therefore
wrong by 1000x, in the direction that hurts: phaze shipped ``"5000"`` meaning five seconds
and got a **5,000-second (83-minute)** per-batch deadline. The analysis itself still
finished -- the exporter runs on its own thread -- but process EXIT hung behind the
exporter's non-daemon worker, which for a k8s analyze Job means a pod that will not die and
a Kueue slot it will not release.

**Why a test and not a comment.** A comment records what was true when it was written. This
asserts it against whatever SDK is installed right now, so the day upstream unifies the
units -- the fix everyone wants -- phaze finds out from a red test rather than from a
1000x-too-short timeout in production.
"""

from __future__ import annotations

import os

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from phaze.telemetry import _env


def test_the_otlp_timeout_is_still_read_in_seconds(monkeypatch) -> None:
    """The whole point: phaze's default must be interpreted the way phaze means it.

    Constructs the REAL exporter -- the artifact's real consumer of this variable -- and
    reads back the timeout it resolved. A test against the environment string alone would
    prove phaze can set a variable and nothing about what the SDK does with it.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://192.0.2.1:4318")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TIMEOUT", raising=False)
    _env.apply_export_defaults()

    exporter = OTLPSpanExporter()
    resolved = exporter._timeout  # the resolved value is not otherwise exposed

    assert os.environ["OTEL_EXPORTER_OTLP_TIMEOUT"] == "5"
    assert resolved == 5, (
        f"the OTLP exporter resolved a timeout of {resolved}; phaze's default of '5' assumes SECONDS. "
        "If the installed SDK now reads this variable in MILLISECONDS (as the OTel specification says), "
        "phaze's default is 1000x too short -- change it in telemetry/_env.py in the same commit as this test."
    )
    assert resolved < 60, "a per-batch export deadline over a minute cannot bound a k8s Job's exit"


def test_the_bsp_and_metric_knobs_really_are_milliseconds() -> None:
    """The asymmetry is the trap, so both sides of it are pinned.

    These are read by the SDK (not the exporter) and are genuinely in milliseconds; their
    values would be absurd read as seconds, which is what makes a same-shaped default on
    the OTLP knob look right.
    """
    assert _env._DEFAULTS["OTEL_BSP_SCHEDULE_DELAY"] == "5000"
    assert _env._DEFAULTS["OTEL_BSP_EXPORT_TIMEOUT"] == "5000"
    assert _env._DEFAULTS["OTEL_METRIC_EXPORT_INTERVAL"] == "15000"
    assert _env._DEFAULTS["OTEL_METRIC_EXPORT_TIMEOUT"] == "5000"
    # And the OTLP one is deliberately NOT of that shape.
    assert _env._DEFAULTS["OTEL_EXPORTER_OTLP_TIMEOUT"] == "5"
