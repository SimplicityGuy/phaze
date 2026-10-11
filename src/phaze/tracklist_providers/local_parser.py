"""Pure, linear, bounded local syntax interpretation; no target resolution or effects."""

from dataclasses import dataclass, field
import hashlib
import re

from phaze.tracklist_providers import local_text
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
    TrackSection,
)
from phaze.tracklist_providers.local_syntax import LABELED, cue_line, note_line


PARSER_VERSION = "local-tracklist-v4"
PARSER_VERSIONS = frozenset({"local-tracklist-v1", "local-tracklist-v2", "local-tracklist-v3", PARSER_VERSION})
_DIRECTIVE = re.compile(r"^([A-Za-z]+)\s+(.+)$")
_QUOTED = re.compile(r'^"([^"\n]*)"$')
_FILE = re.compile(r'^(?:"([^"\n]+)"|(\S+))\s+(WAVE|MP3|AIFF|BINARY|MOTOROLA)$', re.IGNORECASE)
_TRACK = re.compile(r"^(\d{1,4})\s+AUDIO$", re.IGNORECASE)
_INDEX = re.compile(r"^(\d{2})\s+(\d{1,6}):(\d{2}):(\d{2})$")
_TIME = re.compile(r"^(?:(clock)\s+)?(\d{1,6}):(\d{2})(?::(\d{2}))?$", re.IGNORECASE)
_MINUTE = re.compile(r"^(\d{1,6})\s*min$", re.IGNORECASE)
_ANNOTATION = re.compile(r"(?:\[label:\s*([^\[\]]+)\]|\(remix:\s*([^()]+)\)|(\(mashup\)))$", re.IGNORECASE)
_SEPARATOR = re.compile(r"\s[-\u2013\u2014]\s")
_HEADER = re.compile(
    r"^(?:artist|album|title|date|genre|label|venue|event|year|quality|bitrate|encoder|length|tracklist|tracks)\s*:\s*.*$", re.IGNORECASE
)
_UNKNOWN = frozenset({"id", "unknown", "unidentified", "?", "???"})


def _bounded_evidence(values: list[str]) -> tuple[str, ...]:
    return tuple(value[:4096] for value in values[:64])


def _known(value: str | None) -> str | None:
    return None if value is None or value.strip().casefold() in _UNKNOWN else value.strip() or None


def _cue_value(value: str) -> str | None:
    """CUE strings may be quoted, or a single token; malformed quoting stays rejected."""
    quoted = _QUOTED.fullmatch(value)
    if quoted:
        return quoted.group(1)
    return value if re.fullmatch(r'[^\s"]+', value) else None


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
    section_ordinal: int | None = None
    fields: set[str] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)


@dataclass
class _Parse:
    rows: list[_Row] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    artist: str | None = None
    artist_evidence: list[str] = field(default_factory=list)
    sections: list[TrackSection] = field(default_factory=list)
    global_grammar_uncertain: bool = False

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
        line = cue_line(line)
        if not line:
            continue
        directive = _DIRECTIVE.fullmatch(line)
        if directive is None:
            result.reject(line_number, "malformed_directive")
            continue
        keyword, value = directive.group(1).upper(), directive.group(2)
        if keyword == "REM":
            # REM is documented uninterpreted comment/release evidence, not track syntax.
            continue
        if keyword in {"CATALOG", "CDTEXTFILE", "SONGWRITER", "ISRC", "FLAGS", "PREGAP", "POSTGAP", "INFO"}:
            valid = _cue_value(value) is not None
            if keyword == "FLAGS":
                valid = all(flag in {"DCP", "4CH", "PRE", "SCMS"} for flag in value.upper().split())
            elif keyword in {"PREGAP", "POSTGAP"}:
                gap = _INDEX.fullmatch("00 " + value)
                valid = gap is not None and int(gap[3]) < 60 and int(gap[4]) < 75
            if not valid:
                result.reject(line_number, f"malformed_{keyword}")
            elif keyword in {"ISRC", "FLAGS", "PREGAP", "POSTGAP"} and current is None:
                result.reject(line_number, f"orphan_{keyword}")
            elif current is not None:
                current.evidence.append(f"line:{line_number}:{keyword}:{value}")
            else:
                result.evidence.append(f"line:{line_number}:{keyword}:{value}")
            continue
        if keyword == "FILE":
            file_count += 1
            current = None
            file_position_offset = 0
            file_first_track = True
            match = _FILE.fullmatch(value)
            file_origin = f"cue:{origin_key}:file:{file_count}" if match else None
            file_name = (match.group(1) or match.group(2)) if match else None
            file_evidence = f"FILE:{file_count}:{file_name}" if match else None
            if match is None:
                result.reject(line_number, "unsupported_FILE")
            else:
                result.evidence.append(f"line:{line_number}:FILE:{file_count}:{file_name}")
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
            cue_value = _cue_value(value)
            if cue_value is None:
                result.reject(line_number, f"malformed_{keyword}")
                continue
            parsed = _known(cue_value)
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
        if current is None or match is None or int(match.group(3)) > 59:
            result.reject(line_number, "malformed_INDEX")
            continue
        index, minutes, seconds, frames = match.groups()
        current.evidence.append(f"line:{line_number}:INDEX{index}:{value.split()[-1]}")
        if int(frames) > 74:
            warning = f"line:{line_number}:invalid_INDEX{index}_frames:{frames}:expected_0_to_74"
            if len(result.warnings) < 64:
                result.warnings.append(warning)
            current.evidence.append(warning)
            if index == "01":
                if current.timestamp is not None:
                    result.reject(line_number, "conflicting_INDEX01")
                    current.timestamp = current.timestamp.model_copy(update={"offset_usability": "unusable"})
                else:
                    current.timestamp = Timestamp(
                        original=value.split()[-1],
                        kind="offset",
                        origin=current.origin,
                        evidence=(warning, "Invalid CUE frame value; no offset inferred or overflow normalized"),
                    )
            continue
        if index != "01":
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


@dataclass
class _TextSection:
    ordinal: int
    title: str | None
    line: int
    last_number: int | None = None
    zero_based: bool = False
    offset: int = 0
    row_count: int = 0
    declared: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _text_fields(row: _Row, body: str, result: _Parse, section: _TextSection, *, title_allowed: bool) -> bool:
    """Retain identifiable rows, with local warnings where field structure is unresolved."""
    if not body:
        result.reject(row.line, "empty_track_body")
        section.errors.append(f"line:{row.line}:empty_track_body")
        return True
    duration = local_text.DURATION.search(body)
    if duration:
        body = body[: duration.start()].rstrip()
        row.evidence.append(f"line:{row.line}:trailing_time:{duration[1]}:origin_unknown")
    for _annotation in range(3):
        annotation = _ANNOTATION.search(body)
        if annotation is None:
            break
        if annotation.group(1):
            if row.label is not None:
                result.reject(row.line, "duplicate_label")
            row.label = _known(annotation.group(1))
        elif annotation.group(2):
            if row.remix is not None:
                result.reject(row.line, "duplicate_remix")
            row.remix = _known(annotation.group(2))
        else:
            row.mashup = True
        body = body[: annotation.start()].strip()
    if _ANNOTATION.search(body):
        result.reject(row.line, "annotation_cap")
    columns = [column.strip() for column in re.split(r"\t+", body) if column.strip()]
    splits = local_text.separators(body)
    if len(columns) >= 2:
        row.artist, row.title = _known(columns[0]), _known(columns[1])
        if len(columns) > 2:
            section.warnings.append(f"line:{row.line}:additional_columns_unresolved")
            row.evidence.append("Additional columns retained in original text; meaning unresolved")
    elif len(splits) == 1:
        begin, end = splits[0]
        row.artist, row.title = _known(body[:begin]), _known(body[end:])
    elif splits:
        section.warnings.append(f"line:{row.line}:ambiguous_artist_title")
        row.evidence.append("Artist/title split unresolved; original line retained")
    elif local_text.UNKNOWN.match(body):
        row.evidence.append("Unknown identity annotation retained; no invented title")
    elif body.lower().startswith("title:"):
        row.title = _known(body[6:])
    elif title_allowed:
        row.title = _known(body)
        # Without separator whitespace, field attribution cannot be proven. Keep the row.
        if "-" in body:
            row.title = None
            section.warnings.append(f"line:{row.line}:unspaced_artist_title_unresolved")
            row.evidence.append("Unspaced artist/title structure unresolved; original line retained")
    else:
        result.reject(row.line, "unsupported_title_only_body")
        section.errors.append(f"line:{row.line}:unsupported_title_only_body")
        return False
    if not body:
        result.reject(row.line, "empty_track_body")
        section.errors.append(f"line:{row.line}:empty_track_body")
    return True


def _text(lines: list[str], budget: LoadBudget) -> _Parse:
    result = _Parse()
    section: _TextSection | None = None
    sections: list[_TextSection] = []
    explicit_heading = False
    pending_title: tuple[str, int] | None = None
    header_frame = False
    nonempty = [(i, note_line(raw)[0]) for i, raw in enumerate(lines) if note_line(raw)[0]]
    title_table = sum(bool(local_text.ORDERED.fullmatch(line) and local_text.DURATION.search(line)) for _, line in nonempty) >= 2
    numbered_track_context = any(
        (ordered_match := local_text.ORDERED.fullmatch(line)) is not None and local_text.separators(ordered_match[3]) for _, line in nonempty
    )
    ordered_count = sum(local_text.ORDERED.fullmatch(line) is not None for _, line in nonempty)

    def begin(title: str | None, line: int) -> _TextSection:
        if len(sections) >= min(budget.max_tracks, 4096):
            result.reject(line, "section_cap")
            return sections[-1]
        new = _TextSection(len(sections) + 1, title, line, offset=result.rows[-1].position if result.rows else 0)
        sections.append(new)
        return new

    for index, raw in enumerate(lines):
        line_number = index + 1
        line, _start = note_line(raw)
        if local_text.export_frame(lines, index):
            header_frame = True
            continue
        if not line or local_text.DECORATION.fullmatch(line):
            if raw.lstrip().startswith("\\"):
                header_frame = False
            continue
        if len(line) > 4096:
            result.reject(line_number, "line_field_cap")
            continue
        # Only typed export frames establish a header block; bars also frame track rows.
        if header_frame:
            result.evidence.append(f"line:{line_number}:excluded_header_block")
            continue
        if local_text.heading(line):
            explicit_heading = True
            if section is None or section.row_count or section.errors:
                title, at = pending_title if pending_title else (line, line_number)
                section = begin(title, at)
            pending_title = None
            continue
        if local_text.SECTION.fullmatch(line) or local_text.LINEUP.match(line):
            if section is not None and not section.row_count and not section.errors and not section.declared:
                sections.pop()
            section = begin(line, line_number)
            section.declared = True
            explicit_heading = True
            pending_title = None
            continue
        if local_text.ABSENT.match(line):
            if section is None or (pending_title and (section.row_count or section.errors)):
                section = begin(pending_title[0] if pending_title else None, pending_title[1] if pending_title else line_number)
            pending_title = None
            section.errors.append(f"line:{line_number}:no_tracklist_present")
            explicit_heading = False
            continue
        if local_text.DATE_HEADER.match(line) or (local_text.MEDIA_NAME.search(line) and not explicit_heading) or "://" in line:
            result.evidence.append(f"line:{line_number}:excluded_non_track_line")
            if local_text.DATE_HEADER.match(line):
                pending_title = (line, line_number)
            explicit_heading = False
            continue
        if (_HEADER.fullmatch(line) or LABELED.fullmatch(line)) and not local_text.TIMED.fullmatch(line):
            if line.endswith(":") and (label_match := LABELED.fullmatch(line)) is not None and not label_match["value"]:
                pending_title = (line.rstrip(":"), line_number)
            else:
                explicit_heading = False
            continue
        syntax = local_text.prefix(line)
        if syntax.number is not None and ordered_count == 1 and re.match(r"^\d+\s+\D", line) and local_text.adjacent_row(lines, index):
            syntax = local_text.TextRowSyntax(None, line)
        following = local_text.next_numbered(lines, index)
        if (
            syntax.number is None
            and syntax.time is None
            and following is not None
            and following.number in {0, 1}
            and (re.search(r"\b(?:live\s+broadcast|live\s+set)\b|\d{4}-\d{2}-\d{2}", line, re.IGNORECASE))
        ):
            pending_title = (line, line_number)
            explicit_heading = False
            continue
        body = syntax.body
        adjacent = local_text.adjacent_row(lines, index)
        numbered = syntax.number is not None
        timed = syntax.time is not None
        if re.match(r"^[A-Z]\d{1,2}[.)]\s", line):
            result.reject(line_number, "unsupported_track_number")
            if section:
                section.errors.append(f"line:{line_number}:unsupported_track_number")
            continue
        is_row = numbered or timed or (local_text.plain_row(line) and (explicit_heading or adjacent))
        if not is_row:
            if _SEPARATOR.search(line) and re.match(r"^[A-Z]\d{1,2}[.)]\s", line):
                result.reject(line_number, "unsupported_track_number")
                if section:
                    section.errors.append(f"line:{line_number}:unsupported_track_number")
            else:
                result.evidence.append(f"line:{line_number}:excluded_non_track_line")
                # A heading between independent numbered lists is a boundary only when
                # numbering restarts. Never infer a split from duplicate adjacent ordinals.
                if len(line) <= 512:
                    pending_title = (line, line_number)
            continue
        if "[" not in body and local_text.DECORATION.fullmatch(re.sub(r"\d", "", body)):
            continue
        if section is None:
            section = begin(pending_title[0] if pending_title else None, pending_title[1] if pending_title else line_number)
            pending_title = None
        number = syntax.number if syntax.number is not None else (section.last_number + 1 if section.last_number is not None else 1)
        if section.last_number is not None and number <= section.last_number and pending_title:
            section = begin(*pending_title)
            explicit_heading = True
            pending_title = None
        elif section.last_number is not None and number <= section.last_number:
            result.reject(line_number, "conflicting_numbering")
            section.errors.append(f"line:{line_number}:conflicting_numbering")
        if section.last_number is None:
            section.zero_based = number == 0
        section.last_number = number
        intrinsic = section.offset + number + int(section.zero_based)
        intrinsic = max(intrinsic, result.rows[-1].position + 1 if result.rows else 1)
        row = result.add(number, line_number, budget, position=intrinsic)
        if row is None:
            break
        row.section_ordinal = section.ordinal
        section.row_count += 1
        row.evidence.append(f"section:{section.ordinal}:declared_position:{number}")
        if syntax.time:
            original = f"{syntax.time} min" if syntax.minute else syntax.time
            row.timestamp = _text_timestamp(original, result, line_number)
        elif body.startswith("["):
            if "]" in body and re.fullmatch(r"[\d?]+", body[1 : body.index("]")]) and "?" in body[: body.index("]")]:
                original, body = body[1:].split("]", 1)
                row.timestamp = Timestamp(original=original, evidence=(f"line:{line_number}", "Unknown source minute cue; origin unknown"))
                section.warnings.append(f"line:{line_number}:unknown_minute_cue")
                body = body.strip()
            else:
                result.reject(line_number, "unclosed_timestamp")
                section.errors.append(f"line:{line_number}:unclosed_timestamp")
                continue
        supported = _text_fields(
            row, body, result, section, title_allowed=explicit_heading or title_table or timed or (numbered and numbered_track_context)
        )
        if not supported:
            result.rows.remove(row)
            section.row_count -= 1
    # Every non-cap rejection belongs to its enclosing section; callers cannot
    # accept a duplicate/unsupported row by selecting around the global status.
    for error in result.errors:
        error_line = int(error.split(":", 2)[1])
        enclosing = next((item for item in reversed(sections) if item.line <= error_line), None)
        if enclosing is not None and error not in enclosing.errors:
            enclosing.errors.append(error)
        elif enclosing is None:
            result.global_grammar_uncertain = True
    for item in sections:
        has_rows = item.row_count > 0
        errors = item.errors or ([] if has_rows else [f"line:{item.line}:no_tracklist_present"])
        result.sections.append(
            TrackSection(
                ordinal=item.ordinal,
                title=item.title,
                completeness=Completeness(
                    state="incomplete" if errors else "complete",
                    reason="Unsupported or absent section rows" if errors else "Section rows exhausted",
                    evidence=_bounded_evidence(errors),
                ),
                warnings=_bounded_evidence(item.warnings),
                evidence=(f"line:{item.line}:section_heading",),
            )
        )
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
            completeness=Completeness(
                state="incomplete", reason="Partial or unsupported source grammar", evidence=_bounded_evidence([*parsed.errors, *parsed.warnings])
            ),
            sections=tuple(parsed.sections),
            section_scan_complete=bool(parsed.sections)
            and not parsed.global_grammar_uncertain
            and not limited
            and not partial
            and not any(error.endswith(("cap", "partial_input")) for error in parsed.errors),
            provenance=_bounded_evidence([*content.evidence, *parsed.evidence]),
        )
        # Repeated inherited values can expand a small read into a large output. Serialize
        # each row once and reserve room for final cap evidence; never build an oversize DTO.
        size = len(base.model_dump_json().encode("utf-8"))
        tracks: list[ProviderTrack] = []
        for row in parsed.rows:
            track = ProviderTrack(
                position=row.position,
                section_ordinal=row.section_ordinal,
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
        complete = not parsed.errors and all(section.completeness.state == "complete" for section in parsed.sections)
        completeness = Completeness(
            state="complete" if complete else "incomplete",
            reason="Supported source grammar exhausted" if complete else "Partial or unsupported source grammar",
            evidence=_bounded_evidence([*parsed.errors, *parsed.warnings]),
        )
        snapshot = Snapshot.model_validate(
            base.model_dump(mode="python")
            | {
                "tracks": tuple(tracks),
                "completeness": completeness,
                "section_scan_complete": base.section_scan_complete and not any(error.endswith(("cap", "partial_input")) for error in parsed.errors),
            }
        )
        return LoadOutcome(
            status=OutcomeStatus.FOUND if complete else OutcomeStatus.INCOMPLETE,
            code="parsed_local_tracklist" if complete else "partial_local_tracklist",
            scope=content.scope,
            snapshot=snapshot,
        )
