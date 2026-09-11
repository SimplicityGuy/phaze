"""Build order-preserving pytest argument manifests for the two-worker gate.

Each lane still collects the canonical default suite.  Its argument file only
deselects nodes owned by the other lane, which preserves module import order as
well as the relative order of the nodes that execute.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess  # nosec B404 -- collection uses a fixed argv tuple with shell disabled.
import sys
from typing import Any


DEFAULT_SHARDS_PATH = Path("tests/ci_shards.json")
DEFAULT_OUTPUT_DIR = Path(".parallel-test-manifests")
COLLECT_COMMAND = ("uv", "run", "--no-sync", "pytest", "--collect-only", "-q")

LANE_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lane-a", ("shared-registry", "ingest-pipeline", "review-agents", "shared-root")),
    ("lane-b", ("integration-shared-services", "shared-telemetry", "analyze-rest")),
)


class ManifestError(RuntimeError):
    """Raised when collection or partition validation cannot prove safety."""


@dataclass(frozen=True)
class ShardDefinition:
    """One CI shard's path ownership rules."""

    name: str
    include_paths: tuple[PurePosixPath, ...]
    ignored_paths: tuple[PurePosixPath, ...]

    def selects(self, test_file: PurePosixPath) -> bool:
        """Return whether *test_file* belongs to this shard."""

        included = any(_contains(path, test_file) for path in self.include_paths)
        ignored = any(_contains(path, test_file) for path in self.ignored_paths)
        return included and not ignored


@dataclass(frozen=True)
class LaneManifest:
    """The verified nodes and pytest deselection arguments for one lane."""

    name: str
    node_ids: tuple[str, ...]
    pytest_arguments: tuple[str, ...]


@dataclass(frozen=True)
class ManifestPlan:
    """A complete two-lane partition of the canonical collection."""

    canonical_node_ids: tuple[str, ...]
    lanes: tuple[LaneManifest, ...]


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _contains(root: PurePosixPath, candidate: PurePosixPath) -> bool:
    return candidate == root or root in candidate.parents


def _test_path(value: str, *, context: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "tests":
        raise ManifestError(f"{context} must be a safe repository-relative tests/ path: {value!r}")
    return path


def _parse_shard(raw: Any, *, index: int) -> ShardDefinition:
    if not isinstance(raw, dict):
        raise ManifestError(f"shard entry {index} must be an object")

    name = raw.get("name")
    paths = raw.get("paths")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError(f"shard entry {index} has an invalid name")
    if not isinstance(paths, str) or not paths.strip():
        raise ManifestError(f"shard {name!r} has an invalid paths value")

    try:
        tokens = shlex.split(paths)
    except ValueError as exc:
        raise ManifestError(f"shard {name!r} has malformed paths: {exc}") from exc

    includes: list[PurePosixPath] = []
    ignores: list[PurePosixPath] = []
    for token in tokens:
        if token.startswith("--ignore="):
            ignored = token.removeprefix("--ignore=")
            ignores.append(_test_path(ignored, context=f"shard {name!r} ignore"))
        elif token.startswith("-"):
            raise ManifestError(f"shard {name!r} has unsupported pytest option {token!r}")
        else:
            includes.append(_test_path(token, context=f"shard {name!r} include"))

    if not includes:
        raise ManifestError(f"shard {name!r} has no included test path")
    return ShardDefinition(name=name, include_paths=tuple(includes), ignored_paths=tuple(ignores))


def load_shard_definitions(path: Path) -> dict[str, ShardDefinition]:
    """Read and validate the checked-in CI shard ownership manifest."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not read shard manifest {path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ManifestError("shard manifest must be a non-empty list")

    shards: dict[str, ShardDefinition] = {}
    for index, entry in enumerate(raw):
        shard = _parse_shard(entry, index=index)
        if shard.name in shards:
            raise ManifestError(f"duplicate shard name {shard.name!r}")
        shards[shard.name] = shard

    expected = {name for _, names in LANE_BUCKETS for name in names}
    actual = set(shards)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ManifestError(f"shard names do not match the two-lane grouping: missing={missing}, extra={extra}")
    return shards


def collect_canonical_node_ids(repo_root: Path, *, run_command: RunCommand = subprocess.run) -> tuple[str, ...]:
    """Collect the default non-browser suite in its canonical serial order."""

    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    result = run_command(
        COLLECT_COMMAND,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ManifestError(f"canonical pytest collection failed with exit {result.returncode}: {detail}")

    node_ids = tuple(line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line)
    if not node_ids:
        raise ManifestError("canonical pytest collection returned no node IDs")
    duplicates = sorted(node for node, count in Counter(node_ids).items() if count > 1)
    if duplicates:
        raise ManifestError(f"canonical pytest collection contains duplicate node IDs: {duplicates}")
    return node_ids


def validate_lane_partition(canonical_node_ids: Sequence[str], lanes: Sequence[tuple[str, Sequence[str]]]) -> None:
    """Prove exact identity, uniqueness, and canonical relative order."""

    canonical = tuple(canonical_node_ids)
    canonical_counts = Counter(canonical)
    canonical_duplicates = sorted(node for node, count in canonical_counts.items() if count > 1)
    if canonical_duplicates:
        raise ManifestError(f"canonical node list contains duplicates: {canonical_duplicates}")

    assigned = tuple(node for _, nodes in lanes for node in nodes)
    assigned_counts = Counter(assigned)
    duplicates = sorted(node for node, count in assigned_counts.items() if count > 1)
    missing = sorted(set(canonical) - set(assigned))
    extra = sorted(set(assigned) - set(canonical))
    if duplicates or missing or extra:
        raise ManifestError(f"lane union is not exact: missing={missing}, extra={extra}, duplicates={duplicates}")

    for lane_name, node_ids in lanes:
        actual = tuple(node_ids)
        selected = set(actual)
        expected = tuple(node for node in canonical if node in selected)
        if actual != expected:
            raise ManifestError(f"{lane_name} node order drifted from canonical serial order")


def build_manifest_plan(canonical_node_ids: Sequence[str], shards: dict[str, ShardDefinition]) -> ManifestPlan:
    """Assign canonical nodes to the reviewed two-lane bucket grouping."""

    canonical = tuple(canonical_node_ids)
    lane_for_bucket = {bucket: lane for lane, buckets in LANE_BUCKETS for bucket in buckets}
    nodes_by_lane: dict[str, list[str]] = {lane: [] for lane, _ in LANE_BUCKETS}

    for node_id in canonical:
        test_file = _test_path(node_id.split("::", 1)[0], context="collected node")
        owners = [shard.name for shard in shards.values() if shard.selects(test_file)]
        if len(owners) != 1:
            raise ManifestError(f"collected node {node_id!r} must have exactly one shard owner; found {owners}")
        nodes_by_lane[lane_for_bucket[owners[0]]].append(node_id)

    lane_nodes = tuple((name, tuple(nodes_by_lane[name])) for name, _ in LANE_BUCKETS)
    validate_lane_partition(canonical, lane_nodes)

    lanes: list[LaneManifest] = []
    for name, nodes in lane_nodes:
        selected = set(nodes)
        arguments = tuple(f"--deselect={node}" for node in canonical if node not in selected)
        lanes.append(LaneManifest(name=name, node_ids=nodes, pytest_arguments=arguments))
    return ManifestPlan(canonical_node_ids=canonical, lanes=tuple(lanes))


def write_manifest_arguments(plan: ManifestPlan, output_dir: Path) -> tuple[Path, ...]:
    """Write one pytest ``@argsfile`` per verified lane."""

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for lane in plan.lanes:
        path = output_dir / f"{lane.name}.pytest.args"
        path.write_text("".join(f"{argument}\n" for argument in lane.pytest_arguments), encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def create_manifests(repo_root: Path, shards_path: Path, output_dir: Path) -> ManifestPlan:
    """Collect, validate, and write the complete two-lane plan."""

    shards = load_shard_definitions(shards_path)
    canonical = collect_canonical_node_ids(repo_root)
    plan = build_manifest_plan(canonical, shards)
    write_manifest_arguments(plan, output_dir)
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--shards", type=Path, default=DEFAULT_SHARDS_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    shards_path = args.shards if args.shards.is_absolute() else repo_root / args.shards
    output_dir = args.output_dir if args.output_dir.is_absolute() else repo_root / args.output_dir
    try:
        plan = create_manifests(repo_root, shards_path, output_dir)
    except ManifestError as exc:
        sys.stderr.write(f"parallel test manifest error: {exc}\n")
        return 2

    counts = ", ".join(f"{lane.name}={len(lane.node_ids)}" for lane in plan.lanes)
    sys.stdout.write(f"Verified parallel test partition: {counts}; union={len(plan.canonical_node_ids)} nodes in canonical order.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
