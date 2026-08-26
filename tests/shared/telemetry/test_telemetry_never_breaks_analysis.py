"""phaze-m1drf.1 acceptance 7 and phaze-m1drf.2 acceptance 3, discharged by running it.

*A collector that is unreachable, slow or absent CANNOT fail or stall an analysis.* The
deployment this protects is a k8s analyze Job on the burst node reaching a collector on the
home server over Tailscale. homelab reboots. A dropped metric when it does is acceptable;
an analyze job that fails, or sits for 30 s refusing to exit, is not -- and at 1.4951x the
file's own duration, a job lost that way is hours.

**This file runs the REAL analysis** -- real essentia, real audio, the real chunk loop --
against three broken endpoints, and compares the result to the telemetry-off run. Asserting
that the exporter has a timeout configured would be a claim about a setting; running the
pipeline against a black hole is a claim about the outcome.

The three endpoints, and why each is a different failure:

* **absent** -- no endpoint configured at all. The default, and 100% of production today.
* **black hole** -- RFC 5737 TEST-NET-1 (``192.0.2.0/24``, reserved for documentation and
  not routed). A connect here HANGS. This is the failure that could stall something; a
  closed local port would give an instant ECONNREFUSED, which is the easy case.
* **slow listener** -- a real HTTP server on localhost that accepts the connection and then
  sleeps past every export timeout before answering. This is homelab under load rather than
  homelab gone, and it is the one that a naive "is the port open?" check would pass.
"""

from __future__ import annotations

import http.server
import math
import threading
import time
from typing import TYPE_CHECKING, Any
import wave

import numpy as np
import pytest

from phaze.services.analysis import analyze_file
from phaze.telemetry import _env, bootstrap
from tests.shared.telemetry.conftest import reset_otel_globals


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: RFC 5737 TEST-NET-1: reserved for documentation, not routed anywhere.
BLACK_HOLE = "http://192.0.2.1:4318"

_SOURCE_RATE = 8000
_TOTAL_SEC = 120
_FINE_WINDOW_SEC = 30
_COARSE_WINDOW_SEC = 60

#: How long the slow listener sleeps before answering. Comfortably past the 5,000 ms
#: export timeout phaze installs, so every export against it times out.
_SLOW_RESPONSE_SEC = 30.0


@pytest.fixture
def audio(tmp_path: Path) -> str:
    path = str(tmp_path / "sine.wav")
    t = np.arange(_SOURCE_RATE * _TOTAL_SEC) / _SOURCE_RATE
    samples = 0.4 * np.sin(2 * math.pi * 220 * t) + 0.3 * np.sin(2 * math.pi * 331 * t)
    with wave.open(path, "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SOURCE_RATE)
        handle.writeframes((samples * 32767).astype("<i2").tobytes())
    return path


class _StallingHandler(http.server.BaseHTTPRequestHandler):
    """Accepts the request, then sleeps past every timeout before answering."""

    def do_POST(self) -> None:  # BaseHTTPRequestHandler's spelling
        time.sleep(_SLOW_RESPONSE_SEC)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def slow_collector() -> Iterator[str]:
    """A REAL listener that accepts and stalls -- homelab under load, not homelab gone."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StallingHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _analyze(audio_path: str, models_dir: str) -> dict[str, Any]:
    return analyze_file(audio_path, models_dir, fine_window_sec=_FINE_WINDOW_SEC, coarse_window_sec=_COARSE_WINDOW_SEC)


def _comparable(result: dict[str, Any]) -> dict[str, Any]:
    """The parts of an analysis result that must be identical regardless of telemetry.

    Deliberately NOT the whole dict: ``windows`` carries per-window float payloads and the
    comparison that matters is the analysis's own conclusions and its coverage counts.
    """
    return {
        "bpm": result["bpm"],
        "musical_key": result["musical_key"],
        "mood": result["mood"],
        "style": result["style"],
        "danceability": result["danceability"],
        "fine_windows_analyzed": result["fine_windows_analyzed"],
        "fine_windows_total": result["fine_windows_total"],
        "coarse_windows_analyzed": result["coarse_windows_analyzed"],
        "coarse_windows_total": result["coarse_windows_total"],
        "window_count": len(result["windows"]),
    }


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (_env.ENDPOINT_ENV, _env.TRACES_ENDPOINT_ENV, _env.METRICS_ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)
    bootstrap._reset_for_tests()
    reset_otel_globals()
    yield
    bootstrap.shutdown_telemetry(100)
    bootstrap._reset_for_tests()
    reset_otel_globals()


def test_a_black_holed_collector_does_not_change_the_analysis(audio: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE acceptance-7 test: point the endpoint at a black hole, run a REAL analysis to
    completion, and get the same answer."""
    models = str(tmp_path / "no-models")
    baseline = _analyze(audio, models)

    monkeypatch.setenv(_env.ENDPOINT_ENV, BLACK_HOLE)
    assert bootstrap.configure_telemetry("analysis") is True

    started = time.perf_counter()
    with_telemetry = _analyze(audio, models)
    elapsed = time.perf_counter() - started

    assert _comparable(with_telemetry) == _comparable(baseline), "an unreachable collector changed the analysis result"
    assert elapsed < 300, f"the analysis took {elapsed:.1f}s against an unreachable collector"


def test_a_slow_collector_does_not_stall_the_analysis(audio: str, tmp_path: Path, slow_collector: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-m1drf.2 acceptance 3's second arm. A listener that ACCEPTS and then stalls is
    the case a reachability check passes and an analysis could still hang behind."""
    models = str(tmp_path / "no-models")
    baseline = _analyze(audio, models)

    monkeypatch.setenv(_env.ENDPOINT_ENV, slow_collector)
    assert bootstrap.configure_telemetry("analysis") is True

    started = time.perf_counter()
    with_telemetry = _analyze(audio, models)
    elapsed = time.perf_counter() - started

    assert _comparable(with_telemetry) == _comparable(baseline)
    assert elapsed < 300, f"the analysis took {elapsed:.1f}s against a stalling collector"


def test_shutdown_after_a_real_analysis_is_bounded_against_a_stalling_collector(
    audio: str, tmp_path: Path, slow_collector: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short-lived-producer moment (phaze-m1drf.2 acceptance 2), measured.

    An analyze Job's final flush is its ONLY delivery -- there is no scrape to catch its
    tail. That flush must be bounded, because a pod that will not exit holds a Kueue slot
    and the whole burst lane behind it. The bound asserted here is the budget plus generous
    scheduling slack; what it refutes is the SDK's 30 s shutdown default.
    """
    monkeypatch.setenv(_env.ENDPOINT_ENV, slow_collector)
    monkeypatch.setenv(_env.FLUSH_TIMEOUT_ENV, "500")
    assert bootstrap.configure_telemetry("analysis") is True

    _analyze(audio, str(tmp_path / "no-models"))

    started = time.perf_counter()
    flushed = bootstrap.shutdown_telemetry()
    elapsed = time.perf_counter() - started

    # MEASURED REGRESSION. A first implementation bounded only `force_flush` and left the
    # providers' own `shutdown` on their defaults -- `TracerProvider.shutdown()` takes no
    # timeout at all and `MeterProvider.shutdown()` defaults to 30,000 ms -- and took
    # **40.3 s** against a black-holed collector while asking for 3. The bound asserted here
    # is generous against scheduling slack on a loaded machine; what it refutes is that
    # tens-of-seconds shape, which for a k8s analyze Job is a pod refusing to die with a
    # Kueue slot behind it.
    assert elapsed < 8.0, f"shutdown took {elapsed:.2f}s against a stalling collector, budget was 500 ms"
    # The return value means the teardown RAN TO COMPLETION inside the budget -- not that
    # anything was delivered. Against a listener that stalls for 30 s it can legitimately be
    # either: the providers may finish their bounded shutdown inside the budget, or be
    # abandoned at the deadline. Both are correct, and WHICH one happens is a property of the
    # SDK's internal joins, not of phaze's contract. What phaze guarantees is the BOUND above.
    #
    # This assertion previously read `is True`, and that was an artifact rather than a result:
    # before the fixture reset the OTel `Once`, later tests in this file could not install
    # their own providers at all, so the teardown was tearing down something already shut
    # down and always "completed". Asserting a bool that was true for the wrong reason is
    # exactly the kind of green this epic exists to stop trusting.
    assert flushed in {True, False}
    # And a False here must never be read as "data was lost but we recovered" -- the SDK gives
    # no delivery signal at all. Whether homelab received anything is a question for its own
    # collector counters; docs/telemetry/exporter.md section 4 says so.
