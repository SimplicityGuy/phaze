"""Retain one full default collection and its source/runtime bindings in CI.

This is collection evidence, not execution or coverage evidence. Actual shard
reports have their own ordering; canonical order comparisons are per module.
"""

from __future__ import annotations

from collections import Counter
import errno
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from scripts.parallel_test_manifests import (
    ManifestError,
    build_manifest_plan,
    collect_canonical_node_ids,
    load_shard_definitions,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


DEFAULT_OUTPUT = Path("canonical-collection")
# Deliberately excludes database credentials and arbitrary process environment.
SELECTION_ENV = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONPATH", "PYTHONHASHSEED")
CI_ENV = ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "RUNNER_OS", "RUNNER_ARCH")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(repo_root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 -- fixed git executable, internal read-only arguments.
        ("git", *arguments),  # noqa: S607 -- git is the configured checkout tool.
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def source_runtime_bindings(repo_root: Path) -> dict[str, Any]:
    """Bind all tracked inputs and installed plugins to this interpreter/checkout."""

    tracked = _git(repo_root, "ls-files", "-z").split("\0")
    blobs = {name: _sha256((repo_root / name).read_bytes()) for name in tracked if name and (repo_root / name).is_file()}
    imports: dict[str, Any] = {}
    for name in ("phaze", "pytest", "essentia", "scripts.parallel_test_manifests"):
        spec = importlib.util.find_spec(name)
        origin = Path(spec.origin).resolve() if spec and spec.origin else None
        imports[name] = {"origin": str(origin) if origin else None, "sha256": _sha256(origin.read_bytes()) if origin else None}
    loaded_origin = importlib.import_module("phaze").__file__
    if loaded_origin is None:
        raise ManifestError("phaze import has no source origin")
    imports["phaze"]["loaded_origin"] = str(Path(loaded_origin).resolve())
    expected = (repo_root / "src/phaze/__init__.py").resolve()
    if imports["phaze"]["origin"] != str(expected) or imports["phaze"]["loaded_origin"] != str(expected):
        raise ManifestError("phaze import does not bind to the captured checkout")

    native: dict[str, Any] = {}
    for name in ("ffmpeg", "ffprobe", "fpcalc"):
        executable = shutil.which(name)
        if executable is None:
            native[name] = {"available": False}
            continue
        result = subprocess.run(  # noqa: S603 -- executable resolved from the existing CI toolchain.
            (executable, "-version"), capture_output=True, check=False
        )
        native[name] = {
            "path": executable,
            "sha256": _sha256(Path(executable).read_bytes()),
            "exit": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
        }
    essentia_binaries: dict[str, str] = {}
    try:
        distribution = importlib.metadata.distribution("essentia-tensorflow")
    except importlib.metadata.PackageNotFoundError:
        distribution = None
    if distribution:
        essentia_binaries = {
            str(file): _sha256(Path(str(distribution.locate_file(file))).read_bytes())
            for file in distribution.files or ()
            if str(file).endswith((".so", ".dylib", ".dll", ".pyd"))
        }
    pytest_executable = shutil.which("pytest")
    pytest_launcher = {"path": pytest_executable, "sha256": _sha256(Path(pytest_executable).read_bytes()) if pytest_executable else None}
    distributions = sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions())
    plugins = [
        {"name": entry.name, "value": entry.value, "distribution": entry.dist.metadata["Name"] if entry.dist else None}
        for entry in importlib.metadata.entry_points(group="pytest11")
    ]
    return {
        "commit": _git(repo_root, "rev-parse", "HEAD").strip(),
        "trees": {
            name: _git(repo_root, "rev-parse", f"HEAD{suffix}").strip()
            for name, suffix in (("full", "^{tree}"), ("src", ":src"), ("tests", ":tests"))
        },
        "status": _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"),
        "diff": _git(repo_root, "diff", "HEAD", "--"),
        "tracked_sha256": blobs,
        "imports": imports,
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "errno": {name: value for name, value in vars(errno).items() if name.startswith("E") and isinstance(value, int)},
            "sys_path": sys.path,
            "pytest_launcher": pytest_launcher,
        },
        "distributions": distributions,
        "pytest_plugins": plugins,
        "native": native,
        "essentia_binaries": essentia_binaries,
        "ci": {key: os.environ.get(key) for key in CI_ENV},
    }


def capture_receipt(repo_root: Path, output_dir: Path, *, run_command: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run) -> int:
    """Always retain the collection subprocess result; reject incomplete evidence."""

    receipt: dict[str, Any] = {"schema": 1, "complete": False, "selection_environment": {key: os.environ.get(key) for key in SELECTION_ENV}}
    # Stamp before creating artifacts, so the receipt itself does not dirty the source stamp.
    try:
        receipt["bindings"] = source_runtime_bindings(repo_root)
    except (ImportError, ValueError, OSError, subprocess.SubprocessError, ManifestError) as exc:
        receipt["binding_error"] = str(exc)
    output_dir.mkdir(parents=True, exist_ok=True)
    collection_exit: int | None = None

    def retain_command(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal collection_exit
        # Save byte-exact streams, then give the existing helper its text interface.
        kwargs["text"] = False
        receipt["collection"] = {
            "argv": list(arguments),
            "cwd": str(kwargs["cwd"]),
            "exit": None,
            "selection_environment": {key: kwargs["env"].get(key) for key in SELECTION_ENV},
        }
        result = run_command(arguments, **kwargs)
        collection_exit = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        receipt["collection"].update({"exit": collection_exit, "stdout_sha256": _sha256(stdout), "stderr_sha256": _sha256(stderr)})
        (output_dir / "stdout.txt").write_bytes(stdout)
        (output_dir / "stderr.txt").write_bytes(stderr)
        decoded = stdout.decode("utf-8")
        raw_ids = tuple(line for line in decoded.splitlines() if line.startswith("tests/") and "::" in line)
        receipt["node_ids"] = raw_ids
        receipt["multiplicities"] = dict(Counter(raw_ids))
        return subprocess.CompletedProcess(arguments, result.returncode, decoded, stderr.decode("utf-8"))

    exit_code = 2
    try:
        canonical = collect_canonical_node_ids(repo_root, run_command=retain_command)
        plan = build_manifest_plan(canonical, load_shard_definitions(repo_root / "tests/ci_shards.json"))
        receipt["lanes"] = [{"name": lane.name, "node_ids": lane.node_ids, "pytest_arguments": lane.pytest_arguments} for lane in plan.lanes]
        if "binding_error" not in receipt:
            receipt["complete"] = True
            exit_code = 0
    except (ManifestError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        # The full helper diagnostic may contain giant IDs; retain only in the artifact.
        receipt["error"] = str(exc)
        exit_code = collection_exit if collection_exit else 2
    receipt["exit"] = exit_code
    encoded = (json.dumps(receipt, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    (output_dir / "receipt.json").write_bytes(encoded)
    files = {path.name: _sha256(path.read_bytes()) for path in output_dir.iterdir() if path.is_file() and path.name != "sha256.json"}
    (output_dir / "sha256.json").write_text(json.dumps(files, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(
        f"Canonical collection: complete={receipt['complete']}, nodes={len(receipt.get('node_ids', ()))}, receipt sha256={_sha256(encoded)}\n"
    )
    return exit_code


def main() -> int:
    root = Path.cwd().resolve()
    return capture_receipt(root, root / DEFAULT_OUTPUT)


if __name__ == "__main__":
    raise SystemExit(main())
