"""AST source-scan guard (READ-01 / T-82-DRIFT): the three enrich pending helpers in
``services/pipeline.py`` cannot silently regress to a ``FileRecord.state`` read.

Standing insurance behind READ-01: the divergence + all-orderings tests catch a wrong predicate at the
sites they exercise; THIS guard catches a ``FileState`` read reintroduced anywhere inside the three
pending-helper function bodies -- including a NEW read the behavioral tests never touch.

Invariant enforced: inside the bodies of ``get_metadata_pending_files`` /
``get_fingerprint_pending_files`` / ``get_discovered_files_with_duration`` there are ZERO **read** accesses
of ``FileState.DISCOVERED`` / ``FileState.METADATA_EXTRACTED`` / ``FileState.FINGERPRINTED`` -- where a
"read" is the member appearing inside an ``ast.Compare`` (``state == FileState.DISCOVERED``) or as ANY
argument (positional OR keyword, incl. a ``**`` splat) of a ``.where()`` / ``.filter()`` /
``.filter_by()`` / ``.having()`` call.

Why an ``ast.walk`` scan and NEVER a line ``grep`` (Pitfall 1 / ``feedback_mutation_test_guard_tests`` --
Phase 83 shipped two *toothless* guards):

1. ``FileState.DISCOVERED`` is a single unbroken attribute chain, so a ``grep`` FALSE-POSITIVES on a
   docstring mention (each pending helper's docstring narrates the OLD state semantics in prose).
2. The pre-cutover reads were passed POSITIONALLY (``.where(FileRecord.state == FileState.DISCOVERED)``),
   not as a chained ``.where(<Compare>)``; a rule keyed only on ``keyword.arg`` or a chained comparator is
   BLIND to them -- the exact Phase-83 blind spot. This scan walks BOTH the positional ``Call.args`` list
   AND the ``Call.keywords`` list (regardless of ``keyword.arg is None`` for a ``**`` splat).

The scan is SCOPED to the three function-def subtrees -- ``pipeline.py`` has many OTHER functions that
legitimately read ``FileState`` (``get_analysis_failed_count``, ``get_pushing_count``, ...), so a
whole-module scan would false-positive. The negative tests (``test_guard_flags_*`` / ``test_guard_ignores_*``)
mutate crafted source STRINGS, never the real file, so this test is DB-free and leaves no source dirty.
"""

from __future__ import annotations

import ast
from pathlib import Path


# This file is ``tests/shared/test_pending_set_source_scan.py``; parents[2] is the repo root.
_PIPELINE = Path(__file__).resolve().parents[2] / "src" / "phaze" / "services" / "pipeline.py"

# The three enrich pending helpers whose bodies must be free of FileState reads (READ-01).
_PENDING_FUNCS = ("get_metadata_pending_files", "get_fingerprint_pending_files", "get_discovered_files_with_duration")

# The three enrich state members whose READ is forbidden inside those bodies.
_FORBIDDEN_MEMBERS = ("DISCOVERED", "METADATA_EXTRACTED", "FINGERPRINTED")

# SQLAlchemy read-clause entry points. A ``FileState.<member>`` inside ANY argument of one of these is a
# read of ``FileRecord.state`` (the exact thing the cutover removed).
_WHERE_FUNCS = frozenset({"where", "filter", "filter_by", "having"})


def _filestate_occurrences(node: ast.AST, member: str) -> list[ast.Attribute]:
    """Every ``FileState.<member>`` attribute access anywhere in ``node``'s subtree.

    A docstring/comment mention of ``member`` is NOT an ``ast.Attribute`` and is therefore invisible
    here -- the whole point of an AST scan over a line scan.
    """
    return [
        n for n in ast.walk(node) if isinstance(n, ast.Attribute) and n.attr == member and isinstance(n.value, ast.Name) and n.value.id == "FileState"
    ]


def _in_compare(occ: ast.AST, scope: ast.AST) -> bool:
    """True iff ``occ`` appears inside an ``ast.Compare`` (e.g. ``state == FileState.DISCOVERED``)."""
    return any(isinstance(node, ast.Compare) and occ in ast.walk(node) for node in ast.walk(scope))


def _in_where_arg(occ: ast.AST, scope: ast.AST) -> bool:
    """True iff ``occ`` appears in ANY argument of a ``where``/``filter``/``filter_by``/``having`` call.

    Walks the positional ``Call.args`` list AND the ``Call.keywords`` list -- keying on neither
    ``keyword.arg`` nor a chained comparator, so it is not blind to positional ``.where(a, b, c)`` reads
    nor to keyword ``.filter_by(state=...)`` / ``**`` splat reads. (The two Phase-83 blind spots.)
    """
    for node in ast.walk(scope):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _WHERE_FUNCS):
            continue
        for positional in node.args:  # ``.where(a, b, <read>)`` -- positional args, all of them.
            if occ in ast.walk(positional):
                return True
        for keyword in node.keywords:  # ``.filter_by(state=<read>)`` and ``.where(**splat)`` -- keyword args.
            if occ in ast.walk(keyword.value):
                return True
    return False


def _reads(scope: ast.AST, member: str) -> list[ast.Attribute]:
    """Every ``FileState.<member>`` occurrence in ``scope`` that sits in a READ context (Compare / where-arg)."""
    return [occ for occ in _filestate_occurrences(scope, member) if _in_compare(occ, scope) or _in_where_arg(occ, scope)]


def _func_node(tree: ast.AST, name: str) -> ast.AST:
    """Return the (async) function-def node named ``name`` in ``tree``; fail loud if absent (a rename)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in {_PIPELINE} -- was it renamed? update _PENDING_FUNCS.")


# ---------------------------------------------------------------------------
# The real-source guard (the invariant this test exists to protect)
# ---------------------------------------------------------------------------


def test_pending_helpers_have_zero_filestate_reads() -> None:
    """READ-01: none of the three enrich pending helpers reads ``FileState.{DISCOVERED,METADATA_EXTRACTED,FINGERPRINTED}``."""
    tree = ast.parse(_PIPELINE.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for fname in _PENDING_FUNCS:
        fn = _func_node(tree, fname)
        for member in _FORBIDDEN_MEMBERS:
            for occ in _reads(fn, member):
                offenders.append(f"{fname} reads FileState.{member} at line {getattr(occ, 'lineno', '?')}")

    assert offenders == [], (
        "The enrich pending helpers must derive membership from eligible_clause / ~dedup_resolved_clause, "
        "never a FileRecord.state read (READ-01 / T-82-DRIFT). Offending reads:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Mutation directions, encoded permanently (proof the guard has teeth)
# ---------------------------------------------------------------------------


def _reads_in_source(source: str, member: str) -> list[ast.Attribute]:
    return _reads(ast.parse(source), member)


def test_guard_flags_positional_where_read() -> None:
    """MUTATION #1 (RED): a read passed as a POSITIONAL arg of ``.where(a, b)`` is caught (Phase-83 blind spot)."""
    source = "stmt = select(FileRecord).where(\n    FileRecord.file_type.in_(MUSIC_VIDEO_TYPES),\n    FileRecord.state == FileState.DISCOVERED,\n)\n"
    assert len(_reads_in_source(source, "DISCOVERED")) == 1


def test_guard_flags_keyword_filter_by_read() -> None:
    """MUTATION #1b (RED): a read passed as a KEYWORD arg of ``.filter_by(state=...)`` is caught."""
    source = "q = session.query(FileRecord).filter_by(state=FileState.METADATA_EXTRACTED)\n"
    assert len(_reads_in_source(source, "METADATA_EXTRACTED")) == 1


def test_guard_flags_splat_where_read() -> None:
    """MUTATION #1c (RED): a read inside a ``**`` splat of ``.filter_by(**{...})`` is caught (keyword.arg is None)."""
    source = "q = select(FileRecord).filter_by(**{'state': FileState.FINGERPRINTED})\n"
    assert len(_reads_in_source(source, "FINGERPRINTED")) == 1


def test_guard_flags_compare_read() -> None:
    """MUTATION #2 (RED): a bare ``state != FileState.DISCOVERED`` comparison is caught."""
    source = "keep = file.state != FileState.DISCOVERED\n"
    assert len(_reads_in_source(source, "DISCOVERED")) == 1


def test_guard_ignores_docstring_mention() -> None:
    """MUTATION #3 (GREEN false-positive check): a ``DISCOVERED`` mention in a docstring is NOT a read.

    Each pending helper's docstring narrates the OLD state semantics in prose; an AST attribute scan does
    not see docstring text, but a line grep would false-positive here (Pitfall 1).
    """
    source = '"""Previously keyed on ``FileRecord.state == FileState.DISCOVERED``."""\n'
    assert len(_reads_in_source(source, "DISCOVERED")) == 0


def test_guard_ignores_non_where_call_arg() -> None:
    """GREEN false-positive check: a retained non-state read like ``file_type.in_(...)`` is not flagged.

    Confirms the guard keys on the FORBIDDEN FileState members in a read context, not on every ``.where`` arg.
    """
    source = "stmt = select(FileRecord).where(FileRecord.file_type.in_(MUSIC_VIDEO_TYPES))\n"
    for member in _FORBIDDEN_MEMBERS:
        assert len(_reads_in_source(source, member)) == 0
