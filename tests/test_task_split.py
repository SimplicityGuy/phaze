"""D-25 import-boundary test: phaze.tasks.agent_worker must not transitively
import phaze.database, phaze.tasks.session, or sqlalchemy.ext.asyncio.

Run as a SUBPROCESS so a contaminated import in the test process doesn't
poison downstream tests via sys.modules caching.

This is the highest-leverage validation gate in Phase 26. If it goes red,
the agent role's ability to run on a host without Postgres reachability is
broken -- TASK-01 + DIST-03 invariants are violated.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


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
