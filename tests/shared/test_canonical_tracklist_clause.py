"""Guard: `propagated_from_set_key` is read through `Tracklist.is_canonical()` / `is_propagated()`,
never re-spelled by hand as `.is_(None)` / `.is_not(None)` (phaze-97pkq).

WHAT THIS COST, phaze-vtovq
----------------------------

`models/tracklist.py` documented ``CANONICAL_TRACKLIST_CLAUSE`` as spelled once because it is
"BOTH the partial-unique-index predicate AND the filter every ``WHERE external_id = ...`` read
must carry" -- but the constant was DDL-only text, unusable from a Python query, so every read
site re-spelled ``Tracklist.propagated_from_set_key.is_(None)`` by hand. One of those hand-spelled
copies (``tasks/tracklist.py``'s file-id resolution) ANDed the canonical filter directly onto a
file-id predicate where the row in scope was itself a propagated projection -- so the intersection
was always empty. ``refreshed`` stayed 0, no exception was raised, and the "Refresh from
1001Tracklists" button on every propagated file silently did nothing. Nothing but code review
enforced the rule, and code review missed it.

The fix (phaze-97pkq) gives the invariant a Python SEAM -- ``Tracklist.is_canonical()`` /
``Tracklist.is_propagated()``, next to ``CANONICAL_TRACKLIST_CLAUSE`` in ``models/tracklist.py``
-- and routes every SQL read through it. This guard is what makes that mechanical rather than a
promise: a new hand-spelled copy fails the build instead of waiting for the next vtovq.

WHAT MUST NOT BE FLAGGED
-------------------------

``services/tracklist_priority.py:262`` reads ``tracklist.propagated_from_set_key is not None`` --
a plain Python ``is not`` check on an already-loaded INSTANCE, not a SQL predicate built for a
query. Rewriting it as ``Tracklist.is_propagated()`` would not even type-check: that classmethod
returns a SQL expression (``ColumnElement[bool]``), not a ``bool``. The detector below is keyed on
the SQLAlchemy method-call SHAPE (``.is_(`` / ``.is_not(``) specifically so this line's plain
``is not`` operator can never match it, and ``test_an_instance_attribute_check_is_not_flagged``
pins that acquittal against the real file, not just a synthetic string.

DELIBERATELY A SOURCE SCAN
----------------------------

Modelled on ``tests/shared/test_cluster_wide_catalog_scoping.py``: a runtime assertion cannot
catch "the code took a different, wrong path to the same-looking result" the way a source scan
can, and the class of defect here -- a query that returns zero rows instead of raising -- passes
silently at every level except "did anyone re-derive the predicate by hand".

Reuses that module's two refinements rather than reinventing them:

* **Comments and docstrings are excluded from matching**, via the same ``tokenize``/``ast``
  approach, so a future docstring explaining this exact historical bug (quoting the bad shape as
  an example, the way this module's own docstring above does in SQL-shorthand) is not itself
  flagged as a violation.
* **The model file is excluded by path**, not by prose detection -- ``Tracklist.is_canonical()``
  and ``Tracklist.is_propagated()`` are themselves defined as ``cls.propagated_from_set_key.is_(...)``
  in ``models/tracklist.py``, which is the ONE place the hand-spelled form is correct: it is the
  seam itself, not a caller of it.

Unlike the cluster-wide guard, this one does not search for a NEARBY good token -- there is no
"scoped" variant of ``.is_(None)`` against this column, only the "route through the classmethod
instead" fix. So any occurrence of the SQL method-call shape outside the model file is a
violation, full stop; no window logic is needed.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import tokenize
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (REPO_ROOT / "tests", REPO_ROOT / "src")
_THIS_FILE = Path(__file__).resolve()
_MODEL_FILE = (REPO_ROOT / "src" / "phaze" / "models" / "tracklist.py").resolve()

# The two production modules that carried the four hand-spelled call sites this bead fixed.
_KNOWN_CALL_SITES = (
    "src/phaze/tasks/tracklist.py",
    "src/phaze/services/tracklist_drain.py",
)

# A hand-spelled SQL predicate against the column: `.is_(None)` / `.is_not(None)`, however the
# call is reached (`Tracklist.propagated_from_set_key.is_(...)`, `cls....is_not(...)`, an
# aliased/loaded instance, etc). The argument does not matter -- only that the SQLAlchemy
# method-call form is being invoked on the column directly instead of through the classmethod
# seam. Deliberately does NOT match a bare Python `is not None` / `is None` comparison, which is
# how an already-loaded row's own attribute is read (the acquitted shape below).
_CLAUSE_RE = re.compile(r"\bpropagated_from_set_key\s*\.\s*is_(?:not)?\s*\(")


def _sources(roots: Sequence[Path] | None = None) -> list[Path]:
    roots = roots if roots is not None else SCANNED_ROOTS
    return sorted(path for root in roots for path in root.rglob("*.py") if root.exists())


def _char_offsets(text: str) -> list[int]:
    """Cumulative character offset at the start of each 1-indexed source line."""
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _prose_ranges(text: str) -> list[tuple[int, int]]:
    """Character ranges that are commentary rather than executable code.

    Two shapes: ``#`` comments (via ``tokenize``), and bare string-expression statements --
    docstrings, and any other string literal not assigned or passed to anything (via ``ast``). A
    scan that treated these as real code would flag this module's own docstring, and any future
    docstring elsewhere that quotes the historical bug's exact shape as an example.

    Raises ``SyntaxError`` / ``tokenize.TokenizeError`` uncaught if ``text`` does not parse under
    this Python version -- a ``tests/`` or ``src/`` module this interpreter cannot parse is already
    a broken repository state, and silently downgrading to a weaker scan would hide that.
    """
    offsets = _char_offsets(text)

    def to_offset(row: int, col: int) -> int:
        return offsets[row - 1] + col

    ranges: list[tuple[int, int]] = []

    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            ranges.append((to_offset(*tok.start), to_offset(*tok.end)))

    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            assert node.end_lineno is not None
            assert node.end_col_offset is not None
            ranges.append((to_offset(node.lineno, node.col_offset), to_offset(node.end_lineno, node.end_col_offset)))

    return ranges


def _in_any_range(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


def _hand_spelled_occurrences(text: str) -> list[str]:
    """Return a description of each real (non-prose) hand-spelled occurrence in ``text``."""
    prose = _prose_ranges(text)
    offenders = []
    for match in _CLAUSE_RE.finditer(text):
        if _in_any_range(match.start(), prose):
            continue
        line_no = text.count("\n", 0, match.start()) + 1
        offenders.append(f"line {line_no}: {match.group(0)!r}")
    return offenders


def _hand_spelled_clause_files(roots: Sequence[Path] | None = None) -> list[str]:
    """Return files containing at least one real hand-spelled occurrence of the canonical clause.

    Excludes this guard's own module (its docstring and self-tests deliberately contain example
    snippets, both flagged and acquitted shapes, to pin the detector's behaviour) and the model
    file (the ONE place ``.is_(`` / ``.is_not(`` against this column is correct -- it is the
    classmethod seam's own implementation).
    """
    offenders = []
    for path in _sources(roots):
        resolved = path.resolve()
        if resolved in (_THIS_FILE, _MODEL_FILE):
            continue
        text = path.read_text(encoding="utf-8")
        if _hand_spelled_occurrences(text):
            try:
                label = str(path.relative_to(REPO_ROOT))
            except ValueError:
                label = str(path)
            offenders.append(label)
    return offenders


def test_no_module_spells_the_canonical_clause_by_hand() -> None:
    """Every read of ``propagated_from_set_key`` outside the model goes through the classmethod seam.

    Failure here means a new caller re-derived the canonical/propagated predicate by hand instead
    of calling ``Tracklist.is_canonical()`` / ``Tracklist.is_propagated()`` -- exactly the shape
    that produced phaze-vtovq. Fix it by calling the classmethod instead of the raw column method.
    """
    offenders = _hand_spelled_clause_files()
    assert offenders == [], (
        f"these modules hand-spell the canonical/propagated SQL predicate instead of calling "
        f"Tracklist.is_canonical() / Tracklist.is_propagated(): {offenders}"
    )


def test_the_scan_actually_reaches_the_known_call_sites() -> None:
    """The detector must be looking at the real files this bead fixed.

    Because the fix already replaced every hand-spelled occurrence in production code with
    ``Tracklist.is_canonical()`` / ``is_propagated()``, the regex can no longer be exercised
    against live production text -- an over-tight glob would therefore pass vacuously forever with
    no way to notice by re-running against the current tree. This pins two things instead: the
    file-traversal half (both modules that carried the four call sites are among the scanned
    sources), and the regex's recognition of the EXACT pre-fix shape each site used
    (``tasks/tracklist.py:137,145`` and ``services/tracklist_drain.py:693,740``), so a later
    "simplification" of the pattern cannot silently stop recognizing it.
    """
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _sources()}
    for expected in _KNOWN_CALL_SITES:
        assert expected in scanned, f"{expected} carried the hand-spelled clause but the scan does not reach it"

    pre_fix_shapes = (
        "Tracklist.propagated_from_set_key.is_(None)",  # tasks/tracklist.py:137,145; tracklist_drain.py:693
        "Tracklist.propagated_from_set_key.is_not(None)",  # tracklist_drain.py:740
    )
    for shape in pre_fix_shapes:
        assert _CLAUSE_RE.search(shape), f"the detector must still recognize the exact pre-fix shape: {shape!r}"


def test_an_instance_attribute_check_is_not_flagged() -> None:
    """``services/tracklist_priority.py:262`` must never be flagged or rewritten.

    ``tracklist.propagated_from_set_key is not None`` is a plain Python ``is not`` check on an
    already-loaded row, not a SQL predicate -- keying the detector on the SQLAlchemy method-call
    form (``.is_(`` / ``.is_not(``) is what keeps it out of scope. Checked at both levels: the
    regex against the exact real line, and the file-level scan against the real module on disk, so
    a future refactor of either the detector or the file cannot silently start flagging it.
    """
    real_line = "is_propagated=tracklist.propagated_from_set_key is not None,"
    assert _CLAUSE_RE.search(real_line) is None, "a plain `is not` check must not match the SQL-method regex"

    priority_module = REPO_ROOT / "src" / "phaze" / "services" / "tracklist_priority.py"
    text = priority_module.read_text(encoding="utf-8")
    assert "tracklist.propagated_from_set_key is not None" in text, (
        "the real acquittal case moved or was rewritten in services/tracklist_priority.py -- update this pin"
    )
    assert not _hand_spelled_occurrences(text), "services/tracklist_priority.py must be acquitted by the guard"


def test_a_prose_mention_of_the_hand_spelled_shape_is_not_flagged() -> None:
    """A docstring quoting the historical bug's exact code as an example must not self-flag.

    This module's own docstring does exactly that, in SQL-shorthand; this pins the underlying
    behaviour at the detector level with the precise Python-syntax shape, so the exclusion is
    proven rather than merely relied upon.
    """
    text = (
        '"""Before the fix, this call site read:\n'
        "\n"
        "    Tracklist.propagated_from_set_key.is_(None)\n"
        "\n"
        'which is exactly the bug phaze-vtovq describes.\n"""\n'
        "\n"
        "def real_code() -> None:\n"
        "    pass\n"
    )
    assert _hand_spelled_occurrences(text) == []


def test_a_fixture_module_spelling_the_clause_by_hand_fails_the_guard(tmp_path: Path) -> None:
    """The same regression, proven at the ``_hand_spelled_clause_files`` (whole-guard) level."""
    fixture = tmp_path / "fixture_hand_spelled_clause_module.py"
    fixture.write_text(
        "from phaze.models.tracklist import Tracklist\n\n\ndef bad_query() -> object:\n    return Tracklist.propagated_from_set_key.is_(None)\n",
        encoding="utf-8",
    )

    offenders = _hand_spelled_clause_files(roots=[tmp_path])

    assert offenders == [str(fixture)], f"the fixture's hand-spelled clause must be flagged, got: {offenders}"


def test_a_fixture_module_using_the_helper_passes_the_guard(tmp_path: Path) -> None:
    """The counterpart to the previous test: no false positive when the classmethod seam is used."""
    fixture = tmp_path / "fixture_helper_module.py"
    fixture.write_text(
        "from phaze.models.tracklist import Tracklist\n\n\ndef good_query() -> object:\n    return Tracklist.is_canonical()\n",
        encoding="utf-8",
    )

    assert _hand_spelled_clause_files(roots=[tmp_path]) == []
