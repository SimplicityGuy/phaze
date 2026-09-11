"""Focused contracts for the two-worker ordered manifest planner."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from scripts.parallel_test_manifests import (
    ManifestError,
    build_manifest_plan,
    collect_canonical_node_ids,
    load_shard_definitions,
    validate_lane_partition,
    write_manifest_arguments,
)


if TYPE_CHECKING:
    from pathlib import Path


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


def test_rejects_nodes_without_exactly_one_bucket_owner(tmp_path: Path) -> None:
    shards = load_shard_definitions(_write_shards(tmp_path))

    with pytest.raises(ManifestError, match="exactly one shard owner"):
        build_manifest_plan(("tests/browser/test_page.py::test_page",), shards)
