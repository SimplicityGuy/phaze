"""Unit tests for `scripts/stamp_coverage_scope.py` (phaze-vad6y).

CLAUDE.md documented `just branch-check` as "free after any `check`", but nothing recorded which
TREE a full-suite `coverage.json` was measured against, so `scripts/branch_coverage_check.py` had
no way to tell a same-tree report from a stale one and `just branch-check`'s recipe never even
tried to reuse one -- it unconditionally re-ran real tests on every invocation regardless (a
cheaper selected subset, or a full-suite escalation), and every invocation the 2026-09-22/23
dispatch actually recorded escalated, costing a genuine ~20-24 minutes each time. This script is
the missing half of the fix: it stamps `coverage.json` with the tree it was produced against, so
`find_reusable_report` (`scripts/branch_coverage_check.py`) can tell a same-tree report from a
stale one. See `tests/shared/test_branch_coverage_check.py` for the consumer side, including a
round-trip test that runs this script's REAL output through that consumer.

TWO-PHASE, TOKENED DESIGN. Reading HEAD only at the end of a multi-minute run cannot tell a commit
that landed mid-run, or a `git stash` that made a dirty tree look clean by the time the script
runs, from a genuinely trustworthy measurement -- see the module docstring's worked scenarios. So
the script runs as `--record-start` (before the coverage-producing step, printing a per-run TOKEN
to stdout) then `--expect-token TOKEN` (after). The token exists because the marker file is ONE
shared resource: a second, concurrent `--record-start` can overwrite it before the first run's own
stamp call reads it back, and the token is how that run detects "this is not the marker I wrote"
rather than trusting whatever is sitting there (see the interleaving test below).

Most tests here monkeypatch `_head`/`_dirty` so no real git repository is needed; one test at the
end drives a REAL git repository end to end, including the specific claim that a gitignored
`coverage.json` (the artifact every stamped producer writes into the very tree it is measuring)
does not itself make `_dirty()` see a change.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from types import ModuleType

    import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "stamp_coverage_scope.py"


def _load() -> ModuleType:
    assert _SCRIPT.is_file(), f"stamp script missing: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("stamp_coverage_scope", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_report(path: Path, *, branch_data: bool = True) -> None:
    totals: dict[str, float] = {"num_statements": 100, "percent_statements_covered": 100.0}
    if branch_data:
        totals |= {"num_branches": 10, "covered_branches": 10, "percent_branches_covered": 100.0}
    path.write_text(json.dumps({"files": {}, "totals": totals}), encoding="utf-8")


def _record_start(
    module: ModuleType, marker: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *, head: str, dirty: bool = False
) -> str:
    """Run `--record-start` with `_head`/`_dirty` pinned, returning the printed token."""
    monkeypatch.setattr(module, "_head", lambda: head)
    monkeypatch.setattr(module, "_dirty", lambda: dirty)
    rc = module.main(["--record-start", "--start-marker", str(marker)])
    assert rc == 0
    return capsys.readouterr().out.strip()


def _stamp(module: ModuleType, report: Path, marker: Path, token: str | None) -> int:
    args = ["--report", str(report), "--start-marker", str(marker)]
    if token is not None:
        args += ["--expect-token", token]
    return module.main(args)


def test_a_missing_report_is_a_silent_no_op(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A coverage-producing recipe that failed before writing anything has nothing to stamp --
    stamping is not itself a gate, so this must not raise or exit non-zero. The marker is still
    consumed so it cannot leak into an unrelated later run.
    """
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    token = _record_start(module, marker, monkeypatch, capsys, head="deadbeef")

    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    assert _stamp(module, report, marker, token) == 0
    assert not report.exists()
    assert "not found" in capsys.readouterr().err
    assert not marker.exists()


def test_invalid_json_is_left_untouched(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    report.write_text("not json", encoding="utf-8")
    token = _record_start(module, marker, monkeypatch, capsys, head="deadbeef")

    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    assert _stamp(module, report, marker, token) == 0
    assert report.read_text(encoding="utf-8") == "not json"


def test_a_report_without_branch_data_is_left_unstamped(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """An unstamped report reads as unusable to `find_reusable_report` -- degrading to "run the
    tests", never to a false reuse -- so a branch-less report must not be stamped as if it were
    a trustworthy full-suite artifact.
    """
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    _write_report(report, branch_data=False)
    token = _record_start(module, marker, monkeypatch, capsys, head="deadbeef")

    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    assert _stamp(module, report, marker, token) == 0
    assert "_phaze_scope" not in json.loads(report.read_text(encoding="utf-8"))


def test_no_start_marker_leaves_the_report_unstamped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--record-start` was never called first -- e.g. `just test-cov` run directly outside
    `test-validate`'s bracket. Degrades safely to unstamped, never to a false stamp.
    """
    module = _load()
    report = tmp_path / "coverage.json"
    _write_report(report)
    monkeypatch.setattr(module, "_head", lambda: "deadbeef")

    assert _stamp(module, report, tmp_path / "never-written.json", "some-token") == 0
    assert "_phaze_scope" not in json.loads(report.read_text(encoding="utf-8"))


def test_a_mismatched_token_is_never_stamped(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The defense finding 1 asked for directly: even with a perfectly fresh, clean, unmoved
    marker, the WRONG token must never be accepted.
    """
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    _write_report(report)
    _record_start(module, marker, monkeypatch, capsys, head="deadbeef")  # the real token is discarded here

    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    assert _stamp(module, report, marker, "not-the-real-token") == 0
    assert "_phaze_scope" not in json.loads(report.read_text(encoding="utf-8"))


def test_a_second_concurrent_record_start_clobbers_the_marker_and_the_first_run_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact interleaving from review finding 1. Run A starts on a DIRTY tree and records
    that; before A's own coverage run finishes, the operator `git stash`es and Run B starts (on
    the now-clean tree) and overwrites the SAME marker file with ITS OWN token. If a bare marker
    (no token) were trusted, A would finish, read B's clean record, and stamp its own (actually
    dirty) measurement as trustworthy. With the token, A's stamp call carries A's OWN token, which
    no longer matches what the marker (now B's) holds, so A is correctly refused.
    """
    module = _load()
    marker = tmp_path / "start.json"  # the ONE shared marker file both runs use
    report = tmp_path / "coverage.json"
    _write_report(report)

    token_a = _record_start(module, marker, monkeypatch, capsys, head="deadbeef", dirty=True)
    token_b = _record_start(module, marker, monkeypatch, capsys, head="deadbeef", dirty=False)  # clobbers the marker
    assert token_a != token_b

    # Run A finishes its (actually dirty) measurement and tries to stamp with ITS OWN token.
    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    assert _stamp(module, report, marker, token_a) == 0
    assert "_phaze_scope" not in json.loads(report.read_text(encoding="utf-8")), "run A's dirty measurement must not be stamped using run B's marker"


def test_an_unresolvable_head_at_record_time_records_nothing_and_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nonzero exit here is a real signal (phaze-vad6y review finding 2): a caller (e.g.
    `scripts/parallel_test_runner.py`) reads it to decide whether a later stamp call is even worth
    attempting.
    """
    module = _load()
    marker = tmp_path / "start.json"
    monkeypatch.setattr(module, "_head", lambda: "")
    monkeypatch.setattr(module, "_dirty", lambda: False)

    rc = module.main(["--record-start", "--start-marker", str(marker)])

    assert rc == 1
    assert not marker.exists()
    assert capsys.readouterr().out == ""  # no token to capture on failure


def test_record_start_clears_a_stale_marker_even_when_it_then_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding 2: a marker from an earlier, unrelated (or aborted) run must never survive
    a LATER `--record-start` that itself goes on to fail -- otherwise a stamp call that skipped
    straight past a failed record-start (or whose caller ignored the failure) could still read
    that stale marker and misattribute it to the current run.
    """
    module = _load()
    marker = tmp_path / "start.json"
    marker.write_text(json.dumps({"head": "stale-head", "dirty": False, "token": "stale-token"}), encoding="utf-8")

    monkeypatch.setattr(module, "_head", lambda: "")  # this record-start will fail
    rc = module.main(["--record-start", "--start-marker", str(marker)])

    assert rc == 1
    assert not marker.exists(), "record_start must clear any existing marker FIRST, unconditionally"


def test_an_unresolvable_head_at_stamp_time_leaves_the_report_unstamped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    _write_report(report)
    token = _record_start(module, marker, monkeypatch, capsys, head="deadbeef")

    monkeypatch.setattr(module, "_head", lambda: "")
    assert _stamp(module, report, marker, token) == 0
    assert "_phaze_scope" not in json.loads(report.read_text(encoding="utf-8"))


def test_a_tree_dirty_at_the_start_of_the_run_is_never_stamped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `git stash`-mid-run scenario: even though the tree may read as clean BY STAMP TIME,
    a dirty START means the measured content has no exact, git-addressable tree to name.
    """
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    _write_report(report)
    token = _record_start(module, marker, monkeypatch, capsys, head="deadbeef", dirty=True)

    monkeypatch.setattr(module, "_head", lambda: "deadbeef")  # HEAD unchanged, tree "looks" clean now
    assert _stamp(module, report, marker, token) == 0
    assert "_phaze_scope" not in json.loads(report.read_text(encoding="utf-8"))


def test_head_moving_during_the_run_is_never_stamped(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit-lands-mid-run scenario: HEAD at the end differs from HEAD at the start, so the
    tree the report describes and the tree HEAD now names cannot be assumed to be the same bytes.
    """
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    _write_report(report)
    token = _record_start(module, marker, monkeypatch, capsys, head="before-commit")

    monkeypatch.setattr(module, "_head", lambda: "after-commit")
    assert _stamp(module, report, marker, token) == 0
    assert "_phaze_scope" not in json.loads(report.read_text(encoding="utf-8"))


def test_a_branch_carrying_report_with_a_clean_unmoved_start_is_stamped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    _write_report(report)
    token = _record_start(module, marker, monkeypatch, capsys, head="deadbeef", dirty=False)

    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    assert _stamp(module, report, marker, token) == 0
    scope = json.loads(report.read_text(encoding="utf-8"))["_phaze_scope"]
    assert scope == {"kind": "full", "head": "deadbeef"}


def test_the_marker_is_one_shot_and_does_not_survive_a_second_stamp_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `--record-start` marker is consumed by the FIRST stamp attempt that reads it -- a second,
    unrelated `just branch-check` (or a re-run of the stamp step) must not silently reuse it.
    """
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    _write_report(report)
    token = _record_start(module, marker, monkeypatch, capsys, head="deadbeef")

    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    assert _stamp(module, report, marker, token) == 0
    assert not marker.exists()

    # A second report happens to appear (e.g. someone re-ran `coverage json` by hand); without a
    # fresh --record-start there is nothing to compare it against, so this must NOT stamp it.
    _write_report(report)
    assert _stamp(module, report, marker, token) == 0
    assert "_phaze_scope" not in json.loads(report.read_text(encoding="utf-8"))


def test_the_rest_of_the_report_is_preserved(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Stamping must not clobber the report `scripts/coverage_floor.py` reads from the same file."""
    module = _load()
    marker = tmp_path / "start.json"
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps({"files": {"src/phaze/a.py": {"summary": {"num_statements": 5}}}, "totals": {"num_branches": 1, "covered_branches": 1}}),
        encoding="utf-8",
    )
    token = _record_start(module, marker, monkeypatch, capsys, head="deadbeef")

    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    assert _stamp(module, report, marker, token) == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["files"]["src/phaze/a.py"]["summary"]["num_statements"] == 5
    assert data["_phaze_scope"] == {"kind": "full", "head": "deadbeef"}


def test_default_report_and_marker_paths_match_production_wiring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`test-cov` / `parallel_test_runner.py` call this with no `--report` and no `--start-marker`,
    so the defaults must match `DEFAULT_COVERAGE` in `scripts/branch_coverage_check.py` and
    `.gitignore`'s entry exactly.
    """
    module = _load()
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path / "coverage.json")
    monkeypatch.setattr(module, "_head", lambda: "deadbeef")
    monkeypatch.setattr(module, "_dirty", lambda: False)

    assert module.main(["--record-start"]) == 0
    token = capsys.readouterr().out.strip()
    assert (tmp_path / ".coverage-scope-start.json").is_file()

    assert module.main(["--expect-token", token]) == 0
    assert json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))["_phaze_scope"]["kind"] == "full"
    assert not (tmp_path / ".coverage-scope-start.json").exists()


# phaze-vad6y review finding 4: every test above monkeypatches `_head`/`_dirty` -- cheap and
# sufficient for exercising `main()`'s branching, but it never proves `_dirty()` ITSELF reads a
# real `git status` correctly, or that the artifact every stamped producer writes INTO the tree it
# is measuring (a gitignored `coverage.json`) does not make `_dirty()` see its own output as a
# change. A real repository is the only way to check that.
def test_a_real_git_repository_end_to_end_including_a_gitignored_coverage_json_not_counting_as_dirty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    monkeypatch.chdir(tmp_path)
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)  # noqa: S603, S607
    (tmp_path / ".gitignore").write_text("coverage.json\n.coverage-scope-start.json\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "--no-verify", "-m", "base"], check=True)  # noqa: S603
    real_head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603

    assert module._head() == real_head
    assert module._dirty() is False

    # The coverage-producing step's own output lands in the SAME tree it just measured, and it is
    # gitignored -- writing it must not make the tree "dirty" from _dirty()'s point of view.
    _write_report(tmp_path / "coverage.json")
    assert module._dirty() is False

    marker = tmp_path / ".coverage-scope-start.json"
    assert module.main(["--record-start", "--start-marker", str(marker)]) == 0
    token = capsys.readouterr().out.strip()
    assert token, "record-start must print a real token against a real, resolvable repository"

    assert module.main(["--report", str(tmp_path / "coverage.json"), "--start-marker", str(marker), "--expect-token", token]) == 0
    scope = json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))["_phaze_scope"]
    assert scope == {"kind": "full", "head": real_head}

    # A REAL uncommitted change to a TRACKED file must be seen as dirty.
    (tmp_path / "src.py").write_text("x = 2\n", encoding="utf-8")
    assert module._dirty() is True
