"""One-shot operator script: backfill ``files.original_filename_repaired`` (phaze-x4ux).

Standalone ``uv run`` script that connects through the app's configured ``phaze.database``
session factory (``DATABASE_URL`` / app settings -- same connection the running service uses) and
runs the idempotent :func:`phaze.services.text_repair_backfill.backfill_repaired_filenames` over
the whole ``files`` table. Safe to re-run: rows already backfilled are never re-selected (see that
module's docstring).

Usage::

    uv run python scripts/backfill_mojibake_filenames.py
"""

from __future__ import annotations

import asyncio

from phaze.database import async_session
from phaze.services.text_repair_backfill import backfill_repaired_filenames


async def _main() -> int:
    async with async_session() as session:
        return await backfill_repaired_filenames(session)


def main() -> None:
    visited = asyncio.run(_main())
    print(f"backfilled original_filename_repaired for {visited} file(s)")  # noqa: T201


if __name__ == "__main__":
    main()
