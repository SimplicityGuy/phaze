"""phaze-x1qr3.8: the record sidebar's eight facts.

The four facts the record already had (format, duration, sha256, lane) were template literals;
the four the set projection added (windows coverage, median BPM, modal key, dominant mood and
style) are derivations -- "Modal key" (a code named back to a key) is derived here, while Median
BPM and Mood · style are read verbatim off the file's ``AnalysisResult`` row rather than
re-derived (``phaze-duyyw``: the sidebar and the similarity line must read the SAME quantity,
which only holds if there is one place that computes it -- see ``record_facts.py``'s module
docstring). These tests own the derivation and the "not measured" branches;
``tests/shared/routers/test_record_page_layout.py`` owns the rendered list.

No DB and no client -- plain values, so this runs in the fast lane alongside
``test_set_projection.py``.
"""

from __future__ import annotations

import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.services.analysis_timeline import coverage_chip
from phaze.services.record_facts import ABSENT, build_record_facts


def _analysis(**overrides: object) -> AnalysisResult:
    kwargs: dict[str, object] = {"file_id": uuid.uuid4(), "bpm": None, "mood": None, "style": None}
    kwargs.update(overrides)
    return AnalysisResult(**kwargs)  # type: ignore[arg-type]


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
    """The eight facts as ``{label: value}``, built over :func:`_kwargs`' defaults."""
    return {fact.label: fact.value for fact in build_record_facts(**_kwargs(**overrides))}  # type: ignore[arg-type]


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
        analysis=None,
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


def test_the_mood_and_style_row_reads_the_analysis_result_row_verbatim() -> None:
    """``mood``/``style`` are read straight off ``AnalysisResult``, not re-derived from windows."""
    assert _facts(analysis=_analysis(mood="dark", style="techno"))["Mood · style"] == "dark · techno"
    assert _facts(analysis=_analysis())["Mood · style"] == ABSENT
    assert _facts(analysis=None)["Mood · style"] == ABSENT


def test_a_half_measured_mood_and_style_shows_the_half_it_has() -> None:
    """One present value is more useful than an em dash, and is not padded out to look complete."""
    assert _facts(analysis=_analysis(mood="dark"))["Mood · style"] == "dark"


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
    analysis = _analysis(bpm=120.2, mood="dark", style="techno")

    facts = _facts(analysis=analysis)

    similarity_reads = analysis.bpm  # the exact expression routers/record.py hands find_similar_sets
    assert facts["Median BPM"] == str(round(similarity_reads))
    assert facts["Mood · style"] == f"{analysis.mood} · {analysis.style}"
