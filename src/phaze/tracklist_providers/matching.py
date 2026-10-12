"""Minimal set-to-recording evidence (docs/design/tracklist-provider-matching.md).

A pure function from a bounded RecordingContext plus a provider candidate or snapshot to an explainable assessment.
It shows evidence and never authorizes an association: eligibility is always ``review_required``. There is no numeric
acceptance threshold, no companion-chain constant, no Discogs blend, and no I/O, storage or scheduler dependency.
"""

from dataclasses import dataclass
from datetime import date as CalendarDate
from difflib import SequenceMatcher
import re
from typing import Annotated, Final, Literal
import unicodedata

from pydantic import Field

from phaze.tracklist_providers.domain import Completeness, DomainModel, Fact, ProviderCandidate, RecordingContext, Snapshot, SourceIdentity, Text


DateKind = Literal["performance", "broadcast", "publication", "unknown"]
DatePrecision = Literal["day", "month", "year"]
FieldRelation = Literal["exact", "normalized", "fuzzy", "missing", "uncertain", "contradictory"]
DateRelation = Literal["exact", "compatible_alternative", "kind_mismatch", "missing", "uncertain", "contradictory"]
Band = Literal["corroborated", "plausible", "insufficient", "conflicting"]

_FUZZY_LABEL_RATIO: Final = 0.85
"""Similarity at which a near-spelling is LABELLED "fuzzy" evidence.

Uncalibrated and never measured against real data. It only names the relation; it is not an acceptance threshold,
feeds no score, and cannot change eligibility, which is always review_required.
"""
_MAX_COMPARED_CHARACTERS: Final = 512
# Generous margin over the compared cap so slicing before normalization cannot cut a combining sequence that matters.
_RAW_INPUT_CHARACTERS: Final = _MAX_COMPARED_CHARACTERS * 4
_DATE_INPUT_CHARACTERS: Final = 64
MAX_SUBJECTS: Final = 256
_ECHO_CHARACTERS: Final = 4096
_BAND_ORDER: Final[dict[str, int]] = {"corroborated": 0, "plausible": 1, "insufficient": 2, "conflicting": 3}
_RELATION_ORDER: Final[dict[str, int]] = {"exact": 0, "normalized": 1, "fuzzy": 2, "compatible_alternative": 2, "kind_mismatch": 3}
_ISO_DATE: Final = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
_NUMERIC_DATE: Final = re.compile(r"^(\d{1,2})[-/._](\d{1,2})[-/._](\d{4})$")


class FieldEvidence(DomainModel):
    relation: FieldRelation
    recording_value: Text | None = None
    subject_value: Text | None = None
    recording_certainty: str = "unknown"
    subject_certainty: str = "unknown"


class DateEvidence(DomainModel):
    relation: DateRelation
    recording_kind: DateKind = "unknown"
    subject_kind: DateKind = "unknown"
    precision: DatePrecision | None = None
    # Every interpretation of an ambiguous recording date, kept rather than collapsed to one.
    recording_alternatives: tuple[Text, ...] = ()
    subject_value: Text | None = None


class DurationEvidence(DomainModel):
    """Ancillary only: a partial listing or excerpt is not the wrong set, so no verdict is drawn."""

    recording_seconds: float | None = None
    subject_seconds: float | None = None
    difference_seconds: float | None = None


class AssessedSubject(DomainModel):
    """A provider candidate or loaded snapshot plus the facts the provider contract does not carry."""

    source: Snapshot | ProviderCandidate
    date_kind: DateKind = "unknown"
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @property
    def identity(self) -> SourceIdentity:
        return self.source.identity


class MatchAssessment(DomainModel):
    identity: SourceIdentity
    recording_id: str
    artist: FieldEvidence
    event: FieldEvidence
    date: DateEvidence
    duration: DurationEvidence
    band: Band
    reasons: tuple[Text, ...]
    eligibility: Literal["review_required"] = "review_required"
    # Subjects tied on every piece of evidence; display order never resolves them.
    tied_with: Annotated[tuple[SourceIdentity, ...], Field(max_length=256)] = ()
    scope: Completeness


class AssessmentSet(DomainModel):
    ranked: tuple[MatchAssessment, ...]
    scope: Completeness


@dataclass(frozen=True)
class _Dates:
    values: tuple[str, ...]
    precision: DatePrecision | None
    ambiguous: bool


def _bounded(text: str) -> str:
    """Bound BEFORE any normalization so the work is proportional to the cap, never to the input."""
    return text[:_RAW_INPUT_CHARACTERS]


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", _bounded(text)).casefold().strip()[:_MAX_COMPARED_CHARACTERS]


def _squash(text: str) -> str:
    folded = _fold(text)
    return "".join(ch for ch in unicodedata.normalize("NFKD", folded) if ch.isalnum())[:_MAX_COMPARED_CHARACTERS]


def _present(fact: Fact) -> bool:
    return fact.certainty in {"known", "inferred"} and bool(fact.value)


def _echo(value: str | None) -> str | None:
    return value[:_ECHO_CHARACTERS] if value is not None else None


def _compare_field(recording: Fact, subject: Fact) -> FieldEvidence:
    base = {
        "recording_value": _echo(recording.value),
        "subject_value": _echo(subject.value),
        "recording_certainty": recording.certainty,
        "subject_certainty": subject.certainty,
    }
    if recording.certainty == "conflicting" or subject.certainty == "conflicting":
        return FieldEvidence(relation="uncertain", **base)  # type: ignore[arg-type]
    if not (_present(recording) and _present(subject)):
        return FieldEvidence(relation="missing", **base)  # type: ignore[arg-type]
    left, right = str(recording.value), str(subject.value)
    if _fold(left) == _fold(right):
        relation: FieldRelation = "exact"
    elif _squash(left) and _squash(left) == _squash(right):
        relation = "normalized"
    elif SequenceMatcher(None, _squash(left), _squash(right)).ratio() >= _FUZZY_LABEL_RATIO:
        relation = "fuzzy"
    else:
        relation = "contradictory"
    return FieldEvidence(relation=relation, **base)  # type: ignore[arg-type]


def _valid(year: int, month: int, day: int) -> str | None:
    try:
        return CalendarDate(year, month, day).isoformat()
    except ValueError:
        return None


def _interpret(fact: Fact) -> _Dates:
    """Every ISO interpretation of a date; locale-ambiguous numeric forms keep both day/month readings."""
    if not fact.value:
        return _Dates((), None, False)
    value = fact.value[:_DATE_INPUT_CHARACTERS].strip()
    iso = _ISO_DATE.match(value)
    if iso:
        year, month, day = iso.groups()
        if day:
            parsed = _valid(int(year), int(month), int(day))
            return _Dates((parsed,) if parsed else (), "day", False)
        if month and not 1 <= int(month) <= 12:
            return _Dates((), None, False)
        return _Dates((value,), "month" if month else "year", False)
    numeric = _NUMERIC_DATE.match(value)
    if numeric:
        first, second, year = (int(part) for part in numeric.groups())
        readings = dict.fromkeys(filter(None, (_valid(year, second, first), _valid(year, first, second))))
        return _Dates(tuple(readings), "day", len(readings) > 1)
    return _Dates((), None, False)


def _truncate(value: str, precision: DatePrecision) -> str:
    return value[: {"day": 10, "month": 7, "year": 4}[precision]]


_PRECISION_RANK: Final[dict[str, int]] = {"day": 0, "month": 1, "year": 2}


def _compare_date(recording: Fact, subject: Fact, recording_kind: DateKind, subject_kind: DateKind) -> DateEvidence:
    base = {"recording_kind": recording_kind, "subject_kind": subject_kind, "subject_value": _echo(subject.value)}
    if recording.certainty == "conflicting" or subject.certainty == "conflicting":
        return DateEvidence(relation="uncertain", **base)  # type: ignore[arg-type]
    mine, theirs = _interpret(recording), _interpret(subject)
    if not (_present(recording) and _present(subject)) or not mine.values or not theirs.values or mine.precision is None or theirs.precision is None:
        return DateEvidence(relation="missing", **base)  # type: ignore[arg-type]
    precision: DatePrecision = max(mine.precision, theirs.precision, key=_PRECISION_RANK.__getitem__)
    ours = {_truncate(value, precision) for value in mine.values}
    other = {_truncate(value, precision) for value in theirs.values}
    detail = {**base, "precision": precision, "recording_alternatives": mine.values if mine.ambiguous else ()}
    if not ours & other:
        # Only certain performance dates on both sides are a contradiction; other kinds or certainty prove nothing.
        certain = recording.certainty == "known" and subject.certainty == "known" and not mine.ambiguous
        if certain and recording_kind == subject_kind == "performance":
            return DateEvidence(relation="contradictory", **detail)  # type: ignore[arg-type]
        return DateEvidence(relation="kind_mismatch" if recording_kind != subject_kind else "uncertain", **detail)  # type: ignore[arg-type]
    if mine.ambiguous:
        return DateEvidence(relation="compatible_alternative", **detail)  # type: ignore[arg-type]
    if recording_kind != subject_kind or "unknown" in {recording_kind, subject_kind}:
        return DateEvidence(relation="kind_mismatch", **detail)  # type: ignore[arg-type]
    if recording.certainty != "known" or subject.certainty != "known":
        return DateEvidence(relation="uncertain", **detail)  # type: ignore[arg-type]
    return DateEvidence(relation="exact", **detail)  # type: ignore[arg-type]


def _band(artist: FieldEvidence, event: FieldEvidence, date: DateEvidence) -> tuple[Band, tuple[str, ...]]:
    conflicts = [
        f"{name} contradicts the recording"
        for name, relation in (("artist", artist.relation), ("event", event.relation), ("date", date.relation))
        if relation == "contradictory"
    ]
    if conflicts:
        return "conflicting", tuple(conflicts)
    strong = {"exact", "normalized"}
    if artist.relation in strong and event.relation in strong and date.relation == "exact" and date.precision == "day":
        return "corroborated", ("artist, event and performance date agree; metadata alone never authorizes a link",)
    positive = {"exact", "normalized", "fuzzy"}
    if artist.relation in positive and (event.relation in positive or date.relation in {"exact", "compatible_alternative", "kind_mismatch"}):
        return "plausible", ("artist agrees with partial or uncertain corroboration",)
    return "insufficient", ("identity fields are missing or uncorroborated",)


def _subject_facts(subject: AssessedSubject) -> tuple[Fact, Fact, Fact]:
    source = subject.source
    if isinstance(source, Snapshot):
        return source.artist, source.event, source.date
    # A discovery display name is a retrieval guess, never inherited as known metadata.
    return Fact(), Fact(), Fact()


def _scope(scope: Completeness | None) -> Completeness:
    return scope or Completeness(state="incomplete", reason="candidate scope not stated; absence of rivals is not established")


def assess_recording_match(
    recording: RecordingContext,
    subject: AssessedSubject,
    *,
    recording_date_kind: DateKind = "unknown",
    scope: Completeness | None = None,
) -> MatchAssessment:
    artist, event, date = _subject_facts(subject)
    artist_evidence = _compare_field(recording.artist, artist)
    event_evidence = _compare_field(recording.event, event)
    date_evidence = _compare_date(recording.date, date, recording_date_kind, subject.date_kind)
    band, reasons = _band(artist_evidence, event_evidence, date_evidence)
    recording_seconds, subject_seconds = recording.duration_seconds, subject.duration_seconds
    difference = abs(recording_seconds - subject_seconds) if recording_seconds is not None and subject_seconds is not None else None
    return MatchAssessment(
        identity=subject.identity,
        recording_id=recording.recording_id,
        artist=artist_evidence,
        event=event_evidence,
        date=date_evidence,
        duration=DurationEvidence(recording_seconds=recording_seconds, subject_seconds=subject_seconds, difference_seconds=difference),
        band=band,
        reasons=reasons,
        scope=_scope(scope),
    )


def _evidence_key(assessment: MatchAssessment) -> tuple[int, ...]:
    rank = _RELATION_ORDER
    return (
        _BAND_ORDER[assessment.band],
        rank.get(assessment.artist.relation, 9),
        rank.get(assessment.event.relation, 9),
        rank.get(assessment.date.relation, 9),
    )


def assess_candidates(
    recording: RecordingContext,
    subjects: tuple[AssessedSubject, ...],
    *,
    recording_date_kind: DateKind = "unknown",
    scope: Completeness | None = None,
) -> AssessmentSet:
    """Order by explicit evidence only. Identity sorts equals for stable display; it never breaks a semantic tie.

    At most MAX_SUBJECTS are assessed; any excess is named in an incomplete scope rather than silently dropped.
    """
    stated = _scope(scope)
    if len(subjects) > MAX_SUBJECTS:
        reason = f"{len(subjects) - MAX_SUBJECTS} of {len(subjects)} subjects not assessed: subject cap {MAX_SUBJECTS} exceeded"
        stated = Completeness(state="incomplete", reason=f"{stated.reason}; {reason}" if stated.state == "incomplete" else reason)
        subjects = subjects[:MAX_SUBJECTS]
    keyed = [
        (_evidence_key(a), a) for a in (assess_recording_match(recording, s, recording_date_kind=recording_date_kind, scope=stated) for s in subjects)
    ]
    groups: dict[tuple[int, ...], list[MatchAssessment]] = {}
    for key, assessment in keyed:
        groups.setdefault(key, []).append(assessment)
    ranked: list[MatchAssessment] = []
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda a: (a.identity.provider_id, a.identity.native_id))
        ranked.extend(m.model_copy(update={"tied_with": tuple(o.identity for o in members if o is not m)}) for m in members)
    return AssessmentSet(ranked=tuple(ranked), scope=stated)
