"""phaze-x1qr3.10: the inspection PAYLOAD and the hooks the inspection script drives.

WHICH HARNESS TESTS WHAT, and why it is split
==============================================

The bead's first acceptance criterion offers a choice: this file, "or the repo's existing JS
test harness if one exists". One exists and it is ``tests/browser`` -- Playwright driving the
real application, run as its own blocking CI job. There is no node and no jsdom here, and
adding one to execute three lookup functions would buy a second JavaScript runtime that is not
the one the code ships into.

So the criterion is discharged across two files, and the split is on what each harness can
honestly measure:

* **Here (default suite, ``just check-fast``):** everything that is a property of the SERVER --
  that the payload carries per-window energy, camelot and top mood; that it carries the track
  segments, the key runs and the resting peak; that the key runs are numbered as the harmonic
  wheel numbers its nodes; and that every DOM hook the script reaches for is actually rendered
  by the templates. A renamed hook is invisible to a browser test that never opens that page
  and silently disables the mark it drove, so these are pinned by name.
* **``tests/browser/test_analysis_timeline_lookups.py`` (browser suite):** the three pure lookup
  functions -- ``measuredWindow``, ``segmentAt``, ``keyRunAt`` -- executed from the SHIPPED
  ``analysis_timeline.js`` in a real browser, on a synthetic payload, at a window boundary,
  inside a gap and at the file end. ADR-0012 rule 3 is the reason it is not a Python port of
  the same arithmetic: a port is a proxy that cannot exhibit the failure, and the artifact's
  real consumer is a browser.
* **``tests/browser/test_analysis_timeline.py`` (browser suite):** the interaction contract --
  pointer, keyboard and row focus all driving one elapsed time into all seven targets.
"""

from __future__ import annotations

from pathlib import Path
import re
import uuid

from phaze.models.analysis import AnalysisWindow
from phaze.services.analysis_timeline import (
    build_analysis_timeline_context,
    inspection_key_runs,
    inspection_windows,
    top_mood,
)
from phaze.services.harmonic_journey import build_harmonic_journey
from phaze.services.set_projection import MOOD_ORDER
from phaze.services.track_segments import TrackSegment


_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "src" / "phaze" / "static" / "js" / "analysis_timeline.js"
_TEMPLATES = _ROOT / "src" / "phaze" / "templates"
_TIMELINE_TEMPLATE = _TEMPLATES / "proposals" / "partials" / "analysis_timeline.html"
_TRACKLIST_TEMPLATE = _TEMPLATES / "record" / "partials" / "_tracklist_review_body.html"
_WHEEL_TEMPLATE = _TEMPLATES / "record" / "partials" / "_harmonic_wheel.html"
_PRIMITIVES_TEMPLATE = _TEMPLATES / "ui" / "primitives.html"
_RECORD_PAGE = _TEMPLATES / "record" / "record_page.html"
_RECORD_BODY = _TEMPLATES / "record" / "record_body.html"


def _fine(index: int, start: float, end: float, *, bpm: float | None = None, key: str | None = None, camelot: str | None = None) -> AnalysisWindow:
    return AnalysisWindow(
        file_id=uuid.uuid4(),
        tier="fine",
        window_index=index,
        start_sec=start,
        end_sec=end,
        bpm=bpm,
        musical_key=key,
        camelot=camelot,
    )


def _coarse(index: int, start: float, end: float, *, energy: float | None = None, moods: dict[str, float] | None = None) -> AnalysisWindow:
    return AnalysisWindow(
        file_id=uuid.uuid4(),
        tier="coarse",
        window_index=index,
        start_sec=start,
        end_sec=end,
        energy=energy,
        mood_scores=moods,
    )


def _moods(**overrides: float) -> dict[str, float]:
    scores = dict.fromkeys(MOOD_ORDER, 0.1)
    scores.update(overrides)
    return scores


def _segment(position: int, start: float, end: float | None, *, title: str | None = None) -> TrackSegment:
    return TrackSegment(
        position=position,
        start_sec=start,
        end_sec=end,
        bpm=None,
        camelot=None,
        mood=None,
        energy=None,
        title=title,
    )


# --- The payload --------------------------------------------------------------------------


def test_every_window_carries_the_energy_camelot_and_top_mood_the_readout_names() -> None:
    """The readout's four facts come off the SAME window row the lanes were drawn from.

    Pinned per-window rather than per-file: the inspection is a claim about the instant under
    the cursor, so a payload that carried only a file-level key or a file-level mood would let
    the sentence stay right for the set while being wrong for the moment being pointed at.
    """
    windows = [
        _fine(0, 0.0, 30.0, bpm=128.0, key="A minor", camelot="8A"),
        _coarse(0, 0.0, 180.0, energy=0.62, moods=_moods(mood_happy=0.8)),
    ]

    payload = inspection_windows(windows)

    assert payload[0]["camelot"] == "8A"
    assert payload[0]["key"] == "A minor"
    # A fine window has no energy and no mood scores of its own: those are the coarse tier's
    # columns, and manufacturing a zero here would put "energy 0.00" under the cursor.
    assert payload[0]["energy"] is None
    assert payload[0]["mood_top"] is None
    assert payload[1]["energy"] == 0.62
    # 0.8 against the six other moods at 0.1 each: 0.8 / 1.4, the band's own thickness.
    assert payload[1]["mood_top"] == {"name": "mood_happy", "label": "Happy", "share": round(0.8 / 1.4, 4)}


def test_the_top_mood_share_is_the_same_fraction_the_river_draws_as_that_band() -> None:
    """One normalisation, so the sentence and the picture cannot disagree about one window.

    The share is over the SEVEN mood classifiers, not the eleven ``MOOD_ORDER`` names: four of
    those (danceability, gender, tonality, voice_instrumental) are not moods and are not bands
    in the river either. Two moods at 0.8 against five at 0.1 is 0.8/2.1, and the reader is
    told 38% because that is the band's thickness.
    """
    window = _coarse(0, 0.0, 180.0, moods=_moods(mood_happy=0.8, mood_sad=0.8, danceability=0.9))

    top = top_mood(window)

    assert top is not None
    assert top["share"] == round(0.8 / (0.8 + 0.8 + 0.1 * 5), 4)
    # A tie resolves to MOOD_NAMES order, which is stable across renders of identical data.
    assert top["name"] == "mood_happy"


def test_an_unprojected_coarse_window_has_no_top_mood_rather_than_a_manufactured_one() -> None:
    """A window with no scores, and one whose scores are all zero, both report nothing."""
    assert top_mood(_coarse(0, 0.0, 180.0)) is None
    assert top_mood(_coarse(0, 0.0, 180.0, moods=dict.fromkeys(MOOD_ORDER, 0.0))) is None


def test_the_payload_carries_the_track_segments_the_readout_and_the_shaded_span_read() -> None:
    """Each timestamped track arrives as a span with its number and title.

    ``end`` survives as ``None`` for an open-ended final segment: the client reads that as "to
    the end of what we know" rather than inventing a length, which is the same reading
    ``TrackSegment.end_sec`` documents.
    """
    windows = [_fine(0, 0.0, 30.0, bpm=128.0, key="A minor", camelot="8A"), _coarse(0, 0.0, 600.0, energy=0.4)]
    segments = [_segment(1, 0.0, 300.0, title="Opening Track"), _segment(2, 300.0, None, title=None)]

    payload = build_analysis_timeline_context(windows, track_segments=segments)["timeline_inspection"]

    assert payload["segments"] == [  # type: ignore[index]
        {"position": 1, "title": "Opening Track", "start": 0.0, "end": 300.0},
        {"position": 2, "title": None, "start": 300.0, "end": None},
    ]


def test_a_segment_starting_past_the_analyzed_extent_is_dropped_not_clamped_onto_the_end() -> None:
    """A scraped timestamp beyond what was analyzed describes no part of this file.

    Clamping it to the end would put a track under the cursor at the file's last instant that
    the audio there has nothing to do with -- the shape a bad scrape produces, and the one the
    readout must not launder into a confident claim.
    """
    windows = [_fine(0, 0.0, 30.0, bpm=128.0), _coarse(0, 0.0, 60.0, energy=0.4)]

    payload = build_analysis_timeline_context(windows, track_segments=[_segment(9, 6_000.0, None)])["timeline_inspection"]

    assert payload["segments"] == []  # type: ignore[index]


def test_the_resting_peak_is_the_energy_peak_and_is_absent_when_nothing_measured_energy() -> None:
    """``peak_sec`` is where the page rests, and ``None`` is an honest "there is no peak".

    Zero would be indistinguishable from a real peak at the very start of the set, and the
    client labels its resting state "Peak" -- so a defaulted zero would caption an unmeasured
    file with a maximum nobody measured.
    """
    measured = [
        _fine(0, 0.0, 30.0, bpm=128.0),
        _coarse(0, 0.0, 180.0, energy=0.2),
        _coarse(1, 180.0, 360.0, energy=0.9),
        _coarse(2, 360.0, 540.0, energy=0.3),
    ]
    unmeasured = [_fine(0, 0.0, 30.0, bpm=128.0), _coarse(0, 0.0, 180.0)]

    assert build_analysis_timeline_context(measured)["timeline_inspection"]["peak_sec"] == 270.0  # type: ignore[index]
    assert build_analysis_timeline_context(unmeasured)["timeline_inspection"]["peak_sec"] is None  # type: ignore[index]


def test_the_payloads_key_runs_are_numbered_exactly_as_the_wheel_numbers_its_nodes() -> None:
    """The ring is placed BY INDEX, so the two numberings must be one numbering.

    The discriminating case is an unplaceable code between two placeable ones: both sides drop
    it, and a second implementation that dropped it at a different point would still hand back
    an index resolving to a real node -- silently ringing the wrong key from there on. Note the
    run counts below are of what SURVIVED the flicker filter, not of the windows fed in.
    """
    fine = [
        _fine(0, 0.0, 30.0, camelot="8A"),
        _fine(1, 30.0, 60.0, camelot="8A"),
        _fine(2, 60.0, 90.0, camelot="ZZ"),
        _fine(3, 90.0, 120.0, camelot="ZZ"),
        _fine(4, 120.0, 150.0, camelot="5A"),
        _fine(5, 150.0, 180.0, camelot="5A"),
    ]

    runs = inspection_key_runs(fine)
    nodes = build_harmonic_journey(fine).nodes

    assert [run["index"] for run in runs] == [node.index for node in nodes]
    assert [run["code"] for run in runs] == [node.code for node in nodes]
    assert [run["number"] for run in runs] == [node.number for node in nodes]
    assert "ZZ" not in [run["code"] for run in runs]
    assert [(run["start"], run["end"]) for run in runs] == [(0.0, 60.0), (120.0, 180.0)]


def test_a_file_with_no_key_data_yields_no_key_runs_and_no_ring_to_place() -> None:
    assert inspection_key_runs([_fine(0, 0.0, 30.0, bpm=128.0)]) == []


# --- The script's own contract --------------------------------------------------------------


def test_no_global_keydown_handler_is_attached_anywhere_in_the_shipped_script() -> None:
    """Arrow keys belong to the focused control, never to the document.

    A document-level keydown handler would take Left/Right away from every text field and
    native control on the page and would shadow the browser's own shortcuts -- including undo
    and redo. The inspector is a ``role="slider"`` with a tabindex, so arrows reach it exactly
    when it is focused and never otherwise. Greps the SHIPPED file, since there is no build
    step that could rewrite it between here and the browser.
    """
    source = _SCRIPT.read_text(encoding="utf-8")

    assert 'document.addEventListener("keydown"' not in source
    assert not re.search(r"""document\s*\.\s*addEventListener\s*\(\s*['"`]key(down|up|press)['"`]""", source)
    assert not re.search(r"""(window|document\.body)\s*\.\s*addEventListener\s*\(\s*['"`]key(down|up|press)['"`]""", source)
    assert 'inspector.addEventListener("keydown"' in source, "the inspector itself must still handle arrows"


def test_the_pure_lookups_are_exported_so_the_browser_harness_can_unit_test_them() -> None:
    """The three functions the browser unit test reaches for are on the public object.

    Without this the lookup test would silently degrade: ``window.PhazeAnalysisTimeline.segmentAt``
    would be ``undefined``, and a test that called it would fail loudly -- but a test that
    guarded on its presence would pass while measuring nothing. Pinning the export here means a
    rename breaks the DEFAULT suite, where it is seen immediately.
    """
    source = _SCRIPT.read_text(encoding="utf-8")

    assert "window.PhazeAnalysisTimeline = { initialize: initializeWithin, measuredWindow, segmentAt, keyRunAt };" in source
    for name in ("function measuredWindow(", "function segmentAt(", "function keyRunAt("):
        assert name in source


def test_every_dom_hook_the_inspection_drives_is_rendered_by_a_template() -> None:
    """Seven targets, seven hooks, each named in the one template that renders it.

    A hook renamed on either side does not error -- the script's ``querySelector`` returns null
    and that mark simply stops lighting, on a page that still looks fine. These names are the
    contract between the script and the markup, and they are cheap to pin and expensive to lose.
    """
    timeline = _TIMELINE_TEMPLATE.read_text(encoding="utf-8")
    tracklist = _TRACKLIST_TEMPLATE.read_text(encoding="utf-8")
    wheel = _WHEEL_TEMPLATE.read_text(encoding="utf-8")
    primitives = _PRIMITIVES_TEMPLATE.read_text(encoding="utf-8")
    script = _SCRIPT.read_text(encoding="utf-8")

    for hook in ("data-timeline-readout", "data-timeline-tooltip", "data-timeline-cursor", "data-timeline-track-span"):
        assert hook in timeline, f"{hook} is read by analysis_timeline.js and must be rendered"
        assert hook in script
    assert 'data-ribbon-start="{{ ribbon.start_sec }}"' in timeline
    assert 'data-ribbon-end="{{ ribbon.end_sec }}"' in timeline
    assert "data-track-row" in tracklist and "data-track-row" in script
    assert 'data-track-position="{{ track.position }}"' in tracklist
    assert 'data-track-start="{{ segment.start_sec }}"' in tracklist
    assert "data-journey-cursor-ring" in wheel and "data-journey-cursor-ring" in script
    assert "data-set-glyph-cursor" in primitives and "data-set-glyph-cursor" in script


def test_the_satellite_targets_are_looked_up_within_a_scope_never_across_the_document() -> None:
    """Both record presentations mark a scope, and the script never falls back to the document.

    The Files matrix and Changes Review render a glyph per ROW. A document-wide lookup for
    ``[data-set-glyph-cursor]`` would let one file's timeline mark another file's glyph, and
    would do it silently -- the mark lands on a real element, just the wrong one.
    """
    script = _SCRIPT.read_text(encoding="utf-8")

    assert 'const SCOPE_SELECTOR = "[data-inspection-scope]";' in script
    assert "data-inspection-scope" in _RECORD_PAGE.read_text(encoding="utf-8")
    assert "data-inspection-scope" in _RECORD_BODY.read_text(encoding="utf-8")
    for satellite in ("data-tracklist-index", "data-journey-cursor-ring", "data-set-glyph"):
        assert f'document.querySelector("[{satellite}' not in script
        assert f'document.querySelectorAll("[{satellite}' not in script


def test_only_a_timestamped_track_row_becomes_focusable() -> None:
    """A row with no segment has no time to scrub to, so it gets no tab stop.

    The alternative -- one tab stop per scraped row -- adds dozens of stops to the record page's
    tab ring, most of which would do nothing when reached. A tab stop that does nothing is worse
    than no tab stop.
    """
    tracklist = _TRACKLIST_TEMPLATE.read_text(encoding="utf-8")

    row = re.search(r"<tr data-track-row.*?>", tracklist, re.DOTALL)
    assert row is not None
    assert "{% if segment %}" in row.group(0)
    assert 'tabindex="0"{% endif %}' in row.group(0)
    assert re.search(r'<tr data-track-row[^>]*tabindex="0"(?!\{)', row.group(0)) is None, (
        "tabindex must sit inside the `if segment` branch, not unconditionally on the row"
    )


def test_the_inspector_still_declares_aria_valuetext_before_any_script_runs() -> None:
    """The slider is described from the markup up, not only once JavaScript has run."""
    timeline = _TIMELINE_TEMPLATE.read_text(encoding="utf-8")

    assert 'role="slider"' in timeline
    assert "aria-valuetext=" in timeline
    assert "aria-valuemin=" in timeline and "aria-valuemax=" in timeline
