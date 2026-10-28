"""phaze-aje5d: the test-harness Valkey knobs, renamed from ``PHAZE_TEST_REDIS_*``, with the old names kept.

The harness has run Valkey since phaze-8o294, but its knobs still said Redis. phaze-aje5d renamed
them -- ``PHAZE_TEST_VALKEY_PORT`` / ``_CONTAINER`` / ``_DATABASES`` / ``_SEAT_LEASE_HOURS`` and
``PHAZE_INTEGRATION_TEST_VALKEY_PORT``. Operator decision 2026-10-27, durable record bead
phaze-aje5d: asked how far the Valkey rename should go, the selected option label was "Full rename +
compat". So every old ``PHAZE_TEST_REDIS_*`` spelling is still read as a FALLBACK, and the Valkey
name wins when both are set, as the bead's acceptance criteria require. Three independent readers
have to agree, so each is driven for real rather than read as text:

* the justfile, through ``just --evaluate`` (every recipe interpolates these variables);
* ``scripts/parallel_test_runner.py``, whose CLI defaults classify a caller seat against the
  harness the justfile resolves;
* ``scripts/redis-seat-registry.sh``, which reads the lease length straight from the environment.

The container default is also pinned: a hard switch to ``phaze-test-valkey``, with no path back to
the pre-rename ``phaze-test-redis`` (operator decision 2026-10-27 on bead phaze-aje5d, answer as
given: "stop the running container (no one is using it) and then do the hard switch").
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts.parallel_test_runner import _parser, harness_valkey_setting


REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "scripts" / "redis-seat-registry.sh"

# justfile variable -> (new env var, pre-rename env var, default)
_JUSTFILE_KNOBS = {
    "test_valkey_port": ("PHAZE_TEST_VALKEY_PORT", "PHAZE_TEST_REDIS_PORT", "6380"),
    "test_valkey_container": ("PHAZE_TEST_VALKEY_CONTAINER", "PHAZE_TEST_REDIS_CONTAINER", "phaze-test-valkey"),
    "test_valkey_databases": ("PHAZE_TEST_VALKEY_DATABASES", "PHAZE_TEST_REDIS_DATABASES", "64"),
    "integration_valkey_port": ("PHAZE_INTEGRATION_TEST_VALKEY_PORT", "PHAZE_INTEGRATION_TEST_REDIS_PORT", "0"),
}
_ALL_KNOB_NAMES = {name for new, old, _default in _JUSTFILE_KNOBS.values() for name in (new, old)} | {
    "PHAZE_TEST_VALKEY_SEAT_LEASE_HOURS",
    "PHAZE_TEST_REDIS_SEAT_LEASE_HOURS",
}


def _clean_env(**overrides: str) -> dict[str, str]:
    """This process's environment with every harness knob removed, then ``overrides`` applied.

    A developer's own exported ``PHAZE_TEST_VALKEY_PORT`` must not decide what these tests see.
    """
    env = {key: value for key, value in os.environ.items() if key not in _ALL_KNOB_NAMES}
    env.update(overrides)
    return env


def _evaluate(variable: str, env: dict[str, str]) -> str:
    just = shutil.which("just")
    assert just is not None, "just is not on PATH"
    result = subprocess.run(  # noqa: S603 - fixed executable; the variable name is a test literal
        [just, "--evaluate", variable],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.mark.parametrize("variable", sorted(_JUSTFILE_KNOBS))
def test_the_justfile_default_applies_when_neither_name_is_set(variable: str) -> None:
    _new, _old, default = _JUSTFILE_KNOBS[variable]
    assert _evaluate(variable, _clean_env()) == default


@pytest.mark.parametrize("variable", sorted(_JUSTFILE_KNOBS))
def test_the_justfile_still_honours_the_pre_rename_name_alone(variable: str) -> None:
    _new, old, _default = _JUSTFILE_KNOBS[variable]
    assert _evaluate(variable, _clean_env(**{old: "from-old-name"})) == "from-old-name"


@pytest.mark.parametrize("variable", sorted(_JUSTFILE_KNOBS))
def test_the_justfile_prefers_the_valkey_name_when_both_are_set(variable: str) -> None:
    new, old, _default = _JUSTFILE_KNOBS[variable]
    assert _evaluate(variable, _clean_env(**{new: "from-new-name", old: "from-old-name"})) == "from-new-name"


def test_the_harness_container_is_a_hard_switch_to_valkey() -> None:
    assert _evaluate("test_valkey_container", _clean_env()) == "phaze-test-valkey"
    assert _evaluate("integration_valkey_container_prefix", _clean_env()) == "phaze-integration-test-valkey"


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, "default"),
        ({"PHAZE_TEST_REDIS_PORT": "old"}, "old"),
        ({"PHAZE_TEST_VALKEY_PORT": "new"}, "new"),
        ({"PHAZE_TEST_VALKEY_PORT": "new", "PHAZE_TEST_REDIS_PORT": "old"}, "new"),
        # Set-but-empty is still "set", exactly as just's env_var_or_default treats it.
        ({"PHAZE_TEST_VALKEY_PORT": "", "PHAZE_TEST_REDIS_PORT": "old"}, ""),
    ],
)
def test_the_runner_reads_a_knob_with_the_justfiles_precedence(environ: dict[str, str], expected: str) -> None:
    assert harness_valkey_setting(environ, "PORT", "default") == expected


def test_the_runner_cli_defaults_match_the_justfile(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_KNOB_NAMES:
        monkeypatch.delenv(name, raising=False)
    args = _parser().parse_args([])
    assert (args.redis_port, args.redis_container) == ("6380", "phaze-test-valkey")

    monkeypatch.setenv("PHAZE_TEST_REDIS_PORT", "7001")
    monkeypatch.setenv("PHAZE_TEST_REDIS_CONTAINER", "old-container")
    args = _parser().parse_args([])
    assert (args.redis_port, args.redis_container) == ("7001", "old-container")

    monkeypatch.setenv("PHAZE_TEST_VALKEY_PORT", "7002")
    monkeypatch.setenv("PHAZE_TEST_VALKEY_CONTAINER", "new-container")
    args = _parser().parse_args([])
    assert (args.redis_port, args.redis_container) == ("7002", "new-container")


def _lease_hours(env: dict[str, str]) -> str:
    """Run only the registry's own lease-resolution line, extracted verbatim from the script.

    The full script needs a live Valkey before it reads anything; the one assignment under test does
    not, and executing its exact text proves the shell expansion rather than a re-statement of it.
    """
    line = next(line for line in REGISTRY.read_text(encoding="utf-8").splitlines() if line.startswith("lease_hours="))
    bash = shutil.which("bash")
    assert bash is not None, "bash is not on PATH"
    result = subprocess.run(  # noqa: S603 - fixed executable; the program is a line of this repo's own script
        [bash, "-c", f'{line}\nprintf %s "$lease_hours"'],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_the_registry_lease_length_has_the_same_precedence() -> None:
    assert _lease_hours(_clean_env()) == "72"
    assert _lease_hours(_clean_env(PHAZE_TEST_REDIS_SEAT_LEASE_HOURS="5")) == "5"
    assert _lease_hours(_clean_env(PHAZE_TEST_VALKEY_SEAT_LEASE_HOURS="9", PHAZE_TEST_REDIS_SEAT_LEASE_HOURS="5")) == "9"
