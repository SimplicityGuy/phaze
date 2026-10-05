"""phaze-tuy9m: the record page's extracted-metadata card.

Plain values, no DB and no client -- ``build_metadata_card`` takes a ``FileMetadata`` instance (or
``None``) and returns the card's whole render contract, so this runs in the fast lane alongside
``test_record_facts.py``. ``tests/shared/routers/test_record_page_layout.py`` owns the rendered
markup; these tests own the three states and the field formatting.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest

from phaze.models.metadata import FileMetadata
from phaze.services.record_metadata import MetadataField, build_metadata_card


def _metadata(**overrides: object) -> FileMetadata:
    kwargs: dict[str, object] = {"file_id": uuid.uuid4()}
    kwargs.update(overrides)
    return FileMetadata(**kwargs)  # type: ignore[arg-type]


def test_a_missing_row_is_the_missing_state() -> None:
    """No ``FileMetadata`` row at all -- the metadata stage never wrote one for this file."""
    card = build_metadata_card(None)

    assert card.status == "missing"
    assert card.fields == []
    assert card.error_message is None
    assert card.raw_tags_json is None


def test_a_failed_row_surfaces_its_stored_error_message() -> None:
    """A row carrying ``failed_at`` is the failed state, distinct from a merely-empty one."""
    card = build_metadata_card(_metadata(failed_at=datetime.now(UTC), error_message="no ID3 frames found"))

    assert card.status == "failed"
    assert card.error_message == "no ID3 frames found"
    assert card.fields == []


def test_a_failed_row_with_no_stored_message_still_reads_as_failed() -> None:
    """The failure marker alone is enough -- the template supplies the "no detail" wording."""
    card = build_metadata_card(_metadata(failed_at=datetime.now(UTC), error_message=None))

    assert card.status == "failed"
    assert card.error_message is None


def test_a_row_with_no_fields_at_all_is_ok_with_an_empty_field_list() -> None:
    """A row that exists but extracted nothing is an honest empty answer, not an error."""
    card = build_metadata_card(_metadata())

    assert card.status == "ok"
    assert card.fields == []
    assert card.raw_tags_json is None


def test_every_non_null_field_is_present_formatted_and_in_the_fixed_order() -> None:
    """The eight candidate fields render in one fixed order and only the non-null ones appear."""
    card = build_metadata_card(
        _metadata(
            artist="Artist",
            title="Title",
            album="Album",
            year=2024,
            genre="Techno",
            track_number=3,
            bitrate=320_000,
            duration=125.0,
        )
    )

    assert card.status == "ok"
    assert card.fields == [
        MetadataField(label="Artist", value="Artist"),
        MetadataField(label="Title", value="Title"),
        MetadataField(label="Album", value="Album"),
        MetadataField(label="Year", value="2024"),
        MetadataField(label="Genre", value="Techno"),
        MetadataField(label="Track #", value="3"),
        MetadataField(label="Bitrate", value="320 kbps"),
        MetadataField(label="Duration", value="2:05"),
    ]


@pytest.mark.parametrize(
    ("stored_bps", "shown"),
    [(128_000, "128 kbps"), (64_040, "64 kbps"), (127_999, "127 kbps"), (999, "0 kbps")],
)
def test_bitrate_is_stored_bps_and_shown_as_whole_kbps_rounded_down(stored_bps: int, shown: str) -> None:
    """phaze-3aia5: ``FileMetadata.bitrate`` is BITS per second, so 128000 must read "128 kbps", not "128000 kbps"."""
    card = build_metadata_card(_metadata(bitrate=stored_bps))

    assert card.fields == [MetadataField(label="Bitrate", value=shown)]


def test_a_half_populated_row_shows_only_the_fields_it_has() -> None:
    """One present field is more useful than a card padded out with placeholders."""
    card = build_metadata_card(_metadata(artist="Artist", track_number=0))

    # track_number=0 is a real, present value -- it must not be treated as falsy/absent.
    assert card.fields == [
        MetadataField(label="Artist", value="Artist"),
        MetadataField(label="Track #", value="0"),
    ]


def test_raw_tags_are_serialized_sorted_and_indented_once_here() -> None:
    """One place formats the JSON text, so a test (and the template) can pin it exactly."""
    card = build_metadata_card(_metadata(raw_tags={"TIT2": "Title", "TPE1": "Artist"}))

    assert card.raw_tags_json == '{\n  "TIT2": "Title",\n  "TPE1": "Artist"\n}'


def test_an_empty_raw_tags_dict_is_treated_as_absent() -> None:
    """An empty dict carries nothing worth folding open -- same as no raw tags at all."""
    card = build_metadata_card(_metadata(raw_tags={}))

    assert card.raw_tags_json is None
