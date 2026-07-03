"""Unit tests for the per-module coverage floor gate (Phase 64-01, COV-01 / COV-02).

``scripts/coverage_floor.py`` is a CI-load-bearing script: it runs inside ``just coverage-combine``
on the COMBINED ``coverage.json`` and fails the required check if any tracked ``phaze/**`` module
falls below the uniform 85% floor. Because it gates merges, it gets its own tests -- the same
precedent as ``scripts/classify-changed-files.sh`` / ``tests/shared/test_change_gate.py`` (Phase 63).

These tests write synthetic ``coverage.json`` fixtures into ``tmp_path``, ``chdir`` into it, and
call the real ``main()`` over its real interface, asserting on the EXIT CODE (the observable
outcome, D-07). The load-bearing case is :func:`test_missing_coverage_json_fails_closed`: a missing
or unparseable ``coverage.json`` must NEVER exit 0 (T-64-01) -- a fail-open gate would let a
sub-floor module merge silently.

This file lives under ``tests/shared/`` so it rides the shared bucket in the Phase 63 test partition.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from types import ModuleType


# tests/shared/test_coverage_floor.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "coverage_floor.py"


def _load_floor_module() -> ModuleType:
    """Import the real ``scripts/coverage_floor.py`` as a module so we can call ``main()`` directly.

    ``scripts/`` is not an importable package, so load it from its file path. Exercising the real
    ``main()`` (rather than a copy) keeps these tests honest about the shipped gate's behaviour.
    """
    assert _SCRIPT.is_file(), f"floor script missing: {_SCRIPT}"
    spec = importlib.util.spec_from_file_location("coverage_floor", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_coverage_json(tmp_path: Path, files: dict[str, dict[str, float]]) -> None:
    """Write a synthetic ``coverage.json`` (only the ``files{}.summary`` fields the script reads)."""
    payload = {"files": {path: {"summary": summary} for path, summary in files.items()}}
    (tmp_path / "coverage.json").write_text(json.dumps(payload), encoding="utf-8")


def test_sub_floor_module_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A module below 85% -> exit 1 AND its path is reported on stdout (case 1)."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {"src/phaze/services/review.py": {"num_statements": 95, "missing_lines": 16, "percent_covered": 83.16}},
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 1
    assert "src/phaze/services/review.py" in capsys.readouterr().out


def test_all_modules_at_or_above_floor_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tracked module >= 85.0 -> exit 0 (case 2). Boundary 85.0 is not a failure."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {
            "src/phaze/main.py": {"num_statements": 40, "missing_lines": 0, "percent_covered": 100.0},
            "src/phaze/services/pipeline.py": {"num_statements": 200, "missing_lines": 30, "percent_covered": 85.0},
        },
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0


def test_zero_statement_module_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``num_statements == 0`` file reading 0% is skipped, not counted a failure (case 3)."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {
            "src/phaze/__init__.py": {"num_statements": 0, "missing_lines": 0, "percent_covered": 0.0},
            "src/phaze/main.py": {"num_statements": 40, "missing_lines": 0, "percent_covered": 100.0},
        },
    )
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0


def test_exempt_module_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sub-floor module whose path is in EXEMPT is skipped -> exit 0 (case 4, D-09)."""
    module = _load_floor_module()
    _write_coverage_json(
        tmp_path,
        {"src/phaze/services/review.py": {"num_statements": 95, "missing_lines": 16, "percent_covered": 83.16}},
    )
    # Add the only sub-floor module to EXEMPT (with a justification, as D-09 requires).
    monkeypatch.setitem(module.EXEMPT, "src/phaze/services/review.py", "test: exercised via exempt path")
    monkeypatch.chdir(tmp_path)

    assert module.main() == 0


def test_missing_coverage_json_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAIL CLOSED (T-64-01): no coverage.json -> raise (non-zero exit), NEVER exit 0 (case 5)."""
    module = _load_floor_module()
    monkeypatch.chdir(tmp_path)  # tmp_path is empty -> no coverage.json

    with pytest.raises(FileNotFoundError):
        module.main()


def test_empty_coverage_json_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAIL CLOSED (T-64-01): an empty/unparseable coverage.json -> raise, NEVER exit 0 (case 5)."""
    module = _load_floor_module()
    (tmp_path / "coverage.json").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(json.JSONDecodeError):
        module.main()


def test_empty_files_dict_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """FAIL CLOSED (T-64-01): a parseable ``{"files": {}}`` -> exit 1, NEVER exit 0 (WR-01).

    A report with zero tracked modules means the measurement produced nothing (e.g. no shards
    were combined). An empty loop must not be mistaken for an all-clear, or a broken combine
    would merge green with the gate never actually run against real coverage.
    """
    module = _load_floor_module()
    (tmp_path / "coverage.json").write_text(json.dumps({"files": {}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert module.main() == 1
    assert "no tracked files" in capsys.readouterr().out
