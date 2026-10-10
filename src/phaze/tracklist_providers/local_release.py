"""Bounded, effect-free release facts from supplied notes and CUE set headers."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re

from phaze.tracklist_providers.domain import Completeness, LoadBudget, OutcomeStatus, ReleaseFact, SourceRead


RELEASE_PARSER_VERSION = "local-release-v1"
_ALIASES = {
    "artist": "artist",
    "performer": "artist",
    "title": "title",
    "album": "album",
    "event": "event",
    "date": "date",
    "release date": "date",
    "year": "year",
    "venue": "venue",
    "location": "location",
    "label": "label",
    "catalog": "catalog",
    "catalogue": "catalog",
    "catalog number": "catalog",
    "cat no": "catalog",
    "genre": "genre",
    "source": "source",
    "ripper": "ripper",
    "encoder": "encoder",
    "quality": "quality",
    "bitrate": "bitrate",
    "bit rate": "bitrate",
    "duration": "duration",
    "length": "duration",
    "set type": "set_type",
}
_LABELED = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z ._-]{0,63}?)\s*[:=]\s*(?P<value>.*?)\s*$")
_CUE = re.compile(r"^\s*(?P<label>PERFORMER|TITLE|CATALOG|REM\s+[A-Za-z_]+)\s+(?P<value>.*?)\s*$", re.IGNORECASE)
_CUE_TRACK = re.compile(r"^\s*TRACK(?:\s|$)", re.IGNORECASE)
_BITRATE = re.compile(r"(?P<number>\d{1,9}(?:\.\d{1,6})?)\s*(?P<unit>bps|bit/s|kbps|kbit/s|mbps|mbit/s)", re.IGNORECASE)
_DURATION = re.compile(r"(?:(?P<hours>\d{1,6}):)?(?P<minutes>\d{1,3}):(?P<seconds>\d{2})")
_SECONDS = re.compile(r"(?P<number>\d{1,9}(?:\.\d{1,6})?)\s*(?:s|sec|seconds)", re.IGNORECASE)


@dataclass(frozen=True)
class ReleaseExtraction:
    """Facts reference the original SourceRead text; this result never replaces that text."""

    facts: tuple[ReleaseFact, ...]
    completeness: Completeness
    evidence: tuple[str, ...]


def _normalize(field: str, value: str) -> tuple[str | None, str | None, str]:
    """Normalize only explicit, supported values; certainty describes interpretation."""
    if field == "date":
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                return date.fromisoformat(value).isoformat(), None, "known"
            except ValueError:
                return None, None, "unknown"
        if re.fullmatch(r"\d{4}(?:-\d{2})?", value):
            year, _, month = value.partition("-")
            if 1 <= int(year) <= 9999 and (not month or 1 <= int(month) <= 12):
                return value, "year" if not month else "month", "inferred"
        return None, None, "unknown"
    if field == "year":
        return (value, "year", "known") if re.fullmatch(r"\d{4}", value) and int(value) > 0 else (None, None, "unknown")
    if field == "bitrate":
        match = _BITRATE.fullmatch(value)
        if match:
            multiplier = 1000000 if match["unit"].lower().startswith("m") else 1000 if match["unit"].lower().startswith("k") else 1
            number = Decimal(match["number"]) * multiplier
            if number > 0:
                return format(number.normalize(), "f"), "bit/s", "known"
        return None, None, "unknown"
    if field == "duration":
        match = _DURATION.fullmatch(value)
        if match and int(match["seconds"]) < 60 and (match["hours"] is None or int(match["minutes"]) < 60):
            seconds = int(match["hours"] or 0) * 3600 + int(match["minutes"]) * 60 + int(match["seconds"])
            return str(seconds), "s", "known"
        explicit = _SECONDS.fullmatch(value)
        if explicit:
            return format(Decimal(explicit["number"]).normalize(), "f"), "s", "known"
        return None, None, "unknown"
    return value, None, "known"


def _fact(field: str, original: str, line_number: int, start: int, end: int, cue: bool) -> ReleaseFact:
    value = original.strip()
    malformed_quote = cue and '"' in value and not re.fullmatch(r'"[^"\r\n]*"', value)
    if cue and not malformed_quote and len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    normalized, unit, certainty = _normalize(field, value)
    if malformed_quote:
        normalized, unit, certainty = None, None, "unknown"
    return ReleaseFact.model_validate(
        {
            "field": field,
            "value": value or None,
            "original_value": original,
            "normalized_value": normalized if value else None,
            "unit": unit,
            "certainty": certainty if value else "unknown",
            "source_line": line_number,
            "evidence": (
                f"span:characters:{start}:{end}",
                "scope:cue-set-header" if cue else "scope:labeled-release-note",
                *(["syntax:malformed-quote"] if malformed_quote else []),
            ),
        }
    )


def _mark_conflicts(facts: list[ReleaseFact]) -> tuple[ReleaseFact, ...]:
    by_field: dict[str, set[str]] = {}
    for fact in facts:
        if fact.value is not None:
            by_field.setdefault(fact.field, set()).add(fact.normalized_value or fact.value)
    return tuple(fact.model_copy(update={"certainty": "conflicting"}) if len(by_field.get(fact.field, ())) > 1 else fact for fact in facts)


def extract_release_metadata(content: SourceRead, *, source_format: str, budget: LoadBudget | None = None) -> ReleaseExtraction:
    """Scan finite decoded text, preserving uncertainty and excluding CUE track-level fields.

    No disk, network, database, tag mutation or source selection occurs. Span offsets count
    Python characters in the unchanged input, including BOM and CRLF. Unrecognized content
    remains in SourceRead.text; evidence points to its lines without inventing facts.
    """
    content = SourceRead.model_validate(content)
    budget = budget or LoadBudget()
    facts: list[ReleaseFact] = []
    evidence: list[str] = []
    incomplete = content.truncated or content.status != OutcomeStatus.FOUND
    text = content.text or ""
    if content.text is None or content.status not in {OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE}:
        return ReleaseExtraction((), Completeness(state="incomplete", reason="Source read unavailable", evidence=(content.code,)), (content.code,))
    cue = source_format.lower().lstrip(".") == "cue"
    if source_format.lower().lstrip(".") not in {"txt", "nfo", "cue"}:
        return ReleaseExtraction(
            (), Completeness(state="incomplete", reason="Unsupported release syntax", evidence=("format:unsupported",)), ("format:unsupported",)
        )
    if content.revision_scope == "bounded":
        evidence.append("revision:bounded")
    if content.bytes_read > budget.max_bytes:
        incomplete = True
        evidence.append("limit:source-bytes")
    cap = min(budget.max_characters, 262144)
    limited = text[:cap]
    if len(text) > cap:
        incomplete = True
        evidence.append("limit:characters")
    encoded = limited.encode("utf-8")
    if len(encoded) > budget.max_bytes:
        limited = encoded[: budget.max_bytes].decode("utf-8", errors="ignore")
        incomplete = True
        evidence.append("limit:bytes")
    partial_tail = content.truncated or content.status == OutcomeStatus.INCOMPLETE or len(limited) < len(text)
    offset = 0
    track_scope = False
    lines = limited.splitlines(keepends=True)
    for index, raw in enumerate(lines, 1):
        if index > budget.max_lines:
            incomplete = True
            evidence.append("limit:lines")
            break
        line = raw.rstrip("\r\n")
        if index == 1 and line.startswith("\ufeff"):
            line = line[1:]
            line_offset = offset + 1
        else:
            line_offset = offset
        offset += len(raw)
        if partial_tail and index == len(lines) and not raw.endswith(("\n", "\r")):
            evidence.append(f"line:{index}:partial")
            break
        if len(line) > 4096:
            incomplete = True
            evidence.append(f"line:{index}:oversized")
            continue
        if not line.strip():
            continue
        if cue and _CUE_TRACK.match(line):
            track_scope = True
        if cue and track_scope:
            continue
        match = (_CUE if cue else _LABELED).fullmatch(line)
        if match is None:
            evidence.append(f"line:{index}:unrecognized")
            continue
        label = " ".join(match["label"].lower().replace("_", " ").split()).removeprefix("rem ").strip(".")
        field = _ALIASES.get(label)
        if field is None:
            evidence.append(f"line:{index}:unknown-field")
            continue
        if len(facts) == 256:
            incomplete = True
            evidence.append("limit:facts")
            break
        facts.append(_fact(field, match["value"], index, line_offset + match.start("value"), line_offset + match.end("value"), cue))
    if len(evidence) > 64:
        evidence = [*evidence[:63], "limit:evidence"]
        incomplete = True
    completeness = Completeness(
        state="incomplete" if incomplete else "complete",
        reason="Release metadata scan bounded" if incomplete else "Release metadata grammar scan exhausted",
        evidence=("release_metadata_only:no_track_enumeration", *evidence[:63]),
    )
    return ReleaseExtraction(_mark_conflicts(facts), completeness, tuple(evidence))
