"""Unit tests for the repowise coverage-refresh gate (phaze-2rgq2).

``scripts/repowise_coverage_gate.py`` is the last step of ``just repowise-coverage``. It exists to
catch two states that every other surface reports as success:

* the ``.coverage``-only partial ingest -- a populated test-to-code map with ``line_coverage_pct``
  still null, which leaves ``untested_hotspot`` firing on 96-100%-covered files;
* index/coverage commit skew -- coverage measured at one commit folded onto a index built at
  another, mapping it onto line numbers that have moved.

Neither can be verified by running the real 21-minute recipe in a test, so the gate takes repowise's
own ``--format json`` output as data and is exercised here over synthetic status dumps. Same
precedent as ``tests/shared/test_coverage_floor.py``: load the real script from its path, call the
real ``main()`` over its real interface, and assert on the EXIT CODE.

This file lives under ``tests/shared/`` so it rides the shared bucket in the Phase 63 test partition.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from types import ModuleType


# tests/shared/test_repowise_coverage_gate.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "repowise_coverage_gate.py"

_HEAD = "1c85e2ecb5d6a4c3c993b413c79c3539cab3ac3a"
_OTHER_HEAD = "733431ba9f0e4c1d8a2b6f5e3c7d9a1b4e6f8c02"


def _load_gate_module() -> ModuleType:
    """Import the real ``scripts/repowise_coverage_gate.py`` so ``main()`` is the shipped one."""
    assert _SCRIPT.is_file(), f"gate script missing: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("repowise_coverage_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coverage_block(*, sha: str = _HEAD, file_count: int = 249, line_pct: float | None = 98.43) -> dict[str, Any]:
    """The shape ``repowise coverage status`` renders after a successful coverage.xml ingest."""
    return {
        "file_count": file_count,
        "covered_lines": 16688,
        "total_lines": 16955,
        "line_coverage_pct": line_pct,
        "branch_coverage_pct": None,
        "source_format": "cobertura",
        "ingested_at": "2026-08-18 04:41:37.820817",
        "ingested_commit_sha": sha,
    }


def _test_map_block(*, sha: str = _HEAD, pair_count: int = 29317) -> dict[str, Any]:
    """The shape ``repowise coverage status`` renders after a ``.coverage`` (contexts) ingest."""
    return {
        "pair_count": pair_count,
        "test_count": 13049,
        "source_file_count": 195,
        "source_format": "coverage.py",
        "ingested_at": "2026-08-18 04:40:02.393734",
        "ingested_commit_sha": sha,
    }


def _write(tmp_path: Path, name: str, payload: dict[str, Any], *, preamble: str = "") -> Path:
    path = tmp_path / name
    path.write_text(preamble + json.dumps(payload), encoding="utf-8")
    return path


def _run(
    module: ModuleType,
    tmp_path: Path,
    *,
    coverage: dict[str, Any] | None = None,
    test_map: dict[str, Any] | None = None,
    index_sha: str = _HEAD,
    indexed: bool = True,
    expected: str = _HEAD,
) -> int:
    coverage_status = _write(
        tmp_path,
        "coverage-status.json",
        {"repo": str(tmp_path), "indexed": True, "coverage": coverage, "test_map": test_map},
    )
    index_status = _write(
        tmp_path,
        "index-status.json",
        {"repo": str(tmp_path), "indexed": indexed, "state": {"last_sync_commit": index_sha}},
    )
    exit_code = module.main(
        [
            "--coverage-status",
            str(coverage_status),
            "--index-status",
            str(index_status),
            "--expected-commit",
            expected,
        ]
    )
    assert isinstance(exit_code, int)
    return exit_code


def test_both_ingests_at_the_indexed_commit_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The only green state: per-file coverage AND the test map, both pinned to the indexed commit."""
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(), test_map=_test_map_block()) == 0
    assert "verified" in capsys.readouterr().out


def test_coverage_only_map_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """TRAP 1, the whole reason this gate exists: a populated map with NO per-file coverage.

    This is what ``repowise coverage add .coverage`` alone leaves behind. It reports success, and
    ``repowise coverage status`` shows the map, so nothing else in the pipeline notices.
    """
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=None, test_map=_test_map_block()) == 1
    assert "per-file line coverage is EMPTY" in capsys.readouterr().err


def test_zero_file_count_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The other rendering of trap 1: a coverage block that exists but counts zero files."""
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(file_count=0), test_map=_test_map_block()) == 1
    assert "per-file line coverage is EMPTY" in capsys.readouterr().err


def test_null_line_coverage_pct_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Files counted but ``line_coverage_pct`` null is still unusable — it is what feeds the scores."""
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(line_pct=None), test_map=_test_map_block()) == 1
    assert "line_coverage_pct is None" in capsys.readouterr().err


def test_missing_test_map_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The mirror-image partial: per-file coverage without ``--cov-context=test``.

    An empty map is not cosmetic — ``get_risk``'s tests_to_run and the impacted-tests skill return
    nothing, and nothing reads as "no tests" when it means "unknown".
    """
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(), test_map=None) == 1
    assert "test-to-code map is EMPTY" in capsys.readouterr().err


def test_coverage_ingested_at_a_different_commit_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """TRAP 2: coverage measured on a tree the index does not describe."""
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(sha=_OTHER_HEAD), test_map=_test_map_block()) == 1
    assert "per-file coverage was ingested at commit" in capsys.readouterr().err


def test_stale_index_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """TRAP 2 from the other side: fresh coverage folded onto an index left behind at an older commit."""
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(), test_map=_test_map_block(), index_sha=_OTHER_HEAD) == 1
    assert "index is at commit" in capsys.readouterr().err


def test_unindexed_repo_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No index at all -> fail, rather than scoring coverage against nothing."""
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(), test_map=_test_map_block(), indexed=False) == 1
    assert "NOT indexed" in capsys.readouterr().err


def test_abbreviated_sha_is_accepted(tmp_path: Path) -> None:
    """A short sha on either side names the same commit — tolerated, so a future short form is not a false red."""
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(), test_map=_test_map_block(), expected=_HEAD[:12]) == 0


def test_empty_sha_never_matches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Prefix tolerance must not degenerate: an absent sha matches nothing (``"".startswith`` is True)."""
    module = _load_gate_module()

    assert _run(module, tmp_path, coverage=_coverage_block(sha=""), test_map=_test_map_block()) == 1
    assert "ingested at commit <none>" in capsys.readouterr().err


def test_human_preamble_before_the_json_is_tolerated(tmp_path: Path) -> None:
    """Some repowise ``--format json`` surfaces print a banner line first (``repowise health`` does)."""
    module = _load_gate_module()
    coverage_status = _write(
        tmp_path,
        "coverage-status.json",
        {"indexed": True, "coverage": _coverage_block(), "test_map": _test_map_block()},
        preamble=f"repowise coverage — {tmp_path}\n",
    )
    index_status = _write(
        tmp_path,
        "index-status.json",
        {"indexed": True, "state": {"last_sync_commit": _HEAD}},
        preamble="repowise status\n",
    )

    assert module.main(["--coverage-status", str(coverage_status), "--index-status", str(index_status), "--expected-commit", _HEAD]) == 0


def test_missing_status_file_fails_closed(tmp_path: Path) -> None:
    """FAIL CLOSED: an absent status dump raises rather than reporting a verified refresh."""
    module = _load_gate_module()

    with pytest.raises(FileNotFoundError):
        module.main(
            [
                "--coverage-status",
                str(tmp_path / "nope.json"),
                "--index-status",
                str(tmp_path / "also-nope.json"),
                "--expected-commit",
                _HEAD,
            ]
        )


def test_non_json_status_file_fails_closed(tmp_path: Path) -> None:
    """A status dump with no JSON object in it is a broken measurement, never an all-clear."""
    module = _load_gate_module()
    broken = tmp_path / "coverage-status.json"
    broken.write_text("repowise: command failed\n", encoding="utf-8")
    index_status = _write(tmp_path, "index-status.json", {"indexed": True, "state": {"last_sync_commit": _HEAD}})

    with pytest.raises(ValueError, match="no JSON object"):
        module.main(["--coverage-status", str(broken), "--index-status", str(index_status), "--expected-commit", _HEAD])


_COBERTURA = """<?xml version="1.0" ?>
<coverage version="7.6.1">
  <packages>
    <package name="phaze">
      <classes>
        <class name="shell.py" filename="src/phaze/routers/shell.py" line-rate="0.96"/>
        <class name="pipeline.py" filename="src/phaze/services/pipeline.py" line-rate="0.99"/>
        <class name="dupe.py" filename="src/phaze/services/pipeline.py" line-rate="0.99"/>
      </classes>
    </package>
  </packages>
</coverage>
"""


def test_report_file_count_counts_distinct_filenames(tmp_path: Path) -> None:
    """Two <class> entries for one file (a module with several classes) count once."""
    module = _load_gate_module()
    report = tmp_path / "coverage.xml"
    report.write_text(_COBERTURA, encoding="utf-8")

    assert module.count_report_files(report) == 2


def test_fragment_ingest_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """TRAP 3: coverage.xml listed more files than mapped into the index.

    Hit for real while verifying this recipe: ``repowise coverage add`` mapped 1 of 249 files,
    printed "248 report file(s) did not map to the repo tree" as a NOTE, and exited 0. The stored
    line_coverage_pct was real and the map was populated, so every emptiness check passed while the
    ingested coverage described a single file.
    """
    module = _load_gate_module()
    report = tmp_path / "report.xml"
    report.write_text(_COBERTURA, encoding="utf-8")
    coverage_status = _write(
        tmp_path,
        "coverage-status.json",
        {"indexed": True, "coverage": _coverage_block(file_count=1), "test_map": _test_map_block()},
    )
    index_status = _write(tmp_path, "index-status.json", {"indexed": True, "state": {"last_sync_commit": _HEAD}})

    exit_code = module.main(
        [
            "--coverage-status",
            str(coverage_status),
            "--index-status",
            str(index_status),
            "--expected-commit",
            _HEAD,
            "--coverage-xml",
            str(report),
        ]
    )

    assert exit_code == 1
    assert "only 1 of the 2 file(s) in coverage.xml mapped" in capsys.readouterr().err


def test_full_mapping_passes(tmp_path: Path) -> None:
    """Every file in the report mapped -> no trap-3 failure."""
    module = _load_gate_module()
    report = tmp_path / "report.xml"
    report.write_text(_COBERTURA, encoding="utf-8")
    coverage_status = _write(
        tmp_path,
        "coverage-status.json",
        {"indexed": True, "coverage": _coverage_block(file_count=2), "test_map": _test_map_block()},
    )
    index_status = _write(tmp_path, "index-status.json", {"indexed": True, "state": {"last_sync_commit": _HEAD}})

    assert (
        module.main(
            [
                "--coverage-status",
                str(coverage_status),
                "--index-status",
                str(index_status),
                "--expected-commit",
                _HEAD,
                "--coverage-xml",
                str(report),
            ]
        )
        == 0
    )


def test_stale_rows_above_the_report_count_are_not_a_failure(tmp_path: Path) -> None:
    """repowise upserts rather than replaces, so leftovers from an earlier report may exceed it."""
    module = _load_gate_module()
    report = tmp_path / "report.xml"
    report.write_text(_COBERTURA, encoding="utf-8")
    coverage_status = _write(
        tmp_path,
        "coverage-status.json",
        {"indexed": True, "coverage": _coverage_block(file_count=249), "test_map": _test_map_block()},
    )
    index_status = _write(tmp_path, "index-status.json", {"indexed": True, "state": {"last_sync_commit": _HEAD}})

    assert (
        module.main(
            [
                "--coverage-status",
                str(coverage_status),
                "--index-status",
                str(index_status),
                "--expected-commit",
                _HEAD,
                "--coverage-xml",
                str(report),
            ]
        )
        == 0
    )
