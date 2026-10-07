"""One-shot operator script: re-queue tracklist lookups cached as NOT_FOUND by the captcha misclassification (phaze-c9go7).

BACKGROUND
----------
Before bug phaze-a6n3e (PR #674) a 1001Tracklists image captcha was classified ``NO_TRACKLIST`` by
the renderer, and the drain cached it as ``NOT_FOUND`` for the 180-day negative TTL. The only path
that could write it stores ``detail`` as ``render no_tracklist: ...`` (``_render_failed_attempt`` in
``phaze.services.tracklist_drain``). A genuine empty page stores the SAME detail, so the selected
rows are an UPPER BOUND on the false negatives, not a count of them.

WHAT IT DOES
------------
Moves ``expires_at`` of each in-scope row to ``now()``. That is the cache's own expiry semantics
(``phaze.services.tracklist_lookup_cache._decide``): an elapsed ``NOT_FOUND`` is
``NEGATIVE_EXPIRED``, which ``CacheDecision.should_query`` queues. Nothing is deleted;
``attempts``, ``first_attempted_at``, ``last_attempted_at``, ``detail`` and ``source_url`` are
preserved until the drain rewrites the row. A hard DELETE was rejected: it would lose the attempt
record, and ``expires_at = now()`` achieves the same re-lookup.

SELECTOR (re-derived from the live schema, not from the bead wording)::

    outcome = 'not_found'
    AND starts_with(detail, 'render no_tracklist')
    AND (expires_at IS NULL OR expires_at > now())

``starts_with`` rather than ``LIKE`` because ``_`` is a LIKE wildcard. Search-side ``not_found``
rows (a different ``detail``), FOUND rows and every other outcome are never touched. Rows already
expired are excluded: they are due already, and excluding them makes a second run select 0.

The same transaction records the re-queued ``set_key`` values in ``tracklist_requeue_c9go7`` (set
key and timestamp only) so the FOUND / NOT_FOUND / BLOCKED split after the drain re-looks them up
can be reported as a measurement (``--report``). Drop it when the measurement is done::

    DROP TABLE tracklist_requeue_c9go7;

DEPLOY CONDITION -- DO NOT RUN BEFORE THIS HOLDS
------------------------------------------------
The write must NOT run until the captcha fix is DEPLOYED to the running drain. If it runs against
unfixed code, a served captcha is re-cached as NOT_FOUND and the rows are back where they started.
As of 2026-10-07 the running containers were at image revision 110b7f98 (built 2026-10-05), which
contains no ``looks_like_captcha``; main does. This change does not claim the deploy has happened.
Verify the deployed revision before running, in the drain's container::

    python -c "import phaze.services.tracklist_render as m; print(hasattr(m, 'looks_like_captcha'))"

It must print ``True``.

OPERATOR APPROVAL (recorded 2026-10-07, bead phaze-c9go7): "you have my approval for the database
write for phaze-c9go7". That approval covers running this write ONCE, and does not lift the deploy
condition above.

USAGE
-----
Dry run (default; read-only; prints the exact SQL and the count it would change, counts only)::

    uv run python scripts/requeue_render_no_tracklist.py

Apply (single transaction; refuses unless the live count equals ``--expect-count``)::

    uv run python scripts/requeue_render_no_tracklist.py --apply --expect-count 116

The printed SQL script is the same text the tests execute; it can be fed to ``psql`` instead. It
carries its own guard that raises if the count differs from the expected one, so psql and
``--apply`` have the same refusal. Output is counts only: never rows, set keys, urls or ids.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from sqlalchemy import text


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


SELECTOR: str = "outcome = 'not_found' AND starts_with(detail, 'render no_tracklist') AND (expires_at IS NULL OR expires_at > now())"
"""The one place the in-scope rows are defined; every statement below embeds this exact text."""

SIDE_TABLE: str = "tracklist_requeue_c9go7"

COUNT_SQL: str = f"SELECT count(*) FROM tracklist_lookup_cache WHERE {SELECTOR}"  # noqa: S608 -- module constants only, no external input

CREATE_SIDE_TABLE_SQL: str = f"CREATE TABLE IF NOT EXISTS {SIDE_TABLE} (set_key varchar(64) PRIMARY KEY, requeued_at timestamptz NOT NULL)"

RECORD_SQL: str = (
    f"INSERT INTO {SIDE_TABLE} (set_key, requeued_at) SELECT set_key, now() FROM tracklist_lookup_cache WHERE {SELECTOR} "  # noqa: S608
    "ON CONFLICT (set_key) DO UPDATE SET requeued_at = EXCLUDED.requeued_at"
)

UPDATE_SQL: str = f"UPDATE tracklist_lookup_cache SET expires_at = now(), updated_at = now() WHERE {SELECTOR}"  # noqa: S608 -- module constants only

REPORT_SQL: str = (
    "SELECT CASE WHEN c.last_attempted_at < r.requeued_at THEN 'not_yet_looked_up_again' ELSE c.outcome END AS result, count(*) "  # noqa: S608
    f"FROM {SIDE_TABLE} r JOIN tracklist_lookup_cache c ON c.set_key = r.set_key GROUP BY 1 ORDER BY 1"
)
"""Counts per outcome of the re-queued sets. ``last_attempted_at < requeued_at`` = the drain has not re-looked it up yet."""


def guard_sql(expect_count: int) -> str:
    """A statement that raises (aborting the transaction) unless the in-scope count is exactly ``expect_count``."""
    expected = int(expect_count)
    return (
        "DO $guard$ BEGIN "  # noqa: S608
        f"IF (SELECT count(*) FROM tracklist_lookup_cache WHERE {SELECTOR}) <> {expected} THEN "
        f"RAISE EXCEPTION 'requeue refused: in-scope count differs from expected {expected}'; "
        "END IF; END $guard$"
    )


def apply_statements(expect_count: int) -> tuple[str, ...]:
    """The write, in execution order. Built once; both ``--apply`` and the psql script come from this."""
    return (guard_sql(expect_count), CREATE_SIDE_TABLE_SQL, RECORD_SQL, UPDATE_SQL)


def psql_script(expect_count: int) -> str:
    """The exact SQL as one psql-runnable transaction."""
    body = ";\n".join(apply_statements(expect_count))
    return f"BEGIN;\n{body};\nCOMMIT;\n"


async def count_in_scope(session: AsyncSession) -> int:
    """How many rows the selector currently matches."""
    return int((await session.execute(text(COUNT_SQL))).scalar_one())


async def apply_requeue(session: AsyncSession, expect_count: int) -> tuple[int, int]:
    """Run the write inside the caller's transaction; return ``(before, after)`` counts.

    Raises ``ValueError`` without executing any write when the live count differs from ``expect_count``.
    The caller owns commit/rollback, so a failure in any statement leaves nothing changed.
    """
    before = await count_in_scope(session)
    if before != expect_count:
        raise ValueError(f"refusing to apply: in-scope count {before} differs from --expect-count {expect_count}")
    for statement in apply_statements(expect_count):
        await session.execute(text(statement))
    return before, await count_in_scope(session)


async def report(session: AsyncSession) -> dict[str, int]:
    """Counts per result for the re-queued sets (empty when nothing was ever re-queued)."""
    exists = (await session.execute(text(f"SELECT to_regclass('{SIDE_TABLE}') IS NOT NULL"))).scalar_one()
    if not exists:
        return {}
    rows = (await session.execute(text(REPORT_SQL))).all()
    return {str(result): int(count) for result, count in rows}


async def _run(args: argparse.Namespace) -> int:
    from phaze.database import async_session  # noqa: PLC0415 -- keep `--help` and imports free of app settings

    async with async_session() as session:
        if args.report:
            for result, count in (await report(session)).items():
                print(f"{result}: {count}")  # noqa: T201
            return 0
        if not args.apply:
            count = await count_in_scope(session)
            print(f"selector: {SELECTOR}")  # noqa: T201
            print(f"SQL that --apply (or psql) would run, guarded at the current count:\n{psql_script(count)}")  # noqa: T201
            print(f"DRY RUN: {count} row(s) would be re-queued; nothing changed")  # noqa: T201
            return 0
        if args.expect_count is None:
            print("--apply requires --expect-count N (the dry-run count)", file=sys.stderr)  # noqa: T201
            return 2
        try:
            async with session.begin():
                before, after = await apply_requeue(session, args.expect_count)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)  # noqa: T201
            return 2
        print(f"before: {before} in-scope; after: {after} in-scope; re-queued: {before - after}")  # noqa: T201
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--apply", action="store_true", help="perform the write (default is a read-only dry run)")
    parser.add_argument("--expect-count", type=int, default=None, help="refuse to apply unless the live in-scope count equals this")
    parser.add_argument("--report", action="store_true", help="after the drain has run: counts per outcome of the re-queued sets")
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
