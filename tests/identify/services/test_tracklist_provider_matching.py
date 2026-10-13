"""Minimal set-to-recording evidence. Scenarios come from docs/design/tracklist-provider-matching.md; all data is invented."""

import ast
from datetime import UTC, datetime
from pathlib import Path
import unicodedata

import pytest

from phaze.tracklist_providers import matching
from phaze.tracklist_providers.domain import Completeness, Fact, ProviderCandidate, RecordingContext, Snapshot, SourceIdentity
from phaze.tracklist_providers.matching import AssessedSubject, assess_candidates, assess_recording_match


def _known(value: str) -> Fact:
    return Fact(value=value, certainty="known")


def _recording(**overrides: object) -> RecordingContext:
    fields: dict[str, object] = {
        "recording_id": "rec-1",
        "artist": _known("Example Artist"),
        "event": _known("Example Event"),
        "date": _known("2024-06-15"),
    }
    return RecordingContext(**{**fields, **overrides})  # type: ignore[arg-type]


def _snapshot(native_id: str = "set-1", **overrides: object) -> Snapshot:
    fields: dict[str, object] = {
        "identity": SourceIdentity(provider_id="fake", native_id=native_id),
        "retrieved_at": datetime(2026, 1, 1, tzinfo=UTC),
        "artist": _known("Example Artist"),
        "event": _known("Example Event"),
        "date": _known("2024-06-15"),
        "source_format": "fake",
        "parser_version": "1",
        "completeness": Completeness(state="complete", reason="fixture", evidence=("empty",)),
    }
    return Snapshot(**{**fields, **overrides})  # type: ignore[arg-type]


def _subject(kind: str = "performance", **overrides: object) -> AssessedSubject:
    return AssessedSubject(source=_snapshot(**overrides), date_kind=kind)  # type: ignore[arg-type]


def test_all_corroborated_fields_still_require_review() -> None:
    result = assess_recording_match(_recording(), _subject(), recording_date_kind="performance")

    assert result.band == "corroborated"
    assert result.eligibility == "review_required"
    assert result.date.precision == "day"


def test_normalized_separators_are_corroborated_but_review_required() -> None:
    recording = _recording(artist=_known("Example_Artist"), event=_known("Example-Event"))

    result = assess_recording_match(recording, _subject(), recording_date_kind="performance")

    assert (result.artist.relation, result.event.relation) == ("normalized", "normalized")
    assert result.eligibility == "review_required"


def test_fuzzy_artist_with_exact_context_is_plausible_not_corroborated() -> None:
    result = assess_recording_match(_recording(artist=_known("Example Artst")), _subject(), recording_date_kind="performance")

    assert result.artist.relation == "fuzzy"
    assert result.band == "plausible"


def test_contradictory_performance_date_is_shown_as_conflicting() -> None:
    result = assess_recording_match(_recording(), _subject(date=_known("2024-06-22")), recording_date_kind="performance")

    assert result.band == "conflicting"
    assert result.date.relation == "contradictory"
    assert any("date" in reason for reason in result.reasons)
    assert result.eligibility == "review_required"


def test_broadcast_and_performance_dates_are_not_equated() -> None:
    result = assess_recording_match(_recording(), _subject(kind="broadcast", date=_known("2024-06-22")), recording_date_kind="performance")

    assert result.date.relation == "kind_mismatch"
    assert result.band == "plausible"
    assert result.band != "conflicting"

    same_day = assess_recording_match(_recording(), _subject(kind="broadcast"), recording_date_kind="performance")
    assert same_day.date.relation == "kind_mismatch"
    assert same_day.band == "plausible"


def test_ambiguous_locale_date_keeps_alternatives() -> None:
    recording = _recording(date=Fact(value="03-04-2024", certainty="inferred"))

    result = assess_recording_match(recording, _subject(date=_known("2024-04-03")), recording_date_kind="performance")

    assert result.date.relation == "compatible_alternative"
    assert set(result.date.recording_alternatives) == {"2024-03-04", "2024-04-03"}
    assert result.band == "plausible"


def test_artist_only_is_insufficient_and_review_required() -> None:
    recording = _recording(event=Fact(), date=Fact())

    result = assess_recording_match(recording, _subject(), recording_date_kind="performance")

    assert result.band == "insufficient"
    assert result.eligibility == "review_required"


def test_candidate_display_name_is_not_inherited_as_metadata() -> None:
    candidate = ProviderCandidate(identity=SourceIdentity(provider_id="fake", native_id="c"), display_name="Example Artist - Example Event")

    result = assess_recording_match(_recording(), AssessedSubject(source=candidate))

    assert result.band == "insufficient"
    assert result.artist.relation == "missing"


def test_semantic_tie_is_retained_and_not_resolved_by_provider_order() -> None:
    first, second = _subject(native_id="b-set"), _subject(native_id="a-set")

    forward = assess_candidates(_recording(), (first, second), recording_date_kind="performance")
    backward = assess_candidates(_recording(), (second, first), recording_date_kind="performance")

    assert [a.identity.native_id for a in forward.ranked] == [a.identity.native_id for a in backward.ranked] == ["a-set", "b-set"]
    assert all(len(a.tied_with) == 1 and a.eligibility == "review_required" for a in forward.ranked)
    assert forward.ranked[0].tied_with == (forward.ranked[1].identity,)


def test_better_evidence_orders_ahead_of_artist_only() -> None:
    weak = _subject(native_id="a-weak", event=Fact(), date=Fact())
    strong = _subject(native_id="z-strong")

    result = assess_candidates(_recording(), (weak, strong), recording_date_kind="performance")

    assert [a.identity.native_id for a in result.ranked] == ["z-strong", "a-weak"]
    assert result.ranked[0].tied_with == ()


def test_truncated_discovery_marks_scope_incomplete_and_never_absent() -> None:
    scope = Completeness(state="incomplete", reason="discovery truncated at max_candidates")

    result = assess_candidates(_recording(), (_subject(),), recording_date_kind="performance", scope=scope)

    assert result.scope.state == "incomplete"
    assert result.ranked[0].scope.reason == "discovery truncated at max_candidates"
    assert result.ranked[0].band == "corroborated"
    assert result.ranked[0].eligibility == "review_required"


def test_unstated_scope_is_incomplete() -> None:
    assert assess_recording_match(_recording(), _subject()).scope.state == "incomplete"


def test_duration_is_ancillary_and_never_a_verdict() -> None:
    subject = AssessedSubject(source=_snapshot(), date_kind="performance", duration_seconds=1800)

    result = assess_recording_match(_recording(duration_seconds=7200), subject, recording_date_kind="performance")

    assert result.duration.difference_seconds == 5400
    assert result.band == "corroborated"


def test_conflicting_and_partial_dates_are_not_certain() -> None:
    conflicting = assess_recording_match(_recording(date=Fact(certainty="conflicting")), _subject())
    month_only = assess_recording_match(_recording(date=_known("2024-06")), _subject(), recording_date_kind="performance")
    junk = assess_recording_match(_recording(date=_known("2024-13")), _subject())

    assert conflicting.date.relation == "uncertain"
    assert (month_only.date.relation, month_only.date.precision) == ("exact", "month")
    assert month_only.band == "plausible"
    assert junk.date.relation == "missing"


def test_unrelated_artist_is_conflicting() -> None:
    result = assess_recording_match(_recording(artist=_known("Someone Else Entirely")), _subject(), recording_date_kind="performance")

    assert result.band == "conflicting"


def test_function_is_pure() -> None:
    tree = ast.parse(Path(matching.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [None])
        if (isinstance(node, ast.Import) or node.module)
    }
    internal = {name for name in imported if name and name.startswith("phaze")}

    assert internal == {"phaze.tracklist_providers.domain"}
    forbidden = {"sqlalchemy", "saq", "redis", "httpx", "aiohttp", "requests", "socket", "subprocess", "pathlib", "os", "asyncio"}
    assert not {name.split(".")[0] for name in imported if name} & forbidden


def test_oversized_text_is_bounded_before_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real = unicodedata.normalize

    def spy(form: str, text: str) -> str:
        seen.append(len(text))
        return real(form, text)

    monkeypatch.setattr(matching.unicodedata, "normalize", spy)
    huge = "Example Artist " * 400_000
    # model_construct bypasses the Text max_length so the matcher itself must hold the bound.
    recording = RecordingContext.model_construct(
        recording_id="rec-1",
        sources=(),
        artist=Fact.model_construct(value=huge, certainty="known", evidence=()),
        event=Fact(),
        date=Fact(),
        duration_seconds=None,
    )
    subject = AssessedSubject.model_construct(source=_snapshot(artist=_known(huge[:4000])), date_kind="performance", duration_seconds=None)

    assess_recording_match(recording, subject)

    assert seen
    assert max(seen) <= matching._RAW_INPUT_CHARACTERS
    assert len(matching._fold(huge)) <= matching._MAX_COMPARED_CHARACTERS
    assert len(matching._squash(huge)) <= matching._MAX_COMPARED_CHARACTERS


def test_oversized_date_text_is_bounded() -> None:
    fact = Fact.model_construct(value="2024-06-15" + " " * 3_000_000 + "x", certainty="known", evidence=())

    assert matching._interpret(fact).values == ("2024-06-15",)


def test_subject_cap_is_named_in_incomplete_scope_and_never_silent() -> None:
    subjects = tuple(_subject(native_id=f"set-{i:04d}") for i in range(matching.MAX_SUBJECTS + 5))

    result = assess_candidates(_recording(), subjects, recording_date_kind="performance")

    assert len(result.ranked) == matching.MAX_SUBJECTS
    assert result.scope.state == "incomplete"
    assert "5 of 261 subjects not assessed" in result.scope.reason
    assert str(matching.MAX_SUBJECTS) in result.scope.reason
    assert all(a.scope == result.scope for a in result.ranked)


def test_a_full_cap_of_equal_candidates_all_tie_with_each_other() -> None:
    subjects = tuple(_subject(native_id=f"set-{i:04d}") for i in range(matching.MAX_SUBJECTS))

    result = assess_candidates(_recording(), subjects, recording_date_kind="performance")

    assert all(len(a.tied_with) == matching.MAX_SUBJECTS - 1 for a in result.ranked)
    assert [a.identity.native_id for a in result.ranked] == sorted(a.identity.native_id for a in result.ranked)
