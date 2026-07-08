"""Thin ``python -m phaze.cli.shadow_compare`` runner over the shared shadow-compare core (Phase 79, D-01).

This is entry point B for the ONE state↔derived assertion core: a stdlib-argparse operator/rollout CLI
that opens an async session (the default app DB, or a ``--database-url`` override for a live-corpus
restore per D-02), calls the SAME :func:`phaze.services.shadow_compare.run_shadow_compare` -- with NO
duplicated invariant/comparison logic -- prints the :class:`~phaze.services.shadow_compare.Report`, and
returns exit code ``1`` iff any HARD invariant diverged (D-05). The live 200K run itself is DEFERRED to
homelab (D-02) and is NOT driven from here; this is the recorded operator path SC-3 references.

Design notes (mirrors ``phaze.cli.__init__``):
  - :func:`configure_logging` runs FIRST, before any DB session opens, so library/DB log lines render
    through the central structlog pipeline.
  - Secret discipline (T-79-04, cli/__init__.py:16-17): the ``--database-url`` DSN may carry a
    password. It is NEVER passed to ``print()`` or a logger; at most the host/db name is surfaced via
    :func:`sqlalchemy.engine.make_url` (which never exposes the password component we render).
  - ``--sample-cap`` is parsed as ``int`` (argparse ``type=int`` -- V5 input validation, T-79-05) and
    ``--database-url`` is handed straight to :func:`create_async_engine`, never string-concatenated
    into SQL; all queries stay ORM-only (inherited from the Plan-01 core).
"""

from __future__ import annotations

import argparse
import asyncio
from typing import TYPE_CHECKING

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from phaze.database import async_session
from phaze.logging_config import configure_logging
from phaze.services.shadow_compare import run_shadow_compare


if TYPE_CHECKING:
    from phaze.services.shadow_compare import Report


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the shadow-compare runner."""
    parser = argparse.ArgumentParser(
        prog="shadow-compare",
        description="Run the state↔derived shadow-compare gate against the target DB; exit nonzero on hard divergence (MIG-02, D-05).",
    )
    # `type=int` rejects a non-integer cap before any DB opens (input validation, T-79-05).
    parser.add_argument(
        "--sample-cap", dest="sample_cap", type=int, default=20, help="Max divergent file_id UUIDs sampled per invariant (default 20)."
    )
    parser.add_argument("--verbose", dest="verbose", action="store_true", help="Uncap the per-invariant sample (emit the full divergent set).")
    parser.add_argument(
        "--database-url",
        dest="database_url",
        default=None,
        help="Async SQLAlchemy DSN of a live-corpus restore to check (default: the app database). NEVER echoed in full.",
    )
    return parser


def _safe_target(database_url: str) -> str:
    """Return a password-free host/db description of ``database_url`` for operator output (T-79-04).

    Uses :func:`make_url` so the password component is never rendered -- only host and database name.
    """
    url = make_url(database_url)
    return f"{url.host or 'localhost'}/{url.database or '?'}"


async def _run(database_url: str | None, *, sample_cap: int, verbose: bool) -> Report:
    """Open a session (default app DB, or a ``--database-url`` restore) and run the shared core.

    When ``database_url`` is ``None`` the default :data:`phaze.database.async_session` sessionmaker is
    used; when provided, a fresh async engine is built from that DSN (a live-corpus restore, D-02) and
    disposed after the run. Either path calls the SAME :func:`run_shadow_compare` -- no duplicated logic.
    """
    if database_url is None:
        async with async_session() as session:
            return await run_shadow_compare(session, sample_cap=sample_cap, verbose=verbose)

    engine = create_async_engine(database_url)
    try:
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            return await run_shadow_compare(session, sample_cap=sample_cap, verbose=verbose)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns ``1`` iff any HARD invariant diverged, else ``0`` (D-05)."""
    # configure_logging FIRST -- before any DB session -- so DB/library log lines render consistently.
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.database_url is not None:
        # Print at most host/db -- the full DSN (which may carry a password) NEVER reaches stdout/logs.
        print(f"shadow-compare: target database {_safe_target(args.database_url)}")

    report = asyncio.run(_run(args.database_url, sample_cap=args.sample_cap, verbose=args.verbose))
    print(report.render(verbose=args.verbose))
    return 1 if report.hard_fail_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
