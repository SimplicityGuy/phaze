"""Guard: ``docs/design/`` never carries two ADRs with the same leading four-digit number.

phaze-x2z38. The dispatcher seat that filed this bead observed the collision TWICE in two
days: phaze-kbue9's renumber of ``0004-tracklist-candidate-sets.md`` to
``0014-tracklist-candidate-sets.md`` existed specifically to resolve a duplicate 0004 (it
briefly collided with ``0004-ledger-replay-safety.md`` -- the 2026-08-19 documentation audit's
inventory row for the old path, at ``docs/documentation-audit-2026-08-19.md:1404``, records
both files as ``0004`` because that duplicate was real and current on that date). The very
next day a second, unrelated bead independently authored a new ``docs/design/0014-*.md`` off
a stale ``main`` and had to be redirected to ``0015`` before it landed. Two collisions in two
days from two different causes (a stale rename and a stale branch base) is a pattern a
one-line ``ls | uniq -d`` check would catch before either reached review.

This guard does not police numbering GAPS or ORDERING -- only that the leading four digits,
where present, are never shared by two files. A gap (skipping a number) or an out-of-order
addition is not a collision and is not this guard's concern.
"""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DIR = REPO_ROOT / "docs" / "design"

_LEADING_NUMBER = re.compile(r"^(\d{4})-.+\.md$")


def _leading_numbers() -> dict[str, list[str]]:
    """Map each leading four-digit ADR number to every filename that carries it."""
    by_number: dict[str, list[str]] = {}
    for entry in sorted(DESIGN_DIR.iterdir()):
        if not entry.is_file():
            continue
        match = _LEADING_NUMBER.match(entry.name)
        if match is None:
            continue
        by_number.setdefault(match.group(1), []).append(entry.name)
    return by_number


def test_docs_design_has_no_duplicate_leading_adr_numbers() -> None:
    by_number = _leading_numbers()
    duplicates = {number: names for number, names in by_number.items() if len(names) > 1}
    assert not duplicates, (
        f"docs/design/ has two or more ADRs sharing the same leading number -- rename one so every ADR keeps a unique number: {duplicates}"
    )


def test_docs_design_has_at_least_one_numbered_adr() -> None:
    """A guard that can never see a duplicate proves nothing (design section, phaze-x2z38)."""
    assert _leading_numbers(), "expected at least one numbered ADR under docs/design/ to guard"
