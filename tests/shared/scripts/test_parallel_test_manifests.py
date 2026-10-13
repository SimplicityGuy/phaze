"""Focused contracts for the two-worker ordered manifest planner."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from scripts.parallel_test_manifests import (
    LANE_BUCKETS,
    ManifestError,
    build_manifest_plan,
    collect_canonical_node_ids,
    load_shard_definitions,
    validate_lane_partition,
    write_manifest_arguments,
)


if TYPE_CHECKING:
    from collections.abc import Callable


_SHARDS = [
    {"name": "ingest-pipeline", "paths": "tests/discovery tests/analyze/services/pipeline"},
    {"name": "analyze-rest", "paths": "tests/analyze --ignore=tests/analyze/services/pipeline"},
    {"name": "review-agents", "paths": "tests/review tests/agents"},
    {
        "name": "integration-shared-services",
        "paths": "tests/integration tests/shared/cli tests/shared/config tests/shared/core tests/shared/models tests/shared/routers "
        "tests/shared/schemas tests/shared/scripts tests/shared/services tests/shared/static tests/shared/tasks tests/shared/template_helpers "
        "tests/shared/ui tests/shared/utils tests/shared/web",
    },
    {"name": "shared-registry", "paths": "tests/shared/test_redis_seat_registry.py"},
    {
        "name": "shared-root",
        "paths": "tests/shared --ignore=tests/shared/cli --ignore=tests/shared/config --ignore=tests/shared/core "
        "--ignore=tests/shared/models --ignore=tests/shared/routers --ignore=tests/shared/schemas --ignore=tests/shared/scripts "
        "--ignore=tests/shared/services --ignore=tests/shared/static --ignore=tests/shared/tasks --ignore=tests/shared/telemetry "
        "--ignore=tests/shared/template_helpers --ignore=tests/shared/ui --ignore=tests/shared/utils --ignore=tests/shared/web "
        "--ignore=tests/shared/test_redis_seat_registry.py",
    },
    {"name": "shared-telemetry", "paths": "tests/shared/telemetry"},
]

_CANONICAL = (
    "tests/agents/test_one.py::test_agents",
    "tests/analyze/services/pipeline/test_two.py::test_pipeline",
    "tests/analyze/test_three.py::test_analyze",
    "tests/integration/test_four.py::test_integration",
    "tests/review/test_five.py::test_review",
    "tests/shared/telemetry/test_six.py::test_telemetry",
    "tests/shared/test_redis_seat_registry.py::test_registry",
    "tests/shared/test_seven.py::test_shared_root",
)


def _write_shards(tmp_path: Path, shards: object = _SHARDS) -> Path:
    path = tmp_path / "ci_shards.json"
    path.write_text(json.dumps(shards), encoding="utf-8")
    return path


def test_builds_reviewed_two_lane_partition_and_deselection_args(tmp_path: Path) -> None:
    shards = load_shard_definitions(_write_shards(tmp_path))

    plan = build_manifest_plan(_CANONICAL, shards)

    assert [lane.name for lane in plan.lanes] == ["lane-a", "lane-b"]
    assert plan.lanes[0].node_ids == (_CANONICAL[0], _CANONICAL[1], _CANONICAL[4], _CANONICAL[6], _CANONICAL[7])
    assert plan.lanes[1].node_ids == (_CANONICAL[2], _CANONICAL[3], _CANONICAL[5])
    assert plan.lanes[0].pytest_arguments == tuple(f"--deselect={node}" for node in plan.lanes[1].node_ids)
    assert plan.lanes[1].pytest_arguments == tuple(f"--deselect={node}" for node in plan.lanes[0].node_ids)

    paths = write_manifest_arguments(plan, tmp_path / "args")
    assert [path.name for path in paths] == ["lane-a.pytest.args", "lane-b.pytest.args"]
    assert paths[0].read_text(encoding="utf-8").splitlines() == list(plan.lanes[0].pytest_arguments)


@pytest.mark.parametrize(
    ("lanes", "match"),
    [
        ((("lane-a", (_CANONICAL[0],)), ("lane-b", _CANONICAL[2:])), "missing"),
        ((("lane-a", (_CANONICAL[0], "tests/extra.py::test_extra")), ("lane-b", _CANONICAL[1:])), "extra"),
        ((("lane-a", (_CANONICAL[0], _CANONICAL[1])), ("lane-b", _CANONICAL[1:])), "duplicates"),
        ((("lane-a", (_CANONICAL[1], _CANONICAL[0])), ("lane-b", _CANONICAL[2:])), "order drifted"),
    ],
)
def test_rejects_invalid_partition(lanes: tuple[tuple[str, tuple[str, ...]], ...], match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        validate_lane_partition(_CANONICAL, lanes)


@pytest.mark.parametrize(
    "bad_shards",
    [
        [{"name": "ingest-pipeline", "paths": "--maxfail=1 tests/discovery"}, *_SHARDS[1:]],
        [{"name": "ingest-pipeline", "paths": "../tests"}, *_SHARDS[1:]],
        [{"name": "ingest-pipeline", "paths": ""}, *_SHARDS[1:]],
        [*_SHARDS, _SHARDS[0]],
    ],
)
def test_rejects_malformed_bucket_manifest(tmp_path: Path, bad_shards: object) -> None:
    with pytest.raises(ManifestError):
        load_shard_definitions(_write_shards(tmp_path, bad_shards))


def test_rejects_collection_failure(tmp_path: Path) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 4, stdout="", stderr="collection exploded")

    with pytest.raises(ManifestError, match="exit 4: collection exploded"):
        collect_canonical_node_ids(tmp_path, run_command=fail)


def test_collects_only_node_ids_from_successful_pytest_output(tmp_path: Path) -> None:
    def succeed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = "tests/example.py::test_one\ntests/example.py::test_two[value]\n2 tests collected in 0.01s\n"
        return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="warning")

    assert collect_canonical_node_ids(tmp_path, run_command=succeed) == (
        "tests/example.py::test_one",
        "tests/example.py::test_two[value]",
    )


def test_collection_uses_read_only_uv_and_strips_caller_pytest_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    caller_options = "-n auto -m browser --deselect=tests/example.py::test_one"
    monkeypatch.setenv("PYTEST_ADDOPTS", caller_options)
    captured: dict[str, object] = {}

    def succeed(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="tests/example.py::test_one\n", stderr="")

    assert collect_canonical_node_ids(tmp_path, run_command=succeed) == ("tests/example.py::test_one",)
    assert captured["command"] == ("uv", "run", "--no-sync", "pytest", "--collect-only", "-q")
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert "PYTEST_ADDOPTS" not in environment
    assert os.environ["PYTEST_ADDOPTS"] == caller_options


_REPO_ROOT = Path(__file__).resolve().parents[3]

#: phaze-l5uve (R4): whole-module ownership moved between CI shards, as (path, donor, receiver).
#: Every move stays inside one LANE_BUCKETS group, so the two local lanes keep the same nodes.
_R4_TRANSFERS = (
    ("tests/integration/test_migrations", "integration-shared-services", "shared-telemetry"),
    ("tests/shared/core", "integration-shared-services", "shared-telemetry"),
    ("tests/shared/routers", "integration-shared-services", "analyze-rest"),
    ("tests/shared/test_historical_evidence_audit.py", "shared-root", "shared-registry"),
)


def _real_test_file_nodes() -> tuple[str, ...]:
    """One node per real non-browser test file, in sorted path order (an ownership probe, not a collection)."""
    files = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in (_REPO_ROOT / "tests").rglob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_test.py")) and "browser" not in path.relative_to(_REPO_ROOT).parts
    )
    return tuple(f"{file}::test_probe" for file in files)


def _transferred_nodes(nodes: tuple[str, ...], transfer: str) -> list[str]:
    root = PurePosixPath(transfer)
    return [node for node in nodes if (file := PurePosixPath(node.split("::", 1)[0])) == root or root in file.parents]


def _real_shards() -> list[dict[str, str]]:
    raw = json.loads((_REPO_ROOT / "tests" / "ci_shards.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    return raw


def test_r4_transfers_have_one_receiver_owner_and_keep_their_local_lane(tmp_path: Path) -> None:
    """Positive control for the R4 placement, through the real planner and the checked-in manifest."""
    nodes = _real_test_file_nodes()
    shards = load_shard_definitions(_write_shards(tmp_path, _real_shards()))
    plan = build_manifest_plan(nodes, shards)
    lane_of = {node: lane.name for lane in plan.lanes for node in lane.node_ids}
    lane_for_bucket = {bucket: lane for lane, buckets in LANE_BUCKETS for bucket in buckets}

    for transfer, donor, receiver in _R4_TRANSFERS:
        moved = _transferred_nodes(nodes, transfer)
        assert moved, f"{transfer} owns no test file"
        assert lane_for_bucket[donor] == lane_for_bucket[receiver], f"{transfer} would change local lane"
        for node in moved:
            file = PurePosixPath(node.split("::", 1)[0])
            assert [name for name, shard in shards.items() if shard.selects(file)] == [receiver]
            assert lane_of[node] == lane_for_bucket[donor]


def _without_receiver(shards: list[dict[str, str]]) -> list[dict[str, str]]:
    path, _, receiver = _R4_TRANSFERS[1]
    return [{**s, "paths": " ".join(t for t in s["paths"].split() if t != path)} if s["name"] == receiver else s for s in shards]


def _in_two_shards(shards: list[dict[str, str]]) -> list[dict[str, str]]:
    path, donor, _ = _R4_TRANSFERS[3]
    return [{**s, "paths": s["paths"].replace(f" --ignore={path}", "")} if s["name"] == donor else s for s in shards]


def _drop_shard(shards: list[dict[str, str]]) -> list[dict[str, str]]:
    return [s for s in shards if s["name"] != _R4_TRANSFERS[0][2]]


def _duplicate_node(nodes: tuple[str, ...], lanes: list[list[str]]) -> tuple[tuple[str, ...], list[list[str]]]:
    lanes[1].append(lanes[1][0])
    return nodes, lanes


def _reorder_module(nodes: tuple[str, ...], lanes: list[list[str]]) -> tuple[tuple[str, ...], list[list[str]]]:
    first = _transferred_nodes(nodes, _R4_TRANSFERS[1][0])[0]
    module = (f"{first}[a]", f"{first}[b]")
    nodes = tuple(n for node in nodes for n in (module if node == first else (node,)))
    lane = lanes[1]
    at = lane.index(first)
    lane[at : at + 1] = [module[1], module[0]]
    return nodes, lanes


def _ambiguous_parameter_id(nodes: tuple[str, ...], lanes: list[list[str]]) -> tuple[tuple[str, ...], list[list[str]]]:
    first = _transferred_nodes(nodes, _R4_TRANSFERS[1][0])[0]
    return tuple(n for node in nodes for n in ((node, node) if node == first else (node,))), lanes


@pytest.mark.parametrize(
    ("fault", "match"),
    [
        (_without_receiver, r"exactly one shard owner; found \[\]"),
        (_in_two_shards, r"exactly one shard owner; found \['shared-registry', 'shared-root'\]"),
        (_drop_shard, r"do not match the two-lane grouping: missing=\['shared-telemetry'\]"),
        (_duplicate_node, "duplicates="),
        (_reorder_module, "lane-b node order drifted"),
        (_ambiguous_parameter_id, "canonical node list contains duplicates"),
    ],
    ids=["transfer-deleted", "transfer-in-two-shards", "shard-omitted", "duplicate-node", "module-reordered", "ambiguous-node-id"],
)
def test_r4_negative_controls_are_rejected_by_the_real_validator(tmp_path: Path, fault: Callable[..., Any], match: str) -> None:
    """Each R4 fault is refused by the planner's own validators, not by a parallel reimplementation."""
    nodes = _real_test_file_nodes()
    real = _real_shards()
    with pytest.raises(ManifestError, match=match):
        if fault in (_without_receiver, _in_two_shards, _drop_shard):
            build_manifest_plan(nodes, load_shard_definitions(_write_shards(tmp_path, fault(real))))
        else:
            plan = build_manifest_plan(nodes, load_shard_definitions(_write_shards(tmp_path, real)))
            canonical, lanes = fault(nodes, [list(lane.node_ids) for lane in plan.lanes])
            validate_lane_partition(canonical, [(lane.name, tuple(ids)) for lane, ids in zip(plan.lanes, lanes, strict=True)])


def test_rejects_nodes_without_exactly_one_bucket_owner(tmp_path: Path) -> None:
    shards = load_shard_definitions(_write_shards(tmp_path))

    with pytest.raises(ManifestError, match="exactly one shard owner"):
        build_manifest_plan(("tests/browser/test_page.py::test_page",), shards)
