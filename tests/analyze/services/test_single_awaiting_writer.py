"""Anti-drift source guard (D-02): ``hold_awaiting_cloud`` is the SINGLE writer of ``cloud_job.status='awaiting'``.

Phase 83's LOCKED **D-02** requires ONE awaiting writer, reused by the hold path and both over-cap spill
paths -- not three hand-written copies. 83-07 consolidated the two inline spill CAS re-stamps into
``services.backends.hold_awaiting_cloud``. This hermetic (no-DB) AST scan makes that invariant
self-enforcing: it goes RED the moment ANY module under ``src/phaze/`` re-introduces an inline awaiting
WRITE (a ``.values(status=CloudJobStatus.AWAITING...)`` / ``.values(status="awaiting")`` on a SQLAlchemy
insert/update statement) outside ``services/backends.py``.

Because the check keys on ``.values(...)`` -- a WRITE -- the drain / count-card / shadow-invariant / D-14
reaper predicates that merely READ awaiting via ``.where(CloudJob.status == AWAITING...)`` (or the reaper's
``delete(...).where(...)``) are correctly NOT flagged: a WHERE clause is not a ``.values(...)`` call.
"""

from __future__ import annotations

import ast
from pathlib import Path


# Repo-root-relative source root (mirror the sibling migration tests' parents[3] idiom): this file is
# tests/analyze/services/test_single_awaiting_writer.py, so parents[3] is the repo root.
_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "phaze"

# The SOLE module allowed to WRITE cloud_job.status='awaiting' (the single go-forward writer, D-01/D-02).
_ALLOWED_WRITERS = {_SRC_ROOT / "services" / "backends.py"}


def _status_value_writes_awaiting(keyword: ast.keyword) -> bool:
    """True iff a ``status=`` keyword's value AST subtree references AWAITING or the literal ``"awaiting"``."""
    if keyword.arg != "status":
        return False
    for sub in ast.walk(keyword.value):
        # CloudJobStatus.AWAITING(.value) -> an Attribute whose attr == "AWAITING".
        if isinstance(sub, ast.Attribute) and sub.attr == "AWAITING":
            return True
        # A bare string literal "awaiting".
        if isinstance(sub, ast.Constant) and sub.value == "awaiting":
            return True
    return False


def _file_writes_awaiting(path: Path) -> bool:
    """True iff the module contains a ``.values(status=<awaiting>)`` call (a cloud_job awaiting WRITE)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # Match ``<stmt>.values(...)`` calls (covers both pg_insert(CloudJob).values(...) and
        # update(CloudJob).where(...).values(...)); a WHERE/DELETE predicate is not a .values(...) call.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "values"
            and any(_status_value_writes_awaiting(kw) for kw in node.keywords)
        ):
            return True
    return False


def test_hold_awaiting_cloud_is_the_only_awaiting_writer() -> None:
    """D-02 anti-drift: exactly ``services/backends.py`` writes ``cloud_job.status='awaiting'`` via ``.values(...)``.

    Reverting 83-07 (re-adding an inline ``update(CloudJob).values(status=CloudJobStatus.AWAITING.value...)``
    to ``routers/agent_s3.py`` or ``routers/agent_push.py``) turns this RED: that router file would join the
    writer set, breaking the ``== _ALLOWED_WRITERS`` equality.
    """
    writers = {path for path in _SRC_ROOT.rglob("*.py") if _file_writes_awaiting(path)}

    assert writers == _ALLOWED_WRITERS, (
        "cloud_job.status='awaiting' must be written ONLY by services/backends.hold_awaiting_cloud (D-02). "
        f"Unexpected inline awaiting writers: {sorted(str(p) for p in writers - _ALLOWED_WRITERS)}; "
        f"missing expected writers: {sorted(str(p) for p in _ALLOWED_WRITERS - writers)}"
    )
