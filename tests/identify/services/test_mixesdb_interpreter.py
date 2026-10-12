"""MixesDB wikitext interpreter: synthetic case table from docs/spikes/phaze-vwcsv-mixesdb-parsing-provenance.md."""

from datetime import UTC, datetime
import time

import pytest

from phaze.tracklist_providers.domain import (
    LoadBudget,
    LoadOutcome,
    OutcomeStatus,
    ProviderCandidate,
    Snapshot,
    SourceIdentity,
    SourceRead,
)
from phaze.tracklist_providers.mixesdb_parser import PARSER_VERSION, RIGHTS_UNKNOWN, MixesDbInterpreter, mixesdb_revision


NOW = datetime(2026, 1, 1, tzinfo=UTC)
ORIGIN = "mixesdb:elapsed-from-mix-start"
KEY = "Example Set/Part 1"
URL = "https://example.invalid/w/Example_Set/Part_1"


def run(text: str | None, *, interpreter: MixesDbInterpreter | None = None, budget: LoadBudget | None = None, **read: object) -> LoadOutcome:
    candidate = ProviderCandidate(identity=SourceIdentity(provider_id="mixesdb", native_id=KEY), url=URL)
    values: dict[str, object] = {
        "status": OutcomeStatus.FOUND,
        "code": "ok",
        "scope": KEY,
        "text": text,
        "revision": mixesdb_revision(7, "2026-01-01T00:00:00Z"),
        "retrieved_at": NOW,
    }
    values.update(read)
    return (interpreter or MixesDbInterpreter()).interpret(candidate, SourceRead.model_validate(values), budget or LoadBudget())


def snap(outcome: LoadOutcome) -> Snapshot:
    assert outcome.snapshot is not None
    return outcome.snapshot


def test_both_list_forms_are_parsed_in_order() -> None:
    numbered = snap(run("# Example Artist - Opening Track\n# Other Artist - Closing Track\n"))
    listed = snap(run("<list>\nExample Artist - Opening Track\nOther Artist - Closing Track\n</list>\n"))
    for result in (numbered, listed):
        assert [(t.position, t.artist, t.title) for t in result.tracks] == [
            (1, "Example Artist", "Opening Track"),
            (2, "Other Artist", "Closing Track"),
        ]
    assert snap(run("<list>\nA - B\n</list>")).completeness.state == "complete"


def test_unsupported_nesting_is_rejected_explicitly_not_returned_empty() -> None:
    for text in ("# A - B\n## C - D\n", "# A - B\n#* C - D\n", "#: note\n"):
        outcome = run(text)
        assert outcome.status == OutcomeStatus.UNSUPPORTED
        assert outcome.code == "unsupported_nesting"
        assert outcome.snapshot is None
        assert "lines" in outcome.evidence[0]


def test_section_labels_are_preserved_as_sections() -> None:
    result = snap(run("== First Half ==\n# A - B\n# C - D\n=== Second Half ===\n# E - F\n"))
    assert [(s.ordinal, s.title) for s in result.sections] == [(1, "First Half"), (2, "Second Half")]
    assert [t.section_ordinal for t in result.tracks] == [1, 1, 2]
    assert result.section_scan_complete
    leading = snap(run("# A - B\n== Later ==\n# C - D\n"))
    assert [(s.ordinal, s.title) for s in leading.sections] == [(1, None), (2, "Later")]
    assert [t.section_ordinal for t in leading.tracks] == [1, 2]


def test_minute_cue_is_approximate_and_never_exact() -> None:
    result = snap(run("# [05] Example Artist - Example Title [Example Label]\n", interpreter=MixesDbInterpreter(offset_origin=ORIGIN)))
    track = result.tracks[0]
    stamp = track.timestamp
    assert stamp is not None
    assert (stamp.original, stamp.kind, stamp.precision, stamp.offset_usability, stamp.origin) == ("05", "offset", "minute", "approximate", ORIGIN)
    assert stamp.offset is not None and stamp.offset.as_fraction() == 300
    assert track.label == "Example Label"
    assert track.title == "Example Title"
    # Without origin evidence the same syntax proves nothing.
    bare = snap(run("# [05] A - B\n")).tracks[0].timestamp
    assert bare is not None and (bare.kind, bare.offset, bare.offset_usability) == ("unknown", None, "unusable")


def test_partial_and_unknown_cues_keep_original_text_and_are_unusable() -> None:
    result = snap(run("# [07:??] A - B\n# [0?] ID - Unknown\n", interpreter=MixesDbInterpreter(offset_origin=ORIGIN)))
    for track, original in zip(result.tracks, ("07:??", "0?"), strict=True):
        assert track.timestamp is not None
        assert track.timestamp.original == original
        assert (track.timestamp.offset, track.timestamp.offset_usability, track.timestamp.precision) == (None, "unusable", "unknown")
    assert (result.tracks[1].artist, result.tracks[1].title) == (None, None)


def test_ambiguous_colon_tuple_is_not_classified() -> None:
    result = snap(run("# [12:35] A - B\n", interpreter=MixesDbInterpreter(offset_origin=ORIGIN)))
    stamp = result.tracks[0].timestamp
    assert stamp is not None
    assert (stamp.original, stamp.kind, stamp.precision, stamp.offset, stamp.offset_usability) == ("12:35", "unknown", "unknown", None, "unusable")
    assert "ambiguous_colon_tuple" in stamp.evidence
    assert all(t.timestamp is None or t.timestamp.precision != "cue_frame_75" for t in result.tracks)


def test_explicit_clock_sequence_records_midnight_wrap() -> None:
    result = snap(run("# [clock 23:58] A - B\n# [clock 00:03] C - D\n"))
    first, second = (t.timestamp for t in result.tracks)
    assert first is not None and second is not None
    assert (first.kind, second.kind) == ("clock", "clock")
    assert "midnight_wrap" not in first.evidence
    assert "midnight_wrap" in second.evidence
    assert (second.offset, second.offset_usability) == (None, "unusable")


def test_first_cue_after_ten_minutes_is_not_treated_as_clock() -> None:
    interpreter = MixesDbInterpreter(offset_origin=ORIGIN)
    for text in ("# [15] A - B\n", "# [15:00] A - B\n", "# [01:12:30] A - B\n"):
        stamp = snap(run(text, interpreter=interpreter)).tracks[0].timestamp
        assert stamp is not None
        assert stamp.kind != "clock"
    hour = snap(run("# [01:12:30] A - B\n", interpreter=interpreter)).tracks[0].timestamp
    assert hour is not None and (hour.precision, hour.offset_usability) == ("second", "approximate")
    assert hour.offset is not None and hour.offset.as_fraction() == 4350


def test_one_qualified_entry_does_not_upgrade_the_others() -> None:
    text = "# [01:12:30] A - B\n# [07:??] C - D\n# [12:35] E - F\n# G - H\n# [09] I - J\n"
    result = snap(run(text, interpreter=MixesDbInterpreter(offset_origin=ORIGIN)))
    usability = [t.timestamp.offset_usability if t.timestamp else None for t in result.tracks]
    assert usability == ["approximate", "unusable", "unusable", None, "approximate"]
    assert "qualified" not in usability


def test_mashup_and_remix_text_is_preserved_not_split() -> None:
    result = snap(run("# Example Artist - Example Title (Example Remix) w/ Other Artist - Other Title\n"))
    assert len(result.tracks) == 1
    track = result.tracks[0]
    assert track.is_mashup
    assert track.remix_info == "Example Remix"
    assert track.artist == "Example Artist"
    assert track.title == "Example Title (Example Remix) w/ Other Artist - Other Title"


def test_repeated_track_text_keeps_separate_occurrences() -> None:
    result = snap(run("# A - B\n# A - B\n# A - B\n"))
    assert [t.position for t in result.tracks] == [1, 2, 3]
    assert {(t.artist, t.title) for t in result.tracks} == {("A", "B")}


def test_unrecognized_page_is_incomplete_not_found_empty() -> None:
    outcome = run("Just prose.\n[[Category:Example]]\n")
    assert outcome.status == OutcomeStatus.INCOMPLETE
    assert outcome.code == "unrecognized_mixesdb_page"
    assert snap(outcome).tracks == ()
    assert snap(outcome).completeness.state == "incomplete"
    assert run("").status == OutcomeStatus.INCOMPLETE


def test_truncation_is_incomplete_with_the_limit_named() -> None:
    body = "".join(f"# A{i} - T{i}\n" for i in range(10))
    cases = (
        (run(body, budget=LoadBudget(max_lines=3)), "max_lines=3", 3),
        (run(body, budget=LoadBudget(max_tracks=4)), "max_tracks=4", 4),
        (run(body, budget=LoadBudget(max_characters=30)), "max_characters=30", None),
        (run(body, budget=LoadBudget(max_bytes=30)), "max_bytes=30", None),
        (run(body, truncated=True), "source_read_truncated", 10),
    )
    for outcome, limit, count in cases:
        assert outcome.status == OutcomeStatus.INCOMPLETE
        result = snap(outcome)
        assert result.completeness.state == "incomplete"
        assert limit in result.completeness.reason
        assert limit in " ".join(result.completeness.evidence)
        if count is not None:
            assert len(result.tracks) == count
    cut = snap(run(body, budget=LoadBudget(max_characters=25)))
    assert all(t.title and t.title.startswith("T") for t in cut.tracks)
    assert not cut.section_scan_complete


def test_missing_separator_template_and_unterminated_list_are_incomplete_with_positions() -> None:
    result = snap(run("# no separator here\n# A - B {{tmpl}}\n# C - D\n<list>\nE - F\n"))
    assert result.completeness.state == "incomplete"
    joined = " ".join(result.completeness.evidence)
    assert "missing_separator: lines 1" in joined
    assert "nested_template: lines 2" in joined
    assert "unterminated_list: lines 4" in joined
    assert (result.tracks[0].artist, result.tracks[0].title) == (None, None)
    assert "source_text:no separator here" in result.tracks[0].evidence
    assert (result.tracks[2].artist, result.tracks[2].title) == ("C", "D")


def test_slash_containing_key_is_preserved_verbatim() -> None:
    result = snap(run("# A - B\n"))
    assert result.identity.native_id == KEY
    assert result.url == URL
    assert f"native_key:{KEY}" in result.provenance
    assert f"page_url:{URL}" in result.provenance
    assert result.revision == "7@2026-01-01T00:00:00Z"
    assert result.parser_version == PARSER_VERSION
    assert result.rights_evidence == RIGHTS_UNKNOWN
    custom = snap(run("# A - B\n", interpreter=MixesDbInterpreter(rights_evidence=("rights:example",))))
    assert custom.rights_evidence == ("rights:example",)


def test_output_passes_the_real_snapshot_validation() -> None:
    text = "== One ==\n# [05] A - B [L]\n# [clock 23:58] ID - ID\n<list>\n[08] C - D (X Remix)\n</list>\n== Two ==\n# [01:12:30] E - F\n"
    result = snap(run(text, interpreter=MixesDbInterpreter(offset_origin=ORIGIN)))
    assert Snapshot.model_validate(result.model_dump(mode="python")) == result
    assert LoadOutcome.model_validate(run(text).model_dump(mode="python")).status == OutcomeStatus.FOUND


def test_other_provider_and_failed_reads_are_passed_through() -> None:
    candidate = ProviderCandidate(identity=SourceIdentity(provider_id="other", native_id="x"))
    read = SourceRead(status=OutcomeStatus.FOUND, code="ok", scope="x", text="# A - B", retrieved_at=NOW)
    assert MixesDbInterpreter().interpret(candidate, read, LoadBudget()).status == OutcomeStatus.CONTRACT_ERROR
    assert run(None, status=OutcomeStatus.UNAVAILABLE).status == OutcomeStatus.UNAVAILABLE
    assert run(None).code == "missing_decoded_text"


def test_overlong_empty_and_closing_tag_lines_are_handled() -> None:
    result = snap(run("# " + "x" * 5000 + "\n#\n<list>A - B</list>\n<list></list>\n"))
    joined = " ".join(result.completeness.evidence)
    assert "line_too_long: lines 1" in joined
    assert "empty_entry" in joined
    assert [(t.artist, t.title) for t in result.tracks[2:3]] == [("A", "B")]


def test_clock_out_of_range_and_elapsed_edge_shapes_are_unusable() -> None:
    interpreter = MixesDbInterpreter(offset_origin=ORIGIN)
    text = "# [clock 25:00] A - B\n# [00:75:00] C - D\n# [1:2:3] E - F\n# [01:02:03] G - H\n"
    stamps = [t.timestamp for t in snap(run(text, interpreter=interpreter)).tracks]
    assert [s.offset_usability for s in stamps if s] == ["unusable", "unusable", "unusable", "approximate"]
    assert snap(run("# [01:02:03] A - B\n")).tracks[0].timestamp.offset_usability == "unusable"  # type: ignore[union-attr]


def test_leading_non_cue_bracket_and_heading_without_title() -> None:
    result = snap(run("== ==\n# [Label Only] A - B\n"))
    assert result.tracks[0].timestamp is None


@pytest.mark.parametrize(
    "unit",
    [
        "# " + "(" * 2000 + "\n",
        "# " + "[" * 2000 + "\n",
        "# [" + "9" * 30 + "\n",
        "#" + " - " * 600 + "\n",
        "== " + "=" * 2000 + "\n",
        "<list>" * 300 + "\n",
        "# A - B\n",
    ],
)
def test_parser_is_linear_over_adversarial_input(unit: str) -> None:
    budget = LoadBudget(max_lines=16384, max_tracks=100, max_characters=262144, max_bytes=1048576)

    def timed(size: int) -> float:
        text = unit * max(1, size // len(unit))
        start = time.perf_counter()
        outcome = run(text, budget=budget)
        elapsed = time.perf_counter() - start
        if outcome.snapshot is not None:
            assert len(outcome.snapshot.tracks) <= 100
        return elapsed

    small, large = timed(60_000), timed(240_000)
    assert large < max(small * 12, 1.0)
    assert large < 5.0


def test_line_and_track_bounds_stop_pathological_volume() -> None:
    text = "# A - B\n" * 10_000
    tracks = snap(run(text, budget=LoadBudget(max_tracks=100)))
    assert len(tracks.tracks) == 100
    assert "limit:max_tracks=100" in tracks.completeness.evidence
    lines = snap(run("\n" * 20_000 + "# A - B\n", budget=LoadBudget(max_lines=50)))
    assert lines.tracks == ()
    assert "limit:max_lines=50" in lines.completeness.evidence
