"""Explicit synthetic reviewed timing, never a production legacy inference path."""

from fractions import Fraction
import uuid

from phaze.services.cue_generator import parse_timestamp_string
from phaze.tracklist_providers.domain import RationalOffset, Timestamp


def qualified_timing(raw: str | None, media_id: uuid.UUID, *, seconds: Fraction | None = None) -> dict | None:
    if raw is None:
        return None
    parsed = parse_timestamp_string(raw)
    if seconds is None:
        if parsed is None:
            return None
        seconds = Fraction(str(parsed))
    return Timestamp(
        original=raw,
        kind="offset",
        precision="second" if seconds.denominator == 1 else "cue_frame_75",
        origin=f"recording:{media_id}",
        offset=RationalOffset(numerator=seconds.numerator, denominator=seconds.denominator),
        offset_usability="qualified",
        evidence=("synthetic explicit recording-relative review",),
    ).model_dump(mode="json")
