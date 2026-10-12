"""Failure-safe canonical evidence without executing a second suite."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest
import yaml

from scripts import canonical_collection_receipt as receipt
from scripts.parallel_test_manifests import COLLECT_COMMAND


ROOT = Path(__file__).resolve().parents[3]
NODES = (
    "tests/shared/test_redis_seat_registry.py::test_z[" + "large-parameter-" * 200 + "]",
    "tests/shared/test_redis_seat_registry.py::test_a[1]",
    "tests/agents/test_synthetic.py::test_agent",
)


def _capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes, stderr: bytes = b"", code: int = 0) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(receipt, "source_runtime_bindings", lambda _root: {"commit": "synthetic"})
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n auto tests/browser")
    calls = []

    def run(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    output = tmp_path / "artifact"
    result = receipt.capture_receipt(ROOT, output, run_command=run)
    assert len(calls) == 1
    assert calls[0][0] == COLLECT_COMMAND == ("uv", "run", "--no-sync", "pytest", "--collect-only", "-q")
    assert "PYTEST_ADDOPTS" not in calls[0][1]["env"]
    assert calls[0][1]["cwd"] == ROOT
    assert (output / "stdout.txt").read_bytes() == stdout
    assert (output / "stderr.txt").read_bytes() == stderr
    digests = json.loads((output / "sha256.json").read_text())
    assert digests["stdout.txt"] == hashlib.sha256(stdout).hexdigest()
    return result, json.loads((output / "receipt.json").read_text())


def test_exact_order_and_giant_ids_survive_receipt_and_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, saved = _capture(tmp_path, monkeypatch, ("\r\n".join(NODES) + "\r\n3 tests collected\r\n").encode())
    assert code == 0 and saved["complete"]
    assert saved["node_ids"] == list(NODES)
    assert list(saved["multiplicities"].items()) == [(node, 1) for node in NODES]
    assert saved["lanes"][0]["node_ids"] == list(NODES)
    assert saved["lanes"][1]["pytest_arguments"] == [f"--deselect={node}" for node in NODES]
    assert NODES[0] not in capsys.readouterr().out


@pytest.mark.parametrize("code", [1, 2, 5])
def test_actual_failure_exit_and_raw_partial_collection_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    result, saved = _capture(tmp_path, monkeypatch, (NODES[0] + "\n").encode(), b"collection failed\r\n", code)
    assert result == saved["exit"] == saved["collection"]["exit"] == code
    assert not saved["complete"]
    assert saved["node_ids"] == [NODES[0]]


def test_duplicate_is_explicit_failure_without_deduplication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, saved = _capture(tmp_path, monkeypatch, (NODES[0] + "\n" + NODES[0] + "\n").encode())
    assert result != 0 and not saved["complete"]
    assert saved["node_ids"] == [NODES[0], NODES[0]]
    assert saved["multiplicities"][NODES[0]] == 2
    assert "duplicate" in saved["error"]


def test_empty_collection_cannot_produce_complete_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, saved = _capture(tmp_path, monkeypatch, b"no tests collected\n")
    assert result != 0 and not saved["complete"]


def test_workflow_receipt_runs_once_in_existing_serial_leg_after_preparation() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    jobs = workflow["jobs"]
    captures = [
        (job_name, index, step)
        for job_name, job in jobs.items()
        for index, step in enumerate(job["steps"])
        if "scripts.canonical_collection_receipt" in step.get("run", "")
    ]
    assert len(captures) == 1
    job, index, capture = captures[0]
    assert job == "test"
    assert capture["if"] == "${{ matrix.bucket.name == 'shared-registry' }}"
    assert capture["continue-on-error"] is True
    guard = next(step for step in jobs[job]["steps"] if step["name"] == "🧾 Require complete canonical collection")
    assert guard["if"] == "${{ !cancelled() && matrix.bucket.name == 'shared-registry' && steps.canonical-collection.outcome != 'success' }}"
    assert "exit 1" in guard["run"]
    assert capture["id"] == "canonical-collection"
    assert capture["run"] == "uv run --no-sync python -m scripts.canonical_collection_receipt"
    steps = jobs[job]["steps"]
    assert index > next(i for i, step in enumerate(steps) if step.get("run") == "just setup")
    assert index > next(i for i, step in enumerate(steps) if "essentia capability" in step["name"])
    run = next(step for step in steps if step["name"] == "🧪 Run shard tests with coverage")
    assert steps.index(run) > index
    assert run["run"] == 'just test-bucket ${{ matrix.bucket.name }} "${{ matrix.bucket.paths }}"'
    uploads = [step for step in steps if step.get("with", {}).get("name") == "canonical-collection"]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ !cancelled() && matrix.bucket.name == 'shared-registry' }}"
    assert uploads[0]["with"]["if-no-files-found"] == "error"
    assert next(step for step in steps if step.get("with", {}).get("name") == "junit-${{ matrix.bucket.name }}")["with"]["path"] == "junit.xml"
    assert "-n" not in run["run"]


def test_import_drift_is_blocking_but_collection_streams_are_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def drift(_root: Path) -> dict[str, Any]:
        raise receipt.ManifestError("phaze import does not bind to the captured checkout")

    monkeypatch.setattr(receipt, "source_runtime_bindings", drift)
    result = receipt.capture_receipt(
        ROOT, tmp_path, run_command=lambda *args, **_kwargs: subprocess.CompletedProcess(args[0], 0, ("\n".join(NODES) + "\n").encode(), b"")
    )
    saved = json.loads((tmp_path / "receipt.json").read_text())
    assert result != 0 and not saved["complete"]
    assert "import" in saved["binding_error"]
    assert saved["collection"]["exit"] == 0
    assert (tmp_path / "stdout.txt").read_bytes() == ("\n".join(NODES) + "\n").encode()


def test_source_stamp_hashes_tracked_inputs_and_refuses_foreign_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from scripts.parallel_test_manifests import ManifestError

    source = tmp_path / "src/phaze/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text('"""Synthetic checkout."""\n')
    monkeypatch.setattr(receipt, "_git", lambda _root, *_args: "src/phaze/__init__.py\0")
    monkeypatch.setattr(receipt.importlib.util, "find_spec", lambda _name: SimpleNamespace(origin=str(source)))
    monkeypatch.setattr(receipt.importlib, "import_module", lambda _name: SimpleNamespace(__file__=str(source)))
    monkeypatch.setattr(receipt.shutil, "which", lambda _name: None)
    saved = receipt.source_runtime_bindings(tmp_path)
    assert saved["tracked_sha256"]["src/phaze/__init__.py"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert saved["imports"]["phaze"]["loaded_origin"] == str(source)
    assert saved["pytest_plugins"]
    assert saved["runtime"]["python"]
    monkeypatch.setattr(receipt.importlib, "import_module", lambda _name: SimpleNamespace(__file__=str(tmp_path / "foreign/__init__.py")))
    with pytest.raises(ManifestError, match="import does not bind"):
        receipt.source_runtime_bindings(tmp_path)


def test_subprocess_start_failure_retains_attempt_and_incomplete_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(receipt, "source_runtime_bindings", lambda _root: {"commit": "synthetic"})

    def cannot_start(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("uv executable unavailable")

    result = receipt.capture_receipt(ROOT, tmp_path, run_command=cannot_start)
    saved = json.loads((tmp_path / "receipt.json").read_text())
    assert result != 0 and not saved["complete"]
    assert saved["collection"]["argv"] == list(COLLECT_COMMAND)
    assert saved["collection"]["exit"] is None
    assert "unavailable" in saved["error"]
    assert saved["bindings"]["commit"] == "synthetic"
