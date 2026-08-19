"""Guards for the commit-paired GitHub CI -> Repowise coverage refresh."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from types import ModuleType


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "repowise_coverage_from_ci.py"
_JUSTFILE = _REPO_ROOT / "justfile"
_SHA = "a" * 40


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("repowise_coverage_from_ci", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "databaseId": 1234,
        "headSha": _SHA,
        "headBranch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "workflowDatabaseId": 99,
        "workflowName": "CI",
        "url": "https://github.example/actions/runs/1234",
    }
    payload.update(overrides)
    return payload


class FakeRunner:
    def __init__(self, captures: list[str]) -> None:
        self.captures = iter(captures)
        self.commands: list[list[str]] = []

    def capture(self, args: list[str]) -> str:
        self.commands.append(list(args))
        return next(self.captures)

    def run(self, args: list[str]) -> None:
        self.commands.append(list(args))

    def succeeds(self, args: list[str]) -> bool:
        self.commands.append(list(args))
        return True


def test_default_selection_is_latest_successful_main_ci_run() -> None:
    module = _load_module()
    runner = FakeRunner([json.dumps([_run_payload()])])

    selected = module.resolve_workflow_run(runner, "", 99)

    assert selected.run_id == 1234
    command = runner.commands[0]
    assert command[:3] == ["gh", "run", "list"]
    assert command[command.index("--workflow") : command.index("--workflow") + 2] == ["--workflow", "ci.yml"]
    assert command[command.index("--branch") : command.index("--branch") + 2] == ["--branch", "main"]
    assert command[command.index("--event") : command.index("--event") + 2] == ["--event", "push"]
    assert command[command.index("--status") : command.index("--status") + 2] == ["--status", "success"]
    assert command[command.index("--limit") : command.index("--limit") + 2] == ["--limit", "1"]


def test_explicit_selection_uses_exact_numeric_run_id() -> None:
    module = _load_module()
    runner = FakeRunner([json.dumps(_run_payload())])

    selected = module.resolve_workflow_run(runner, "1234", 99)

    assert selected.run_id == 1234
    assert runner.commands[0][:4] == ["gh", "run", "view", "1234"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"headBranch": "feature"}, "expected 'main'"),
        ({"event": "pull_request"}, "expected 'push'"),
        ({"status": "in_progress", "conclusion": ""}, "expected 'completed'"),
        ({"conclusion": "failure"}, "expected 'success'"),
        ({"workflowDatabaseId": 100}, "expected CI workflow 99"),
    ],
)
def test_ineligible_runs_fail_closed(overrides: dict[str, Any], message: str) -> None:
    module = _load_module()
    runner = FakeRunner([json.dumps(_run_payload(**overrides))])

    with pytest.raises(module.IngestError, match=message):
        module.resolve_workflow_run(runner, "1234", 99)


def test_non_numeric_explicit_run_id_is_rejected_before_gh() -> None:
    module = _load_module()
    runner = FakeRunner([])

    with pytest.raises(module.IngestError, match="positive numeric"):
        module.resolve_workflow_run(runner, "latest", 99)
    assert runner.commands == []


def _artifact_payload(**workflow_overrides: Any) -> dict[str, Any]:
    workflow = {"id": 1234, "head_sha": _SHA, "head_branch": "main"}
    workflow.update(workflow_overrides)
    return {
        "total_count": 1,
        "artifacts": [
            {
                "id": 5678,
                "name": "coverage-combined",
                "expired": False,
                "size_in_bytes": 100,
                "workflow_run": workflow,
            }
        ],
    }


def test_artifact_metadata_must_match_the_selected_run_sha() -> None:
    module = _load_module()
    run = module._workflow_run_from_json(_run_payload())
    runner = FakeRunner([json.dumps(_artifact_payload(head_sha="b" * 40))])

    with pytest.raises(module.IngestError, match="does not match selected run SHA"):
        module.resolve_coverage_artifact(runner, "owner/repo", run)


@pytest.mark.parametrize(
    "payload",
    [
        {"total_count": 0, "artifacts": []},
        {"total_count": 2, "artifacts": _artifact_payload()["artifacts"] * 2},
    ],
)
def test_missing_or_duplicate_combined_artifact_is_rejected(payload: dict[str, Any]) -> None:
    module = _load_module()
    run = module._workflow_run_from_json(_run_payload())
    runner = FakeRunner([json.dumps(payload)])

    with pytest.raises(module.IngestError, match="exactly one"):
        module.resolve_coverage_artifact(runner, "owner/repo", run)


def test_dirty_worktree_is_rejected() -> None:
    module = _load_module()
    runner = FakeRunner([" M justfile\n?? local-notes.txt"])

    with pytest.raises(module.IngestError, match="durable checkout is dirty"):
        module.assert_clean_worktree(runner)


def test_report_download_uses_the_exact_selected_run_and_requires_both_files(tmp_path: Path) -> None:
    module = _load_module()
    runner = FakeRunner([])

    with pytest.raises(module.IngestError, match="missing a non-empty regular file"):
        module.download_coverage_reports(runner, 1234, tmp_path)

    assert runner.commands == [["gh", "run", "download", "1234", "--name", "coverage-combined", "--dir", str(tmp_path)]]


def test_downloaded_reports_require_context_data_and_cobertura_file_entries(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / ".coverage").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "coverage.xml").write_text(
        '<coverage><packages><package><classes><class filename="src/phaze/example.py"/></classes></package></packages></coverage>',
        encoding="utf-8",
    )

    binary, cobertura = module.validate_downloaded_reports(tmp_path)

    assert binary.name == ".coverage"
    assert cobertura.name == "coverage.xml"


def _git(directory: Path, *args: str) -> str:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603
        [git, *args],
        cwd=directory,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def test_temporary_checkout_restores_branch_and_head_after_failure(tmp_path: Path) -> None:
    module = _load_module()
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "test: first")
    first = _git(tmp_path, "rev-parse", "HEAD")
    tracked.write_text("two\n", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "test: second")
    second = _git(tmp_path, "rev-parse", "HEAD")
    runner = module.SubprocessRunner(tmp_path)

    with pytest.raises(RuntimeError, match="boom"), module.checkout_at_run(runner, first):
        assert _git(tmp_path, "rev-parse", "HEAD") == first
        raise RuntimeError("boom")

    assert _git(tmp_path, "branch", "--show-current") == "main"
    assert _git(tmp_path, "rev-parse", "HEAD") == second
    assert tracked.read_text(encoding="utf-8") == "two\n"


def test_update_and_ingest_wires_one_sha_to_both_reports_and_gate(tmp_path: Path) -> None:
    module = _load_module()
    binary = tmp_path / ".coverage"
    cobertura = tmp_path / "coverage.xml"
    runner = FakeRunner([_SHA, _SHA, "{}", "{}"])

    module.update_and_ingest(runner, binary, cobertura, _SHA, tmp_path)

    assert ["repowise", "update", "--no-workspace", "--no-agents"] in runner.commands
    assert ["repowise", "coverage", "add", str(binary)] in runner.commands
    assert ["repowise", "coverage", "add", str(cobertura)] in runner.commands
    gate = next(command for command in runner.commands if command[:4] == ["uv", "run", "python", "scripts/repowise_coverage_gate.py"])
    assert gate[gate.index("--expected-commit") + 1] == _SHA
    assert gate[gate.index("--coverage-xml") + 1] == str(cobertura)


def test_just_recipe_documents_default_and_explicit_run_wiring() -> None:
    justfile = _JUSTFILE.read_text(encoding="utf-8")

    assert 'repowise-coverage-ci run_id="":' in justfile
    assert 'uv run python scripts/repowise_coverage_from_ci.py "{{run_id}}"' in justfile
    assert "newest successful `CI` push on main" in justfile
