"""phaze-x1qr3.8: the record sidebar's nine facts.

The four facts the record already had (format, duration, sha256, lane) were template literals;
the five the set projection added (windows coverage, median BPM, modal key, mood chips, dominant
style) are derivations -- "Modal key" (a code named back to a key) is derived here, Median BPM
and Style are read verbatim off the file's ``AnalysisResult`` row rather than re-derived
(``phaze-duyyw``: the sidebar and the similarity line must read the SAME quantity, which only
holds if there is one place that computes it -- see ``record_facts.py``'s module docstring), and
Mood is a set-wide derivation over ``SetProfile.mean_vector`` / the coarse windows'
``mood_scores`` (``phaze-4ye5i`` -- see the module docstring for why Mood, unlike BPM and Style,
is no longer read off ``AnalysisResult.mood`` at all: that column is a lossy top-3 string from
ONE representative window, exactly the ellipsis-truncated value this bead replaces). These tests
own the derivation and the "not measured" branches; ``tests/shared/routers/test_record_page_layout.py``
owns the rendered list.

No DB and no client -- plain values, so this runs in the fast lane alongside
``test_set_projection.py``.
"""

from __future__ import annotations

import uuid

import pytest

from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.services.analysis_timeline import MOOD_HUES, MOOD_LABELS, coverage_chip
from phaze.services.record_facts import ABSENT, MoodChip, build_record_facts


def _analysis(**overrides: object) -> AnalysisResult:
    kwargs: dict[str, object] = {"file_id": uuid.uuid4(), "bpm": None, "mood": None, "dominant_style": None}
    kwargs.update(overrides)
    return AnalysisResult(**kwargs)  # type: ignore[arg-type]


def _coarse_window(mood_scores: dict[str, float] | None) -> AnalysisWindow:
    return AnalysisWindow(file_id=uuid.uuid4(), tier="coarse", window_index=0, start_sec=0.0, end_sec=60.0, mood_scores=mood_scores)


# A realistic stored mean vector, positional in MOOD_ORDER: the 7 mood dims are the operator
# report's own numbers (`electronic=0.88` / `party=0.72` / `aggressive=0.40`) plus the remaining
# 4 as independent probabilities, never all equal -- the shape essentia actually writes, not a
# placeholder string.
_REALISTIC_MEAN_VECTOR = [0.12, 0.88, 0.40, 0.15, 0.30, 0.05, 0.72, 0.65, 0.50, 0.60, 0.20]


def _kwargs(**overrides: object) -> dict[str, object]:
    """The builder's keyword arguments with workable defaults, for a test to override one of."""
    kwargs: dict[str, object] = {
        "file_type": "mp3",
        "sha256_hash": "a" * 64,
        "total_sec": 3600.0,
        "lane": "local",
        "lane_kind": "local",
        "coverage_text": None,
        "analysis": None,
        "camelot_modal": None,
    }
    kwargs.update(overrides)
    return kwargs


def _facts(**overrides: object) -> dict[str, str]:
    """The nine facts as ``{label: value}``, built over :func:`_kwargs`' defaults."""
    return {fact.label: fact.value for fact in build_record_facts(**_kwargs(**overrides))}  # type: ignore[arg-type]


def test_the_nine_facts_are_always_present_and_in_the_sidebars_order() -> None:
    """The list's SHAPE never changes with the data; only the values do."""
    facts = build_record_facts(
        file_type=None,
        sha256_hash=None,
        total_sec=0.0,
        lane="local",
        lane_kind=None,
        coverage_text=None,
        analysis=None,
        camelot_modal=None,
    )

    assert [fact.label for fact in facts] == [
        "Format",
        "Duration",
        "sha256",
        "Lane",
        "Windows",
        "Median BPM",
        "Modal key",
        "Mood",
        "Style",
    ]
    # Every unmeasured fact renders as an em dash rather than vanishing: an absent row reads as
    # a layout difference, a dashed one reads as "not measured", and only the latter is true.
    assert [fact.value for fact in facts if fact.label != "Lane"] == [ABSENT] * 8


def test_duration_is_the_analyzed_extent_formatted_as_hours_minutes_seconds() -> None:
    """A multi-hour concert set is the normal case here, so the hours component is not optional."""
    assert _facts(total_sec=4 * 3600.0 + 5 * 60 + 6)["Duration"] == "4:05:06"
    assert _facts(total_sec=125.0)["Duration"] == "2:05"
    assert _facts(total_sec=0.0)["Duration"] == ABSENT


def test_duration_falls_back_to_metadata_duration_when_there_are_no_analysis_windows(  # phaze-tuy9m
) -> None:
    """No windows at all (``total_sec == 0``) is exactly the shape a fresh, un-analyzed file has --
    the tag-reported length is the only duration there is to show, and it must say so."""
    facts = _facts(total_sec=0.0, metadata_duration=125.0)

    assert facts["Duration"] == "2:05 (from tags)"


def test_duration_prefers_the_analyzed_extent_over_metadata_duration_when_windows_exist(  # phaze-tuy9m
) -> None:
    """A file with windows must never show the tag value instead of what was actually analyzed --
    the fallback is for ``total_sec == 0`` only, never a substitute the rest of the time."""
    facts = _facts(total_sec=90.0, metadata_duration=999.0)

    assert facts["Duration"] == "1:30"


def test_a_zero_or_missing_metadata_duration_does_not_fall_back_to_a_fake_reading(  # phaze-tuy9m
) -> None:
    """``metadata_duration`` absent, ``None``, or a non-positive stored value all degrade to the
    ordinary absent marker -- a fallback must never manufacture a duration from nothing."""
    assert _facts(total_sec=0.0)["Duration"] == ABSENT
    assert _facts(total_sec=0.0, metadata_duration=None)["Duration"] == ABSENT
    assert _facts(total_sec=0.0, metadata_duration=0.0)["Duration"] == ABSENT


def test_the_digest_row_abbreviates_for_display_and_keeps_the_whole_value_as_its_title() -> None:
    """Truncation is display only -- the row still carries the full digest for copy/compare."""
    digest = "0123456789abcdef" * 4
    fact = next(fact for fact in build_record_facts(**_kwargs(sha256_hash=digest)) if fact.label == "sha256")

    assert fact.value == "0123456789ab…"
    assert fact.title == digest
    assert fact.mono is True


@pytest.mark.parametrize(
    ("lane_kind", "expected"),
    [("local", "local"), ("compute", "compute"), ("kueue", "kueue"), (None, ""), ("retired-kind", "retired-kind")],
)
def test_the_lane_row_hands_its_kind_to_the_shared_label_macro(lane_kind: str | None, expected: str) -> None:
    """phaze-lljfx + phaze-x6ql9: the Lane fact carries the file's REAL kind (never defaulting to local) and
    renders through ``ui.kind_label``; glyph and tone are the macro's, so the fact holds neither."""
    fact = next(fact for fact in build_record_facts(**_kwargs(lane_kind=lane_kind, lane="a1")) if fact.label == "Lane")

    assert (fact.lane_kind, fact.tone, fact.value) == (expected, "neutral", "a1")


def test_the_windows_row_reuses_the_coverage_chips_own_sentence_verbatim() -> None:
    """One sentence, two places: the sidebar cannot claim a coverage the chip does not show."""
    assert _facts(coverage_text="20 coarse · 120 fine · gaps")["Windows"] == "20 coarse · 120 fine · gaps"
    assert _facts(coverage_text=None)["Windows"] == ABSENT


def test_the_windows_row_follows_the_coverage_chip_to_unknown_not_zero_on_a_null_tier() -> None:
    """phaze-ox2m0: the sidebar fact is the chip's own text, so its NULL-tier fix reaches here too.

    ``fine=1440/1440, coarse=NULL`` (a pod that died right after the fine tier) must not read
    as "0 coarse windows" in the sidebar any more than in the chip itself -- both surfaces are
    driven by the same sentence.

    Restored by the PR #556 cleanup: `phaze-duyyw`'s rewrite of this module dropped it, and the
    test above cannot stand in for it -- that one hands the builder a literal, so it asserts the
    sidebar copies whatever string it is given and is structurally unable to see the chip
    producing the wrong string.
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


def test_the_style_row_reads_analysis_result_dominant_style_verbatim() -> None:
    """``dominant_style`` is read straight off ``AnalysisResult``, not re-derived from windows --
    unlike Mood, this row's source did not change in phaze-4ye5i."""
    assert _facts(analysis=_analysis(dominant_style="techno"))["Style"] == "techno"
    assert _facts(analysis=_analysis())["Style"] == ABSENT
    assert _facts(analysis=None)["Style"] == ABSENT
    # ``AnalysisResult.mood`` is no longer read for anything the Facts card shows -- an
    # analysis carrying it but nothing else must still show the honest absent state.
    assert _facts(analysis=_analysis(mood="dark"))["Style"] == ABSENT


def test_the_mood_row_sources_set_wide_chips_from_the_mean_vector() -> None:
    """The primary source: ``SetProfile.mean_vector``, normalised over its 7 mood dims exactly
    like the mood river -- never the raw independent per-classifier probabilities the old
    ``AnalysisResult.mood`` string carried (see the module docstring for why)."""
    facts = build_record_facts(**_kwargs(mean_vector=_REALISTIC_MEAN_VECTOR))
    mood = next(fact for fact in facts if fact.label == "Mood")

    assert mood.value == "Electronic 34% · Party 27% · Aggressive 15%"
    assert [chip.label for chip in mood.chips] == ["Electronic", "Party", "Aggressive"]
    assert [chip.pct for chip in mood.chips] == [34, 27, 15]
    # Every chip's hue is read straight from the one shared mood-colour table -- never
    # invented here -- so it always agrees with the mood river's own swatch for that mood.
    assert [chip.hue for chip in mood.chips] == [MOOD_HUES["mood_electronic"], MOOD_HUES["mood_party"], MOOD_HUES["mood_aggressive"]]
    assert [chip.label for chip in mood.chips] == [
        MOOD_LABELS["mood_electronic"],
        MOOD_LABELS["mood_party"],
        MOOD_LABELS["mood_aggressive"],
    ]


def test_the_mood_rows_title_carries_the_full_ranked_list_not_just_the_top_3() -> None:
    """Acceptance: the full ranked list is available on hover, via the row's ``title``."""
    facts = build_record_facts(**_kwargs(mean_vector=_REALISTIC_MEAN_VECTOR))
    mood = next(fact for fact in facts if fact.label == "Mood")

    assert mood.title == "Electronic 34% · Party 27% · Aggressive 15% · Happy 11% · Relaxed 6% · Acoustic 5% · Sad 2%"


def test_the_mood_row_falls_back_to_per_window_mood_scores_without_a_mean_vector() -> None:
    """No ``set_profile`` (or one with a NULL ``mean_vector`` -- a fine-only file) falls back to
    averaging the coarse windows' own ``mood_scores``, normalised exactly as the mood river
    normalises them (``analysis_timeline.mood_stack``)."""
    windows = [
        _coarse_window(
            {
                "mood_acoustic": 0.1,
                "mood_electronic": 0.9,
                "mood_aggressive": 0.2,
                "mood_relaxed": 0.05,
                "mood_happy": 0.05,
                "mood_sad": 0.02,
                "mood_party": 0.3,
            }
        ),
        _coarse_window(
            {
                "mood_acoustic": 0.05,
                "mood_electronic": 0.5,
                "mood_aggressive": 0.1,
                "mood_relaxed": 0.1,
                "mood_happy": 0.1,
                "mood_sad": 0.05,
                "mood_party": 0.9,
            }
        ),
    ]

    facts = build_record_facts(**_kwargs(mean_vector=None, coarse_windows=windows))
    mood = next(fact for fact in facts if fact.label == "Mood")

    assert mood.value == "Electronic 42% · Party 34% · Aggressive 9%"


def test_the_mood_row_prefers_the_mean_vector_over_the_per_window_fallback() -> None:
    """A file with BOTH a usable ``mean_vector`` and coarse windows must read the set-wide
    vector, never the per-window average -- the fallback is for when the vector is unusable,
    not a tie-break between two available sources."""
    windows = [_coarse_window({"mood_acoustic": 1.0})]  # would read as 100% Acoustic if used

    facts = build_record_facts(**_kwargs(mean_vector=_REALISTIC_MEAN_VECTOR, coarse_windows=windows))
    mood = next(fact for fact in facts if fact.label == "Mood")

    assert mood.value == "Electronic 34% · Party 27% · Aggressive 15%"


def test_the_mood_row_shows_the_absent_state_with_no_mean_vector_and_no_scored_windows() -> None:
    """Neither source has data (a fresh file, or one with only fine windows and no coarse mood
    scores) -- the honest absent state, never a manufactured chip."""
    facts = build_record_facts(**_kwargs(mean_vector=None, coarse_windows=[]))
    mood = next(fact for fact in facts if fact.label == "Mood")

    assert mood.value == ABSENT
    assert mood.title == ""
    assert mood.chips == ()

    # A coarse window with no mood_scores at all (never projected) is the same honest gap.
    facts = build_record_facts(**_kwargs(mean_vector=None, coarse_windows=[_coarse_window(None)]))
    assert next(fact for fact in facts if fact.label == "Mood").value == ABSENT


def test_a_mean_vector_tie_resolves_to_mood_names_order() -> None:
    """Ties resolve to ``MOOD_ORDER``'s own order, the same rule
    :func:`~phaze.services.analysis_timeline.top_mood` uses -- stable across renders."""
    tied_vector = [0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # acoustic == electronic

    facts = build_record_facts(**_kwargs(mean_vector=tied_vector))
    mood = next(fact for fact in facts if fact.label == "Mood")

    assert [chip.name for chip in mood.chips[:2]] == ["mood_acoustic", "mood_electronic"]


def test_mood_chip_is_exported_for_callers_that_type_against_it() -> None:
    chip = MoodChip(name="mood_electronic", label="Electronic", hue=268, pct=34)
    assert chip.pct == 34


def test_the_median_bpm_row_reads_analysis_result_bpm_and_rounds_for_display() -> None:
    """BPM is read off ``AnalysisResult.bpm``, and a sidebar row is not the place for decimals."""
    assert _facts(analysis=_analysis(bpm=128.6))["Median BPM"] == "129"
    assert _facts(analysis=_analysis(bpm=None))["Median BPM"] == ABSENT
    assert _facts(analysis=None)["Median BPM"] == ABSENT


def test_the_median_bpm_row_matches_the_similarity_terms_own_reading_of_analysis_result_bpm() -> None:
    """The sidebar and the similarity line's BPM term must never show two different numbers for
    the same file (``phaze-duyyw``). Both are keyed off THE SAME ``AnalysisResult.bpm`` -- this
    module no longer re-derives its own median over the fine windows, so it cannot drift from
    ``routers/record.py``'s ``analysis.bpm`` read for ``find_similar_sets`` (the similarity
    term's own source; see ``services/set_similarity.py``'s module docstring and
    ``_categorical_agreement``'s reading of ``AnalysisResult.bpm``/``.mood``/``.style``).

    A regression fixture: an ``AnalysisResult`` whose ``bpm`` a window-median re-derivation could
    never reproduce, because there are no windows behind it at all here -- only the stored
    aggregate exists, exactly the shape of a file whose only fine window was gated to
    ``confidence == 0.0`` at write time and so contributed nothing to ``aggregate_bpm``, yet the
    aggregate itself is still a real, persisted number.
    """
    analysis = _analysis(bpm=120.2, mood="dark", dominant_style="techno")

    facts = _facts(analysis=analysis)

    similarity_reads = analysis.bpm  # the exact expression routers/record.py hands find_similar_sets
    assert facts["Median BPM"] == str(round(similarity_reads))
    assert facts["Style"] == analysis.dominant_style
