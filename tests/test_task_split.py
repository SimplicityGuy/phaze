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

    Phase 48 CLOUDAGENT-02 compute-agent invariant (reaffirmed here): a compute agent
    runs this EXACT module (``phaze.tasks.agent_worker``) on the SAME ``phaze-agent-<id>``
    queue and PUTs results to the SAME ``/api/internal/agent/analysis/{file_id}`` HTTP
    endpoint as a file-server agent — there is ZERO compute-specific worker code. Its
    security guarantee is therefore the import boundary this test already enforces: a
    compute agent reaches ONLY the SAQ Postgres broker (``saq.queue.postgres``, asserted
    present below) + cache Redis + the HTTP API, and NEVER the app ORM / async DB engine
    (``phaze.database`` / ``phaze.tasks.session`` / ``sqlalchemy.ext.asyncio``, asserted
    absent below). This proves a compute agent cannot reach the app's Postgres tables.

    The complementary "no media filesystem" half of CLOUDAGENT-02 is NOT import-enforced
    (the worker legitimately reads the scratch audio path it is handed) — it is a RUNTIME
    guarantee delivered by empty scan roots + no media mount on the cloud host, a Phase 51
    compose concern. Do NOT add an essentia/file-read ban here (48-RESEARCH §Pitfall 4).

    The subprocess sets the minimum env required for `phaze.config.get_settings()`
    to return AgentSettings without raising (Plan 01 validator):
    - PHAZE_ROLE=agent
    - PHAZE_AGENT_API_URL=http://test
    - PHAZE_AGENT_TOKEN=phaze_agent_test
    - PHAZE_AGENT_QUEUE=phaze-agent-test
    - PHAZE_QUEUE_URL=postgresql://...    (Phase 36 Postgres broker DSN; parsed at import,
      no connection — the PostgresQueue pool is built ``open=False``)
    - PHAZE_REDIS_URL=redis://localhost:6379/0   (cache plane; parsed at import; no connection)
    - PHAZE_AGENT_SCAN_ROOTS=/tmp (AgentSettings validator requires non-empty list)

    Phase 36 D-25 reinforcement: the broker is now ``PostgresQueue`` (psycopg3). Importing
    ``agent_worker`` therefore SHOULD pull the psycopg3 broker (``saq.queue.postgres``) but
    MUST NOT pull ``sqlalchemy.ext.asyncio`` — psycopg3/psycopg_pool are the agent role's
    only Postgres surface, and the ORM async engine staying out keeps the agent runnable on a
    host without the SQLAlchemy engine (TASK-01 + DIST-03). The test asserts both directions.
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_QUEUE", "phaze-agent-test-agent")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks.agent_worker  # noqa: F401

        # The ORM async engine must NEVER be dragged into the agent import graph. psycopg3
        # (psycopg / psycopg_pool / saq.queue.postgres) is explicitly NOT forbidden — it is
        # the Phase-36 broker the agent role is allowed to carry. Phase 53 (KSTAGE-02): the S3
        # SDK (aioboto3/botocore) MUST also stay out — the agent transfers bytes httpx-only over
        # presigned URLs and holds no bucket credentials (T-53-11).
        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio", "aioboto3", "botocore")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            # Print full importer chain for debugging which import dragged it in.
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)

        # Positive boundary: the Postgres broker (psycopg3) MUST be the wired backend — proves
        # the broker swap happened and that it carried psycopg3, not the SQLAlchemy ORM engine.
        if "saq.queue.postgres" not in sys.modules:
            sys.stderr.write("EXPECTED BROKER MISSING: saq.queue.postgres not imported\\n")
            sys.exit(2)
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


def test_push_task_stays_postgres_free() -> None:
    """Phase 50 (50-03) extension of D-25: phaze.tasks.push is Postgres-free.

    ``push_file`` runs on the fileserver agent worker (registered in agent_worker.settings)
    and must NOT drag the app ORM / async DB engine into the agent import graph. It imports
    only stdlib (asyncio/subprocess/pathlib/tempfile), phaze.config (no DB), phaze.schemas
    (Pydantic), and references PhazeAgentClient via ctx at runtime. Verified by subprocess so
    a contaminated import in the test process cannot poison downstream tests via sys.modules.
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
        import phaze.tasks.push  # noqa: F401

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
    assert result.returncode == 0, f"push task import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_upload_task_stays_postgres_free_and_sdk_free() -> None:
    """Phase 53 (53-03) extension of D-25: phaze.tasks.s3_upload is Postgres-free AND S3-SDK-free.

    ``upload_file_s3`` runs on the file-server agent worker (registered in
    agent_worker.settings) and transfers bytes httpx-only to presigned part URLs. It must NOT
    drag the app ORM / async DB engine NOR the S3 SDK (aioboto3/botocore) into the agent import
    graph (KSTAGE-02 / T-53-11). It imports only stdlib (asyncio/pathlib), phaze.config (no DB),
    phaze.schemas.agent_s3 (Pydantic), and references PhazeAgentClient via ctx at runtime.
    Verified by subprocess so a contaminated import cannot poison downstream tests via sys.modules.
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
        import phaze.tasks.s3_upload  # noqa: F401

        forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio", "aioboto3", "botocore")
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
    assert result.returncode == 0, f"s3_upload import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_submit_cloud_job_is_control_only_not_in_agent_worker() -> None:
    """Phase 54 (54-05): submit_cloud_job is a CONTROL-only function, never registered on the agent.

    It needs ``ctx["async_session"]`` + the kube creds that live on the control plane (DIST-01), so
    it is registered in ``phaze.tasks.controller.settings['functions']`` ONLY. This asserts the agent
    worker does NOT carry it: the agent worker is imported in a subprocess with the agent env (its
    module-level Queue construction requires PHAZE_AGENT_QUEUE), and ``submit_cloud_job`` must be
    absent from its registered function names. Run as a subprocess so the agent-role import + env do
    not leak into the in-process control-role test session.
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_QUEUE", "phaze-agent-test")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks.agent_worker as aw

        fn_names = {getattr(fn, "__name__", "") for fn in aw.settings["functions"]}
        if "submit_cloud_job" in fn_names:
            sys.stderr.write("submit_cloud_job must NOT be registered on the agent worker\\n")
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
    assert result.returncode == 0, f"submit_cloud_job leaked onto the agent worker:\nstdout={result.stdout}\nstderr={result.stderr}"

    # Complementary control-side assertion (in-process under the control-default test env): the task
    # IS a registered controller function, with no CronJob (Phase 55 owns the trigger).
    from phaze.tasks import controller

    fn_names = {getattr(fn, "__name__", "") for fn in controller.settings["functions"]}
    cron_names = {getattr(cj.function, "__name__", "") for cj in controller.settings["cron_jobs"]}
    assert "submit_cloud_job" in fn_names
    assert "submit_cloud_job" not in cron_names


def test_reconcile_cloud_jobs_is_control_only_not_in_agent_worker() -> None:
    """Phase 54 (54-06): reconcile_cloud_jobs is a CONTROL-only cron, never registered on the agent.

    The */5 reconcile cron needs ``ctx["async_session"]`` + the kube creds that live on the control
    plane (DIST-01), so it is registered in ``phaze.tasks.controller.settings`` ONLY -- in BOTH
    ``functions`` and ``cron_jobs`` (mirroring ``reap_stalled_scans``). This asserts the agent worker
    does NOT carry it (subprocess with the agent env so the agent-role import does not leak into the
    in-process control-role test session), AND that it is cron-only on the controller (a CronJob, and
    intentionally absent from ``enqueue_router.CONTROLLER_TASKS`` -- not operator-routable).
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_QUEUE", "phaze-agent-test")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_QUEUE_URL", "postgresql://phaze:phaze@localhost:5432/phaze")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks.agent_worker as aw

        fn_names = {getattr(fn, "__name__", "") for fn in aw.settings["functions"]}
        cron_names = {getattr(cj.function, "__name__", "") for cj in aw.settings.get("cron_jobs", [])}
        if "reconcile_cloud_jobs" in fn_names or "reconcile_cloud_jobs" in cron_names:
            sys.stderr.write("reconcile_cloud_jobs must NOT be registered on the agent worker\\n")
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
    assert result.returncode == 0, f"reconcile_cloud_jobs leaked onto the agent worker:\nstdout={result.stdout}\nstderr={result.stderr}"

    # Complementary control-side assertions (in-process under the control-default test env): it IS a
    # registered controller function AND a */5 CronJob, and is NOT in the routable CONTROLLER_TASKS set.
    from phaze.services.enqueue_router import CONTROLLER_TASKS
    from phaze.tasks import controller

    fn_names = {getattr(fn, "__name__", "") for fn in controller.settings["functions"]}
    reconcile_crons = [cj for cj in controller.settings["cron_jobs"] if getattr(cj.function, "__name__", "") == "reconcile_cloud_jobs"]
    assert "reconcile_cloud_jobs" in fn_names
    assert len(reconcile_crons) == 1, "reconcile_cloud_jobs must be registered as exactly one CronJob"
    assert reconcile_crons[0].cron == "*/5 * * * *", "the reconcile cron must run every 5 minutes (D-03)"
    assert "reconcile_cloud_jobs" not in CONTROLLER_TASKS, "reconcile_cloud_jobs is cron-only -- never operator-routable"


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


def test_job_runner_does_not_import_phaze_database() -> None:
    """Phase 52 (52-02) extension of D-25: phaze.job_runner is Postgres-free (T-52-02).

    The one-shot Kueue Job entrypoint runs in a DB-less pod that reaches ONLY the
    object store (presigned GET) + the control-plane HTTPS callback. It must NOT
    import the app ORM / async DB engine: phaze.database, phaze.tasks.session, or
    sqlalchemy.ext.asyncio. Verified by subprocess so a contaminated import in the
    test process cannot poison downstream tests via sys.modules caching.

    The subprocess sets the minimal agent env required for ``get_settings()`` to
    return AgentSettings without raising (the module imports phaze.config at load
    time, but the essentia-bound analyze import is deferred so module load stays
    Postgres-free AND essentia-free).
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.job_runner  # noqa: F401

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
    assert result.returncode == 0, f"job_runner import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_stage_control_stays_postgres_free() -> None:
    """Phase 37 T-37-04 invariant: phaze.tasks._shared.stage_control is Postgres-free.

    The ``apply_stage_control`` before-enqueue hook is registered on EVERY queue via
    ``build_pipeline_queue`` -- including the agent worker's queue. The agent never enqueues
    stage jobs, but it DOES import the hook module at queue construction, so the module must
    NOT pull in:
    - phaze.database
    - phaze.tasks.session
    - sqlalchemy.ext.asyncio

    The hook reads ``pipeline_stage_control`` through the queue's psycopg3 ``pool`` (NOT
    SQLAlchemy), so importing it under ``PHAZE_ROLE=agent`` must leave sys.modules clean.
    Parallels ``test_shared_bootstrap_stays_postgres_free`` and is verified by subprocess so a
    contaminated import cannot poison downstream tests via sys.modules caching.
    """
    script = textwrap.dedent("""
        import os
        import sys
        os.environ.setdefault("PHAZE_ROLE", "agent")
        os.environ.setdefault("PHAZE_AGENT_API_URL", "http://localhost:8000")
        os.environ.setdefault("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
        os.environ.setdefault("PHAZE_AGENT_SCAN_ROOTS", "/tmp")
        os.environ.setdefault("PHAZE_REDIS_URL", "redis://localhost:6379/0")
        import phaze.tasks._shared.stage_control  # noqa: F401

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
    assert result.returncode == 0, f"stage_control import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"
