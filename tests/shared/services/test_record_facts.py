"""phaze-x1qr3.8: the record sidebar's eight facts, and the three derivations behind them.

The four facts the record already had (format, duration, sha256, lane) were template literals;
the four the set projection added (windows coverage, median BPM, modal key, dominant mood and
style) are derivations, which is why they moved into Python. These tests own the derivations and
the "not measured" branches; ``tests/shared/routers/test_record_page_layout.py`` owns the
rendered list.

No DB and no client -- plain values and unsaved ``AnalysisWindow`` rows, so this runs in the
fast lane alongside ``test_set_projection.py``.
"""

from __future__ import annotations

import uuid

import pytest

from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.services.analysis_timeline import coverage_chip
from phaze.services.record_facts import ABSENT, build_record_facts, dominant_label, median_bpm


def _window(
    index: int,
    *,
    tier: str = "fine",
    start: float | None = None,
    end: float | None = None,
    bpm: float | None = None,
    mood: str | None = None,
    style: str | None = None,
) -> AnalysisWindow:
    return AnalysisWindow(
        file_id=uuid.uuid4(),
        tier=tier,
        window_index=index,
        start_sec=index * 30.0 if start is None else start,
        end_sec=(index + 1) * 30.0 if end is None else end,
        bpm=bpm,
        mood=mood,
        style=style,
    )


def _kwargs(**overrides: object) -> dict[str, object]:
    """The builder's keyword arguments with workable defaults, for a test to override one of."""
    kwargs: dict[str, object] = {
        "file_type": "mp3",
        "sha256_hash": "a" * 64,
        "total_sec": 3600.0,
        "lane": "local",
        "lane_kind": "local",
        "coverage_text": None,
        "windows": [],
        "camelot_modal": None,
    }
    kwargs.update(overrides)
    return kwargs


def _facts(**overrides: object) -> dict[str, str]:
    """The eight facts as ``{label: value}``, built over :func:`_kwargs`' defaults."""
    return {fact.label: fact.value for fact in build_record_facts(**_kwargs(**overrides))}  # type: ignore[arg-type]


# --- the derivations -------------------------------------------------------------------


def test_median_bpm_is_a_median_so_one_half_time_detection_cannot_drag_it() -> None:
    """The reason it is not a mean: one 64 BPM misdetection in a 128 BPM set moves a mean 8 BPM."""
    windows = [_window(i, bpm=bpm) for i, bpm in enumerate([128.0, 128.0, 129.0, 130.0, 64.0])]

    assert median_bpm(windows) == 128.0


def test_median_bpm_ignores_absent_zero_and_non_finite_values() -> None:
    """A BPM column can carry a null or a nonsense value; neither is a tempo measurement."""
    windows = [_window(i, bpm=bpm) for i, bpm in enumerate([None, 0.0, -1.0, float("nan"), 120.0, 124.0])]

    assert median_bpm(windows) == 122.0
    assert median_bpm([_window(0, bpm=None)]) is None
    assert median_bpm([]) is None


def test_the_dominant_label_is_duration_weighted_not_counted() -> None:
    """What a set mostly IS is a question about time, so one long window outweighs two short ones."""
    windows = [
        _window(0, tier="coarse", start=0.0, end=600.0, mood="dark"),
        _window(1, tier="coarse", start=600.0, end=630.0, mood="happy"),
        _window(2, tier="coarse", start=630.0, end=660.0, mood="happy"),
    ]

    assert dominant_label(windows, "mood") == "dark"


def test_the_dominant_label_skips_empty_values_and_non_finite_bounds() -> None:
    """A window with no label, or with bounds that cannot be measured, weighs nothing."""
    windows = [
        _window(0, tier="coarse", mood=None),
        _window(1, tier="coarse", mood=""),
        _window(2, tier="coarse", start=float("nan"), end=float("nan"), mood="ignored"),
        _window(3, tier="coarse", mood="techno"),
    ]

    assert dominant_label(windows, "mood") == "techno"
    assert dominant_label([], "mood") is None
    assert dominant_label([_window(0, tier="coarse", mood=None)], "mood") is None


# --- the eight facts -------------------------------------------------------------------


def test_the_eight_facts_are_always_present_and_in_the_sidebars_order() -> None:
    """The list's SHAPE never changes with the data; only the values do."""
    facts = build_record_facts(
        file_type=None,
        sha256_hash=None,
        total_sec=0.0,
        lane="local",
        lane_kind=None,
        coverage_text=None,
        windows=[],
        camelot_modal=None,
    )

    assert [fact.label for fact in facts] == ["Format", "Duration", "sha256", "Lane", "Windows", "Median BPM", "Modal key", "Mood · style"]
    # Every unmeasured fact renders as an em dash rather than vanishing: an absent row reads as
    # a layout difference, a dashed one reads as "not measured", and only the latter is true.
    assert [fact.value for fact in facts if fact.label != "Lane"] == [ABSENT] * 7


def test_duration_is_the_analyzed_extent_formatted_as_hours_minutes_seconds() -> None:
    """A multi-hour concert set is the normal case here, so the hours component is not optional."""
    assert _facts(total_sec=4 * 3600.0 + 5 * 60 + 6)["Duration"] == "4:05:06"
    assert _facts(total_sec=125.0)["Duration"] == "2:05"
    assert _facts(total_sec=0.0)["Duration"] == ABSENT


def test_the_digest_row_abbreviates_for_display_and_keeps_the_whole_value_as_its_title() -> None:
    """Truncation is display only -- the row still carries the full digest for copy/compare."""
    digest = "0123456789abcdef" * 4
    fact = next(fact for fact in build_record_facts(**_kwargs(sha256_hash=digest)) if fact.label == "sha256")

    assert fact.value == "0123456789ab…"
    assert fact.title == digest
    assert fact.mono is True


@pytest.mark.parametrize(
    ("lane_kind", "glyph", "tone"),
    [("local", "🖥️", "ok"), ("compute", "☁️", "info"), ("kueue", "⎈", "warn"), (None, "▪", "unknown"), ("retired-kind", "▪", "unknown")],
)
def test_the_lane_row_keeps_the_analyze_matrixs_own_glyph_for_every_kind(lane_kind: str | None, glyph: str, tone: str) -> None:
    """phaze-lljfx: an unrecognised or deregistered kind must never silently read as local."""
    fact = next(fact for fact in build_record_facts(**_kwargs(lane_kind=lane_kind, lane="a1")) if fact.label == "Lane")

    assert (fact.glyph, fact.tone, fact.value) == (glyph, tone, "a1")


def test_the_windows_row_reuses_the_coverage_chips_own_sentence_verbatim() -> None:
    """One sentence, two places: the sidebar cannot claim a coverage the chip does not show."""
    assert _facts(coverage_text="20 coarse · 120 fine · gaps")["Windows"] == "20 coarse · 120 fine · gaps"
    assert _facts(coverage_text=None)["Windows"] == ABSENT


def test_the_windows_row_follows_the_coverage_chip_to_unknown_not_zero_on_a_null_tier() -> None:
    """phaze-ox2m0: the sidebar fact is the chip's own text, so its NULL-tier fix reaches here too.

    ``fine=1440/1440, coarse=NULL`` (a pod that died right after the fine tier) must not read
    as "0 coarse windows" in the sidebar any more than in the chip itself -- both surfaces are
    driven by the same sentence.
    """
    chip = coverage_chip(AnalysisResult(file_id=uuid.uuid4(), fine_windows_analyzed=1440, fine_windows_total=1440))

    assert chip is not None
    assert chip["text"] == "? coarse · 1440 fine · gaps"
    assert _facts(coverage_text=chip["text"])["Windows"] == "? coarse · 1440 fine · gaps"


def test_the_modal_key_row_names_the_code_and_the_key_it_stands_for() -> None:
    """An operator who does not read the wheel still gets the key name beside the code."""
    assert _facts(camelot_modal="8A")["Modal key"] == "8A · A minor"
    assert _facts(camelot_modal="12B")["Modal key"] == "12B · E major"
    assert _facts(camelot_modal=None)["Modal key"] == ABSENT
    # A code outside the 24-entry wheel names itself rather than inventing a key or vanishing.
    assert _facts(camelot_modal="99Z")["Modal key"] == "99Z"


def test_the_mood_and_style_row_reads_the_coarse_windows_only() -> None:
    """``mood`` and ``style`` are coarse-tier columns; a fine window carrying one is not a source."""
    windows = [
        _window(0, tier="coarse", mood="dark", style="techno"),
        _window(1, tier="coarse", mood="dark", style="techno"),
        _window(0, tier="fine", mood="happy", style="pop"),
    ]

    assert _facts(windows=windows)["Mood · style"] == "dark · techno"
    assert _facts(windows=[])["Mood · style"] == ABSENT


def test_a_half_measured_mood_and_style_shows_the_half_it_has() -> None:
    """One present value is more useful than an em dash, and is not padded out to look complete."""
    windows = [_window(i, tier="coarse", mood="dark") for i in range(2)]

    assert _facts(windows=windows)["Mood · style"] == "dark"


def test_the_median_bpm_row_reads_the_fine_windows_only_and_rounds_for_display() -> None:
    """BPM is a fine-tier column, and a sidebar row is not the place for six decimal places."""
    windows = [
        *[_window(i, tier="fine", bpm=bpm) for i, bpm in enumerate([127.4, 128.6, 128.6])],
        _window(0, tier="coarse", bpm=60.0),
    ]

    assert _facts(windows=windows)["Median BPM"] == "129"
    assert _facts(windows=[])["Median BPM"] == ABSENT
