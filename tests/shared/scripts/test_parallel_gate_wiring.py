"""Static contracts for selecting the local two-worker coverage gate."""

from pathlib import Path
import re

from scripts.parallel_test_manifests import LANE_BUCKETS


_ROOT = Path(__file__).resolve().parents[3]
_JUSTFILE = (_ROOT / "justfile").read_text(encoding="utf-8")
_RUNNER = (_ROOT / "scripts/parallel_test_runner.py").read_text(encoding="utf-8")


def _recipe(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\n((?: +.*\n|\n)+)", _JUSTFILE, re.MULTILINE)
    assert match is not None, f"recipe {name!r} is missing"
    return match.group(1)


def test_parallel_recipe_and_two_worker_cap_are_explicit() -> None:
    assert _recipe("test-cov-parallel").strip() == "uv run python scripts/parallel_test_runner.py"
    assert [name for name, _ in LANE_BUCKETS] == ["lane-a", "lane-b"]


def test_local_validation_selects_parallel_and_external_environment_falls_back_loudly() -> None:
    validate = _recipe("test-validate")

    assert 'if [ -n "${TEST_DATABASE_URL:-}" ]; then' in validate
    assert "SERIAL FALLBACK" in validate
    assert "just test-cov" in validate
    assert "PHAZE_TEST_PARALLEL" in validate
    assert "just test-validate-serial" in validate
    assert "TWO-WORKER COVERAGE" in validate
    assert "just test-cov-parallel" in validate


def test_serial_coverage_primitive_is_retained() -> None:
    serial = _recipe("test-cov")

    assert "uv run pytest --cov {{cov_reports}}" in serial
    assert "uv run python scripts/coverage_floor.py" in serial


def test_parallel_path_has_no_shared_or_forced_cleanup_escape_hatch() -> None:
    assert 'environment.pop("PHAZE_TEST_DB_ALLOW_SHARED", None)' in _RUNNER
    assert 'environment.pop("PYTEST_ADDOPTS", None)' in _RUNNER
    assert "PHAZE_TEST_DB_FORCE_DOWN" not in _RUNNER
    assert "test-db-down" not in _RUNNER
    assert "--force" not in _RUNNER
