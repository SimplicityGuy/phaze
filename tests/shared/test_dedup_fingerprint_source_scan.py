"""AST source-scan guard (D-14 / READ-04): the FileState cutover in ``services/dedup.py`` and
``services/fingerprint.py`` cannot silently regress.

Standing insurance behind READ-04: the divergence + real-DB tests catch a wrong predicate at the sites
they exercise; THIS guard catches a ``FileRecord.state`` read reintroduced at a NEW site the behavioral
tests never touch.

Invariants enforced:

* ``services/dedup.py`` may reference ``FileState.DUPLICATE_RESOLVED`` ZERO times. Phase 90 (D-09, PR-B)
  removed the last dual-writer ``f.state = FileState.DUPLICATE_RESOLVED`` (the DedupResolution marker is now
  the sole authority), so neither a write NOR a read of that state may exist -- clean absence.
* ``services/fingerprint.py`` may reference ``FileState.FINGERPRINTED`` ZERO times (there is no writer of
  that state in that file -- clean absence).

Why this is an ``ast.walk`` scan and NEVER a line-oriented ``grep`` (Pitfall 1 / project memory
``feedback_mutation_test_guard_tests`` -- Phase 83 shipped two *toothless* guards):

1. ``FileState.DUPLICATE_RESOLVED`` is a single unbroken attribute chain, so a ``grep`` would
   FALSE-POSITIVE on the surviving dual-writer at ``dedup.py``.
2. ``fingerprint.py`` contains the token ``FINGERPRINTED`` inside a *docstring* (prose describing the old
   predicate). A line scan FALSE-POSITIVES on that docstring; an AST attribute scan does not see it.
3. The nine former read sites passed the clause POSITIONALLY (``.where(a, b, c)``), not as a chained
   ``.where(<Compare>)``. A rule keyed only on ``keyword.arg`` or on a chained comparator is BLIND to
   them -- precisely the blind spot that made Phase 83's guards toothless. This scan walks BOTH the
   positional ``Call.args`` list AND the ``Call.keywords`` list (regardless of ``keyword.arg is None``).

The negative tests below (``test_guard_flags_*`` / ``test_guard_allows_*`` / ``test_guard_ignores_*``)
are the mutation directions encoded permanently and hermetically -- they mutate crafted source STRINGS,
never the real files, so this test is DB-free and leaves no source dirty.
"""

from __future__ import annotations

import ast
from pathlib import Path


# This file is ``tests/shared/test_dedup_fingerprint_source_scan.py``; parents[2] is the repo root.
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "phaze"
_DEDUP = _SRC_ROOT / "services" / "dedup.py"
_FINGERPRINT = _SRC_ROOT / "services" / "fingerprint.py"

# SQLAlchemy read-clause entry points. A ``FileState.<member>`` inside ANY argument of one of these is a
# read of ``FileRecord.state`` (the exact thing the cutover removed).
_WHERE_FUNCS = frozenset({"where", "filter", "filter_by", "having"})


def _filestate_occurrences(tree: ast.AST, member: str) -> list[ast.Attribute]:
    """Every ``FileState.<member>`` attribute access in ``tree``.

    Matches ``ast.Attribute`` nodes whose ``.attr`` is ``member`` and whose ``.value`` resolves to the
    ``FileState`` name. A docstring/comment mention of ``member`` is NOT an ``ast.Attribute`` and is
    therefore invisible here -- the whole point of an AST scan over a line scan.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == member and isinstance(node.value, ast.Name) and node.value.id == "FileState"
    ]


def _is_state_write(occ: ast.AST, tree: ast.AST) -> bool:
    """True iff ``occ`` is (nested inside) the RHS of an assignment whose target ends in ``.state``.

    This is the ONE allowed occurrence: the surviving dual-writer ``f.state = FileState.DUPLICATE_RESOLVED``.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "state" for t in node.targets)
            and occ in ast.walk(node.value)
        ):
            return True
    return False


def _in_compare(occ: ast.AST, tree: ast.AST) -> bool:
    """True iff ``occ`` appears inside an ``ast.Compare`` (e.g. ``state != FileState.DUPLICATE_RESOLVED``)."""
    return any(isinstance(node, ast.Compare) and occ in ast.walk(node) for node in ast.walk(tree))


def _in_where_arg(occ: ast.AST, tree: ast.AST) -> bool:
    """True iff ``occ`` appears in ANY argument of a ``where``/``filter``/``filter_by``/``having`` call.

    Walks the positional ``Call.args`` list AND the ``Call.keywords`` list -- keying on neither
    ``keyword.arg`` nor a chained comparator, so it is not blind to positional ``.where(a, b, c)`` reads
    nor to keyword ``.filter_by(state=FileState.DUPLICATE_RESOLVED)`` reads. (The two Phase-83 blind spots.)
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _WHERE_FUNCS):
            continue
        for positional in node.args:  # ``.where(a, b, <read>)`` -- positional args, all of them.
            if occ in ast.walk(positional):
                return True
        for keyword in node.keywords:  # ``.filter_by(state=<read>)`` and ``.where(**splat)`` -- keyword args.
            if occ in ast.walk(keyword.value):
                return True
    return False


def _classify(source: str, member: str) -> tuple[list[ast.Attribute], list[ast.Attribute], list[ast.Attribute], list[ast.Attribute]]:
    """Classify every ``FileState.<member>`` occurrence in ``source`` into (all, writes, reads, other)."""
    tree = ast.parse(source)
    occurrences = _filestate_occurrences(tree, member)
    writes: list[ast.Attribute] = []
    reads: list[ast.Attribute] = []
    other: list[ast.Attribute] = []
    for occ in occurrences:
        if _in_compare(occ, tree) or _in_where_arg(occ, tree):
            reads.append(occ)  # a read context wins over anything else (conservative).
        elif _is_state_write(occ, tree):
            writes.append(occ)
        else:
            other.append(occ)
    return occurrences, writes, reads, other


# ---------------------------------------------------------------------------
# The real-source guard (the invariant this test exists to protect)
# ---------------------------------------------------------------------------


def test_dedup_has_zero_duplicate_resolved() -> None:
    """``dedup.py``: ZERO ``FileState.DUPLICATE_RESOLVED`` attribute accesses.

    Phase 90 (D-09, PR-B) removed the last ``f.state = FileState.DUPLICATE_RESOLVED`` dual-writer -- the
    DedupResolution marker (``dedup_resolved_clause``) is the sole authority. Neither a write nor a read of
    that state may exist (a reintroduced read was already forbidden by READ-04; a reintroduced write would
    resurrect the dead column dependency PR-C is about to drop).
    """
    occurrences, writes, reads, other = _classify(_DEDUP.read_text(encoding="utf-8"), "DUPLICATE_RESOLVED")

    assert len(reads) == 0, (
        f"services/dedup.py reintroduced a FileState.DUPLICATE_RESOLVED READ ({len(reads)} found) at "
        f"lines {[getattr(n, 'lineno', '?') for n in reads]}. The cutover replaced every read with "
        "~dedup_resolved_clause() (READ-04 / D-14)."
    )
    assert len(writes) == 0, (
        f"services/dedup.py reintroduced a FileState.DUPLICATE_RESOLVED WRITE ({len(writes)} found) at "
        f"lines {[getattr(n, 'lineno', '?') for n in writes]}. Phase 90 PR-B removed the last dual-writer; "
        "the DedupResolution marker is the sole authority."
    )
    assert len(other) == 0, (
        f"services/dedup.py has a FileState.DUPLICATE_RESOLVED occurrence in an unrecognised position at "
        f"lines {[getattr(n, 'lineno', '?') for n in other]}. It must be absent entirely."
    )
    # Belt-and-suspenders: total count == 0 (clean absence, mirroring fingerprint.py).
    assert len(occurrences) == 0


def test_fingerprint_has_zero_fingerprinted() -> None:
    """``fingerprint.py``: ZERO ``FileState.FINGERPRINTED`` attribute accesses (clean absence -- no writer).

    The ``FINGERPRINTED`` token in the module's docstring is prose, not an ``ast.Attribute``, so an AST
    scan never sees it (a line-oriented grep would false-positive here -- Pitfall 1).
    """
    occurrences, _writes, _reads, _other = _classify(_FINGERPRINT.read_text(encoding="utf-8"), "FINGERPRINTED")

    assert len(occurrences) == 0, (
        f"services/fingerprint.py reintroduced FileState.FINGERPRINTED ({len(occurrences)} found) at "
        f"lines {[getattr(n, 'lineno', '?') for n in occurrences]}. get_fingerprint_progress derives "
        "completed from done_clause(Stage.FINGERPRINT), never a state read (READ-04 / D-10)."
    )


# ---------------------------------------------------------------------------
# Mutation directions, encoded permanently (proof the guard has teeth)
# ---------------------------------------------------------------------------


def test_guard_flags_positional_where_read() -> None:
    """MUTATION #1 (RED): a read passed as a POSITIONAL 2nd arg of ``.where(a, b, c)`` is caught.

    This is the Phase-83 positional-arg blind spot. ``.where()`` receives the read comparator positionally,
    NOT as a chained ``.where(<Compare>)``, so a rule keyed on chained/keyword args alone would miss it.
    """
    source = (
        "from phaze.models.file import FileRecord, FileState\n"
        "stmt = select(FileRecord).where(\n"
        "    FileRecord.sha256_hash == group_hash,\n"
        "    FileRecord.state != FileState.DUPLICATE_RESOLVED,\n"
        ")\n"
    )
    _occ, writes, reads, other = _classify(source, "DUPLICATE_RESOLVED")
    assert len(reads) == 1
    assert len(writes) == 0
    assert len(other) == 0


def test_guard_flags_keyword_filter_by_read() -> None:
    """MUTATION #1b (RED): a read passed as a KEYWORD arg of ``.filter_by(state=...)`` is caught.

    A keyword read is NOT wrapped in a Compare, so Compare-detection alone misses it -- the keyword-arg
    walk is what catches it. Confirms the walker inspects ``Call.keywords`` too.
    """
    source = "q = session.query(FileRecord).filter_by(state=FileState.DUPLICATE_RESOLVED)\n"
    _occ, writes, reads, other = _classify(source, "DUPLICATE_RESOLVED")
    assert len(reads) == 1
    assert len(writes) == 0
    assert len(other) == 0


def test_guard_flags_compare_read() -> None:
    """MUTATION #2 (RED): a bare ``state == FileState.DUPLICATE_RESOLVED`` comparison is caught."""
    source = "keep = file.state == FileState.DUPLICATE_RESOLVED\n"
    _occ, writes, reads, _other = _classify(source, "DUPLICATE_RESOLVED")
    assert len(reads) == 1
    assert len(writes) == 0


def test_guard_allows_state_writer() -> None:
    """MUTATION #3 (GREEN false-positive check): the surviving dual-writer is ALLOWED, not flagged."""
    source = "f.state = FileState.DUPLICATE_RESOLVED\n"
    occ, writes, reads, other = _classify(source, "DUPLICATE_RESOLVED")
    assert len(occ) == 1
    assert len(writes) == 1
    assert len(reads) == 0
    assert len(other) == 0


def test_guard_flags_fingerprinted_reintroduction() -> None:
    """MUTATION #4 (RED): reintroducing ``FileState.FINGERPRINTED`` anywhere is caught."""
    source = "completed = count(FileRecord.id).where(FileRecord.state == FileState.FINGERPRINTED)\n"
    occ, _writes, reads, _other = _classify(source, "FINGERPRINTED")
    assert len(occ) == 1
    assert len(reads) == 1


def test_guard_ignores_fingerprinted_docstring() -> None:
    """MUTATION #5 (GREEN false-positive check): ``FINGERPRINTED`` in a docstring is NOT an occurrence.

    Mirrors the live trap at ``fingerprint.py`` where the docstring reads ``state == FINGERPRINTED``. An
    AST attribute scan does not see docstring text; a line grep would false-positive here.
    """
    source = '"""Previously it read ``state == FINGERPRINTED``, whose sole writer was retry_analysis_failed."""\n'
    occ, _writes, _reads, _other = _classify(source, "FINGERPRINTED")
    assert len(occ) == 0
