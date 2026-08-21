"""Shared fixtures for the ``tests/shared`` bucket.

Currently one fixture: a behavioural harness for ``scripts/provision-test-seat.sh``
(phaze-bk9el.23), used by both ``test_redis_seat_registry.py`` and
``test_redis_worktree_isolation.py``.

WHY A HARNESS RATHER THAN A TEXT ASSERTION. Those two modules guard properties that were silently
lost once before (phaze-fwo7: every worktree landed on the same logical Redis DB): that seat
allocation goes through the atomic registry, and that the isolation workflow hands the engineer a
``PHAZE_REDIS_URL`` and not only the Postgres exports. They used to assert those as literal text
inside the ``test-db-for`` justfile recipe. phaze-bk9el.23 moved that body into
``scripts/provision-test-seat.sh`` so ``just test-validate`` could provision an IDENTICAL seat
instead of falling back to the shared ``phaze_test`` pair -- and a text assertion merely re-pointed
at the new file would still only check WHERE the code lives. A guard that checks where code lives
is weaker than one that checks what it does, so these run the script.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Callable


_REAL_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture
def run_provisioner() -> Callable[..., tuple[str, str]]:
    """Return ``run(tmp_path, seat=..., allocated_index=...) -> (stdout, recorded stub argv)``.

    The real ``provision-test-seat.sh`` is copied into a temp ``scripts/`` directory next to STUB
    siblings, because it resolves ``ensure-pg-database.sh`` and ``redis-seat-registry.sh`` through
    its own ``${script_dir}``. The stubs append the argv they were called with to a log and print
    what the real ones print on stdout, so this needs neither Docker nor a live Postgres/Redis and
    runs anywhere CI does.

    ``derive-seat-name.sh`` is copied REAL, not stubbed: the normalization it applies (phaze-fmfk --
    hyphens to underscores plus a hash of the raw name, so ``my-seat`` and ``my_seat`` cannot
    collide onto one seat) is part of the behaviour under test, not a dependency to fake out.
    """

    def run(tmp_path: Path, seat: str = "some-seat", allocated_index: str = "7") -> tuple[str, str]:
        scripts = tmp_path / "scripts"
        scripts.mkdir(exist_ok=True)
        for name in ("provision-test-seat.sh", "derive-seat-name.sh"):
            shutil.copy(_REAL_SCRIPTS / name, scripts / name)
        argv_log = tmp_path / "argv.log"
        for name, stdout in (("ensure-pg-database.sh", ""), ("redis-seat-registry.sh", allocated_index)):
            stub = scripts / name
            body = f'#!/usr/bin/env bash\nprintf "%s %s\\n" "{name}" "$*" >> "{argv_log}"\n'
            if stdout:
                body += f'printf "%s\\n" "{stdout}"\n'
            stub.write_text(body, encoding="utf-8")
            stub.chmod(0o755)
        bash = shutil.which("bash")
        assert bash is not None, "bash is not on PATH"
        result = subprocess.run(  # noqa: S603 - resolved executable; argv is a test literal
            [
                bash,
                str(scripts / "provision-test-seat.sh"),
                "--seat",
                seat,
                "--pg-container",
                "phaze-test-db",
                "--pg-port",
                "5433",
                "--redis-container",
                "phaze-test-redis",
                "--redis-port",
                "6380",
                "--redis-capacity",
                "64",
                "--origin",
                str(tmp_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"provisioner failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        return result.stdout, argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""

    return run
