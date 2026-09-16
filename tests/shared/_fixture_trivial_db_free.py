"""Trivial, DB-free test target for the subprocess pytest runs in test_explicit_test_db_url_fails_closed.py.

Deliberately named without a ``test_`` prefix so the NORMAL suite never collects it on its own --
it exists only to be passed as an explicit file argument to a subprocess ``pytest`` invocation,
which collects an explicitly-named file regardless of the ``python_files`` pattern. Keeping it
separate from the guard test module is load-bearing: the guard module's own tests spawn subprocess
``pytest`` runs, and pointing a subprocess at the guard module ITSELF would re-collect those very
tests inside the subprocess, spawning further subprocesses recursively. Targeting this file instead
keeps the subprocess's collected set to exactly the two trivial tests below, with no recursion.
"""

from __future__ import annotations


def test_one_plus_one() -> None:
    assert 1 + 1 == 2


def test_true_is_true() -> None:
    assert True
