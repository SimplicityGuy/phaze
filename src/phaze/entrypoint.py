"""Application-server entrypoint shim (Phase 29 D-02 / RESEARCH Pattern 2).

Invoked from the api container as ``uv run python -m phaze.entrypoint``.
Runs ``phaze.cert_bootstrap.ensure_certs_present`` BEFORE uvicorn binds,
then ``os.execvp``-replaces this process with uvicorn so signals + PID-1
propagate cleanly (no shell, no subprocess wrapping).

Reads three env vars (all with safe defaults so dev-mode "just docker
compose up" works without any .env knobs):
    - ``PHAZE_CERTS_DIR``    -- default ``/certs`` (bind-mount target)
    - ``PHAZE_API_HOST``     -- default ``localhost`` (CN baked into the leaf)
    - ``PHAZE_API_TLS_SANS`` -- default ``localhost,127.0.0.1,api`` (SAN list)

The cert bootstrap is idempotent: containers that restart against an
existing populated ``/certs/`` skip re-generation and only ``execvp`` the
uvicorn process.

IMPORT-BOUNDARY INVARIANT (inherited from cert_bootstrap):
    MUST NOT import phaze.database, phaze.tasks.session, sqlalchemy.ext.asyncio,
    or phaze.config (no settings load at this layer -- the operator env vars
    are read directly via ``os.environ.get``). Verified transitively by
    ``tests/test_task_split.py::test_cert_bootstrap_stays_postgres_free``.
"""

from __future__ import annotations

import os
from pathlib import Path

from phaze.cert_bootstrap import ensure_certs_present


def main() -> None:
    """Run cert bootstrap, then ``execvp`` uvicorn."""
    certs_dir = Path(os.environ.get("PHAZE_CERTS_DIR", "/certs"))
    cn = os.environ.get("PHAZE_API_HOST", "localhost")
    sans = os.environ.get("PHAZE_API_TLS_SANS", "localhost,127.0.0.1,api")

    ensure_certs_present(certs_dir, cn=cn, sans_csv=sans)

    # os.execvp replaces this process with uvicorn so signals + PID-1 propagate
    # cleanly. uvicorn's ``--ssl-keyfile`` / ``--ssl-certfile`` flags require
    # the files to exist at process start; ``ensure_certs_present`` above is
    # the guarantee.
    keyfile = str(certs_dir / "phaze-server.key")
    certfile = str(certs_dir / "phaze-server.crt")
    # Intentional entrypoint exec via PATH-resolved `uv` -- this is the design.
    # The container always has `uv` on PATH; absolute pathing it would tie us to
    # the install prefix. S606 (untrusted-input-shell) is not applicable here:
    # this is a Python ``os.execvp``, not a shell. S607 (partial-path) is the
    # explicit choice -- the alternative is fragility.
    os.execvp(  # noqa: S606  # nosec B606 B607  # PATH-resolved `uv`; not a shell
        "uv",  # noqa: S607
        [
            "uv",
            "run",
            "uvicorn",
            "phaze.main:app",
            "--host",
            "0.0.0.0",  # noqa: S104  # nosec B104  # container-bound; bind-IP enforced at Docker port level
            "--port",
            "8000",
            "--ssl-keyfile",
            keyfile,
            "--ssl-certfile",
            certfile,
        ],
    )


if __name__ == "__main__":  # pragma: no cover  # CLI invocation guard
    main()
