"""Refresh the durable Repowise index from one commit-paired GitHub CI artifact.

GitHub's ``coverage-combined`` artifact has two complementary reports: ``.coverage`` carries
per-test contexts, while ``coverage.xml`` carries per-file totals. The workflow run metadata is
the authority that pairs both reports with source. This script resolves exactly one successful
``CI`` push on ``main``, verifies the artifact's own run/SHA metadata, temporarily checks out that
SHA at the durable repository path, and only then updates and ingests into Repowise.

The temporary checkout is necessary. Repowise keys an index to the repository's absolute path, so
indexing a throwaway clone/worktree would create a different, empty repository record instead of
refreshing the durable local index. The original branch/detached commit is restored in ``finally``;
the Repowise index intentionally remains at the selected CI SHA so its source lines and coverage
stay paired.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import subprocess  # nosec B404
import sys
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Protocol

from defusedxml import ElementTree


# Every subprocess call uses an argv list with shell=False; user input is numeric-validated.
if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


_ARTIFACT_NAME = "coverage-combined"
_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_RUN_ID = re.compile(r"[1-9][0-9]*")


class IngestError(RuntimeError):
    """A fail-closed validation error with an actionable user-facing message."""


class CommandRunner(Protocol):
    """Small command surface that keeps selection/orchestration directly unit-testable."""

    def capture(self, args: Sequence[str]) -> str:
        """Run ``args`` and return stdout, raising on a non-zero exit."""

    def run(self, args: Sequence[str]) -> None:
        """Run ``args`` with output attached to the terminal, raising on failure."""

    def succeeds(self, args: Sequence[str]) -> bool:
        """Return whether ``args`` exits successfully without printing output."""


class SubprocessRunner:
    """Shell-free subprocess adapter rooted at the durable repository path."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def capture(self, args: Sequence[str]) -> str:
        # This boundary is only called by this module's fixed command builders; no shell is used.
        result = subprocess.run(  # noqa: S603  # nosec B603
            list(args),
            cwd=self.root,
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def run(self, args: Sequence[str]) -> None:
        # This boundary is only called by this module's fixed command builders; no shell is used.
        subprocess.run(  # noqa: S603  # nosec B603
            list(args), cwd=self.root, check=True
        )

    def succeeds(self, args: Sequence[str]) -> bool:
        # This boundary is only called by this module's fixed command builders; no shell is used.
        result = subprocess.run(  # noqa: S603  # nosec B603
            list(args),
            cwd=self.root,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0


@dataclass(frozen=True)
class WorkflowRun:
    """The authoritative GitHub Actions run metadata used for the entire refresh."""

    run_id: int
    head_sha: str
    head_branch: str
    event: str
    status: str
    conclusion: str
    workflow_id: int
    workflow_name: str
    url: str


@dataclass(frozen=True)
class CoverageArtifact:
    """GitHub's metadata binding one named artifact to its workflow run and commit."""

    artifact_id: int
    run_id: int
    head_sha: str
    head_branch: str
    expired: bool
    size_bytes: int


@dataclass(frozen=True)
class OriginalCheckout:
    """The exact checkout identity restored after the temporary detached checkout."""

    head_sha: str
    branch: str | None


def _parse_json(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise IngestError(f"{label} returned invalid JSON: {error}") from error


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IngestError(f"{label} must be a JSON object, got {type(value).__name__}")
    return value


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IngestError(f"{label} must be an integer")
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise IngestError(f"{label} must be a non-empty string")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise IngestError(f"{label} must be a string")
    return value


def resolve_ci_workflow_id(runner: CommandRunner) -> int:
    """Resolve the reusable CI entrypoint by its tracked workflow path, not its mutable name."""
    raw = runner.capture(["gh", "workflow", "list", "--all", "--limit", "100", "--json", "id,path,state"])
    workflows = _parse_json(raw, "`gh workflow list`")
    if not isinstance(workflows, list):
        raise IngestError("`gh workflow list` must return a JSON array")
    matches = [item for item in workflows if isinstance(item, dict) and item.get("path") == _CI_WORKFLOW_PATH]
    if len(matches) != 1:
        raise IngestError(f"expected exactly one GitHub workflow at {_CI_WORKFLOW_PATH}, found {len(matches)}")
    if matches[0].get("state") != "active":
        raise IngestError(f"GitHub workflow {_CI_WORKFLOW_PATH} is not active")
    return _require_int(matches[0].get("id"), "CI workflow id")


def _workflow_run_from_json(value: Any) -> WorkflowRun:
    item = _require_dict(value, "workflow run")
    return WorkflowRun(
        run_id=_require_int(item.get("databaseId"), "workflow run databaseId"),
        head_sha=_require_str(item.get("headSha"), "workflow run headSha"),
        head_branch=_require_str(item.get("headBranch"), "workflow run headBranch"),
        event=_require_str(item.get("event"), "workflow run event"),
        status=_require_str(item.get("status"), "workflow run status"),
        conclusion=_require_string(item.get("conclusion"), "workflow run conclusion"),
        workflow_id=_require_int(item.get("workflowDatabaseId"), "workflow run workflowDatabaseId"),
        workflow_name=_require_str(item.get("workflowName"), "workflow run workflowName"),
        url=_require_str(item.get("url"), "workflow run url"),
    )


def validate_workflow_run(run: WorkflowRun, expected_workflow_id: int) -> None:
    """Reject any run that cannot be trusted as a successful main CI source."""
    failures: list[str] = []
    if run.workflow_id != expected_workflow_id:
        failures.append(f"workflow id is {run.workflow_id}, expected CI workflow {expected_workflow_id}")
    if run.head_branch != "main":
        failures.append(f"head branch is {run.head_branch!r}, expected 'main'")
    if run.event != "push":
        failures.append(f"event is {run.event!r}, expected 'push'")
    if run.status != "completed":
        failures.append(f"status is {run.status!r}, expected 'completed'")
    if run.conclusion != "success":
        failures.append(f"conclusion is {run.conclusion!r}, expected 'success'")
    if _FULL_SHA.fullmatch(run.head_sha) is None:
        failures.append(f"head SHA is not a full lowercase Git SHA: {run.head_sha!r}")
    if failures:
        details = "\n  - ".join(failures)
        raise IngestError(f"workflow run {run.run_id} is not eligible:\n  - {details}")


def resolve_workflow_run(runner: CommandRunner, requested_run_id: str, expected_workflow_id: int) -> WorkflowRun:
    """Resolve either an explicit run ID or the newest successful main CI push."""
    fields = "databaseId,headSha,headBranch,event,status,conclusion,workflowDatabaseId,workflowName,url"
    if requested_run_id:
        if _RUN_ID.fullmatch(requested_run_id) is None:
            raise IngestError("the optional run ID must be a positive numeric GitHub Actions run ID")
        raw = runner.capture(["gh", "run", "view", requested_run_id, "--json", fields])
        run = _workflow_run_from_json(_parse_json(raw, f"GitHub run {requested_run_id}"))
        if run.run_id != int(requested_run_id):
            raise IngestError(f"requested run {requested_run_id}, but GitHub returned run {run.run_id}")
    else:
        raw = runner.capture(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "ci.yml",
                "--branch",
                "main",
                "--event",
                "push",
                "--status",
                "success",
                "--limit",
                "1",
                "--json",
                fields,
            ]
        )
        runs = _parse_json(raw, "latest successful main CI run lookup")
        if not isinstance(runs, list) or len(runs) != 1:
            count = len(runs) if isinstance(runs, list) else 0
            raise IngestError(f"expected one latest successful CI push run on main, found {count}")
        run = _workflow_run_from_json(runs[0])
    validate_workflow_run(run, expected_workflow_id)
    return run


def _coverage_artifact_from_json(value: Any) -> CoverageArtifact:
    item = _require_dict(value, "coverage artifact")
    workflow_run = _require_dict(item.get("workflow_run"), "coverage artifact workflow_run")
    expired = item.get("expired")
    if not isinstance(expired, bool):
        raise IngestError("coverage artifact expired must be a boolean")
    return CoverageArtifact(
        artifact_id=_require_int(item.get("id"), "coverage artifact id"),
        run_id=_require_int(workflow_run.get("id"), "coverage artifact workflow run id"),
        head_sha=_require_str(workflow_run.get("head_sha"), "coverage artifact head SHA"),
        head_branch=_require_str(workflow_run.get("head_branch"), "coverage artifact head branch"),
        expired=expired,
        size_bytes=_require_int(item.get("size_in_bytes"), "coverage artifact size"),
    )


def resolve_coverage_artifact(runner: CommandRunner, repository: str, run: WorkflowRun) -> CoverageArtifact:
    """Read artifact metadata from the selected run and prove its commit pairing."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise IngestError(f"GitHub returned an invalid repository name: {repository!r}")
    endpoint = f"repos/{repository}/actions/runs/{run.run_id}/artifacts?name={_ARTIFACT_NAME}&per_page=100"
    payload = _require_dict(_parse_json(runner.capture(["gh", "api", endpoint]), "workflow artifact lookup"), "artifact response")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise IngestError("workflow artifact lookup has no artifacts array")
    named = [item for item in artifacts if isinstance(item, dict) and item.get("name") == _ARTIFACT_NAME]
    if len(named) != 1:
        raise IngestError(f"run {run.run_id} must have exactly one {_ARTIFACT_NAME!r} artifact; found {len(named)}")
    artifact = _coverage_artifact_from_json(named[0])
    failures: list[str] = []
    if artifact.expired:
        failures.append("artifact has expired")
    if artifact.size_bytes <= 0:
        failures.append("artifact is empty")
    if artifact.run_id != run.run_id:
        failures.append(f"artifact run id {artifact.run_id} does not match selected run {run.run_id}")
    if artifact.head_sha != run.head_sha:
        failures.append(f"artifact SHA {artifact.head_sha} does not match selected run SHA {run.head_sha}")
    if artifact.head_branch != run.head_branch:
        failures.append(f"artifact branch {artifact.head_branch!r} does not match run branch {run.head_branch!r}")
    if failures:
        details = "\n  - ".join(failures)
        raise IngestError(f"coverage artifact {artifact.artifact_id} is not eligible:\n  - {details}")
    return artifact


def validate_downloaded_reports(directory: Path) -> tuple[Path, Path]:
    """Require both non-empty report formats before mutating the Repowise index."""
    binary = directory / ".coverage"
    cobertura = directory / "coverage.xml"
    for report in (binary, cobertura):
        if report.is_symlink() or not report.is_file() or report.stat().st_size == 0:
            raise IngestError(f"downloaded artifact is missing a non-empty regular file: {report.name}")
    with binary.open("rb") as stream:
        sqlite_header = stream.read(16)
    if sqlite_header != b"SQLite format 3\x00":
        raise IngestError("downloaded .coverage is not a coverage.py SQLite data file")
    try:
        root = ElementTree.parse(cobertura).getroot()
    except ElementTree.ParseError as error:
        raise IngestError(f"downloaded coverage.xml is malformed: {error}") from error
    if root is None or root.tag != "coverage" or not root.findall(".//class[@filename]"):
        raise IngestError("downloaded coverage.xml has no Cobertura class/file entries")
    return binary, cobertura


def download_coverage_reports(runner: CommandRunner, run_id: int, directory: Path) -> tuple[Path, Path]:
    """Download the named artifact from the already-validated exact run ID."""
    runner.run(
        [
            "gh",
            "run",
            "download",
            str(run_id),
            "--name",
            _ARTIFACT_NAME,
            "--dir",
            str(directory),
        ]
    )
    return validate_downloaded_reports(directory)


def assert_clean_worktree(runner: CommandRunner) -> None:
    """Refuse tracked or untracked dirt: a checkout must never overwrite user work."""
    dirty = runner.capture(["git", "status", "--porcelain", "--untracked-files=all"])
    if dirty:
        raise IngestError(
            "refusing to change commits because the durable checkout is dirty; commit, stash, or remove these files first:\n"
            + "\n".join(f"  {line}" for line in dirty.splitlines())
        )


def ensure_commit_available(runner: CommandRunner, sha: str) -> None:
    """Fetch only when the run commit is not already in the local object database."""
    object_name = f"{sha}^{{commit}}"
    if not runner.succeeds(["git", "cat-file", "-e", object_name]):
        runner.run(["git", "fetch", "--no-tags", "origin", sha])
    if not runner.succeeds(["git", "cat-file", "-e", object_name]):
        raise IngestError(f"GitHub run commit {sha} is not available after fetching it from origin")


def _current_checkout(runner: CommandRunner) -> OriginalCheckout:
    head = runner.capture(["git", "rev-parse", "HEAD"])
    branch = (
        runner.capture(["git", "symbolic-ref", "--quiet", "--short", "HEAD"]) if runner.succeeds(["git", "symbolic-ref", "--quiet", "HEAD"]) else None
    )
    return OriginalCheckout(head_sha=head, branch=branch)


@contextmanager
def checkout_at_run(runner: CommandRunner, target_sha: str) -> Iterator[OriginalCheckout]:
    """Temporarily detach at ``target_sha`` and restore the exact original checkout identity."""
    original = _current_checkout(runner)
    switched = original.head_sha != target_sha
    if switched:
        runner.run(["git", "switch", "--detach", "--quiet", target_sha])
    try:
        actual = runner.capture(["git", "rev-parse", "HEAD"])
        if actual != target_sha:
            raise IngestError(f"temporary checkout is at {actual}, expected workflow SHA {target_sha}")
        yield original
    finally:
        if switched:
            restore_target = original.branch if original.branch is not None else original.head_sha
            restore_args = ["git", "switch", "--quiet", restore_target]
            if original.branch is None:
                restore_args.insert(2, "--detach")
            try:
                runner.run(restore_args)
            except (OSError, subprocess.CalledProcessError) as error:
                raise IngestError(f"could not restore the original checkout; recover with `git switch {restore_target}`") from error


def update_and_ingest(
    runner: CommandRunner,
    binary_report: Path,
    cobertura_report: Path,
    expected_sha: str,
    status_directory: Path,
) -> None:
    """Update and ingest both report halves, then reuse the existing fail-closed gate."""
    started_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    _out(f"1/4 🔁 repowise update at {expected_sha[:12]}")
    runner.run(["repowise", "update", "--no-workspace", "--no-agents"])
    actual = runner.capture(["git", "rev-parse", "HEAD"])
    if actual != expected_sha:
        raise IngestError(f"HEAD moved during repowise update: expected {expected_sha}, found {actual}")

    _out("2/4 📥 ingest .coverage (per-test contexts)")
    runner.run(["repowise", "coverage", "add", str(binary_report)])
    _out("3/4 📥 ingest coverage.xml (per-file totals)")
    runner.run(["repowise", "coverage", "add", str(cobertura_report)])
    _out("4/4 🩺 fold coverage into Repowise health")
    runner.run(["repowise", "health"])

    actual = runner.capture(["git", "rev-parse", "HEAD"])
    if actual != expected_sha:
        raise IngestError(f"HEAD moved during coverage ingestion: expected {expected_sha}, found {actual}")
    coverage_status = status_directory / "coverage-status.json"
    index_status = status_directory / "index-status.json"
    coverage_status.write_text(runner.capture(["repowise", "coverage", "status", "--format", "json"]), encoding="utf-8")
    index_status.write_text(runner.capture(["repowise", "status", "--no-workspace", "--format", "json"]), encoding="utf-8")
    runner.run(
        [
            "uv",
            "run",
            "python",
            "scripts/repowise_coverage_gate.py",
            "--coverage-status",
            str(coverage_status),
            "--index-status",
            str(index_status),
            "--coverage-xml",
            str(cobertura_report),
            "--started-at",
            started_at,
            "--expected-commit",
            expected_sha,
        ]
    )


def _repository_root() -> Path:
    git = shutil.which("git")
    if git is None:
        raise IngestError("required command not found on PATH: git")
    # The executable is resolved locally and the argv is fixed; no shell is used.
    result = subprocess.run(  # noqa: S603  # nosec B603
        [git, "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    )
    return Path(result.stdout.strip()).resolve()


def _check_prerequisites(root: Path) -> None:
    missing = [command for command in ("gh", "git", "repowise", "uv") if shutil.which(command) is None]
    if missing:
        raise IngestError(f"required command(s) not found on PATH: {', '.join(missing)}")
    if not (root / ".repowise").is_dir():
        raise IngestError(f"no durable .repowise index exists in {root}; run this recipe from the durable checkout after `repowise init`")


def _out(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _err(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def run(requested_run_id: str) -> None:
    """Execute one fully paired CI-to-Repowise refresh."""
    root = _repository_root()
    _check_prerequisites(root)
    runner = SubprocessRunner(root)
    assert_clean_worktree(runner)

    workflow_id = resolve_ci_workflow_id(runner)
    workflow_run = resolve_workflow_run(runner, requested_run_id, workflow_id)
    repository = runner.capture(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    artifact = resolve_coverage_artifact(runner, repository, workflow_run)
    _out(f"✅ selected run {workflow_run.run_id}: {workflow_run.head_sha}")
    _out(f"   {workflow_run.url}")
    _out(f"   artifact {artifact.artifact_id}: {artifact.size_bytes} bytes")

    ensure_commit_available(runner, workflow_run.head_sha)
    with TemporaryDirectory(prefix="phaze-repowise-ci-") as temporary:
        temp = Path(temporary)
        artifact_directory = temp / "artifact"
        artifact_directory.mkdir()
        binary_report, cobertura_report = download_coverage_reports(runner, workflow_run.run_id, artifact_directory)

        _out(f"🔒 temporarily indexing the exact workflow SHA {workflow_run.head_sha}")
        with checkout_at_run(runner, workflow_run.head_sha) as original:
            update_and_ingest(runner, binary_report, cobertura_report, workflow_run.head_sha, temp)
            assert_clean_worktree(runner)

    restored = _current_checkout(runner)
    _out(f"✅ Repowise index and coverage are paired at {workflow_run.head_sha}")
    _out(f"↩️  checkout restored to {restored.branch or 'detached HEAD'} at {restored.head_sha}")
    if restored.head_sha != original.head_sha or restored.branch != original.branch:
        raise IngestError("the checkout did not restore to the exact branch and commit that started the refresh")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the durable Repowise index and coverage from one successful main CI workflow run.")
    parser.add_argument(
        "run_id",
        nargs="?",
        default="",
        help="numeric GitHub Actions run ID (default: newest successful CI push on main)",
    )
    args = parser.parse_args(argv)
    try:
        run(args.run_id)
    except IngestError as error:
        _err(f"❌ {error}")
        return 1
    except subprocess.CalledProcessError as error:
        command = " ".join(str(part) for part in error.cmd)
        if error.stderr:
            _err(error.stderr.rstrip())
        _err(f"❌ command failed with exit {error.returncode}: {command}")
        return 1
    except KeyboardInterrupt:
        _err("❌ interrupted; attempted to restore the original checkout")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
