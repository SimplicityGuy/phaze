"""Guard: SQLAlchemy's asyncio bridge needs ``greenlet`` in the RUNTIME dependency set (phaze-dixnj).

The dev group also pins ``greenlet`` (coverage tooling), which masks a missing runtime dependency in every
local ``uv run``. The api image installs without dev, so main went red when patchright, the only transitive
source of greenlet, was removed. This test reads the lock's runtime-only closure, not the dev group.
"""

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def _runtime_closure() -> set[str]:
    """Package names reachable from [project].dependencies in uv.lock (dev group excluded)."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    done: set[tuple[str, tuple[str, ...]]] = set()
    pkgs = {p["name"]: p for p in lock["package"]}
    seen: set[str] = set()
    stack = list(pkgs["phaze"].get("dependencies", []))
    while stack:
        dep = stack.pop()
        name = dep["name"]
        if name not in pkgs:
            continue
        extras = tuple(dep.get("extra", []))
        if (name, extras) in done:
            continue
        done.add((name, extras))
        seen.add(name)
        stack.extend(pkgs[name].get("dependencies", []))
        for extra in extras:
            stack.extend(pkgs[name].get("optional-dependencies", {}).get(extra, []))
    return seen


def test_greenlet_resolves_from_runtime_dependencies_only() -> None:
    assert "greenlet" in _runtime_closure(), (
        "greenlet is not reachable from [project].dependencies; the api image will fail on SQLAlchemy asyncio. "
        "Declare sqlalchemy[asyncio] (or greenlet) in the runtime dependencies, not only the dev group."
    )


def test_runtime_declares_sqlalchemy_asyncio_extra() -> None:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.replace(" ", "").lower().startswith("sqlalchemy[asyncio]") for d in deps)
