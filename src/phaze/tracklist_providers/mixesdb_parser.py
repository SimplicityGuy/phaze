"""Pure, linear, bounded MixesDB wikitext interpretation; no effects, no target resolution.

Specified by docs/spikes/phaze-vwcsv-mixesdb-parsing-provenance.md. Every cost below is linear in the
bounded input: text is cut to the budget before it is split, each line is cut to ``_MAX_LINE`` before any
pattern sees it, and no pattern nests an unbounded quantifier. asyncio deadlines cannot preempt this code.
"""

from dataclasses import dataclass, field
import re

from phaze.tracklist_providers.domain import (
    Completeness,
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


PROVIDER_ID = "mixesdb"
PARSER_VERSION = "mixesdb-wikitext-v1"
SOURCE_FORMAT = "mixesdb-wikitext"
# Placeholder until the rights decision lands: an empty REST license object clears nothing.
RIGHTS_UNKNOWN: tuple[str, ...] = ("rights:unknown (rights decision pending; page URL is carried for attribution only)",)

_MAX_LINE = 4096
_RAW_EVIDENCE = 512
_POSITIONS_SHOWN = 32
_OUTPUT_CAP = 4 * 1024 * 1024 - 4096
_CUE_LIKE = re.compile(r"(?:clock\s+)?[0-9?]{1,6}(?::[0-9?]{1,6}){0,2}", re.IGNORECASE)
_CLOCK = re.compile(r"clock\s+(\d{1,2}):(\d{2})", re.IGNORECASE)
_MINUTES = re.compile(r"\d{1,3}")
_ELAPSED = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})")
_TWO_PART = re.compile(r"\d{1,6}:\d{1,6}")
_SEPARATOR = re.compile(r"\s[-\u2013\u2014]\s")
_PAREN = re.compile(r"\(([^()]{1,200})\)")
_REMIX = re.compile(r"\b(?:remix|edit|bootleg|rework|vip|mix|mashup)\b", re.IGNORECASE)
_UNKNOWN = frozenset({"id", "unknown", "unidentified", "?", "???"})


def mixesdb_revision(latest_id: int | str, latest_timestamp: str) -> str:
    """Opaque revision for ``SourceRead.revision`` from the REST page ``latest.id`` and ``latest.timestamp``."""
    return f"{latest_id}@{latest_timestamp}"


def _resolved(value: str) -> str | None:
    value = value.strip()
    return None if not value or value.casefold() in _UNKNOWN else value


def _unusable(original: str, *evidence: str, kind: str = "unknown", precision: str = "unknown") -> Timestamp:
    return Timestamp(original=original, kind=kind, precision=precision, evidence=evidence)  # type: ignore[arg-type]


@dataclass
class _Scan:
    rows: list[dict[str, object]] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    problems: dict[str, list[int]] = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    nested: list[int] = field(default_factory=list)
    clock_minute: int | None = None

    def problem(self, code: str, line: int) -> None:
        self.problems.setdefault(code, []).append(line)


def _positions(lines: list[int]) -> str:
    shown = ",".join(str(n) for n in lines[:_POSITIONS_SHOWN])
    return shown + (f" (+{len(lines) - _POSITIONS_SHOWN} more)" if len(lines) > _POSITIONS_SHOWN else "")


def _timestamp(raw: str, origin: str | None, scan: _Scan) -> Timestamp:
    """Qualify one bracketed cue on its own; neighbouring entries never contribute evidence."""
    original = raw.strip()
    clock = _CLOCK.fullmatch(original)
    if clock:
        hours, minutes = int(clock.group(1)), int(clock.group(2))
        if hours > 23 or minutes > 59:
            return _unusable(original, "clock_out_of_range")
        minute_of_day = hours * 60 + minutes
        wrapped = scan.clock_minute is not None and minute_of_day < scan.clock_minute
        scan.clock_minute = minute_of_day
        evidence = ("explicit_clock_marker", "midnight_wrap") if wrapped else ("explicit_clock_marker",)
        return _unusable(original, *evidence, kind="clock", precision="minute")
    if "?" in original:
        return _unusable(original, "partial_or_unknown_value")
    if _MINUTES.fullmatch(original):
        if origin is None:
            return _unusable(original, "minute_origin_not_evidenced")
        return Timestamp(
            original=original,
            kind="offset",
            precision="minute",
            origin=origin,
            offset=RationalOffset(numerator=int(original) * 60, denominator=1),
            offset_usability="approximate",
            evidence=("bracketed_minute_cue", "supplied_origin_evidence"),
        )
    elapsed = _ELAPSED.fullmatch(original)
    if elapsed:
        hours, minutes, seconds = (int(part) for part in elapsed.groups())
        if minutes > 59 or seconds > 59:
            return _unusable(original, "elapsed_out_of_range")
        if origin is None:
            return _unusable(original, "elapsed_origin_not_evidenced")
        # No independent duration is available here, so a second-precision cue is never promoted to qualified.
        return Timestamp(
            original=original,
            kind="offset",
            precision="second",
            origin=origin,
            offset=RationalOffset(numerator=hours * 3600 + minutes * 60 + seconds, denominator=1),
            offset_usability="approximate",
            evidence=("hour_form_cue", "supplied_origin_evidence", "no_duration_cross_check"),
        )
    if _TWO_PART.fullmatch(original):
        return _unusable(original, "ambiguous_colon_tuple")
    return _unusable(original, "unrecognized_cue_shape")


def _split_cue(body: str) -> tuple[str | None, str]:
    if not body.startswith("["):
        return None, body
    end = body.find("]", 1, 40)
    if end < 0 or not _CUE_LIKE.fullmatch(body[1:end].strip()):
        return None, body
    return body[1:end], body[end + 1 :].strip()


def _split_label(text: str) -> tuple[str | None, str]:
    if not text.endswith("]"):
        return None, text
    start = text.rfind("[")
    inner = text[start + 1 : -1]
    if start < 0 or "]" in inner or "[" in inner or not inner.strip() or len(inner) > 200:
        return None, text
    return inner.strip(), text[:start].strip()


def _entry(body: str, number: int, overlong: bool, origin: str | None, scan: _Scan, heading: int) -> None:
    evidence = [f"line:{number}"]
    flawed = False
    cue, text = _split_cue(body)
    timestamp = _timestamp(cue, origin, scan) if cue is not None else None
    label, text = _split_label(text)
    artist = title = remix = None
    if overlong:
        scan.problem("line_too_long", number)
        flawed = True
    elif not text:
        scan.problem("empty_entry", number)
        flawed = True
    elif "{{" in text or "}}" in text:
        scan.problem("nested_template", number)
        flawed = True
    else:
        separator = _SEPARATOR.search(text)
        if separator:
            artist, title = _resolved(text[: separator.start()]), _resolved(text[separator.end() :])
        elif text.casefold() not in _UNKNOWN:
            scan.problem("missing_separator", number)
            flawed = True
        for match in _PAREN.finditer(text):
            if _REMIX.search(match.group(1)):
                remix = match.group(1).strip()
                break
    if artist is None or title is None:
        evidence.append(f"source_text:{text[:_RAW_EVIDENCE]}")
    scan.rows.append(
        {
            "artist": artist,
            "title": title,
            "timestamp": timestamp,
            "label": label,
            "remix_info": remix,
            "is_mashup": " w/ " in text or "mashup" in text.casefold(),
            "evidence": tuple(evidence),
            "heading": heading,
            "flawed": flawed,
        }
    )


def _scan(lines: list[str], budget: LoadBudget, origin: str | None) -> _Scan:
    scan = _Scan()
    in_list = False
    list_opened = 0
    heading = -1
    for number, raw in enumerate(lines, 1):
        overlong = len(raw) > _MAX_LINE
        stripped = raw[:_MAX_LINE].strip()
        if len(stripped) > 2 and stripped.startswith("=") and stripped.endswith("="):
            title = stripped.strip("=").strip()
            if title:
                scan.headings.append(title)
                heading = len(scan.headings) - 1
                continue
        body: str | None = None
        if in_list:
            body = stripped
        elif stripped[:6].lower() == "<list>":
            in_list, list_opened, body = True, number, stripped[6:].strip()
        elif stripped.startswith("#"):
            if len(stripped) > 1 and stripped[1] in "#*:":
                scan.nested.append(number)
                continue
            body = stripped[1:].strip()
        if body is None:
            continue
        closed = in_list and body[-7:].lower() == "</list>"
        if closed:
            in_list, body = False, body[:-7].strip()
        if body or not (closed or in_list):
            if len(scan.rows) >= budget.max_tracks:
                scan.limits.append(f"max_tracks={budget.max_tracks}")
                break
            _entry(body, number, overlong, origin, scan, heading)
    if in_list:
        scan.problem("unterminated_list", list_opened)
    return scan


class MixesDbInterpreter:
    """Interpret one decoded MixesDB wikitext page into an incomplete-by-default ``Snapshot``.

    ``offset_origin`` is the caller's evidence that bracketed cues count elapsed time from the start of the
    mix. Without it every cue stays unusable; with it, cues are at most ``approximate`` and never ``qualified``.
    """

    def __init__(self, *, offset_origin: str | None = None, rights_evidence: tuple[str, ...] = RIGHTS_UNKNOWN) -> None:
        self.offset_origin = offset_origin
        self.rights_evidence = rights_evidence

    def interpret(self, candidate: ProviderCandidate, content: SourceRead, budget: LoadBudget) -> LoadOutcome:
        candidate = ProviderCandidate.model_validate(candidate)
        content = SourceRead.model_validate(content)
        budget = LoadBudget.model_validate(budget)
        if candidate.identity.provider_id != PROVIDER_ID:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="wrong_provider", scope=candidate.identity.native_id)
        if content.status == OutcomeStatus.FOUND and content.text is None:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="missing_decoded_text", scope=content.scope)
        if content.status not in {OutcomeStatus.FOUND, OutcomeStatus.INCOMPLETE} or content.text is None:
            return LoadOutcome(status=content.status, code=content.code, scope=content.scope, evidence=content.evidence)

        limits: list[str] = []
        if content.truncated:
            limits.append("source_read_truncated")
        if content.status == OutcomeStatus.INCOMPLETE:
            limits.append("source_read_incomplete")
        text = content.text
        cut = content.truncated or content.status == OutcomeStatus.INCOMPLETE
        if len(text) > budget.max_characters:
            text, cut = text[: budget.max_characters], True
            limits.append(f"max_characters={budget.max_characters}")
        encoded = text.encode("utf-8")
        if len(encoded) > budget.max_bytes or content.bytes_read > budget.max_bytes:
            text = encoded[: budget.max_bytes].decode("utf-8", errors="ignore")
            cut = True
            limits.append(f"max_bytes={budget.max_bytes}")
        lines = text.removeprefix("﻿").splitlines()
        # A cut last line is raw evidence, never a certain track.
        if cut and text and not text.endswith(("\n", "\r")):
            lines = lines[:-1]
        if len(lines) > budget.max_lines:
            lines = lines[: budget.max_lines]
            limits.append(f"max_lines={budget.max_lines}")

        scan = _scan(lines, budget, self.offset_origin)
        if scan.nested:
            return LoadOutcome(
                status=OutcomeStatus.UNSUPPORTED,
                code="unsupported_nesting",
                scope=content.scope,
                detail="Nested list entries are not supported",
                evidence=(f"unsupported_nesting: lines {_positions(scan.nested)}",),
            )
        limits.extend(scan.limits)
        return self._outcome(candidate, content, text, scan, limits)

    def _outcome(self, candidate: ProviderCandidate, content: SourceRead, text: str, scan: _Scan, limits: list[str]) -> LoadOutcome:
        implicit = bool(scan.headings) and any(row["heading"] == -1 for row in scan.rows)
        shift = 2 if implicit else 1  # section ordinal of heading index 0
        flawed_sections = {(-1 if row["heading"] == -1 else row["heading"]) for row in scan.rows if row["flawed"]}
        incomplete_all = bool(limits)

        def ordinal(heading: object) -> int | None:
            if not scan.headings:
                return None
            return 1 if heading == -1 else int(str(heading)) + shift

        problem_evidence = tuple(f"{code}: lines {_positions(lines)}" for code, lines in scan.problems.items())
        evidence = (*problem_evidence, *(f"limit:{limit}" for limit in limits))
        complete = bool(scan.rows) and not evidence
        if complete:
            reason = "Supported source grammar exhausted"
        elif limits:
            reason = "Truncated at " + ", ".join(limits)
        elif not scan.rows:
            reason = "No recognized track lines"
        else:
            reason = "Partial or unsupported source grammar"

        sections = tuple(
            TrackSection(
                ordinal=index,
                title=None if implicit and index == 1 else scan.headings[index - shift][:4096],
                completeness=Completeness(
                    state="incomplete" if incomplete_all or (-1 if implicit and index == 1 else index - shift) in flawed_sections else "complete",
                    reason="Section scanned",
                ),
            )
            for index in range(1, len(scan.headings) + shift if implicit else len(scan.headings) + 1)
        )
        tracks: list[ProviderTrack] = []
        base_provenance = (
            *content.evidence,
            *((f"page_url:{candidate.url}",) if candidate.url else ()),
            f"native_key:{candidate.identity.native_id}",
        )
        snapshot_args: dict[str, object] = {
            "identity": candidate.identity,
            "revision": content.revision,
            "revision_scope": content.revision_scope,
            "retrieved_at": content.retrieved_at,
            "url": candidate.url,
            "text": text,
            "encoding": content.encoding,
            "source_format": SOURCE_FORMAT,
            "parser_version": PARSER_VERSION,
            "provenance": base_provenance[:64],
            "rights_evidence": self.rights_evidence,
            "sections": sections,
            "section_scan_complete": bool(sections) and not incomplete_all,
        }
        size = len(text.encode("utf-8")) * 2 + 16384
        for position, row in enumerate(scan.rows, 1):
            track = ProviderTrack(position=position, section_ordinal=ordinal(row["heading"]), **{k: row[k] for k in _TRACK_FIELDS})  # type: ignore[arg-type]
            size += len(track.model_dump_json().encode("utf-8")) + 1
            if size > _OUTPUT_CAP:
                limits.append("snapshot_output_cap")
                evidence = (*evidence, "limit:snapshot_output_cap")
                complete, reason = False, "Truncated at " + ", ".join(limits)
                break
            tracks.append(track)
        snapshot = Snapshot(
            **snapshot_args,  # type: ignore[arg-type]
            tracks=tuple(tracks),
            completeness=Completeness(state="complete" if complete else "incomplete", reason=reason, evidence=evidence[:64]),
        )
        if complete:
            return LoadOutcome(status=OutcomeStatus.FOUND, code="parsed_mixesdb_wikitext", scope=content.scope, snapshot=snapshot)
        code = "mixesdb_truncated" if limits else "unrecognized_mixesdb_page" if not scan.rows else "partial_mixesdb_wikitext"
        return LoadOutcome(status=OutcomeStatus.INCOMPLETE, code=code, scope=content.scope, snapshot=snapshot)


_TRACK_FIELDS = ("artist", "title", "timestamp", "label", "remix_info", "is_mashup", "evidence")
