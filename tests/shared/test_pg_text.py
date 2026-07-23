"""DB-free unit tests for `sanitize_pg_text` (Phase 81 security audit, T-81-03-04 / T-81-05-03 PG-invalid limb).

Bucket: ``shared``. Stdlib-only module, no Postgres needed here — the end-to-end proof that a
NUL-bearing error_message actually persists and clears the ledger lives in the router suites.

Background: both failure-writer threats are titled "PG-invalid free text (NUL/surrogates) **or**
oversized error", but only the oversized limb was originally mitigated (a `max_length=2000` bound).
NUL passes pydantic validation — only lone surrogates are rejected there, as `string_unicode` — and
Postgres then rejects the write with `CharacterNotInRepertoireError`, aborting the transaction that
also clears the scheduling-ledger row.
"""

from __future__ import annotations

import pytest

from phaze.services.pg_text import contains_pg_invalid_chars, sanitize_pg_text


def test_strips_nul() -> None:
    assert sanitize_pg_text("bad\x00frame") == "badframe"


def test_strips_lone_surrogates() -> None:
    assert sanitize_pg_text("bad\ud800frame") == "badframe"
    assert sanitize_pg_text("\udfff") == ""


def test_preserves_other_control_chars_and_noncharacters() -> None:
    """Only NUL and lone surrogates are unstorable; stripping more would corrupt legitimate text."""
    for keep in ("\x01", "\x1f", "\x7f", "￾", "￿", "﷐", "\n", "\t"):
        assert sanitize_pg_text(f"a{keep}b") == f"a{keep}b", f"wrongly stripped {keep!r}"


def test_preserves_astral_characters() -> None:
    """Astral chars are single code points in a Python str, never surrogate pairs — must survive."""
    assert sanitize_pg_text("emoji \U0001f3b5 ok") == "emoji \U0001f3b5 ok"


def test_is_idempotent_and_noop_on_clean_text() -> None:
    clean = "boom: bad frame at 0x10"
    assert sanitize_pg_text(clean) == clean
    assert sanitize_pg_text(sanitize_pg_text("a\x00b")) == sanitize_pg_text("a\x00b")


def test_sanitize_can_only_shorten() -> None:
    """Order matters at the call sites: sanitize BEFORE truncating, since stripping never lengthens."""
    for s in ("", "abc", "a\x00b\ud800c", "\x00" * 10):
        assert len(sanitize_pg_text(s)) <= len(s)


@pytest.mark.parametrize("payload", ["\x00", "pre\x00post", "\x00\x00", "x\ud800\x00y"])
def test_output_is_encodable_to_utf8(payload: str) -> None:
    """The property Postgres actually requires: the result must encode to UTF-8 without error."""
    sanitize_pg_text(payload).encode("utf-8")


# phaze-jpji: pipeline_scans.py's trigger_scan needs to REJECT a NUL/surrogate-bearing path
# rather than silently strip it (a stripped NUL would point the scan at a different filesystem
# path than the operator typed), so it uses this detector alongside sanitize_pg_text.


def test_contains_pg_invalid_chars_detects_nul() -> None:
    assert contains_pg_invalid_chars("music\x00evil") is True


def test_contains_pg_invalid_chars_detects_lone_surrogate() -> None:
    assert contains_pg_invalid_chars("music\ud800evil") is True
    assert contains_pg_invalid_chars("\udfff") is True


def test_contains_pg_invalid_chars_false_on_clean_text() -> None:
    assert contains_pg_invalid_chars("") is False
    assert contains_pg_invalid_chars("/data/music/2026/set.flac") is False


def test_contains_pg_invalid_chars_false_on_other_control_chars_and_astral() -> None:
    """Only NUL and lone surrogates are unstorable; every other char must NOT trip the detector."""
    for keep in ("\x01", "\x1f", "\x7f", "\n", "\t", "\U0001f3b5"):
        assert contains_pg_invalid_chars(f"a{keep}b") is False, f"wrongly flagged {keep!r}"


def test_contains_pg_invalid_chars_agrees_with_sanitize_pg_text() -> None:
    """The detector and the stripper must agree on which strings are affected."""
    for s in ("clean", "a\x00b", "a\ud800b", "a\U0001f3b5b", ""):
        assert contains_pg_invalid_chars(s) == (sanitize_pg_text(s) != s)
