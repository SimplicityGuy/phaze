"""The record page's extracted-metadata card (``phaze-tuy9m``).

The file detail page has always loaded the file's ``FileMetadata`` row -- ``routers/record.py``
reads it to answer the metadata stage's pass/fail pill and to feed the tracklist index a fallback
duration -- but never handed the row itself to a template, so the tags mutagen actually extracted
(artist, title, album, year, genre, track number, bitrate, duration, the raw tag dict) never
appeared anywhere an operator could see them.

Built here for the same reason ``record_facts.py`` builds the sidebar facts in Python rather than
Jinja: which fields are present varies per file, "no fields at all" and "extraction failed" are
two different empty states that must not read alike, and a derivation written in the template is
one nothing can test. The template's job stays a loop over ``.fields`` plus one branch on
``.status``.

Named ``build_metadata_card`` / "Extracted metadata" rather than bare "Metadata": the record
content partial already has a "Metadata & identity" fold whose left column is about *proposed*
filename/tag changes (the Identify stage's output), not the tags already on the file. Reusing the
bare word for both would read as the same thing when they are not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import TYPE_CHECKING, Final, Literal

from phaze.services.analysis_timeline import format_elapsed_time


if TYPE_CHECKING:
    from phaze.models.metadata import FileMetadata


# The card's three mutually-exclusive states. "ok" covers both a fully-tagged file and one whose
# row exists but carries no non-null field (an honest "nothing was extracted", not an error) --
# the template tells those two apart by whether `.fields` is empty, not by a fourth status.
MetadataCardStatus = Literal["missing", "failed", "ok"]

# The label order the card renders fields in -- fixed, so the shape does not reshuffle with which
# fields happen to be present (the same reasoning `record_facts.ABSENT` documents for the sidebar).
_TRACK_LABEL: Final[str] = "Track #"


@dataclass(frozen=True)
class MetadataField:
    """One labelled, already-formatted row of the metadata card."""

    label: str
    value: str


@dataclass(frozen=True)
class MetadataCard:
    """The extracted-metadata card's whole render contract.

    ``raw_tags_json`` is pre-serialized (sorted, indented) rather than left as a dict for the
    template to ``tojson`` -- one place formats it, so a test can pin the exact text a collapsed
    ``<details>`` reveals rather than trusting the template filter's own escaping.
    """

    status: MetadataCardStatus
    error_message: str | None = None
    fields: list[MetadataField] = field(default_factory=list)
    raw_tags_json: str | None = None


def _formatted_fields(file_metadata: FileMetadata) -> list[MetadataField]:
    """Every non-null tag field, formatted for display, in the card's fixed order."""
    candidates: list[tuple[str, str | None]] = [
        ("Artist", file_metadata.artist),
        ("Title", file_metadata.title),
        ("Album", file_metadata.album),
        ("Year", str(file_metadata.year) if file_metadata.year is not None else None),
        ("Genre", file_metadata.genre),
        (_TRACK_LABEL, str(file_metadata.track_number) if file_metadata.track_number is not None else None),
        ("Bitrate", f"{file_metadata.bitrate} kbps" if file_metadata.bitrate is not None else None),
        ("Duration", format_elapsed_time(file_metadata.duration) if file_metadata.duration else None),
    ]
    return [MetadataField(label=label, value=value) for label, value in candidates if value]


def build_metadata_card(file_metadata: FileMetadata | None) -> MetadataCard:
    """Build the card's render contract from the file's (at most one) ``FileMetadata`` row.

    ``None`` -- the metadata stage never wrote a row for this file (not yet run, or an orphaned
    enqueue that never landed) -- is ``"missing"``. A row carrying ``failed_at`` is ``"failed"``,
    surfacing the SAME stored ``error_message`` the stage-eligibility pill already reads
    (``routers/record.py``'s ``stage_failure_reasons["metadata"]``) so the two never disagree about
    why. Anything else is ``"ok"``, whether or not any individual field came back non-null.
    """
    if file_metadata is None:
        return MetadataCard(status="missing")
    if file_metadata.failed_at is not None:
        return MetadataCard(status="failed", error_message=file_metadata.error_message)
    raw_tags_json = json.dumps(file_metadata.raw_tags, indent=2, sort_keys=True) if file_metadata.raw_tags else None
    return MetadataCard(status="ok", fields=_formatted_fields(file_metadata), raw_tags_json=raw_tags_json)


__all__ = [
    "MetadataCard",
    "MetadataCardStatus",
    "MetadataField",
    "build_metadata_card",
]
