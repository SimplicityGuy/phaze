"""Regression tests for the `just test-db-for` seat-name normalization (phaze-fmfk).

``just test-db-for <name>`` used to interpolate the seat name straight into
``CREATE DATABASE phaze_<name>_test`` without validating or quoting it, so a hyphen -- which is
not valid in an unquoted Postgres identifier -- produced a raw ``syntax error at or near "-"``
with no hint at the cause. That is a real trap: this repo's own worktree convention IS hyphenated
(``wt/bead/issue/phaze-fq9h-1``), so naming a seat after the worktree is the obvious, natural
thing to do.

``scripts/derive-seat-name.sh`` is the single place that now decides what a raw seat name
normalizes to, and the ``test-db-for`` justfile recipe (and its Redis-index registry key) both
key off its output. These tests feed the *real* script over a subprocess (the same interface the
justfile recipe uses) rather than reimplementing its rules, so a change to the script's behavior
is caught here instead of silently drifting from what the recipe actually does.

Two properties matter as much as "hyphens no longer explode":

* **Idempotency** -- CLAUDE.md and the Redis logical-DB allocation registry both rely on the same
  raw seat name always resolving to the same derived identifier. The derivation uses a
  deterministic hash (sha256 of the raw name), not a random one, specifically to preserve this;
  :func:`test_derivation_is_idempotent_across_repeated_calls` pins it.
* **No hyphen/underscore/dot collision** -- normalizing ``-`` and ``.`` to ``_`` means
  ``my-seat``, ``my_seat`` and ``my.seat`` would otherwise collapse onto the identical identifier
  and silently share ONE Postgres database and ONE Redis logical DB -- the exact shared-database
  defect (phaze-fwo7) this whole mechanism exists to prevent. The derived name disambiguates them
  with a hash of the raw, pre-normalization name; :func:`test_hyphen_and_underscore_variants_do_not_collide`
  and :func:`test_hyphen_underscore_and_dot_variants_do_not_collide` pin it. Dots are accepted at
  all (phaze-robzi.4) because every beadhive epic-child bead id contains one (``phaze-o8sie.3``),
  and CLAUDE.md's own isolation instruction, ``just test-db-for <bead-id>``, requires the id to be
  usable verbatim; :func:`test_dotted_bead_id_succeeds_with_a_safe_identifier` pins it.
* **The 63-byte Postgres identifier limit** -- an unquoted identifier longer than 63 bytes
  (``NAMEDATALEN - 1``) is not rejected, it is SILENTLY TRUNCATED, so two long seat names that
  agree on their first 63 characters would otherwise land on one database with no error at all --
  the same collision the hash exists to prevent, reintroduced at the tail end, and made MORE
  likely by the 9 characters the disambiguating suffix itself adds. The script budgets the
  normalized part backwards from the longest generated name
  (``phaze_<name>_migrations_test``, 16 characters longer than ``phaze_<name>_test``) and
  truncates only that normalized part, never the hash, so the hash keeps carrying the uniqueness
  past the cut. :func:`test_generated_identifiers_stay_within_the_postgres_limit` and
  :func:`test_long_names_differing_only_past_the_cut_still_resolve_distinctly` pin this.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest


# tests/shared/test_seat_name_normalization.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "derive-seat-name.sh"

# Postgres unquoted identifiers: the derived name must be safe to splice straight into
# `CREATE DATABASE phaze_<name>_test` with no quoting. Letters, digits, underscore only.
_SAFE_UNQUOTED_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")

# Postgres unquoted identifiers are capped at NAMEDATALEN - 1 = 63 bytes; anything longer is
# SILENTLY TRUNCATED, not rejected. Mirrors the `phaze_${name}_test` / `phaze_${name}_migrations_test`
# shapes the `test-db-for` justfile recipe actually builds, so this test catches the recipe and
# the script drifting apart on the exact same defect the script exists to prevent.
_POSTGRES_IDENTIFIER_MAX = 63


def _full_db_names(derived: str) -> tuple[str, str]:
    """The two database identifiers `test-db-for` builds from a derived seat identifier."""
    return f"phaze_{derived}_test", f"phaze_{derived}_migrations_test"


def _derive(raw_name: str) -> subprocess.CompletedProcess[str]:
    """Run the real script on ``raw_name``, same interface the justfile recipe uses."""
    assert _SCRIPT.is_file(), f"seat-name derivation script missing: {_SCRIPT}"
    return subprocess.run(  # noqa: S603 - fixed, in-repo executable; input is a test literal
        [str(_SCRIPT), raw_name],
        capture_output=True,
        text=True,
        check=False,
    )


def _derive_ok(raw_name: str) -> str:
    result = _derive(raw_name)
    assert result.returncode == 0, f"expected success for {raw_name!r}, got stderr: {result.stderr}"
    derived = result.stdout.strip()
    assert derived, f"expected non-empty stdout for {raw_name!r}"
    return derived


# --- the original bug: a hyphenated name must succeed, never a raw Postgres syntax error -------


@pytest.mark.parametrize(
    "raw_name",
    [
        "review-polite",
        "phaze-fq9h-1",  # this repo's own hyphenated worktree-branch convention
        "laqf",  # a plain name with no hyphen must keep working unchanged in shape
        "a",  # single-letter, still valid
    ],
)
def test_hyphenated_name_succeeds_with_a_safe_identifier(raw_name: str) -> None:
    """A hyphenated seat name must derive a database-safe identifier, not error."""
    derived = _derive_ok(raw_name)
    assert _SAFE_UNQUOTED_IDENTIFIER.match(derived), f"{derived!r} derived from {raw_name!r} is not a safe unquoted Postgres identifier"
    assert "-" not in derived, f"derived identifier {derived!r} still contains a hyphen"


# --- phaze-robzi.4: a dotted bead id must succeed too, not just a hyphenated one -----------------


@pytest.mark.parametrize(
    "raw_name",
    [
        "phaze-o8sie.3",  # a real beadhive epic-child bead id, dot and all
        "phaze-o8sie.9",
        "a.b",  # minimal dotted case
    ],
)
def test_dotted_bead_id_succeeds_with_a_safe_identifier(raw_name: str) -> None:
    """Every beadhive epic-child bead id contains a dot, and CLAUDE.md's own isolation
    instruction is `just test-db-for <bead-id>` -- the id must be usable verbatim rather than
    forcing every seat to hand-hyphenate it (the observed cost: nine seats, two divergent
    improvisations, phaze-robzi.4).
    """
    derived = _derive_ok(raw_name)
    assert _SAFE_UNQUOTED_IDENTIFIER.match(derived), f"{derived!r} derived from {raw_name!r} is not a safe unquoted Postgres identifier"
    assert "." not in derived, f"derived identifier {derived!r} still contains a dot"


def test_dotted_name_derivation_is_idempotent_across_repeated_calls() -> None:
    """Criterion 3: re-running `test-db-for` for the same dotted bead id must reuse one seat."""
    first = _derive_ok("phaze-o8sie.3")
    second = _derive_ok("phaze-o8sie.3")
    assert first == second


# --- invalid input is rejected with a message stating the rule, not a raw parse error -----------


@pytest.mark.parametrize(
    "raw_name",
    [
        "Bad-Name",  # uppercase
        "bad!name",  # disallowed punctuation -- '.' is now accepted, but not arbitrary punctuation
        "1leading-digit",  # must start with a letter
        "bad name",  # whitespace
        "bad;drop table",  # a genuinely hostile name must be refused, not sanitized-and-run
        "",  # empty
    ],
)
def test_invalid_names_are_rejected_with_a_stated_rule(raw_name: str) -> None:
    result = _derive(raw_name)
    assert result.returncode != 0, f"expected {raw_name!r} to be rejected"
    assert not result.stdout.strip(), "a rejected name must not also print a derived identifier"
    # The failure must NAME the constraint -- a bare Postgres parse error is exactly what this
    # bead exists to eliminate.
    assert "must be lowercase" in result.stderr


# --- idempotency: the same raw name always derives the same identifier -------------------------


def test_derivation_is_idempotent_across_repeated_calls() -> None:
    """Re-running for the same raw name must yield the identical derived identifier.

    `just test-db-for` re-running for the same worktree is required to be idempotent (CLAUDE.md),
    and the Redis logical-DB allocation registry depends on it: a fresh value per call would mint
    a new Postgres database and burn a new Redis index (of a fixed-size pool) on every re-run.
    """
    first = _derive_ok("review-polite")
    second = _derive_ok("review-polite")
    third = _derive_ok("review-polite")
    assert first == second == third


# --- collision safety: hyphen vs underscore variants must NOT land on the same identifier -------


def test_hyphen_and_underscore_variants_do_not_collide() -> None:
    """'my-seat' and 'my_seat' normalize to the same text but must derive DIFFERENT identifiers.

    Collapsing '-' to '_' for the SQL identifier means these two raw names become textually
    identical after normalization. Without a disambiguator they would silently share one Postgres
    database and one Redis logical DB -- the exact shared-database defect (phaze-fwo7) this
    mechanism exists to prevent. The derived identifiers must differ, and both must still be safe,
    hyphen-free Postgres identifiers.
    """
    hyphenated = _derive_ok("my-seat")
    underscored = _derive_ok("my_seat")

    assert hyphenated != underscored, "hyphen and underscore variants of the same name must not collide onto one seat"
    for derived in (hyphenated, underscored):
        assert _SAFE_UNQUOTED_IDENTIFIER.match(derived)
    # Both must still be recognizable as belonging to 'my_seat' (readability: an agent copying the
    # printed exports should still see which seat name it asked for).
    assert hyphenated.startswith("my_seat_")
    assert underscored.startswith("my_seat_")


def test_hyphen_and_underscore_variants_are_each_individually_idempotent() -> None:
    """The collision-avoidance discriminator must not itself reintroduce nondeterminism."""
    assert _derive_ok("my-seat") == _derive_ok("my-seat")
    assert _derive_ok("my_seat") == _derive_ok("my_seat")


def test_hyphen_underscore_and_dot_variants_do_not_collide() -> None:
    """Criterion 2 (phaze-robzi.4): 'phaze-o8sie-3', 'phaze_o8sie_3' and 'phaze-o8sie.3' all
    normalize to the same text but must derive THREE DISTINCT identifiers, because the hash is
    taken over the raw, pre-normalization string. A fix that folded dots into the existing
    hyphen/underscore class WITHOUT keeping the original in the hash would reintroduce exactly
    the silent collision the hash exists to prevent -- and would do so specifically for the
    dotted form every beadhive epic-child bead id takes (`phaze-o8sie.3`).
    """
    hyphenated = _derive_ok("phaze-o8sie-3")
    underscored = _derive_ok("phaze_o8sie_3")
    dotted = _derive_ok("phaze-o8sie.3")

    derived = {hyphenated, underscored, dotted}
    assert len(derived) == 3, f"expected three distinct identifiers, got {derived}"
    for value in derived:
        assert _SAFE_UNQUOTED_IDENTIFIER.match(value)
        assert value.startswith("phaze_o8sie_3_"), value


# --- the 63-byte Postgres identifier limit: silently truncated, never rejected -------------------


def test_generated_identifiers_stay_within_the_postgres_limit() -> None:
    """Every database name `test-db-for` builds from a long seat name must be <= 63 characters.

    Postgres does not error on an over-long unquoted identifier -- it silently truncates it to 63
    bytes and carries on. An unbudgeted derivation would let two long, differently-named seats
    collide on an identical truncated database name with no error at all, which is exactly the
    collision the disambiguating hash exists to prevent, reintroduced at the tail end.
    """
    long_name = "a-very-long-worktree-seat-name-like-this-and-then-some-extra-padding-past-the-limit"
    derived = _derive_ok(long_name)

    main_db, migrations_db = _full_db_names(derived)
    assert len(main_db) <= _POSTGRES_IDENTIFIER_MAX, f"{main_db!r} is {len(main_db)} chars, over the {_POSTGRES_IDENTIFIER_MAX}-char Postgres limit"
    assert len(migrations_db) <= _POSTGRES_IDENTIFIER_MAX, (
        f"{migrations_db!r} is {len(migrations_db)} chars, over the {_POSTGRES_IDENTIFIER_MAX}-char Postgres limit "
        "-- this is the longer of the two names and overflows first"
    )
    assert _SAFE_UNQUOTED_IDENTIFIER.match(derived)


def test_long_names_differing_only_past_the_cut_still_resolve_distinctly() -> None:
    """Two long names that agree on everything up to the truncation point must NOT collide.

    Truncating the normalized part to fit the 63-byte budget means two seat names sharing a long
    common prefix can end up with an IDENTICAL truncated prefix. The hash (computed from the full,
    untruncated raw name, then never itself truncated) is what must keep them apart -- if the
    truncation ever ate into the hash instead of the normalized part, this test would catch the
    two names colliding onto one seat.
    """
    prefix = "a-very-long-worktree-seat-name-like-this-suffix-"
    first_raw = prefix + "one"
    second_raw = prefix + "two"

    first = _derive_ok(first_raw)
    second = _derive_ok(second_raw)

    assert first != second, f"{first_raw!r} and {second_raw!r} collided onto the same derived identifier {first!r}"
    for derived in (first, second):
        assert _SAFE_UNQUOTED_IDENTIFIER.match(derived)
        main_db, migrations_db = _full_db_names(derived)
        assert len(main_db) <= _POSTGRES_IDENTIFIER_MAX
        assert len(migrations_db) <= _POSTGRES_IDENTIFIER_MAX
