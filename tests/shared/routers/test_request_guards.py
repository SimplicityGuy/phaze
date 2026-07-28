"""Unit tests for the untrusted-input contract helpers (phaze-wkqk).

``src/phaze/routers/request_guards.py`` is THE contract every request path composes for parse and
shape guards. Rule 6 of that contract makes a documented "no HTTP 500" promise a test obligation, so
the helper's own guarantees are pinned here independently of any one router.
"""

import json
from typing import Any

from fastapi import HTTPException
import pytest

from phaze.routers.request_guards import MALFORMED_PAYLOAD_STATUS, MAX_ARRAY_ITEMS, parse_json_array_payload


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


def test_deeply_nested_input_raises_422_not_recursion_error() -> None:
    """phaze-xe5a: json.loads raises RecursionError (not JSONDecodeError) on deeply nested input --
    a sibling of ValueError/JSONDecodeError that escaped the guard's original except clause as an
    unhandled 500. `"[" * 2000` (the originally reported repro) only raises JSONDecodeError in this
    interpreter; 100_000 reliably crosses the recursion limit instead."""
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload("[" * 100_000, field="file_states")

    assert exc_info.value.status_code == MALFORMED_PAYLOAD_STATUS
    assert "file_states" in str(exc_info.value.detail)


def test_oversized_array_raises_422_not_500() -> None:
    """phaze-17ut: a well-formed but enormous array is an envelope failure too -- it must never
    reach ``undo_resolve``'s unchunked expanding-``IN`` DELETE, where it would blow asyncpg's
    32767-bind-parameter cap and 500. Passes an explicit small ``max_items`` so the test stays
    fast rather than building a 5000-entry payload just to exercise the branch."""
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload("[1, 2, 3, 4]", field="file_states", max_items=3)

    assert exc_info.value.status_code == MALFORMED_PAYLOAD_STATUS
    assert "file_states" in str(exc_info.value.detail)
    assert "too many items" in str(exc_info.value.detail)


def test_array_at_the_bound_is_accepted() -> None:
    """The bound is inclusive: exactly ``max_items`` entries must pass, only ``max_items + 1`` must
    reject -- an off-by-one here would either reject a legitimate maximal payload or admit one item
    past the documented DoS ceiling."""
    raw = json.dumps(list(range(5)))
    assert parse_json_array_payload(raw, field="file_states", max_items=5) == list(range(5))


def test_array_one_over_the_bound_is_rejected() -> None:
    raw = json.dumps(list(range(6)))
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload(raw, field="file_states", max_items=5)

    assert exc_info.value.status_code == MALFORMED_PAYLOAD_STATUS


def test_default_max_items_matches_the_documented_constant() -> None:
    """Guards against the default silently drifting from the documented DoS bound."""
    raw = json.dumps(list(range(MAX_ARRAY_ITEMS + 1)))
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload(raw, field="file_states")

    assert exc_info.value.status_code == MALFORMED_PAYLOAD_STATUS
    assert str(MAX_ARRAY_ITEMS) in str(exc_info.value.detail)


def test_huge_integer_literal_raises_422_not_500() -> None:
    """phaze-06zm: CPython's integer-string conversion limit (3.11+ DoS hardening) makes a huge
    integer literal anywhere in the payload raise a bare ``ValueError`` from ``json.loads`` --
    not a ``JSONDecodeError`` subclass, so it escaped both existing except arms as an unhandled 500.
    Same guard, same contract, different mechanism than the unparseable-JSON and oversized-array
    cases above."""
    huge_digit_string = "9" * 5000
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload(f"[{huge_digit_string}]", field="file_states")

    assert exc_info.value.status_code == MALFORMED_PAYLOAD_STATUS
    assert "file_states" in str(exc_info.value.detail)


def test_error_detail_names_the_offending_field() -> None:
    """A client posting several form fields must be able to tell which one it got wrong."""
    with pytest.raises(HTTPException) as exc_info:
        parse_json_array_payload("not-json", field="group_hashes")

    assert "group_hashes" in str(exc_info.value.detail)
