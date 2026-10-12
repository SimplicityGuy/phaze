"""Pure interpretation of a TrackId audiostream detail document into a source-neutral Snapshot.

No network, clock or storage access: the caller supplies the already-read JSON text in a
``SourceRead``. Contract points measured against the live API (see the remote-providers epic):

* Tracks live at ``detectionProcesses[].detectionProcessMusicTracks[]``.
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


PARSER_VERSION = "trackid-detail-v1"
SOURCE_FORMAT = "trackid_audiostream_detail_json"
STATUS_READY = 3

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


def _process_id(process: dict[str, Any]) -> int:
    raw = process.get("id")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else -1


def _select_process(document: dict[str, Any]) -> dict[str, Any] | None:
    processes = document.get("detectionProcesses")
    candidates = [p for p in processes if isinstance(p, dict)] if isinstance(processes, list) else []
    # Highest id is the most recent detection run. max() keeps the first of equal ids.
    return max(candidates, key=_process_id) if candidates else None


class TrackIdInterpreter:
    """``SourceInterpreter`` for the TrackId audiostream detail document."""

    def interpret(self, candidate: ProviderCandidate, content: SourceRead, budget: LoadBudget) -> LoadOutcome:  # noqa: ARG002
        scope = content.scope
        try:
            document = json.loads(content.text or "")
        except ValueError:
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_detail_json", scope=scope)
        if not isinstance(document, dict):
            return LoadOutcome(status=OutcomeStatus.CONTRACT_ERROR, code="invalid_detail_json", scope=scope)

        origin = f"{candidate.identity.provider_id}:stream:{candidate.identity.native_id}"
        problems: list[str] = []
        status = document.get("status")
        if status != STATUS_READY or isinstance(status, bool):
            problems.append(f"stream status {status!r} is not Ready ({STATUS_READY})")

        process = _select_process(document)
        raw_rows = process.get("detectionProcessMusicTracks") if process else None
        rows = raw_rows if isinstance(raw_rows, list) else []

        tracks: list[ProviderTrack] = []
        for row in rows:
            if not isinstance(row, dict):
                problems.append("a track row was not an object and was skipped")
                continue
            end = row.get("endTime")
            tracks.append(
                ProviderTrack(
                    position=len(tracks) + 1,
                    artist=_text(row.get("artist")),
                    title=_text(row.get("title")),
                    label=_text(row.get("label")),
                    timestamp=_timestamp(row.get("startTime"), origin),
                    evidence=(f"endTime={end}",) if end is not None else (),
                )
            )
        if not tracks:
            # No evidence the API ever reports an explicitly empty Ready stream, so absence of rows is not proof of absence.
            problems.append("no track rows were read")

        complete = not problems
        completeness = Completeness(
            state="complete" if complete else "incomplete",
            reason="All detection-process track rows read" if complete else "Stream not Ready or no usable track rows",
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
            provenance=content.evidence,
        )
        return LoadOutcome(
            status=OutcomeStatus.FOUND if complete else OutcomeStatus.INCOMPLETE,
            code="parsed_trackid_detail" if complete else "partial_trackid_detail",
            scope=scope,
            snapshot=snapshot,
        )
