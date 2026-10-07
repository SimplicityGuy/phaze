"""The declared-row-count canary (phaze-xtwjg).

The REAL captures are never modified. Every mismatch / absent-count page below is SYNTHETIC: derived from a
capture's bytes in memory at test time and named as such. Agreement between declared and rendered counts is
measured on exactly two real pages (52 and 12 rows); on any other real page it is UNMEASURED.
"""

from pathlib import Path
import re

import pytest

from phaze.services.tracklist_parser import TracklistParseError, parse_tracklist_tracks


FIXTURES = Path(__file__).parent.parent / "fixtures" / "tracklist_render"

CAPTURES = {
    "25fhn7c9": {"rows": 52, "unresolved": 10},
    "19h6nw7t": {"rows": 12, "unresolved": 2},
}
_NUM_TRACKS = re.compile(r'<meta[^>]*itemprop="numTracks"[^>]*>')


def _html(slug: str) -> str:
    return (FIXTURES / f"{slug}-ok.html").read_text(encoding="utf-8")


def _synthetic_with_declared_count(slug: str, declared: int) -> str:
    """SYNTHETIC: the real capture with its declared numTracks rewritten. Generated at test time."""
    html, substitutions = _NUM_TRACKS.subn(f'<meta itemprop="numTracks" content="{declared}">', _html(slug))
    assert substitutions == 1, "the capture is expected to carry exactly one numTracks meta"
    return html


def _synthetic_without_declared_count(slug: str) -> str:
    """SYNTHETIC: the real capture with its numTracks meta removed. Generated at test time."""
    html, substitutions = _NUM_TRACKS.subn("", _html(slug))
    assert substitutions == 1
    return html


@pytest.mark.parametrize("slug", sorted(CAPTURES))
def test_real_captures_pass_the_row_count_canary(slug: str) -> None:
    html = _html(slug)
    declared = [int(m) for m in re.findall(r'itemprop="numTracks"\s+content="(\d+)"', html)]

    tracks = parse_tracklist_tracks(html)  # raises on a mismatch

    assert declared == [CAPTURES[slug]["rows"]]
    assert len(tracks) == declared[0]


@pytest.mark.parametrize("slug", sorted(CAPTURES))
def test_synthetic_mismatched_row_count_raises_the_parse_failure(slug: str) -> None:
    rows = CAPTURES[slug]["rows"]
    for declared in (rows + 1, rows - 1, 0):
        with pytest.raises(TracklistParseError) as exc_info:
            parse_tracklist_tracks(_synthetic_with_declared_count(slug, declared))
        assert exc_info.value.declared_count == declared
        assert exc_info.value.container_count == rows
        assert "numTracks" in str(exc_info.value)


@pytest.mark.parametrize("slug", sorted(CAPTURES))
def test_absent_declared_count_skips_the_canary(slug: str) -> None:
    tracks = parse_tracklist_tracks(_synthetic_without_declared_count(slug))
    assert len(tracks) == CAPTURES[slug]["rows"]


def test_synthetic_unreadable_declared_count_skips_the_canary() -> None:
    """A malformed hint is not a mismatch: the canary only fires on a readable declared count."""
    html = _NUM_TRACKS.sub('<meta itemprop="numTracks" content="many">', _html("19h6nw7t"))
    assert len(parse_tracklist_tracks(html)) == 12
