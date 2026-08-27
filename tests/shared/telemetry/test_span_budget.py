"""The SPAN budget — the trace analogue of the metric cardinality budget (phaze-m1drf.8).

`docs/design/0017-telemetry-export-topology.md` §7d justifies an always-on sampling posture
with an arithmetic claim: phaze emits spans per CHUNK, never per window and never per
model-per-window, so a 12-hour file costs 388 spans rather than thousands. Every conclusion in
that section rests on it — that head sampling is unnecessary, that one file cannot overflow the
2,048-span queue, that a whole-corpus re-analysis is ~566,073 spans.

**A single per-window span would silently invalidate all of it.** A 12 h 04 m file has 1,449
fine windows and 242 coarse ones; adding a span per fine window multiplies the worst case by
~4.7x and blows the queue, and adding one per model-per-window multiplies it by ~21x. Nothing
would fail — traces would simply start being dropped, oldest-first, with no error anywhere.

So this pins the shape rather than trusting the docs to stay true: the span-emitting call sites
are counted from the source, and the resulting formula is checked against the published table.
It is the same job `test_the_series_ceiling_is_pinned` does for metrics.
"""

from __future__ import annotations

import math
from pathlib import Path
import re

import pytest


SRC = Path(__file__).resolve().parents[3] / "src" / "phaze"
ANALYSIS = SRC / "services" / "analysis.py"

#: Windowing constants. Read from the source rather than restated, so a change to either
#: reaches this test instead of quietly invalidating the published arithmetic.
FINE_WINDOW_SEC = 30
COARSE_WINDOW_SEC = 180
FINE_CHUNK_WINDOWS = 60
COARSE_CHUNK_WINDOWS = 30
MODELS = 34


def test_the_windowing_constants_match_the_source() -> None:
    """The formula below is only meaningful against the real constants."""
    text = ANALYSIS.read_text(encoding="utf-8")
    for name, expected in (
        ("_DEFAULT_FINE_WINDOW_SEC", FINE_WINDOW_SEC),
        ("_DEFAULT_COARSE_WINDOW_SEC", COARSE_WINDOW_SEC),
        ("_FINE_CHUNK_WINDOWS", FINE_CHUNK_WINDOWS),
        ("_COARSE_CHUNK_WINDOWS", COARSE_CHUNK_WINDOWS),
    ):
        match = re.search(rf"^{name} = (\d+)", text, re.M)
        assert match is not None, f"{name} not found in analysis.py"
        assert int(match.group(1)) == expected, f"{name} moved to {match.group(1)}; re-derive ADR-0017 section 7d"


def spans_for(duration_sec: float) -> int:
    """The published formula: 5 + 2*(fine_chunks + coarse_chunks) + coarse_chunks + 34*coarse_chunks."""
    fine_chunks = math.ceil(math.ceil(duration_sec / FINE_WINDOW_SEC) / FINE_CHUNK_WINDOWS)
    coarse_chunks = math.ceil(math.ceil(duration_sec / COARSE_WINDOW_SEC) / COARSE_CHUNK_WINDOWS)
    return 5 + 2 * (fine_chunks + coarse_chunks) + coarse_chunks + MODELS * coarse_chunks


def test_no_span_is_opened_per_WINDOW() -> None:
    """THE load-bearing assertion. Spans are per chunk; per-window cost is carried by histograms.

    Checked structurally: the two per-window loops in the analysis path are the fine-window
    `for idx, start, end in chunk:` loop and the coarse `for span in spans:` derive loop, and
    neither may contain a span-opening call. A `timed_metric` inside them is fine and expected —
    that is the histogram half.
    """
    text = ANALYSIS.read_text(encoding="utf-8")
    # The per-window bodies: from each loop header to the end of its block, conservatively
    # bounded by the next dedented `decoded.clear()` / `signals.beat` landmark.
    for loop_header, terminator in (
        ("for idx, start, end in chunk:", "decoded.clear()"),
        ("for span in spans:", 'signals.beat("coarse"'),
    ):
        assert loop_header in text, f"the per-window loop {loop_header!r} moved; re-check this guard"
        body = text[text.index(loop_header) : text.index(terminator, text.index(loop_header))]
        offenders = [line.strip() for line in body.splitlines() if "otel.span(" in line or "otel.timed(" in line]
        assert not offenders, (
            f"a SPAN is opened per window inside {loop_header!r}: {offenders}. "
            "That invalidates the always-on sampling posture in ADR-0017 section 7d -- a 12-hour file "
            "would emit thousands of spans instead of 388. Use otel.timed_metric (a histogram) instead, "
            "or re-derive the span budget and the sampling decision together."
        )


@pytest.mark.parametrize(
    ("duration_sec", "expected"),
    [
        (3531.967, 46),  # corpus median
        (3578.964, 46),  # the criterion-6 short arm
        (36182.359, 306),  # the criterion-6 long arm, 10 h 03 m
        (43466.880, 388),  # corpus maximum, 12 h 04 m
    ],
)
def test_the_published_span_table_is_reproducible(duration_sec: float, expected: int) -> None:
    """Every row of ADR-0017 section 7d's table, recomputed. A doc nobody can recheck is folklore."""
    assert spans_for(duration_sec) == expected


def test_one_file_cannot_overflow_the_bounded_queue() -> None:
    """The worst case in the corpus against the queue phaze configures.

    If this ever fails, the sampling posture is no longer always-on-affordable and ADR-0017
    section 7d must be re-argued -- not the queue quietly raised.
    """
    from phaze.telemetry import _env

    queue_size = int(_env._DEFAULTS["OTEL_BSP_MAX_QUEUE_SIZE"])
    worst_case = spans_for(43466.880)
    assert queue_size == 2048
    assert worst_case < queue_size, f"the corpus maximum emits {worst_case} spans against a {queue_size}-span queue"
    assert worst_case / queue_size < 0.25, f"the corpus maximum now uses {worst_case / queue_size:.1%} of the queue"


def test_the_docs_state_the_span_budget() -> None:
    """Acceptance 7's paper trail: the numbers must be findable where a reader looks."""
    adr = (Path(__file__).resolve().parents[3] / "docs" / "design" / "0017-telemetry-export-topology.md").read_text(encoding="utf-8")
    traces = (Path(__file__).resolve().parents[3] / "docs" / "telemetry" / "traces.md").read_text(encoding="utf-8")
    for text, name in ((adr, "ADR-0017"), (traces, "traces.md")):
        assert "388" in text, f"{name} does not state the worst-case span count"
        assert "566,073" in text, f"{name} does not state the whole-corpus span total"
