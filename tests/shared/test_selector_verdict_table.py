"""Guard: every verdict ``scripts/select_impacted_tests.py`` can print must appear in CLAUDE.md's
selector table (phaze-eaf3y).

phaze-xveae landed the table CLAUDE.md now keys on the ``🎯 selector:`` line -- one row per arm of
``test-fast``'s ``case`` (``run``, ``docs``, ``escalate``, ``fail``, plus the fifth "not check-fast
at all" row). That table went stale once already: ``docs`` (phaze-fqfds) landed as a fourth verdict
without the then-two-row prose being updated, and nothing caught it until a seat hit the gap by hand.
This file is the mechanical guard phaze-xveae's own bead asked for so the same drift cannot recur
silently.

Both halves are parsed from their SOURCE rather than restated as a hardcoded list, same precedent as
``tests/shared/test_fast_gate.py`` and ``tests/shared/test_validation_gate_recipes.py``: a guard that
carries its own copy of "the four verdicts" only proves the copy agrees with itself, and would not
have caught the ``docs`` gap either. ``scripts/select_impacted_tests.py`` is authoritative (CLAUDE.md
says so at the line right after its own table): the verdict set comes from ``VERDICT_EXIT``'s keys
(``escalate``, ``fail``, ``docs`` -- each refused with its own exit code) union the literal word each
``print(f"...`` call in ``main()`` opens with (which is how ``run`` and ``docs`` reach a transcript --
``run`` has no ``VERDICT_EXIT`` entry because the success path exits 0 implicitly).
"""

from __future__ import annotations

import ast
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTOR_PATH = REPO_ROOT / "scripts" / "select_impacted_tests.py"
CLAUDE_MD_PATH = REPO_ROOT / "CLAUDE.md"

# The exact header CLAUDE.md's selector table opens with (`## CLAUDE.md ... #### Capturing a gate's
# status`). Matching on the full header rather than a fragment means a heading rename fails this
# file's own assertions loudly instead of silently reading the wrong table.
_TABLE_HEADER = "| selector line (`just check-fast`) | what ran | coverage line |"


def _selector_source_verdicts(source: str) -> set[str]:
    """Every verdict word ``select_impacted_tests.py`` can report, derived from its source text.

    Two independent signals, unioned so neither alone has to carry the whole claim:

    * ``VERDICT_EXIT``'s keys -- every verdict routed through ``Refuse`` (``escalate``, ``fail``)
      plus the docs short-circuit (``docs``), each mapped to its own dedicated exit code.
    * the literal word each ``print(f"...`` call in ``main()`` opens with. This is what actually
      reaches a transcript's ``🎯 selector:`` line, and it is the only place ``run`` (the success
      path, exit 0, no ``VERDICT_EXIT`` entry) is ever written down.
    """
    dict_match = re.search(r"VERDICT_EXIT\s*=\s*(\{[^}]*\})", source)
    assert dict_match, "select_impacted_tests.py no longer defines VERDICT_EXIT -- update this guard's parsing"
    verdict_exit: dict[str, int] = ast.literal_eval(dict_match.group(1))
    assert verdict_exit, "VERDICT_EXIT parsed empty -- the regex above matched the wrong span"

    printed = set(re.findall(r'print\(f"(\w+)[ "]', source))
    assert printed, "no print(f\"<word> ...) call matched -- the selector's verdict-printing shape changed"

    return set(verdict_exit) | printed


def _claude_md_selector_verdicts(text: str) -> set[str]:
    """Verdict tokens named by CLAUDE.md's selector table.

    Reads only the table body: from the header row through the blank line that ends it. Each row
    that documents a real verdict opens its first cell with a backtick-quoted word (`` `run ...` ``,
    `` `docs ...` ``, `` `escalate ...` ``, `` `fail ...` ``); the fifth row -- *no selector line*,
    for a transcript that is not ``check-fast`` at all -- opens with no backtick and correctly
    contributes nothing, since it is not a verdict the script prints.
    """
    assert _TABLE_HEADER in text, "CLAUDE.md no longer carries the selector table header this guard keys on"
    body = text.split(_TABLE_HEADER, 1)[1]
    body = body.split("\n\n", 1)[0]  # stop at the paragraph break right after the table
    return set(re.findall(r"^\|\s*`(\w+)", body, re.MULTILINE))


def test_the_selector_source_parser_finds_exactly_the_shipped_arms() -> None:
    """Pins the parser's own output against a known-good read of the shipped script.

    If this drifts, the failure is in the PARSER (a regex regression), not in the thing it guards --
    catching that here, separately from the cross-check below, keeps the two failure modes from
    being confused with each other.
    """
    assert _selector_source_verdicts(SELECTOR_PATH.read_text(encoding="utf-8")) == {"run", "docs", "escalate", "fail"}


def test_the_claude_md_table_parser_finds_exactly_the_documented_arms() -> None:
    """Same job as the test above, for the other half of the cross-check."""
    assert _claude_md_selector_verdicts(CLAUDE_MD_PATH.read_text(encoding="utf-8")) == {"run", "docs", "escalate", "fail"}


def test_every_selector_verdict_appears_in_claude_mds_table() -> None:
    """The guard this bead exists to add: a verdict the script can print but the table does not name.

    ``scripts/select_impacted_tests.py`` is the recipe's own selector and CLAUDE.md says so is
    authoritative ("the recipe is authoritative, and a selector verdict this table does not name is
    a shape it does not describe") -- so this checks selector-source verdicts against the table,
    never the reverse: the table is allowed to describe shapes (like "not check-fast at all") that
    are not selector verdicts at all.
    """
    selector_verdicts = _selector_source_verdicts(SELECTOR_PATH.read_text(encoding="utf-8"))
    claude_md_verdicts = _claude_md_selector_verdicts(CLAUDE_MD_PATH.read_text(encoding="utf-8"))

    missing = selector_verdicts - claude_md_verdicts
    assert not missing, (
        f"scripts/select_impacted_tests.py can report verdict(s) {sorted(missing)} that CLAUDE.md's "
        'selector table (the "Capturing a gate\'s status" section, the table keyed on the '
        "`\U0001f3af selector:` line) does not name. Add a row, or this table goes stale the same way "
        "it did for `docs` (phaze-xveae)."
    )


def test_the_guard_actually_discriminates_a_missing_arm() -> None:
    """Mutation proof (the bead's own acceptance criterion): confirm the guard goes RED on a gap.

    A guard that can only pass proves nothing about drift -- it would have been just as green before
    phaze-xveae closed the `docs` gap as after. This injects a fake fifth arm into a copy of the real
    selector source (never into CLAUDE.md, never into the shipped script) and asserts the SAME
    computation the test above runs would report it missing, which is what "the guard fails when a
    selector verdict exists in the recipe but not in the CLAUDE.md table" means in a suite that
    cannot literally run pytest against a broken CLAUDE.md as a sub-test.
    """
    real_source = SELECTOR_PATH.read_text(encoding="utf-8")
    mutated_source = real_source.replace(
        'VERDICT_EXIT = {"escalate": 3, "fail": 1, "docs": 4}',
        'VERDICT_EXIT = {"escalate": 3, "fail": 1, "docs": 4, "bogus": 5}',
    )
    assert mutated_source != real_source, (
        "the fixture edit found nothing to replace -- select_impacted_tests.py's VERDICT_EXIT literal "
        "no longer matches this test's fixture text, so the mutation below would silently do nothing"
    )

    claude_md_verdicts = _claude_md_selector_verdicts(CLAUDE_MD_PATH.read_text(encoding="utf-8"))
    mutated_verdicts = _selector_source_verdicts(mutated_source)

    missing = mutated_verdicts - claude_md_verdicts
    assert missing == {"bogus"}, (
        f"expected the injected 'bogus' arm to be the only verdict missing from CLAUDE.md's table, got "
        f"{missing} -- the guard's discriminator is not catching what this mutation was built to prove"
    )
