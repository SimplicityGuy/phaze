"""Changed segments remain plain data; the real shared row owns safe HTML rendering."""

from __future__ import annotations

from bs4 import BeautifulSoup
import pytest

from phaze.routers.proposals import templates
from phaze.services.review_diff import changed_segments


@pytest.mark.parametrize(
    ("before", "after", "removed", "added"),
    [
        ("song.mp3", "new-song.mp3", "", "new-"),
        ("new-song.mp3", "song.mp3", "new-", ""),
        ("old.mp3", "new.mp3", "old", "new"),
        ("same.mp3", "same.mp3", "", ""),
        ("", "", "", ""),
        ("", "song.mp3", "", "song.mp3"),
        ("song.mp3", "", "song.mp3", ""),
        ("音楽.mp3", "音楽-live.mp3", "", "-live"),
    ],
)
def test_segments_reconstruct_both_inputs_and_mark_only_the_changes(before: str, after: str, removed: str, added: str) -> None:
    result = changed_segments(before, after)
    assert "".join(segment.text for segment in result.before) == before
    assert "".join(segment.text for segment in result.after) == after
    assert "".join(segment.text for segment in result.before if segment.changed) == removed
    assert "".join(segment.text for segment in result.after if segment.changed) == added


def _render_row(**overrides: object) -> str:
    context: dict[str, object] = {
        "row_id_prefix": "rename-row",
        "pid": "synthetic-row",
        "file": "old.mp3",
        "before": "old.mp3",
        "after": "new.mp3",
        "diff_label": "Filename",
        "approve_url": "/proposals/synthetic-row/approve",
        "skip_url": "/proposals/synthetic-row/reject",
        "edit_url": "/proposals/synthetic-row/edit",
        "edit_facet": "filename",
    }
    return templates.get_template("pipeline/partials/_diff_row.html").render(context | overrides)


def test_hostile_filename_segments_are_plain_text_and_autoescaped_by_the_real_row() -> None:
    before = '<img src="x" onerror="alert(1)">.mp3'
    after = '<script>alert("changed")</script>.mp3'
    segments = changed_segments(before, after)
    assert "".join(segment.text for segment in segments.before) == before
    assert "".join(segment.text for segment in segments.after) == after
    body = _render_row(before=before, after=after, file=before)
    soup = BeautifulSoup(body, "html.parser")
    assert not soup.select("script, img, [onerror]")
    assert "&lt;" in body
    for side, value in (("before", before), ("after", after)):
        code = soup.select_one(f'code[data-diff-side="{side}"]')
        assert code is not None
        assert code.get_text(types=None) == value
        assert code["title"] == value


def test_removed_and_added_segments_have_non_colour_marks_in_both_facets() -> None:
    soup = BeautifulSoup(_render_row(secondary_label="Destination", secondary_before="old/song.mp3", secondary_after="new/song.mp3"), "html.parser")
    before = soup.select('code[data-diff-side="before"]')
    after = soup.select('code[data-diff-side="after"]')
    assert len(before) == len(after) == 2
    for code in before:
        assert "text-danger" in code["class"] and "line-through" in code["class"]
        assert code.select_one("del[data-diff-changed].font-semibold") is not None
    for code in after:
        assert "text-ok" in code["class"]
        assert code.select_one("ins[data-diff-changed].underline.font-semibold") is not None


@pytest.mark.parametrize("confidence", [0.0, 0.88, 1.0])
def test_confidence_meter_and_text_report_the_same_number(confidence: float) -> None:
    soup = BeautifulSoup(_render_row(confidence=confidence), "html.parser")
    percent = round(confidence * 100)
    meter = soup.select_one('meter[aria-label="Confidence"]')
    assert meter is not None and meter["value"] == str(percent)
    assert meter["min"] == "0" and meter["max"] == "100"
    assert meter["aria-valuetext"] == f"{percent}% confidence"
    assert f"Confidence: {percent}%" in meter.parent.get_text(" ", strip=True)


@pytest.mark.parametrize("overrides", [{}, {"confidence": None}])
def test_missing_confidence_is_textually_unavailable_without_a_misleading_meter(overrides: dict[str, object]) -> None:
    soup = BeautifulSoup(_render_row(**overrides), "html.parser")
    assert "Confidence: Unavailable" in soup.get_text(" ", strip=True)
    assert soup.select_one("meter") is None
