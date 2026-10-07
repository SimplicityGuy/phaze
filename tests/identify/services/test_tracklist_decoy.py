"""Decoy-page detection for 1001Tracklists detail pages (phaze-y5fc7), offline only.

**ZERO live requests.** The clean-page evidence is the two real captures in
``tests/identify/fixtures/tracklist_render/`` (52-row anchor + 12-row short listing), read as-is and
never modified. Every decoy page in this file is SYNTHETIC -- fabricated by this test module, and
named so -- because no real decoy capture exists: phaze has never been served one. A fabricated page
standing in for a real decoy is acceptable only because it is labelled as such; it proves the
detector rejects the shape the bead describes (real layout, randomised visible names), not that the
site's decoys look exactly like this.
"""

from __future__ import annotations

from pathlib import Path
import random

from bs4 import BeautifulSoup, NavigableString
import pytest

from phaze.services.tracklist_parser import DECOY_MIN_PAIRED_ROWS, DecoyAssessment, assess_decoy, parse_tracklist_tracks


RENDER_FIXTURES = Path(__file__).parent.parent / "fixtures" / "tracklist_render"


def load_capture(external_id: str) -> str:
    """Return a recorded detail page's HTML, byte-for-byte as captured."""
    return (RENDER_FIXTURES / f"{external_id}-ok.html").read_text(encoding="utf-8")


def fabricate_synthetic_decoy(html: str, *, seed: int = 20261006) -> str:
    """SYNTHETIC: return a FABRICATED decoy built from a real capture's layout.

    Every text node inside every `.trackValue` is replaced with seeded pseudo-words, the shape the
    bead describes a flagged client receiving. Microdata, cues, labels and row structure are left
    untouched. The input capture on disk is never written to.
    """
    rng = random.Random(seed)  # noqa: S311 -- deterministic test data, not security
    soup = BeautifulSoup(html, "lxml")
    for value in soup.select(".trackValue"):
        for node in list(value.find_all(string=True)):
            if node.strip() and node.strip() != "-":
                word = "".join(rng.choice("bcdfghjklmnpqrstvwxz") + rng.choice("aeiou") for _ in range(3))
                node.replace_with(NavigableString(word.capitalize()))
    return str(soup)


def synthetic_page(rows: list[tuple[str | None, str | None]]) -> str:
    """SYNTHETIC: a FABRICATED minimal detail page, one `.tlpItem` per ``(meta_name, visible_name)``.

    ``None`` for the meta omits the microdata (as on a real unresolved "ID - ID" row); ``None`` for
    the visible side omits the `.trackValue`. Two page-level name metas sit outside every row, as on
    the real captures, to prove they are never paired.
    """
    body = []
    for index, (meta, visible) in enumerate(rows):
        meta_html = f'<meta itemprop="name" content="{meta}">' if meta is not None else ""
        visible_html = f'<span class="trackValue"><span>{visible}</span></span>' if visible is not None else ""
        body.append(f'<div class="tlpItem" data-trno="{index}"><div itemscope itemprop="tracks">{meta_html}{visible_html}</div></div>')
    return (
        '<html><head><meta itemprop="name" content="Synthetic Playlist"></head><body>'
        '<div itemscope><meta itemprop="name" content="Synthetic Org"></div>' + "".join(body) + "</body></html>"
    )


AGREE = ("Artist A - Title A (Some Mix)", "Artist A - Title A ( Some Mix )")
DISAGREE = ("Artist A - Title A", "Zorvak - Plimdu")


class TestRealCaptures:
    """The real, known-clean captures are the calibration. If either ever disagrees, the rule is mis-tuned."""

    @pytest.mark.parametrize(
        ("external_id", "rows", "paired", "name_metas"),
        [
            # 52 rows, 46 name metas: 42 rows paired, 10 "ID - ID" rows with no microdata, 4 page-level metas.
            ("25fhn7c9", 52, 42, 46),
            # 12 rows, 13 name metas: 10 rows paired, 2 "ID - ID" rows, 3 page-level metas.
            ("19h6nw7t", 12, 10, 13),
        ],
    )
    def test_real_captures_are_never_classified_decoy(self, external_id: str, rows: int, paired: int, name_metas: int) -> None:
        html = load_capture(external_id)
        soup = BeautifulSoup(html, "lxml")
        # The premise the per-row pairing exists for: metas do not map one-to-one onto rows.
        assert len(soup.select(".tlpItem")) == rows
        assert len(soup.select('meta[itemprop="name"]')) == name_metas

        assessment = assess_decoy(html)

        assert assessment == DecoyAssessment(paired_rows=paired, disagreeing_rows=0)
        assert assessment.disagreement_rate == 0.0
        assert not assessment.is_decoy


class TestSyntheticDecoy:
    def test_synthetic_decoy_is_rejected(self) -> None:
        """A FABRICATED decoy on the anchor's real layout: every paired row's visible name is randomised."""
        decoy_html = fabricate_synthetic_decoy(load_capture("25fhn7c9"))

        assessment = assess_decoy(decoy_html)

        assert assessment.paired_rows == 42, "fabrication must not change which rows pair"
        assert assessment.disagreeing_rows == 42
        assert assessment.is_decoy
        # The point of the detector: this page still parses as a perfectly plausible tracklist.
        assert len(parse_tracklist_tracks(decoy_html)) == 52

    def test_the_fabrication_leaves_the_capture_on_disk_untouched(self) -> None:
        before = (RENDER_FIXTURES / "25fhn7c9-ok.html").read_bytes()
        fabricate_synthetic_decoy(before.decode("utf-8"))
        assert (RENDER_FIXTURES / "25fhn7c9-ok.html").read_bytes() == before


class TestBoundaries:
    @pytest.mark.parametrize("rows", [[DISAGREE, DISAGREE], [DISAGREE], []])
    def test_fewer_than_three_paired_rows_never_classify_decoy(self, rows: list[tuple[str, str]]) -> None:
        assessment = assess_decoy(synthetic_page(list(rows)))
        assert assessment.paired_rows < DECOY_MIN_PAIRED_ROWS
        assert assessment.disagreeing_rows == assessment.paired_rows, "every paired row disagrees -- and it still is not decoy"
        assert not assessment.is_decoy

    def test_exactly_half_of_four_paired_rows_is_decoy(self) -> None:
        assessment = assess_decoy(synthetic_page([DISAGREE, DISAGREE, AGREE, AGREE]))
        assert (assessment.paired_rows, assessment.disagreeing_rows) == (4, 2)
        assert assessment.disagreement_rate == 0.5
        assert assessment.is_decoy

    def test_two_of_three_paired_rows_is_decoy(self) -> None:
        assessment = assess_decoy(synthetic_page([DISAGREE, DISAGREE, AGREE]))
        assert (assessment.paired_rows, assessment.disagreeing_rows) == (3, 2)
        assert assessment.is_decoy

    def test_just_under_half_is_not_decoy(self) -> None:
        # 4 of 9 = 44%, and 1 of 3 = 33%: the smallest shapes just below the line.
        nine = assess_decoy(synthetic_page([DISAGREE] * 4 + [AGREE] * 5))
        assert (nine.paired_rows, nine.disagreeing_rows) == (9, 4)
        assert not nine.is_decoy
        three = assess_decoy(synthetic_page([DISAGREE, AGREE, AGREE]))
        assert (three.paired_rows, three.disagreeing_rows) == (3, 1)
        assert not three.is_decoy

    def test_rows_missing_either_side_are_excluded_from_numerator_and_denominator(self) -> None:
        """2 paired rows plus many one-sided rows: still under the 3-row floor, still not decoy.

        If one-sided rows counted in the DENOMINATOR, adding agreeing ones would dilute a real decoy;
        if they counted in the NUMERATOR (a missing side read as "disagrees"), every page with
        ID rows -- both real captures -- would drift toward decoy.
        """
        one_sided: list[tuple[str | None, str | None]] = [
            (None, "ID - ID"),
            ("Artist B - Title B", None),
            ("", "Artist C - Title C"),
            ("Artist D", "  "),
        ]
        assessment = assess_decoy(synthetic_page([DISAGREE, DISAGREE, *one_sided * 3]))
        assert assessment == DecoyAssessment(paired_rows=2, disagreeing_rows=2)
        assert not assessment.is_decoy

        # And with 3 paired rows, the one-sided rows do not dilute: 2 of 3 still classifies decoy.
        diluted = assess_decoy(synthetic_page([DISAGREE, DISAGREE, AGREE, *one_sided * 3]))
        assert diluted == DecoyAssessment(paired_rows=3, disagreeing_rows=2)
        assert diluted.is_decoy

    def test_a_row_with_two_name_metas_is_not_guessed_at(self) -> None:
        html = synthetic_page([AGREE, AGREE, AGREE]).replace(
            '<div class="tlpItem" data-trno="0"><div itemscope itemprop="tracks">',
            '<div class="tlpItem" data-trno="0"><div itemscope itemprop="tracks"><meta itemprop="name" content="Other - Thing">',
        )
        assert assess_decoy(html) == DecoyAssessment(paired_rows=2, disagreeing_rows=0)

    def test_formatting_only_differences_agree(self) -> None:
        """Bracket spacing, case, ampersand spacing and compatibility forms are formatting, not names."""
        assessment = assess_decoy(
            synthetic_page(
                [
                    ("Voodoos &amp; Taboos - Animae", "Voodoos  &amp;  Taboos - Animae"),
                    ("Len Faki - Temple (Ø [Phase] Remix)", "len faki - temple ( ø [ phase ] remix )"),
                    ("Run DMC vs. Jason Nevins - It's Like That", "Run DMC vs . Jason Nevins - It ' s Like That"),
                ]
            )
        )
        assert assessment == DecoyAssessment(paired_rows=3, disagreeing_rows=0)

    def test_no_rows_has_a_zero_rate(self) -> None:
        assert assess_decoy("<html></html>").disagreement_rate == 0.0
