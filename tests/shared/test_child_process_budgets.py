"""Guard: no test may bound a child process with its own round number (phaze-0vlnp).

The dominant flake class the phaze-0vlnp inventory found under concurrent full-suite gates was a
test that spawns a real interpreter and waits on it with a hand-picked ``timeout=``. Every such
number was a hang guard -- the tests assert on the child's exit code and output, never on how long
it took -- yet each one was picked on a quiet machine, so a loaded one turned it into a verdict:
``timeout=20`` on an import probe that takes 4.58 s alone, ``timeout=60`` on a child ``pytest`` that
takes 9-11 s alone, both red as the only failure in a full-suite gate. ``tests/_child_process_budget``
has the full account and the one ceiling that replaces them.

What this pins: every ``subprocess.run`` / ``call`` / ``check_call`` / ``check_output`` under
``tests/``, and every ``.wait(timeout=...)`` / ``.communicate(timeout=...)`` on a name bound from
``subprocess.Popen`` in the same function, that passes ``timeout=`` at all passes exactly
``CHILD_PROCESS_HANG_GUARD_SEC``. A bare number, or a module constant of its own, is refused -- a
named round number is still a round number, and ``_HDIUTIL_TIMEOUT_SEC = 60`` was one.

A call that is genuinely NOT a hang guard goes in :data:`EXEMPT` with the reason, keyed by file and
enclosing function, where a reviewer can read it. Leaving ``timeout=`` off entirely is not policed
here: an unbounded child can hang a gate, but it cannot turn a slow machine into a red.

Scoped to CHILD PROCESSES on purpose. In-process waits (``asyncio.wait_for``, polling deadlines,
watchdog thresholds) are the other class the inventory found, and a source scan cannot tell their
legitimate uses apart: ``asyncio.wait_for(probe(), timeout=0.05)`` against a probe that never returns
is the assertion in ``test_bounded_is_available_times_out_on_a_hanging_probe``, not a budget. Their
remedy is per test -- the clock under the test's control (``_BeatClock``, phaze-avw21) or a wait on the
condition itself (``tests/_lock_barrier.py``, phaze-lz69g) -- and no mechanical rule separates the
two without a false positive on the first kind.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
GUARD_NAME = "CHILD_PROCESS_HANG_GUARD_SEC"

_SUBPROCESS_WAITS = frozenset({"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output"})
_POPEN_WAITS = frozenset({"wait", "communicate"})

# "<path relative to the repo root>::<enclosing function>" -> why its timeout is not a hang guard.
EXEMPT: dict[str, str] = {
    "tests/browser/conftest.py::live_server": (
        "a SIGTERM grace period at teardown: TimeoutExpired is suppressed and the server is then killed, so no test outcome depends on the value"
    ),
}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _timeout_violations(source: str, rel: str) -> list[str]:
    """Every child-process wait in ``source`` whose ``timeout=`` is not the shared hang guard."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    # Names bound from subprocess.Popen, per enclosing function (None = module level).
    popen_names: dict[ast.AST | None, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "subprocess.Popen":
            scope = _enclosing_function(node, parents)
            popen_names.setdefault(scope, set()).update(target.id for target in node.targets if isinstance(target, ast.Name))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        scope = _enclosing_function(node, parents)
        name = getattr(scope, "name", "<module>")
        if f"{rel}::{name}" in EXEMPT:
            continue
        callee = ast.unparse(node.func)
        is_popen_wait = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _POPEN_WAITS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in popen_names.get(scope, set())
        )
        if callee not in _SUBPROCESS_WAITS and not is_popen_wait:
            continue
        found.extend(
            f"{rel}:{node.lineno} {callee}(timeout={ast.unparse(keyword.value)}) in {name}"
            for keyword in node.keywords
            if keyword.arg == "timeout" and not (isinstance(keyword.value, ast.Name) and keyword.value.id == GUARD_NAME)
        )
    return found


def test_every_child_process_wait_under_tests_uses_the_shared_hang_guard() -> None:
    violations = [
        violation
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        for violation in _timeout_violations(path.read_text(encoding="utf-8"), path.relative_to(REPO_ROOT).as_posix())
    ]
    assert not violations, (
        "A child process in a test is bounded by its own number. That number is a hang guard, not a verdict, and a "
        f"loaded machine turns it into one -- use tests._child_process_budget.{GUARD_NAME} (or add an EXEMPT entry "
        "saying why this wait is not a hang guard):\n  " + "\n  ".join(violations)
    )


def test_the_guard_refuses_a_bare_number_and_a_private_constant() -> None:
    """Discriminating, not just green: both shapes the phaze-0vlnp inventory found are caught."""
    source = (
        "import subprocess\n"
        "_OWN_TIMEOUT_SEC = 60\n"
        "def probe():\n"
        "    subprocess.run(['python'], timeout=20)\n"
        "    subprocess.check_output(['python'], timeout=_OWN_TIMEOUT_SEC)\n"
        "def reap():\n"
        "    child = subprocess.Popen(['python'])\n"
        "    child.wait(timeout=30)\n"
    )
    assert _timeout_violations(source, "tests/example.py") == [
        "tests/example.py:4 subprocess.run(timeout=20) in probe",
        "tests/example.py:5 subprocess.check_output(timeout=_OWN_TIMEOUT_SEC) in probe",
        "tests/example.py:8 child.wait(timeout=30) in reap",
    ]


def test_the_guard_accepts_the_shared_ceiling_and_ignores_non_child_waits() -> None:
    source = (
        "import asyncio, subprocess, threading\n"
        f"def probe():\n"
        f"    subprocess.run(['python'], timeout={GUARD_NAME})\n"
        "    threading.Event().wait(timeout=5)\n"
        "    subprocess.TimeoutExpired(['python'], timeout=5)\n"
        "async def hang():\n"
        "    await asyncio.wait_for(asyncio.sleep(1), timeout=0.05)\n"
    )
    assert _timeout_violations(source, "tests/example.py") == []
