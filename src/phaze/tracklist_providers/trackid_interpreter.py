"""Pure interpretation of a TrackId audiostream detail document into a source-neutral Snapshot.

No network, clock or storage access: the caller supplies the already-read JSON text in a
``SourceRead``. Contract points measured against the live API (see the remote-providers epic):

* Tracks live at ``detectionProcesses[].detectionProcessMusicTracks[]``. A reprocessed stream has several
  detection processes whose rows are disjoint parts of one tracklist (the list ``trackCount`` is their SUM, see
  docs/spikes/phaze-rjsdy-trackid-revision-signal.md), so rows are merged from ALL processes, never taken from one.
* ``startTime``/``endTime`` are TimeSpans of mixed precision (``00:02:43`` and ``00:00:00.2700000``).
* The detail ``trackCount`` is unreliable (0 beside populated rows); only rows actually read count.
* Offsets are relative to the platform stream, never to a local file, so they are never qualified.
"""

from fractions import Fraction
import json
import re
from typing import Any

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
)


PARSER_VERSION = "trackid-detail-v2"
SOURCE_FORMAT = "trackid_audiostream_detail_json"
STATUS_READY = 3
MAX_TRACKS = 4096  # the Snapshot ceiling
_EVIDENCE_VALUE_LIMIT = 64

# [d.]hh:mm:ss[.fffffff] -- the .NET TimeSpan constant format.
_TIMESPAN = re.compile(r"^(?:(?P<days>\d+)\.)?(?P<h>\d{1,2}):(?P<m>[0-5]\d):(?P<s>[0-5]\d)(?:\.(?P<frac>\d{1,9}))?$")
_ORIGIN_NOTE = "TrackId offset is relative to the platform stream, not the local recording; usable only as approximate evidence"


def _parse_timespan(text: str) -> tuple[Fraction, bool] | None:
    """Return (seconds, has_fraction), or None when the text is not a TimeSpan."""
    match = _TIMESPAN.match(text.strip())
    if match is None:
        return None
    seconds = Fraction(int(match["days"] or 0) * 86400 + int(match["h"]) * 3600 + int(match["m"]) * 60 + int(match["s"]))
    if match["frac"] is not None:
        seconds += Fraction(int(match["frac"]), 10 ** len(match["frac"]))
    return seconds, match["frac"] is not None


def _timestamp(raw: object, origin: str) -> Timestamp | None:
    if raw is None:
        return None
    original = str(raw)
    parsed = _parse_timespan(original) if isinstance(raw, str) else None
    if parsed is None:
        return Timestamp(original=original[:4096], evidence=("TrackId startTime is not a recognised TimeSpan",))
    seconds, fractional = parsed
    return Timestamp(
        original=original,
        kind="offset",
        # "second" precision requires whole seconds, so any fractional text is "unknown".
        precision="unknown" if fractional else "second",
        origin=origin,
        offset=RationalOffset(numerator=seconds.numerator, denominator=seconds.denominator),
        offset_usability="approximate",
        evidence=(_ORIGIN_NOTE,),
    )


def _text(value: object) -> str | None:
    return value.strip()[:4096] or None if isinstance(value, str) else None


def _bounded(value: object) -> str:
    text = str(value)
    return text if len(text) <= _EVIDENCE_VALUE_LIMIT else f"{text[:_EVIDENCE_VALUE_LIMIT]}..."


def _process_id(process: dict[str, Any]) -> int:
    raw = process.get("id")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else -1


def _processes(document: dict[str, Any]) -> list[dict[str, Any]]:
    processes = document.get("detectionProcesses")
    return [p for p in processes if isinstance(p, dict)] if isinstance(processes, list) else []


def _start_seconds(raw: object) -> Fraction | None:
    parsed = _parse_timespan(raw) if isinstance(raw, str) else None
    return parsed[0] if parsed else None


def _repeat_key(row: dict[str, Any]) -> tuple[object, object, object] | None:
    """Identity of an exact repeat: same track id AND start AND end. A track id alone never identifies one."""
    track_id = row.get("musicTrackId")
    return None if track_id is None else (track_id, row.get("startTime"), row.get("endTime"))


class TrackIdInterpreter:
    """``SourceInterpreter`` for the TrackId audiostream detail document.

    Rows are taken in the API's list order within a process (an assumption: the API documents no ordering) and merged
    across processes by start time, ties broken by process id then row order. Rows whose start time is missing or
    unparseable sort after every timed row.
    """

    def interpret(self, candidate: ProviderCandidate, content: SourceRead, budget: LoadBudget) -> LoadOutcome:
        scope = content.scope
        try:
            document = json.loads(content.text or "")
        except ValueError:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_detail_json", scope=scope)
        if not isinstance(document, dict):
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_detail_json", scope=scope)

        origin = f"{candidate.identity.provider_id}:stream:{candidate.identity.native_id}"
        problems: list[str] = []
        reasons: list[str] = []
        status = document.get("status")
        if status != STATUS_READY or isinstance(status, bool):
            problems.append(f"stream status {status!r} is not Ready ({STATUS_READY})")
            reasons.append("Stream not Ready")

        processes = _processes(document)
        # (start, process id, row order, row): untimed rows sort last.
        pending: list[tuple[tuple[int, Fraction], int, int, dict[str, Any]]] = []
        for process in processes:
            pid = _process_id(process)
            if process.get("endDate") is None:
                problems.append(f"detection process {pid} is still in flight (null endDate)")
                reasons.append(f"Detection process {pid} is still in flight (null endDate)")
            raw_rows = process.get("detectionProcessMusicTracks")
            for order, row in enumerate(raw_rows if isinstance(raw_rows, list) else []):
                if not isinstance(row, dict):
                    problems.append("a track row was not an object and was skipped")
                    continue
                start = _start_seconds(row.get("startTime"))
                pending.append(((0, start) if start is not None else (1, Fraction(0)), pid, order, row))
        pending.sort(key=lambda item: item[:3])

        # Drop only an EXACT repeat found in a different process, and say so on the row that was kept.
        kept: list[tuple[int, dict[str, Any], list[int]]] = []
        seen: dict[tuple[object, object, object], int] = {}
        for _, pid, _, row in pending:
            key = _repeat_key(row)
            if key is not None and key in seen and kept[seen[key]][0] != pid:
                kept[seen[key]][2].append(pid)
                continue
            if key is not None:
                seen[key] = len(kept)
            kept.append((pid, row, []))

        cap = min(budget.max_tracks, MAX_TRACKS)
        if len(kept) > cap:
            problems.append(f"track cap {cap} reached; {len(kept) - cap} rows not read")
            reasons.append(f"Track cap {cap} reached")
        tracks: list[ProviderTrack] = []
        for pid, row, repeats in kept[:cap]:
            end = row.get("endTime")
            evidence = [f"detection_process={pid}"]
            evidence.extend(f"exact repeat from detection process {other} dropped" for other in dict.fromkeys(repeats))
            if end is not None:
                evidence.append(f"endTime={_bounded(end)}")
            tracks.append(
                ProviderTrack(
                    position=len(tracks) + 1,
                    artist=_text(row.get("artist")),
                    title=_text(row.get("title")),
                    label=_text(row.get("label")),
                    timestamp=_timestamp(row.get("startTime"), origin),
                    evidence=tuple(evidence),
                )
            )
        if not tracks:
            # No evidence the API ever reports an explicitly empty Ready stream, so absence of rows is not proof of absence.
            problems.append("no track rows were read")
            reasons.append("No usable track rows")

        provenance = list(content.evidence)
        if processes:
            latest = max(processes, key=_process_id)
            provenance.append(
                f"highest detection process id={_process_id(latest)} endDate={_bounded(latest.get('endDate'))} (revision evidence; unproven)"
            )

        complete = not problems
        completeness = Completeness(
            state="complete" if complete else "incomplete",
            reason="All detection-process track rows read" if complete else "; ".join(dict.fromkeys(reasons)) or "Unusable track rows",
            evidence=tuple(problems[:64]),
        )
        snapshot = Snapshot(
            identity=candidate.identity,
            retrieved_at=content.retrieved_at,
            url=candidate.url,
            tracks=tuple(tracks),
            source_format=SOURCE_FORMAT,
            parser_version=PARSER_VERSION,
            completeness=completeness,
            provenance=tuple(provenance[:64]),
        )
        return LoadOutcome(
            status=OutcomeStatus.FOUND if complete else OutcomeStatus.INCOMPLETE,
            code="parsed_trackid_detail" if complete else "partial_trackid_detail",
            scope=scope,
            snapshot=snapshot,
        )
