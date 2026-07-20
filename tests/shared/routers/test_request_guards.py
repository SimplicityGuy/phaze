"""Unit tests for the untrusted-input contract helpers (phaze-wkqk).

``src/phaze/routers/request_guards.py`` is THE contract every request path composes for parse and
shape guards. Rule 6 of that contract makes a documented "no HTTP 500" promise a test obligation, so
the helper's own guarantees are pinned here independently of any one router.
"""

from typing import Any

from fastapi import HTTPException
import pytest

from phaze.routers.request_guards import MALFORMED_PAYLOAD_STATUS, parse_json_array_payload


def test_malformed_payload_status_is_422() -> None:
    """Contract rule 1 picks ONE code for an envelope failure, and it is 422 (not 400)."""
    assert MALFORMED_PAYLOAD_STATUS == 422


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[]", []),
        ('[{"id": "x"}]', [{"id": "x"}]),
        ("[1, 2]", [1, 2]),  # elements are NOT validated here -- that is the consumer's job (rule 2)
        ("[null]", [None]),
    ],
)
def test_parse_json_array_payload_returns_the_decoded_array(raw: str, expected: list[Any]) -> None:
    """A well-formed array passes through untouched, elements included."""
    assert parse_json_array_payload(raw, field="file_states") == expected


@pytest.mark.parametrize("raw", ["not-json", "", "{", "[1,", "undefined"])
def test_unparseable_input_raises_422(raw: str) -> None:
    """Anything ``json.loads`` cannot decode is an envelope failure -> 422, never a bare JSONDecodeError."""
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload(raw, field="file_states")

    assert exc_info.value.status_code == MALFORMED_PAYLOAD_STATUS
    assert "file_states" in str(exc_info.value.detail)


@pytest.mark.parametrize("raw", ["{}", '{"id": "x"}', "null", "42", '"a string"', "true"])
def test_valid_json_of_the_wrong_container_raises_422(raw: str) -> None:
    """The crux of rule 2: parsing successfully is NOT receiving the expected structure."""
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload(raw, field="file_states")

    assert exc_info.value.status_code == MALFORMED_PAYLOAD_STATUS
    assert "must be a JSON array" in str(exc_info.value.detail)


def test_error_detail_names_the_offending_field() -> None:
    """A client posting several form fields must be able to tell which one it got wrong."""
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload("not-json", field="group_hashes")

    assert "group_hashes" in str(exc_info.value.detail)
