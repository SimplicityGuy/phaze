"""Coverage runs on sys.monitoring by default, and on CTracer+greenlet only where contexts need it (phaze-bein3).

WHY THE CORE MATTERS. coverage.py 7.16.1 on Python 3.14 picks its ``sysmon`` core (PEP 669
``sys.monitoring``) by default, but REFUSES it -- falling back to the settrace-based CTracer --
whenever ``concurrency`` names ``greenlet``. Spike phaze-6qhh2 measured the CTracer at +55% CPU
over an uncovered run on the same 1,712 tests. ``greenlet`` was added in c8bbd16f because the
CTracer, which keeps a per-thread frame stack, loses lines that run after SQLAlchemy's async
greenlet bridge switches (measured for this bead: CTracer WITHOUT greenlet drops the two lines of
a coroutine that follow ``conn.run_sync``). sys.monitoring keeps no frame stack, so it needs no
greenlet support: measured against a probe driving ``greenlet_spawn``/``await_only``,
``AsyncConnection.run_sync`` and a ``before_cursor_execute`` listener over asyncpg, its executed
lines AND arcs are identical to CTracer+greenlet, and a line reachable only inside the greenlet
flips covered/missed identically under both cores.

WHY NOT SYSMON EVERYWHERE. ``--cov-context=test`` is not supported by the sysmon core: coverage
warns "Dynamic contexts aren't supported with core=sysmon; context data may be incomplete", and
measured, a line two tests execute records ONE context instead of two (sys.monitoring disables a
location after its first hit). Those contexts build repowise's per-test map, which is what
``just check-fast``'s selector reads, so a thinned map silently under-selects tests.

SO THE SPLIT. pyproject's ``concurrency = ["thread", "${PHAZE_COVERAGE_GREENLET-thread}"]`` is
sysmon-capable when the variable is unset, and every launcher that passes ``--cov-context=test``
sets ``PHAZE_COVERAGE_GREENLET=greenlet`` to opt back into CTracer+greenlet. Each half is pinned
here, and a context-bearing run that lands on sysmon anyway is an ERROR, not a warning, via the
``filterwarnings`` entry in pyproject's pytest table.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

import coverage
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

OPT_IN_VAR = "PHAZE_COVERAGE_GREENLET"

# The launchers that exist today. The scan below must FIND each of them, so it cannot pass by
# matching nothing; a new launcher it finds must also opt in, whether or not it is listed here.
KNOWN_CONTEXT_LAUNCHERS = frozenset({"justfile", "scripts/parallel_test_runner.py", "scripts/repowise-coverage.sh"})

# A shell/just line that runs pytest with per-test contexts, or a Python argv element passing it.
_SHELL_LAUNCH = re.compile(r"\bpytest\b.*--cov-context[= ]")
_PY_ARGV_ELEMENT = re.compile(r"""^\s*["']--cov-context(?:=[^"']*)?["'],?\s*$""")
_PY_OPT_IN = re.compile(rf"""["']{OPT_IN_VAR}["']\s*:\s*["']greenlet["']""")


def _tracked_launcher_candidates() -> list[str]:
    """Every tracked file that could launch pytest: everything except tests and prose."""
    listed = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 - git is a prerequisite of this repo
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return [path for path in listed if not path.startswith(("tests/", "docs/", ".planning/")) and not path.endswith(".md")]


def _context_launch_sites() -> dict[str, list[str]]:
    """Map each tracked file to the lines in it that launch pytest with ``--cov-context``."""
    sites: dict[str, list[str]] = {}
    for rel in _tracked_launcher_candidates():
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "--cov-context" not in text:
            continue
        # Comments and progress messages mention the flag without launching anything.
        lines = [line for line in text.splitlines() if not line.lstrip().startswith(("#", "echo ", "printf "))]
        hits = [line for line in lines if (_PY_ARGV_ELEMENT.match(line) if rel.endswith(".py") else _SHELL_LAUNCH.search(line))]
        if hits:
            sites[rel] = hits
    return sites


def test_the_scan_finds_every_known_context_launcher() -> None:
    """Guards the guard: a scan that matched nothing would pass the opt-in test vacuously."""
    missing = KNOWN_CONTEXT_LAUNCHERS - set(_context_launch_sites())
    assert not missing, f"the --cov-context scan no longer finds {sorted(missing)}; fix the scan, not the list"


def test_every_context_launcher_opts_into_greenlet() -> None:
    """Every pytest launch carrying ``--cov-context`` must run on CTracer+greenlet, not sysmon.

    Shell and just lines must set the variable ON THE LAUNCH LINE, so it cannot drift away from
    the command it governs. A Python launcher must put it in the environment it builds.
    """
    offenders: list[str] = []
    for rel, hits in _context_launch_sites().items():
        if rel.endswith(".py"):
            if not _PY_OPT_IN.search((REPO_ROOT / rel).read_text(encoding="utf-8")):
                offenders.append(f"{rel}: builds a --cov-context argv but never sets {OPT_IN_VAR}=greenlet")
        else:
            offenders.extend(f"{rel}: {line.strip()}" for line in hits if f"{OPT_IN_VAR}=greenlet" not in line)
    assert not offenders, "these --cov-context launches would run on sysmon and record only the FIRST test per line:\n" + "\n".join(offenders)


def _core_under(env_value: str | None) -> tuple[str, str]:
    """Start coverage with the repo's real config in a fresh process; return (core, concurrency)."""
    env = {k: v for k, v in os.environ.items() if k not in {OPT_IN_VAR, "COVERAGE_CORE", "COVERAGE_RCFILE"}}
    if env_value is not None:
        env[OPT_IN_VAR] = env_value
    probe = (
        "import coverage, sys\n"
        f"cov = coverage.Coverage(config_file={str(PYPROJECT_PATH)!r}, data_file=None)\n"
        "cov.start()\n"
        "info = dict(cov.sys_info())\n"
        "cov.stop()\n"
        "print(info['core'])\n"
        "print(','.join(sorted(set(cov.config.concurrency))))\n"
    )
    # A fresh interpreter: starting a second tracer inside this one would disturb the outer run's.
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, env=env, check=False, capture_output=True, text=True)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr
    core, concurrency = result.stdout.split()
    return core, concurrency


def test_the_default_config_runs_on_sysmon() -> None:
    """Unset, the config names no greenlet, so coverage.py is free to pick sys.monitoring."""
    assert _core_under(None) == ("SysMonitor", "thread")


def test_the_opt_in_runs_on_ctracer_with_greenlet() -> None:
    assert _core_under("greenlet") == ("CTracer", "greenlet,thread")


def _write_context_probe(tmp_path: Path) -> None:
    """A throwaway project wired with the repo's REAL concurrency list and warning filter."""
    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    concurrency = pyproject["tool"]["coverage"]["run"]["concurrency"]
    filters = pyproject["tool"]["pytest"]["ini_options"]["filterwarnings"]
    (tmp_path / "probed.py").write_text("def f(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8")
    (tmp_path / "test_probed.py").write_text(
        "from probed import f\n\n\ndef test_one():\n    assert f(1) == 1\n\n\ndef test_two():\n    assert f(1) == 1\n",
        encoding="utf-8",
    )
    # A JSON array of strings is also a valid TOML array, escaping included.
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        f"filterwarnings = {json.dumps(filters)}\n"
        'testpaths = ["."]\n'
        "\n[tool.coverage.run]\n"
        "branch = true\n"
        f"concurrency = {json.dumps(concurrency)}\n"
        'source = ["probed"]\n',
        encoding="utf-8",
    )


def _run_context_probe(tmp_path: Path, env_value: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {OPT_IN_VAR, "COVERAGE_CORE", "COVERAGE_RCFILE"}}
    env |= {"COVERAGE_FILE": str(tmp_path / ".coverage.ctxprobe"), "PYTHONPATH": str(tmp_path)}
    if env_value is not None:
        env[OPT_IN_VAR] = env_value
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--cov", "--cov-context=test", "--cov-fail-under=0", "--cov-report=", "-p", "no:cacheprovider", "-q"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_a_context_run_on_sysmon_fails_instead_of_warning(tmp_path: Path) -> None:
    """The repo's own warning filter turns the thinned-context case into a red run.

    Executed rather than read back: the filter is a regex against coverage.py's message text, and
    a coverage upgrade that rewords it would leave a filter that matches nothing and reads as armed.
    """
    _write_context_probe(tmp_path)
    result = _run_context_probe(tmp_path, None)
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"a --cov-context run on sysmon passed; the warning filter is not armed:\n{output}"
    assert "Dynamic contexts aren't supported" in output, output


def test_a_context_run_with_the_opt_in_records_every_test(tmp_path: Path) -> None:
    """With the opt-in, both tests that execute ``f`` are recorded against its lines."""
    _write_context_probe(tmp_path)
    result = _run_context_probe(tmp_path, "greenlet")
    assert result.returncode == 0, result.stdout + result.stderr

    data = coverage.CoverageData(str(tmp_path / ".coverage.ctxprobe"))
    data.read()
    [measured] = [name for name in data.measured_files() if name.endswith("probed.py")]
    contexts_by_line = data.contexts_by_lineno(measured)
    assert contexts_by_line, "no contexts were recorded at all"
    first_line_contexts = contexts_by_line[2]
    assert sorted(first_line_contexts) == ["test_probed.py::test_one|run", "test_probed.py::test_two|run"], contexts_by_line


@pytest.mark.parametrize("env_value", ["", "sysmon"])
def test_a_malformed_opt_in_fails_loudly(env_value: str) -> None:
    """Anything but unset or ``greenlet`` is a config error, never a silent fallback."""
    env = {k: v for k, v in os.environ.items() if k not in {OPT_IN_VAR, "COVERAGE_CORE", "COVERAGE_RCFILE"}}
    env[OPT_IN_VAR] = env_value
    probe = f"import coverage\ncoverage.Coverage(config_file={str(PYPROJECT_PATH)!r}, data_file=None).start()\n"
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, env=env, check=False, capture_output=True, text=True)  # noqa: S603
    assert result.returncode != 0, f"{OPT_IN_VAR}={env_value!r} was accepted:\n{result.stdout}{result.stderr}"
