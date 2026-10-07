"""Unresolved "ID - ID" rows (phaze-8yvb0).

The REAL captures under ``tests/identify/fixtures/tracklist_render/`` are never modified.
``tracklist_parser_golden/`` holds the parser's output for both captures recorded BEFORE this change,
so "the other rows are byte-identical" is a comparison against a file, not against the code under test.
"""

from dataclasses import asdict
import json
from pathlib import Path
import re

import pytest

from phaze.services.tracklist_parser import TracklistTrackPayload, parse_tracklist_tracks


FIXTURES = Path(__file__).parent.parent / "fixtures" / "tracklist_render"
GOLDEN = Path(__file__).parent.parent / "fixtures" / "tracklist_parser_golden"

CAPTURES = {
    "25fhn7c9": {"rows": 52, "unresolved": 10},
    "19h6nw7t": {"rows": 12, "unresolved": 2},
}
_NUM_TRACKS = re.compile(r'<meta[^>]*itemprop="numTracks"[^>]*>')


def _html(slug: str) -> str:
    return (FIXTURES / f"{slug}-ok.html").read_text(encoding="utf-8")


def _golden(slug: str) -> list[dict[str, object]]:
    return json.loads((GOLDEN / f"{slug}-ok.parsed.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.mark.parametrize("slug", sorted(CAPTURES))
def test_unresolved_rows_are_marked_not_stored_as_id(slug: str) -> None:
    tracks = parse_tracklist_tracks(_html(slug))
    unresolved = [t for t in tracks if t.is_unresolved]

    assert len(tracks) == CAPTURES[slug]["rows"]
    assert len(unresolved) == CAPTURES[slug]["unresolved"]
    assert all(t.artist is None and t.title is None for t in unresolved)
    assert not [t for t in tracks if t.artist == "ID" or t.title == "ID"]
    assert not [t for t in tracks if not t.is_unresolved and t.artist is None and t.title is None]


@pytest.mark.parametrize("slug", sorted(CAPTURES))
def test_resolved_rows_are_byte_identical_to_the_golden(slug: str) -> None:
    golden = _golden(slug)
    current = [asdict(t) for t in parse_tracklist_tracks(_html(slug))]
    assert len(current) == len(golden)

    resolved = 0
    for before, after in zip(golden, current, strict=True):
        was_placeholder = before["artist"] == "ID" and before["title"] == "ID"
        marker = after.pop("is_unresolved")
        if was_placeholder:
            assert marker is True
            # Everything except artist/title is what it always was (position, label, cue, ...).
            assert {k: v for k, v in after.items() if k not in {"artist", "title"}} == {
                k: v for k, v in before.items() if k not in {"artist", "title"}
            }
        else:
            resolved += 1
            assert marker is False
            assert after == before
    assert resolved == CAPTURES[slug]["rows"] - CAPTURES[slug]["unresolved"]  # 42 and 10


def test_a_row_the_site_marks_identified_is_never_treated_as_unresolved() -> None:
    """SYNTHETIC: an identified row whose text happens to read "ID - ID" keeps its text."""
    html = _html("19h6nw7t")
    # Mark the first (unresolved) row identified: it must then parse exactly as the old parser did.
    marked = html.replace('data-isid="true"', 'data-isided="true"', 1)
    first = parse_tracklist_tracks(marked)[0]
    assert first.is_unresolved is False
    assert (first.artist, first.title) == ("ID", "ID")


def test_payload_marker_defaults_false() -> None:
    assert TracklistTrackPayload(position=1, artist="A", title="B").is_unresolved is False
