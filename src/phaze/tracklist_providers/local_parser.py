"""Pure, linear, bounded local syntax interpretation; no target resolution or effects."""

from dataclasses import dataclass, field
import hashlib
import re

from phaze.tracklist_providers.domain import (
    Completeness,
    Fact,
    LoadBudget,
    LoadOutcome,
    OutcomeStatus,
    ProviderCandidate,
    ProviderTrack,
    RationalOffset,
    Snapshot,
    SourceRead,
    Timestamp,
)


PARSER_VERSION = "local-tracklist-v1"
_DIRECTIVE = re.compile(r"^([A-Za-z]+)\s+(.+)$")
_QUOTED = re.compile(r'^"([^"\n]*)"$')
_FILE = re.compile(r'^"([^"\n]+)"\s+(WAVE|MP3|AIFF)$', re.IGNORECASE)
_TRACK = re.compile(r"^(\d{1,4})\s+AUDIO$", re.IGNORECASE)
_INDEX = re.compile(r"^(00|01)\s+(\d{1,6}):(\d{2}):(\d{2})$")
_ORDERED = re.compile(r"^(\d{1,4})[.)]\s+(.+)$")
_TIME = re.compile(r"^(?:(clock)\s+)?(\d{1,6}):(\d{2})(?::(\d{2}))?$", re.IGNORECASE)
_MINUTE = re.compile(r"^(\d{1,6})\s*min$", re.IGNORECASE)
_ANNOTATION = re.compile(r"(?:\[label:\s*([^\[\]]+)\]|\(remix:\s*([^()]+)\)|(\(mashup\)))$", re.IGNORECASE)
_SEPARATOR = re.compile(r"\s[-\u2013\u2014]\s")
_HEADER = re.compile(
    r"^(?:artist|album|title|date|genre|label|venue|event|year|quality|bitrate|encoder|length|tracklist|tracks)\s*:\s*.*$", re.IGNORECASE
)
_DECORATION = re.compile(r"^[\s\-=+*_~|.:/\\\[\]─-▟]+$")
_UNKNOWN = frozenset({"id", "unknown", "unidentified", "?", "???"})


def _bounded_evidence(values: list[str]) -> tuple[str, ...]:
    return tuple(value[:4096] for value in values[:64])


def _known(value: str | None) -> str | None:
    return None if value is None or value.strip().casefold() in _UNKNOWN else value.strip() or None


@dataclass
class _Row:
    number: int
    line: int
    position: int
    artist: str | None = None
    title: str | None = None
    timestamp: Timestamp | None = None
    label: str | None = None
    remix: str | None = None
    mashup: bool = False
    origin: str | None = None
    fields: set[str] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)


@dataclass
class _Parse:
    rows: list[_Row] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    artist: str | None = None
    artist_evidence: list[str] = field(default_factory=list)

    def reject(self, line: int, reason: str) -> None:
        if len(self.errors) < 64:
            self.errors.append(f"line:{line}:{reason}")
        elif reason.endswith("cap") or reason == "partial_input":
            self.errors[-1] = f"line:{line}:{reason}"

    def add(self, number: int, line: int, budget: LoadBudget, *, position: int | None = None) -> _Row | None:
        if len(self.rows) >= budget.max_tracks:
            self.reject(line, "track_cap")
            return None
        previous = self.rows[-1].position if self.rows else 0
        position = number if position is None else position
        if position <= previous:
            self.reject(line, "conflicting_numbering")
            position = previous + 1
        row = _Row(number=number, line=line, position=position, evidence=[f"line:{line}", f"declared_position:{number}"])
        self.rows.append(row)
        return row


def _cue(lines: list[str], budget: LoadBudget, origin_key: str) -> _Parse:
    result = _Parse()
    current: _Row | None = None
    file_origin: str | None = None
    file_count = 0
    seen_track = False
    set_fields: set[str] = set()
    file_evidence: str | None = None
    file_position_offset = 0
    file_first_track = True
    offsets: dict[str, int] = {}
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        if len(line) > 4096:
            result.reject(line_number, "line_field_cap")
            continue
        directive = _DIRECTIVE.fullmatch(line)
        if directive is None:
            result.reject(line_number, "malformed_directive")
            continue
        keyword, value = directive.group(1).upper(), directive.group(2)
        if keyword == "REM":
            # REM is documented uninterpreted comment/release evidence, not track syntax.
            continue
        if keyword == "FILE":
            file_count += 1
            current = None
            file_position_offset = 0
            file_first_track = True
            match = _FILE.fullmatch(value)
            file_origin = f"cue:{origin_key}:file:{file_count}" if match else None
            file_evidence = f"FILE:{file_count}:{match.group(1)}" if match else None
            if match is None:
                result.reject(line_number, "unsupported_FILE")
            else:
                result.evidence.append(f"line:{line_number}:FILE:{file_count}:{match.group(1)}")
            continue
        if keyword == "TRACK":
            seen_track = True
            match = _TRACK.fullmatch(value)
            current = None
            if match:
                number = int(match.group(1))
                previous = result.rows[-1].position if result.rows else 0
                if file_first_track and file_count > 1 and 0 < number <= previous:
                    file_position_offset = previous
                file_first_track = False
                current = result.add(number, line_number, budget, position=file_position_offset + number)
            if match is None:
                result.reject(line_number, "unsupported_TRACK")
            elif current is not None:
                current.origin = file_origin
                if file_evidence is not None:
                    current.evidence.append(file_evidence)
                if file_origin is None:
                    result.reject(line_number, "missing_FILE_origin")
            continue
        if keyword in {"PERFORMER", "TITLE"}:
            match = _QUOTED.fullmatch(value)
            if match is None:
                result.reject(line_number, f"malformed_{keyword}")
                continue
            parsed = _known(match.group(1))
            if current is not None:
                attribute = "artist" if keyword == "PERFORMER" else "title"
                if keyword in current.fields:
                    result.reject(line_number, f"duplicate_{keyword}")
                else:
                    setattr(current, attribute, parsed)
                    current.fields.add(keyword)
                    current.evidence.append(f"line:{line_number}:{keyword}")
            elif not seen_track:
                if keyword in set_fields:
                    result.reject(line_number, f"duplicate_set_{keyword}")
                    continue
                set_fields.add(keyword)
                if keyword == "PERFORMER":
                    result.artist = parsed
                    result.artist_evidence = [f"line:{line_number}:set_PERFORMER"]
            else:
                result.reject(line_number, f"orphan_{keyword}")
            continue
        if keyword != "INDEX":
            result.reject(line_number, f"unsupported_{keyword}")
            continue
        match = _INDEX.fullmatch(value)
        if current is None or match is None or int(match.group(3)) > 59 or int(match.group(4)) > 74:
            result.reject(line_number, "malformed_INDEX")
            continue
        index, minutes, seconds, frames = match.groups()
        current.evidence.append(f"line:{line_number}:INDEX{index}:{value.split()[-1]}")
        if index == "00":
            continue
        total = int(minutes) * 60 * 75 + int(seconds) * 75 + int(frames)
        if current.timestamp is not None:
            result.reject(line_number, "conflicting_INDEX01")
            current.timestamp = current.timestamp.model_copy(update={"offset_usability": "unusable"})
            continue
        usable = current.origin is not None
        if current.origin is not None:
            if total < offsets.get(current.origin, 0):
                usable = False
                result.reject(line_number, "nonmonotonic_INDEX01")
            offsets[current.origin] = max(offsets.get(current.origin, 0), total)
        current.timestamp = Timestamp(
            original=value.split()[-1],
            kind="offset",
            precision="cue_frame_75",
            origin=current.origin,
            offset=RationalOffset(numerator=total, denominator=75),
            offset_usability="qualified" if usable else "unusable",
            evidence=("CUE INDEX01 is relative to its FILE origin; target recording unresolved", f"line:{line_number}"),
        )
    for row in result.rows:
        if "PERFORMER" not in row.fields:
            row.artist = result.artist
            row.evidence.extend(result.artist_evidence)
    return result


def _text_timestamp(original: str, result: _Parse, line: int) -> Timestamp:
    match = _TIME.fullmatch(original)
    minute = _MINUTE.fullmatch(original)
    if match:
        clock, _first, second, third = match.groups()
        valid = int(second) <= 59 and (third is None or int(third) <= 59)
        if not valid:
            result.reject(line, "malformed_timestamp")
        return Timestamp(
            original=original,
            kind="clock" if clock else "unknown",
            precision="second" if valid else "unknown",
            evidence=(f"line:{line}", "No demonstrated recording-relative origin"),
        )
    if minute:
        return Timestamp(original=original, precision="minute", evidence=(f"line:{line}", "Minute source precision; origin unknown"))
    result.reject(line, "unsupported_timestamp")
    return Timestamp(original=original, evidence=(f"line:{line}",))


def _text(lines: list[str], budget: LoadBudget) -> _Parse:
    result = _Parse()
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or _DECORATION.fullmatch(line) or _HEADER.fullmatch(line):
            continue
        if len(line) > 4096:
            result.reject(line_number, "line_field_cap")
            continue
        match = _ORDERED.fullmatch(line)
        if match is None:
            result.reject(line_number, "excluded_non_grammar_line")
            continue
        row = result.add(int(match.group(1)), line_number, budget)
        if row is None:
            continue
        body = match.group(2)
        if body.startswith("[") and "]" in body:
            time, body = body[1:].split("]", 1)
            row.timestamp = _text_timestamp(time, result, line_number)
            body = body.strip()
        elif body.startswith("["):
            result.reject(line_number, "unclosed_timestamp")
            continue
        # At most three supported suffix annotations: finite scans, even for adversarial input.
        for _annotation in range(3):
            annotation = _ANNOTATION.search(body)
            if annotation is None:
                break
            if annotation.group(1):
                if row.label is not None:
                    result.reject(line_number, "duplicate_label")
                row.label = _known(annotation.group(1))
            elif annotation.group(2):
                if row.remix is not None:
                    result.reject(line_number, "duplicate_remix")
                row.remix = _known(annotation.group(2))
            else:
                row.mashup = True
            body = body[: annotation.start()].strip()
        if _ANNOTATION.search(body):
            result.reject(line_number, "annotation_cap")
        separators = list(_SEPARATOR.finditer(body))
        if len(separators) == 1:
            separator = separators[0]
            row.artist, row.title = _known(body[: separator.start()]), _known(body[separator.end() :])
        elif separators:
            result.reject(line_number, "ambiguous_artist_title")
            row.evidence.append("Artist/title split unresolved; original line retained")
        else:
            if body.lower().startswith("title:"):
                row.title = _known(body[6:])
            elif body.strip().casefold() in _UNKNOWN:
                row.title = None
            else:
                result.reject(line_number, "unsupported_title_only_body")
                result.rows.remove(row)
        if not body:
            result.reject(line_number, "empty_track_body")
    return result


class LocalSourceInterpreter:
    """Interpret supplied decoded text; registration and persistence remain explicit callers."""

    def interpret(self, candidate: ProviderCandidate, content: SourceRead, budget: LoadBudget) -> LoadOutcome:
        candidate = ProviderCandidate.model_validate(candidate)
        content = SourceRead.model_validate(content)
        budget = LoadBudget.model_validate(budget)
        if candidate.source is None:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="missing_source_reference", scope=candidate.identity.native_id)
        if content.status == OutcomeStatus.FOUND and content.text is None:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="missing_decoded_text", scope=content.scope)
        if content.status not in {OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE} or content.text is None:
            return LoadOutcome(status=content.status, code=content.code, scope=content.scope, evidence=content.evidence)
        source_format = candidate.source.format.lower().lstrip(".")
        if source_format not in {"cue", "txt", "nfo", "embedded", "comment", "comments", "description", "lyrics", "tracklist"}:
            return LoadOutcome(status=OutcomeStatus.UNSUPPORTED, code="unsupported_tracklist_format", scope=content.scope)
        text = content.text
        limited = len(text) > budget.max_characters or content.bytes_read > budget.max_bytes
        text = text[: budget.max_characters]
        encoded = text.encode("utf-8")
        if len(encoded) > budget.max_bytes:
            text = encoded[: budget.max_bytes].decode("utf-8", errors="ignore")
            limited = True
        partial = content.truncated or content.status == OutcomeStatus.INCOMPLETE or limited
        lines = text.removeprefix("\ufeff").splitlines()
        # A truncated last line is raw evidence, never certain metadata, even if it
        # happens to resemble a syntactically valid track or CUE directive prefix.
        if partial and text and not text.endswith(("\n", "\r")):
            lines = lines[:-1]
        limited |= len(lines) > budget.max_lines
        lines = lines[: budget.max_lines]
        origin_key = hashlib.sha256(candidate.identity.provider_id.encode() + b"\0" + candidate.identity.native_id.encode()).hexdigest()
        parsed = _cue(lines, budget, origin_key) if source_format == "cue" else _text(lines, budget)
        if content.truncated or content.status == OutcomeStatus.INCOMPLETE or limited:
            parsed.reject(0, "partial_input")
        if not parsed.rows:
            parsed.reject(0, "recognition_only_no_supported_tracks")
        base = Snapshot(
            identity=candidate.identity,
            revision=content.revision,
            revision_scope=content.revision_scope,
            retrieved_at=content.retrieved_at,
            text=text,
            encoding=content.encoding,
            source_format=source_format,
            parser_version=PARSER_VERSION,
            artist=Fact(value=parsed.artist, certainty="known" if parsed.artist else "unknown", evidence=_bounded_evidence(parsed.artist_evidence)),
            completeness=Completeness(state="incomplete", reason="Partial or unsupported source grammar", evidence=_bounded_evidence(parsed.errors)),
            provenance=_bounded_evidence([*content.evidence, *parsed.evidence]),
        )
        # Repeated inherited values can expand a small read into a large output. Serialize
        # each row once and reserve room for final cap evidence; never build an oversize DTO.
        size = len(base.model_dump_json().encode("utf-8"))
        tracks: list[ProviderTrack] = []
        for row in parsed.rows:
            track = ProviderTrack(
                position=row.position,
                artist=row.artist,
                title=row.title,
                timestamp=row.timestamp,
                label=row.label,
                remix_info=row.remix,
                is_mashup=row.mashup,
                evidence=_bounded_evidence(row.evidence),
            )
            size += len(track.model_dump_json().encode("utf-8")) + 1
            if size > 4 * 1024 * 1024 - 4096:
                parsed.reject(row.line, "snapshot_output_cap")
                break
            tracks.append(track)
        complete = not parsed.errors
        completeness = Completeness(
            state="complete" if complete else "incomplete",
            reason="Supported source grammar exhausted" if complete else "Partial or unsupported source grammar",
            evidence=_bounded_evidence(parsed.errors),
        )
        snapshot = Snapshot.model_validate(base.model_dump(mode="python") | {"tracks": tuple(tracks), "completeness": completeness})
        return LoadOutcome(
            status=OutcomeStatus.FOUND if complete else OutcomeStatus.INCOMPLETE,
            code="parsed_local_tracklist" if complete else "partial_local_tracklist",
            scope=content.scope,
            snapshot=snapshot,
        )
