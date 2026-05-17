"""D-25 import-boundary test: phaze.tasks.agent_worker must not transitively
import phaze.database, phaze.tasks.session, or sqlalchemy.ext.asyncio.

Run as a SUBPROCESS so a contaminated import in the test process doesn't
poison downstream tests via sys.modules caching.

This is the highest-leverage validation gate in Phase 26. If it goes red,
the agent role's ability to run on a host without Postgres reachability is
broken -- TASK-01 + DIST-03 invariants are violated.

Phase 27 D-22 extends this file with two parallel cases:
- ``test_agent_watcher_does_not_import_phaze_database``: same invariant
  applied to the new watcher entry point, PLUS ``phaze.tasks.agent_worker``
  is banned from the watcher's import graph (RESEARCH Pitfall 5 -- the
  watcher uses ``asyncio.run``, not SAQ, so dragging in agent_worker would
  fail at module-load without PHAZE_AGENT_QUEUE). Skipped pre-Plan-05 via
  ``importlib.util.find_spec`` -- becomes a hard gate once Plan 05 lands.
- ``test_shared_bootstrap_stays_postgres_free``: the new
  ``phaze.tasks._shared.agent_bootstrap`` module (Phase 27 D-17) must stay
  Postgres-free; runs immediately.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap

import pytest


def test_agent_worker_does_not_import_phaze_database() -> None:
    """Banned modules: phaze.database, phaze.tasks.session, sqlalchemy.ext.asyncio.

    The subprocess sets the minimum env required for `phaze.config.get_settings()`
    to return AgentSettings without raising (Plan 01 validator):
    - PHAZE_ROLE=agent
    - PHAZE_AGENT_API_URL=http://test
    - PHAZE_AGENT_TOKEN=phaze_agent_test
    - PHAZE_AGENT_QUEUE=phaze-agent-test
    - PHAZE_REDIS_URL=redis://localhost:6379/0   (parsed at import; no connection)
    - PHAZE_AGENT_SCAN_ROOTS=/tmp (AgentSettings validator requires non-empty list)
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_QUEUE", "phaze-agent-test-agent")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks.agent_worker  # noqa: F401

        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            # Print full importer chain for debugging which import dragged it in.
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run(  # noqa: S603  # trusted input: literal sys.executable + literal -c script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"agent_worker import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_agent_worker_module_import_fails_when_phaze_agent_queue_unset() -> None:
    """Module-import-time guard: missing PHAZE_AGENT_QUEUE raises RuntimeError before SAQ event loop starts.

    Runs in a subprocess because the module-level Queue construction is one-shot
    and would otherwise be cached for the whole pytest session via sys.modules.
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ["PHAZE_ROLE"] = "agent"
        os.environ["PHAZE_AGENT_API_URL"] = "http://localhost:8000"
        os.environ["PHAZE_AGENT_TOKEN"] = "phaze_agent_test-token-1234567890abcdef"
        os.environ["PHAZE_AGENT_SCAN_ROOTS"] = "/tmp"
        os.environ["PHAZE_REDIS_URL"] = "redis://localhost:6379/0"
        os.environ.pop("PHAZE_AGENT_QUEUE", None)
        try:
            import phaze.tasks.agent_worker  # noqa: F401
        except RuntimeError as exc:
            sys.stdout.write(str(exc))
            sys.exit(0)
        sys.exit(1)
    """)
    result = subprocess.run(  # noqa: S603  # trusted input
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"expected RuntimeError at import; got rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert "PHAZE_AGENT_QUEUE" in result.stdout


@pytest.mark.skipif(
    importlib.util.find_spec("phaze.agent_watcher") is None,
    reason="phaze.agent_watcher created in Plan 05; test becomes a hard gate then",
)
def test_agent_watcher_does_not_import_phaze_database() -> None:
    """Phase 27 D-22 extension of D-25: watcher must stay Postgres-free.

    Banned modules:
    - phaze.database / phaze.tasks.session / sqlalchemy.ext.asyncio (D-25 base)
    - phaze.tasks.agent_worker (RESEARCH Pitfall 5 -- the watcher uses
      asyncio.run, not SAQ; importing agent_worker would fail at module-load
      time when PHAZE_AGENT_QUEUE is unset, which is the watcher's normal env)

    PHAZE_AGENT_QUEUE is explicitly popped from the subprocess env to prove
    the watcher does NOT require it.
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        # Pitfall 5: watcher must NOT depend on PHAZE_AGENT_QUEUE
        os.environ.pop("PHAZE_AGENT_QUEUE", None)
        import phaze.agent_watcher  # noqa: F401

        forbidden = (
            "phaze.database",
            "phaze.tasks.session",
            "sqlalchemy.ext.asyncio",
            "phaze.tasks.agent_worker",
        )
        present = [m for m in forbidden if m in sys.modules]
        if present:
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run(  # noqa: S603  # trusted input: literal sys.executable + literal -c script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"agent_watcher import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_cert_bootstrap_stays_postgres_free() -> None:
    """Phase 29 D-22 extension of D-25: phaze.cert_bootstrap stays Postgres-free.

    The cert bootstrap runs in the api container's pre-uvicorn entrypoint
    (Phase 29 D-02 / RESEARCH Pattern 2). It must NOT import:
    - phaze.database
    - phaze.tasks.session
    - sqlalchemy.ext.asyncio

    Verified by subprocess so a contaminated import in the test process
    cannot poison downstream tests via sys.modules caching.

    No env vars are required: cert_bootstrap does not call get_settings().
    """
    script = textwrap.dedent("""
        import sys
        import phaze.cert_bootstrap  # noqa: F401

        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run(  # noqa: S603  # trusted input: literal sys.executable + literal -c script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"cert_bootstrap import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_shared_bootstrap_stays_postgres_free() -> None:
    """Phase 27 D-17 invariant: phaze.tasks._shared.agent_bootstrap is Postgres-free.

    The shared module imports only:
    - phaze.config (no DB)
    - phaze.services.agent_client (httpx + tenacity only)
    - phaze.schemas.agent_identity (Pydantic only)

    None of those pull in phaze.database, phaze.tasks.session, or
    sqlalchemy.ext.asyncio. This test fails CI if the shared module is
    later extended with a Postgres-touching import.
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks._shared.agent_bootstrap  # noqa: F401

        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run(  # noqa: S603  # trusted input: literal sys.executable + literal -c script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"shared bootstrap import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_model_bootstrap_stays_postgres_free() -> None:
    """Phase 29 D-21 invariant: phaze.tasks._shared.model_bootstrap is Postgres-free.

    Parallel to test_shared_bootstrap_stays_postgres_free (which covers
    agent_bootstrap.py only). The model_bootstrap module imports:
    - stdlib (logging, pathlib)
    - phaze.scripts.download_models (which imports httpx only)

    None of those pull in phaze.database, phaze.tasks.session, or
    sqlalchemy.ext.asyncio. This test fails CI if the model_bootstrap module
    is later extended with a Postgres-touching import (e.g., to track
    download progress in the DB).

    Phase 29 BLOCKER-1 resolution.
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_QUEUE", "phaze-agent-test")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks._shared.model_bootstrap  # noqa: F401

        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run(  # noqa: S603  # trusted input: literal sys.executable + literal -c script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"model_bootstrap import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"
