"""TrackId detail JSON interpretation. Fixtures are synthetic copies of the response shape."""

import asyncio
from datetime import UTC, datetime
from fractions import Fraction
import json

from phaze.tracklist_providers import local_registry
from phaze.tracklist_providers.domain import (
    DiscoveryBudget,
    LoadBudget,
    LoadOutcome,
    OutcomeStatus,
    ProviderCandidate,
    Snapshot,
    SourceIdentity,
    SourceRead,
    SourceReference,
)
from phaze.tracklist_providers.trackid_interpreter import TrackIdInterpreter


NOW = datetime(2026, 1, 1, tzinfo=UTC)
IDENTITY = SourceIdentity(provider_id="local", native_id="stream-0001")
REFERENCE = SourceReference(identity=IDENTITY, channel="companion", format="trackid_audiostream_detail_json")
CANDIDATE = ProviderCandidate(identity=IDENTITY, source=REFERENCE)


def row(start: object = "00:02:43", artist: object = "Artist A", title: object = "Title A", **extra: object) -> dict[str, object]:
    return {"artist": artist, "title": title, "label": "Label A", "startTime": start, "endTime": "00:05:00", **extra}


def detail(rows: list[object], *, status: int = 3, track_count: int = 0, process_id: int = 10) -> dict[str, object]:
    return {
        "status": status,
        "trackCount": track_count,
        "detectionProcesses": [{"id": process_id, "detectionProcessMusicTracks": rows}],
    }


def read(document: object) -> SourceRead:
    text = document if isinstance(document, str) else json.dumps(document)
    return SourceRead(status=OutcomeStatus.FOUND, code="read", scope="stream-0001", text=text, retrieved_at=NOW)


def interpret(document: object) -> LoadOutcome:
    return TrackIdInterpreter().interpret(CANDIDATE, read(document), LoadBudget())


def only_snapshot(document: object) -> Snapshot:
    snapshot = interpret(document).snapshot
    assert snapshot is not None
    return snapshot


def test_whole_second_offset_is_approximate_with_second_precision() -> None:
    stamp = only_snapshot(detail([row("00:02:43")])).tracks[0].timestamp
    assert stamp is not None
    assert stamp.kind == "offset"
    assert stamp.offset is not None
    assert stamp.offset.as_fraction() == 163
    assert stamp.offset_usability == "approximate"
    assert stamp.precision == "second"
    assert stamp.origin == "local:stream:stream-0001"
    assert stamp.original == "00:02:43"


def test_fractional_offset_is_approximate_with_unknown_precision() -> None:
    stamp = only_snapshot(detail([row("00:00:00.2700000")])).tracks[0].timestamp
    assert stamp is not None
    assert stamp.original == "00:00:00.2700000"
    assert stamp.precision == "unknown"
    assert stamp.offset is not None
    assert stamp.offset.as_fraction() == Fraction(27, 100)
    assert stamp.offset_usability == "approximate"


def test_day_prefixed_offset_is_read() -> None:
    stamp = only_snapshot(detail([row("1.01:00:00")])).tracks[0].timestamp
    assert stamp is not None
    assert stamp.offset is not None
    assert stamp.offset.as_fraction() == 86400 + 3600


def test_no_offset_is_ever_qualified() -> None:
    rows = [row("00:02:43"), row("00:00:00.2700000"), row("garbage"), row(None), row(12)]
    for track in only_snapshot(detail(rows)).tracks:
        assert track.timestamp is None or track.timestamp.offset_usability != "qualified"


def test_unparseable_or_missing_start_time_is_unusable_or_absent() -> None:
    tracks = only_snapshot(detail([row("garbage"), row(None)])).tracks
    assert tracks[0].timestamp is not None
    assert tracks[0].timestamp.kind == "unknown"
    assert tracks[0].timestamp.offset is None
    assert tracks[0].timestamp.offset_usability == "unusable"
    assert tracks[1].timestamp is None


def test_row_count_comes_from_rows_not_detail_trackcount() -> None:
    rows = [row(f"00:{i:02d}:00") for i in range(11)]
    outcome = interpret(detail(rows, track_count=0))
    assert outcome.snapshot is not None
    assert len(outcome.snapshot.tracks) == 11
    assert [t.position for t in outcome.snapshot.tracks] == list(range(1, 12))
    assert outcome.status == OutcomeStatus.FOUND


def test_unresolved_rows_are_preserved_nullable_and_unplaceholdered() -> None:
    tracks = only_snapshot(detail([row(artist=None, title=None), row(artist="", title="  "), row("00:03:00", artist="Artist B", title=None)])).tracks
    assert len(tracks) == 3
    assert (tracks[0].artist, tracks[0].title) == (None, None)
    assert (tracks[1].artist, tracks[1].title) == (None, None)
    assert (tracks[2].artist, tracks[2].title) == ("Artist B", None)
    assert tracks[0].timestamp is not None


def test_non_ready_status_is_incomplete_never_found_empty() -> None:
    for status in (1, 2, 4, None, "3", True):
        document = detail([row()], status=0)
        document["status"] = status
        outcome = interpret(document)
        assert outcome.status == OutcomeStatus.INCOMPLETE
        assert outcome.snapshot is not None
        assert outcome.snapshot.completeness.state == "incomplete"


def test_ready_stream_with_no_rows_is_incomplete_not_found_empty() -> None:
    outcome = interpret(detail([]))
    assert outcome.status == OutcomeStatus.INCOMPLETE
    assert outcome.snapshot is not None
    assert outcome.snapshot.tracks == ()


def test_highest_detection_process_id_wins() -> None:
    document = {
        "status": 3,
        "detectionProcesses": [
            {"id": 7, "detectionProcessMusicTracks": [row(title="Old")]},
            {"id": 12, "detectionProcessMusicTracks": [row(title="New 1"), row(title="New 2")]},
            {"id": 9, "detectionProcessMusicTracks": [row(title="Mid")]},
        ],
    }
    assert [t.title for t in only_snapshot(document).tracks] == ["New 1", "New 2"]


def test_malformed_documents_are_contract_errors_or_incomplete() -> None:
    assert interpret("not json").status == OutcomeStatus.CONTRACT_ERROR
    assert interpret("[1]").status == OutcomeStatus.CONTRACT_ERROR
    assert interpret({"status": 3}).status == OutcomeStatus.INCOMPLETE
    assert interpret({"status": 3, "detectionProcesses": [None, {"id": "x"}]}).status == OutcomeStatus.INCOMPLETE
    skipped = interpret(detail([row(), "junk"]))
    assert skipped.status == OutcomeStatus.INCOMPLETE
    assert skipped.snapshot is not None
    assert len(skipped.snapshot.tracks) == 1


def test_output_passes_the_real_snapshot_validation() -> None:
    """The local provider's real path: LocalProvider load, with the registry re-validating its output."""
    document = detail([row("00:02:43"), row("00:00:00.2700000", artist=None), row("00:09:10")])

    class Effects:
        async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
            del budget
            assert source == REFERENCE
            return read(document)

    result = asyncio.run(local_registry(TrackIdInterpreter()).load(CANDIDATE, LoadBudget(), Effects()))
    assert result.status == OutcomeStatus.FOUND, result.detail
    assert result.snapshot is not None
    assert len(result.snapshot.tracks) == 3
    assert Snapshot.model_validate(result.snapshot.model_dump()) == result.snapshot
    assert LoadOutcome.model_validate_json(result.model_dump_json()) == result
