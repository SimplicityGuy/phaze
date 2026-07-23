"""DB-free unit tests for `escape_like` / `like_wildcard` (phaze-0dd2 / phaze-ba79 LIKE-escaping fix).

Bucket: ``shared``. Stdlib-only module, no Postgres needed here -- the end-to-end proof that the
escaped pattern actually matches (or fails to match) as intended lives in the
`services/test_proposal_queries.py` and `identify/services/test_search_queries.py` suites, which
run real ILIKE predicates against Postgres.
"""

from __future__ import annotations

from phaze.services.like_escape import LIKE_ESCAPE_CHAR, escape_like, like_wildcard


def test_escape_like_escapes_backslash_percent_underscore() -> None:
    assert escape_like("\\") == "\\\\"
    assert escape_like("%") == "\\%"
    assert escape_like("_") == "\\_"


def test_escape_like_backslash_escaped_before_percent_and_underscore() -> None:
    """Escaping order matters: backslash MUST run first, or the `\\` introduced by escaping `%`/`_`
    would itself be re-escaped, doubling up and corrupting the pattern."""
    assert escape_like("\\_") == "\\\\\\_"
    assert escape_like("\\%") == "\\\\\\%"


def test_escape_like_leaves_plain_text_untouched() -> None:
    assert escape_like("Coachella 2024") == "Coachella 2024"
    assert escape_like("") == ""


def test_escape_like_ac_dc_round_trips() -> None:
    """The phaze-ba79 scenario: an artist tag containing a literal backslash must survive escaping
    so it can match itself when re-wrapped in a wildcard pattern."""
    assert escape_like("AC\\DC") == "AC\\\\DC"


def test_like_wildcard_wraps_escaped_value() -> None:
    assert like_wildcard("set_live_2024") == "%set\\_live\\_2024%"
    assert like_wildcard("50%off") == "%50\\%off%"
    assert like_wildcard("AC\\DC") == "%AC\\\\DC%"


def test_like_escape_char_is_backslash() -> None:
    assert LIKE_ESCAPE_CHAR == "\\"
