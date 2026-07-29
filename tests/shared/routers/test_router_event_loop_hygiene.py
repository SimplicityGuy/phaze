"""phaze-9dwb: no router function performs blocking filesystem I/O.

The hub runs a SINGLE uvicorn event loop with no ``--workers``, and it is shared by the
execution-progress SSE stream (``routers/execution.py``), the 5s chrome poll, ``/health`` (which
the compose healthcheck can restart the container on), and every agent callback POST. A synchronous
filesystem call on that loop freezes ALL of them for the duration of the I/O -- milliseconds on a
healthy local disk for an ``iterdir()`` over a concert-set directory with thousands of siblings, and
indefinitely against a hard-mounted hung NFS/SMB server.

The repo already treats this as a defect: ``execute_tag_write`` was moved to ``asyncio.to_thread``
under phaze-qfxv precisely because blocking calls here "freeze every SSE stream, 5s poll, /health
check, and agent callback". ``routers/cue.py`` was the last violator (``write_cue_file``'s
``exists()`` + ``iterdir()`` + ``open("w")``, plus a redundant second ``_get_cue_version`` sweep on
both the success and the ERROR path -- the latter re-scanning a directory already known to be
misbehaving). phaze-6bkk removed the archive filesystem from the api container's reach entirely, so
the honest guard is now the general one: routers do no blocking disk I/O at all.

Enforced structurally rather than by grep so a rename or an alias cannot slip past it. Scoped to
FUNCTION BODIES: module-scope setup (``Path(__file__).resolve()``, ``Jinja2Templates(directory=...)``)
runs once at import, before the loop exists, and is not what this guards.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROUTERS_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "routers"

# Blocking filesystem operations named UNAMBIGUOUSLY -- an attribute call with one of these names
# is a disk touch under any plausible receiver (pathlib.Path, os, shutil). Deliberately EXCLUDES
# names that are overwhelmingly non-filesystem in this codebase (``sort_state.resolve(...)``,
# ``datetime.replace(tzinfo=...)``, ``str.replace(...)``): a guard that cries wolf on those gets
# suppressed wholesale, which is worse than a narrower guard that stays credible. The Path-rooted
# rule below catches the ambiguous names when the receiver is provably a path.
_BLOCKING_FS_ATTRS: frozenset[str] = frozenset(
    {
        # pathlib.Path
        "exists",
        "iterdir",
        "glob",
        "rglob",
        "lstat",
        "is_file",
        "is_dir",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "unlink",
        "rmdir",
        "mkdir",
        "touch",
        "samefile",
        "open",
        # os / shutil
        "listdir",
        "scandir",
        "makedirs",
        "copyfile",
        "copytree",
        "rmtree",
    }
)

# Ambiguous outside a filesystem context -- flagged ONLY on a ``Path(...)``-rooted receiver chain.
_PATH_ROOTED_ATTRS: frozenset[str] = frozenset({"resolve", "stat", "replace", "rename", "with_suffix"})

# Bare ``open(...)`` -- always the builtin file opener.
_OPEN_NAMES: frozenset[str] = frozenset({"open"})

# The escape hatch: anything wrapped in ``asyncio.to_thread(...)`` is OFF the loop by construction,
# which is the whole point. Calls nested inside such a wrapper are not flagged.
_OFFLOAD_ATTRS: frozenset[str] = frozenset({"to_thread", "run_in_executor", "run_sync"})


def _is_path_rooted(node: ast.expr) -> bool:
    """True when ``node``'s receiver chain bottoms out in a ``Path(...)`` construction."""
    while True:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "Path":
                return True
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.BinOp):  # Path(a) / "b"
            node = node.left
        else:
            return False


def _is_offload_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr in _OFFLOAD_ATTRS


def _blocking_fs_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Return ``(call_name, lineno)`` for every blocking FS call inside a function body."""
    found: list[tuple[str, int]] = []

    def walk_calls(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                if _is_offload_call(child):
                    # The offloaded callable is a reference, not a call, so there is nothing to
                    # recurse into that could be on-loop. Skip the whole subtree.
                    continue
                if isinstance(child.func, ast.Attribute) and (
                    child.func.attr in _BLOCKING_FS_ATTRS or (child.func.attr in _PATH_ROOTED_ATTRS and _is_path_rooted(child.func.value))
                ):
                    found.append((child.func.attr, child.lineno))
                elif isinstance(child.func, ast.Name) and child.func.id in _OPEN_NAMES:
                    found.append((child.func.id, child.lineno))
            walk_calls(child)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            walk_calls(node)
    return found


def test_no_router_function_does_blocking_filesystem_io() -> None:
    """Every ``src/phaze/routers/*.py`` function body is free of synchronous disk I/O."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        calls = _blocking_fs_calls(ast.parse(path.read_text()))
        if calls:
            offenders[path.name] = [f"{name}() at line {lineno}" for name, lineno in calls]

    assert not offenders, (
        "blocking filesystem I/O found in router function bodies -- it runs on the single shared "
        "uvicorn event loop and freezes every SSE stream, poll, /health check and agent callback "
        "for its whole duration:\n"
        + "\n".join(f"  {name}: {', '.join(hits)}" for name, hits in offenders.items())
        + "\nFix: move the work to the agent that owns the mount (phaze-6bkk), or wrap the whole "
        "blocking sequence in a single asyncio.to_thread(...)."
    )


def test_the_guard_actually_detects_the_pattern_it_bans() -> None:
    """Mutation check: the AST walk must FIRE on the exact shape phaze-9dwb removed from cue.py.

    Without this, a bug in the walker (or an over-eager offload skip) would leave the guard above
    passing vacuously on every future regression.
    """
    source = """
def _get_cue_version(file_path):
    audio_path = Path(file_path)
    base_cue = audio_path.parent / f"{audio_path.stem}.cue"
    if not base_cue.exists():
        return 0
    for f in audio_path.parent.iterdir():
        pass
    return 1
"""
    hits = {name for name, _lineno in _blocking_fs_calls(ast.parse(source))}
    assert hits == {"exists", "iterdir"}


def test_the_guard_flags_an_ambiguous_name_only_when_path_rooted() -> None:
    """``sort_state.resolve(...)`` / ``datetime.replace(tzinfo=...)`` are NOT disk touches.

    Both are common in these routers. Flagging them would make the guard noise and get it
    suppressed; missing ``Path(p).resolve()`` would make it useless. Assert both halves.
    """
    benign = """
def handler(view, when):
    sort_state = PROPOSE_SORT.resolve(sort=view.sort, order=view.order)
    return when.replace(tzinfo=UTC)
"""
    assert _blocking_fs_calls(ast.parse(benign)) == []

    path_rooted = """
def handler(raw):
    return Path(raw).resolve()
"""
    assert [name for name, _ in _blocking_fs_calls(ast.parse(path_rooted))] == ["resolve"]


def test_the_guard_accepts_an_offloaded_sequence() -> None:
    """A blocking sequence wrapped in ``asyncio.to_thread`` is compliant, not an offender.

    This is the phaze-qfxv escape hatch the codebase already uses (``_write_and_verify_sync``): one
    offload for the WHOLE sequence rather than several, so it must not be flagged.
    """
    source = """
async def handler(path):
    return await asyncio.to_thread(_write_and_verify_sync, path, tags)
"""
    assert _blocking_fs_calls(ast.parse(source)) == []
