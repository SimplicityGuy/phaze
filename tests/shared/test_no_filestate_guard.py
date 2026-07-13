"""D-08 anti-drift guard: no EXECUTABLE ``FileState`` / ``FileRecord.state`` / ``files.state`` may
reappear in ``src/phaze`` after Phase 90 (MIG-04) deleted the enum + column + index.

mypy + ruff + ``import phaze`` are the PRIMARY anti-drift guard (a reintroduced ``FileState`` import
or ``FileRecord.state`` read fails to resolve). This ONE source-scan test is the standing SECONDARY
backstop for the forms the type checker can miss -- most importantly a ``.values(state=...)`` write
against the ``files`` table, whose kwarg name is not a resolvable attribute.

**Why ``tokenize`` and NOT a line ``grep`` (project memory ``feedback_mutation_test_guard_tests`` --
Phase 83 shipped two toothless guards):** ~20 src files still carry ``FileState`` in DOCSTRINGS and
comments as prose describing the retirement. A ``#``-only comment strip is INSUFFICIENT because a
docstring is a triple-quoted STRING, not a ``#`` comment. This guard tokenizes each file and blanks
BOTH ``COMMENT`` and ``STRING`` (incl. f-string literal) tokens before scanning, so prose can document
the retirement without self-failing the guard. Blanking preserves byte offsets + line count, so the
multi-line ``.values(\n    state=...)`` form is still matched by a DOTALL scan of the blanked text.

**Honest ``.values(**splat)`` limitation:** a dynamically-built ``{"state": ...}`` splatted into
``.values(**payload)`` cannot be statically resolved to a ``state`` key, so it is a SOFT WARNING only.
The real backstop there is structural: the ``files.state`` column no longer exists, so any such write
fails at SQL compile/execute and is caught by any test exercising that path.

**Mutation observation (recorded in 90-04-SUMMARY.md):** reintroducing a real ``FileState`` import or
a ``.values(state=...)`` into a src file turns ``test_no_filestate_in_src`` RED; removing it restores
GREEN. A green guard proves nothing -- the run is documented in the SUMMARY.
"""

from __future__ import annotations

import io
from pathlib import Path
import re
import tokenize


_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "phaze"

# --- Forbidden EXECUTABLE forms (matched against the comment/string-blanked token stream) ----------
# 1. The bare ``FileState`` token: the class is deleted, so ANY executable reference is a reintroduction.
_FILESTATE = re.compile(r"\bFileState\b")
# 2. Scoped attribute reads/writes on the file record / table -- NOT a bare ``\w+\.state`` (that would
#    false-positive on FastAPI ``app.state`` / ``request.app.state`` / ``websocket.state``).
_FILERECORD_STATE = re.compile(r"\bFileRecord\.state\b")
_FILES_STATE = re.compile(r"\bfiles\.state\b")
# 3. A ``.values(state=...)`` write, INCLUDING the multi-line ``.values(\n    state=...)`` form. DOTALL
#    so ``[^)]*`` (state anywhere in a single-level paren group) spans newlines. Line-grep is blind to
#    this (memory ``feedback_mutation_test_guard_tests``).
_VALUES_STATE = re.compile(r"\.values\([^)]*\bstate\s*=", re.DOTALL)

_FORBIDDEN = (_FILESTATE, _FILERECORD_STATE, _FILES_STATE, _VALUES_STATE)

# Soft-warn only: a dict splat cannot be statically resolved to a ``state`` key (see module docstring).
_VALUES_SPLAT = re.compile(r"\.values\(\s*\*\*")

# f-string literal token types (3.12+); blank their text like any other string literal.
_STRING_LIKE = {tokenize.STRING, tokenize.COMMENT}
for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    _t = getattr(tokenize, _name, None)
    if _t is not None:
        _STRING_LIKE.add(_t)


def _iter_src_files() -> list[Path]:
    return sorted(_SRC_ROOT.rglob("*.py"))


def _code_only(source: str) -> str:
    """Return ``source`` with every COMMENT + STRING (incl. f-string literal) token blanked to spaces.

    Byte offsets and line count are preserved (blanked spans keep their length + embedded newlines), so
    a DOTALL regex over the result still matches a multi-line ``.values(\\n    state=...)`` write, and
    reported line numbers stay accurate. Executable NAME/OP tokens (incl. those inside f-string ``{...}``
    replacement fields) are left intact.
    """
    lines = source.split("\n")
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover - src is always valid
        return source
    for tok in toks:
        if tok.type not in _STRING_LIKE:
            continue
        (start_row, start_col), (end_row, end_col) = tok.start, tok.end
        for row in range(start_row, end_row + 1):
            line = lines[row - 1]
            a = start_col if row == start_row else 0
            b = end_col if row == end_row else len(line)
            lines[row - 1] = line[:a] + (" " * (b - a)) + line[b:]
    return "\n".join(lines)


def _violations() -> list[str]:
    """Return ``"<path>: <pattern>"`` for every EXECUTABLE forbidden form found across src/phaze."""
    found: list[str] = []
    for path in _iter_src_files():
        code = _code_only(path.read_text(encoding="utf-8"))
        for pattern in _FORBIDDEN:
            if pattern.search(code):
                found.append(f"{path}: matched /{pattern.pattern}/")
    return found


def _splat_warnings() -> list[str]:
    """Return ``.values(**...)`` sites (soft-warn only -- statically unresolvable; see module docstring)."""
    warns: list[str] = []
    for path in _iter_src_files():
        code = _code_only(path.read_text(encoding="utf-8"))
        if _VALUES_SPLAT.search(code):
            warns.append(str(path))
    return warns


def test_no_filestate_in_src() -> None:
    """No EXECUTABLE FileState / FileRecord.state / files.state / .values(state=) survives in src/phaze."""
    py_files = _iter_src_files()
    # Vacuous-glob assert: a silent empty glob must not pass (guard must actually scan real files).
    assert py_files, f"guard scanned no src/phaze files under {_SRC_ROOT}"

    violations = _violations()
    assert not violations, "executable FileState reintroduced in src/phaze (D-08):\n" + "\n".join(violations)

    # Soft warning only -- a `.values(**payload)` cannot be statically resolved to a `state` key. Emit for
    # manual review; the structural backstop is that `files.state` no longer exists (write fails at execute).
    for site in _splat_warnings():
        print(f"[D-08 soft-warn] {site}: `.values(**...)` splat cannot be statically checked for a `state` key")


def test_guard_flags_planted_matches() -> None:
    """The regex set MATCHES each forbidden form (incl. multi-line + splat) and NOT the lookalikes.

    Proves the guard is not a vacuous no-op: reintroducing any of these executable forms would fail
    ``test_no_filestate_in_src``. The negative cases lock in the FastAPI-``app.state`` specificity.
    """
    # Positive: bare token, both scoped attribute forms, single-line + multi-line values(state=), splat.
    assert _FILESTATE.search("state = FileState.DISCOVERED")
    assert _FILERECORD_STATE.search("select(FileRecord.state).where(...)")
    assert _FILES_STATE.search("select(func.count()).select_from(files).where(files.state == x)")
    assert _VALUES_STATE.search(".values(state=FileState.PUSHING)")
    assert _VALUES_STATE.search(".values(\n    state='pushing',\n    backend_id=b,\n)")
    assert _VALUES_STATE.search(".values(backend_id=b, state='pushing')")
    assert _VALUES_SPLAT.search(".values(**payload)")

    # Negative lookalikes -- legitimate FastAPI / sidecar / derived-status references must NOT match.
    assert not _FILESTATE.search("from phaze.services import stage_status")
    assert not _FILESTATE.search("cloud_job.status == 'awaiting'")
    assert not _FILERECORD_STATE.search("app.state.redis")
    assert not _FILERECORD_STATE.search("request.app.state")
    assert not _FILES_STATE.search("app.state.redis")
    assert not _FILES_STATE.search("websocket.state")
    assert not _VALUES_STATE.search(".values(status=CloudJobStatus.SUBMITTED.value)")


def test_guard_strips_docstring_and_comment_filestate_mentions() -> None:
    """A ``FileState`` mention inside a docstring / comment / string literal must NOT trip the guard.

    This is the load-bearing difference from a ``#``-only strip: docstrings are triple-quoted STRINGs.
    """
    sample = (
        '"""A docstring mentioning FileState and FileRecord.state and .values(state=x).\n\n'
        'Multi-line prose about the retired FileState enum.\n"""\n'
        "# a comment referencing FileState and .values(state=y)\n"
        "x = 'FileState in a string literal'\n"
        "y = f'still FileState in an f-string literal'\n"
        "z = files_processed + 1  # executable, no forbidden token\n"
    )
    code = _code_only(sample)
    assert "FileState" not in code, "tokenize strip must blank FileState in docstrings/comments/strings"
    assert not any(p.search(code) for p in _FORBIDDEN)
