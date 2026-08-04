"""Learn per-release-group date-order conventions from the corpus (phaze-5fta.3).

## What this computes, and why the corpus can answer what a filename cannot

Scene filenames in this archive carry the event date as ``\\d{2}-\\d{2}-\\d{4}`` and nothing in the
string says which of the first two components is the day. The epic's measurement (whole archive,
2026-07-18): of 6,910 filenames carrying that shape, 2,430 (35.2%) are individually ambiguous --
both leading components ``<= 12``, so either reading is a real date. The other 4,480 resolve
themselves, because a component ``> 12`` can only be a day.

Grouped by the trailing scene release-group tag, those self-resolving files show UNANIMOUS
per-group conventions -- talion MM-DD (1,373 supporting / 0 contradicting), 1king DD-MM (232 / 0),
and so on. That is the whole point: one uploader is internally consistent, so their 4,480
self-resolving files answer the question their 2,430 ambiguous ones cannot. This module turns that
observation into rows in ``filename_convention``.

## Full-refresh cache semantics

``filename_convention`` is a CACHE of this computation (see the model's docstring), so this learner
is a **full refresh**, never an incremental update: it recomputes every
``(scope='release_group', convention_kind='date_order')`` row from the files corpus and REPLACES
the previous generation in ONE transaction. A bad run is therefore discarded wholesale and
regenerated -- there is no partially-updated state to reconcile, and no code path that could leave
half a generation behind. Concretely:

* the **read** phase streams the corpus in keyset-paged batches and holds no write lock and no
  transaction across the sweep;
* the **write** phase is one short transaction that takes a fixed advisory lock (so two concurrent
  learners serialize rather than racing DELETE-then-INSERT into a unique violation), deletes the
  rows this learner owns, inserts the new generation, and commits.

The delete is scoped to ``(scope, convention_kind)`` -- the exact key space this learner
recomputes -- and NOT to ``scope`` alone. A future ``separator_style`` learner will write
``scope='release_group'`` rows this one knows nothing about; deleting by scope alone would destroy
evidence this run cannot regenerate, which is the opposite of full-refresh safety.

## Contradictions are the signal, not noise

A self-resolving file whose order disagrees with its own group's majority is the ONE observation
that says the group's convention is not actually unanimous -- drift, a shared tag, or an extractor
error. Per the epic's PRECEDENCE RULES it is **logged with its filename and counted, never silently
dropped**: every contradiction gets its own structured log line (no per-group cap -- a cap IS the
silent drop this rule exists to forbid) and lands in ``contradicting_count``, which drives the
DB-derived ``confidence`` the phaze-5fta.4 gate reads. Swallowing them would leave a group reading
as unanimous when it is not, and that is precisely the failure that would make the whole store
untrustworthy.

## Out of scope, and deliberately non-fatal

Only ``\\d{2}-\\d{2}-\\d{4}`` is read. ``YYYY.MM.DD``, bare years, month names, and every other
date shape in the corpus are **skipped cleanly** and tallied under
:attr:`LearnerReport.skipped_no_date` -- they must never crash the sweep. Likewise a filename whose
release group cannot be claimed is counted by rejection reason
(:attr:`LearnerReport.rejection_reasons`) rather than discarded, so the operator can see how much
of the corpus the extractor declined and why.

``ambiguous_count`` is PAYOFF SIZING, not evidence: it counts how many individually-ambiguous
filenames in a group would benefit if the convention were applied. It never influences
``confidence`` -- the database's generated column already enforces that, and this module must not
try to work around it (it never writes ``confidence`` at all; Postgres rejects any statement that
names a ``GENERATED ALWAYS`` column).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
import re
from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import delete, func, insert, select
import structlog

from phaze.models.file import FileRecord
from phaze.models.filename_convention import FilenameConvention
from phaze.services.release_group import extract_release_group_detail


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

    SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


__all__ = [
    "CONVENTION_KIND",
    "CONVENTION_SCOPE",
    "DEFAULT_SCAN_BATCH_SIZE",
    "DateOrder",
    "DateReading",
    "DateVerdict",
    "GroupTally",
    "LearnerReport",
    "learn_release_group_date_conventions",
    "read_date_order",
]


logger = structlog.get_logger(__name__)


CONVENTION_SCOPE = "release_group"
"""The ``filename_convention.scope`` this learner owns. The epic's future scopes (``directory``,
``agent``, ``global``) are other learners' key spaces and are never touched here."""

CONVENTION_KIND = "date_order"
"""The ``filename_convention.convention_kind`` this learner owns -- the FIRST kind, not the only
one the table is designed for. Paired with :data:`CONVENTION_SCOPE` it bounds both the rows this
learner writes and the rows its full refresh is allowed to delete."""

DEFAULT_SCAN_BATCH_SIZE = 5_000
"""Corpus rows read per keyset page. The archive is ~250,000 files, so the sweep is paged rather
than materialized: each page is a short read, and no transaction spans the whole sweep."""

_ADVISORY_LOCK_KEY = f"filename_convention:{CONVENTION_SCOPE}:{CONVENTION_KIND}"
"""Application-defined key for the transaction-scoped advisory lock the WRITE phase takes. Two
concurrent full refreshes would otherwise interleave DELETE and INSERT and collide on
``uq_filename_convention_scope_scope_value_convention_kind``; serializing them makes the second run
a clean regeneration of the first's output instead of a unique violation."""

DATE_TOKEN_RE = re.compile(r"(?<![0-9])([0-9]{2})-([0-9]{2})-([0-9]{4})(?![0-9])")
"""The ONE date shape this learner reads. The lookarounds are load-bearing: without them a
``2014-04-05-2014`` run would match at an offset and invent a date the filename does not contain."""


class DateOrder(StrEnum):
    """Which component of ``NN-NN-YYYY`` is the day.

    The string values are the ``convention_value`` written to the store and read back by the
    phaze-5fta.4 gate, so they are part of the persisted contract -- they match the epic's own
    vocabulary (``MM-DD`` / ``DD-MM``) and must not be reworded.
    """

    DAY_FIRST = "DD-MM"
    MONTH_FIRST = "MM-DD"


class DateVerdict(StrEnum):
    """What one filename's date token says about its own order.

    ``NOT_A_DATE`` is distinct from ``NONE_PRESENT`` on purpose: ``13-32-2014`` matches the shape
    but is not a date under EITHER reading, which is a different fact about the corpus than "this
    filename has no ``\\d{2}-\\d{2}-\\d{4}`` token at all" and is counted separately.
    """

    NONE_PRESENT = "none_present"
    NOT_A_DATE = "not_a_date"
    MULTIPLE = "multiple"
    AMBIGUOUS = "ambiguous"
    DAY_FIRST = "day_first"
    MONTH_FIRST = "month_first"


@dataclass(frozen=True, slots=True)
class DateReading:
    """One filename's date verdict, the raw token it was read from, and that token's components."""

    verdict: DateVerdict
    raw: str | None = None
    first: int | None = None
    second: int | None = None
    year: int | None = None

    @property
    def order(self) -> DateOrder | None:
        """The self-resolved order, or ``None`` when the filename does not resolve itself."""
        if self.verdict is DateVerdict.DAY_FIRST:
            return DateOrder.DAY_FIRST
        if self.verdict is DateVerdict.MONTH_FIRST:
            return DateOrder.MONTH_FIRST
        return None

    def as_date(self, order: DateOrder) -> date | None:
        """The calendar date this token means when read in *order*, or ``None`` if it means none.

        Kept here rather than in the caller so the ONE place that knows which component is the day
        is the same place that decided the verdict. Returns ``None`` for a reading with no token at
        all -- callers resolving an ambiguous date pass the order they obtained elsewhere (a
        learned convention), and must still get a ``None`` rather than an exception for a filename
        that never carried a date.
        """
        if self.first is None or self.second is None or self.year is None:
            return None
        day, month = (self.first, self.second) if order is DateOrder.DAY_FIRST else (self.second, self.first)
        return _as_calendar_date(self.year, month, day)


def _as_calendar_date(year: int, month: int, day: int) -> date | None:
    """``date(year, month, day)`` or ``None`` -- the single legality test, delegated to the stdlib.

    Deliberately NOT a ``<= 12`` / ``<= 31`` range check. The calendar is the actual constraint the
    epic's "a component > 12 must be a day" rule is a shorthand for, and the shorthand is wrong at
    both ends: ``00`` and ``32`` are neither a month nor a day yet clear a naive ``> 12`` test as
    "definitely a day", and ``31-02-2014`` passes both range checks while naming no date at all.
    """
    try:
        return date(year, month, day)
    except ValueError:
        return None


def read_date_order(filename: str) -> DateReading:
    """Classify the ``\\d{2}-\\d{2}-\\d{4}`` date in *filename*. Pure, total, never raises.

    A component that cannot be a month can only be a day, which is what makes a filename
    self-resolving. Legality is decided by CONSTRUCTING the calendar date both ways
    (:func:`_as_calendar_date`), not by range-checking the components:

    * legal as ``DD-MM`` iff ``date(year, second, first)`` exists
    * legal as ``MM-DD`` iff ``date(year, first, second)`` exists

    Both legal is :attr:`DateVerdict.AMBIGUOUS` (the 35.2% the epic exists to resolve); exactly one
    legal is self-resolving; neither legal is :attr:`DateVerdict.NOT_A_DATE` -- which is the right
    verdict for ``00-13-2014`` AND for ``29-02-2014`` (February 29 does not exist in 2014, and 29
    is not a month), both of which a range check would have miscounted as evidence.

    A filename carrying two DIFFERENT date tokens returns :attr:`DateVerdict.MULTIPLE` and
    contributes no evidence. There is no way to tell the event date from an encode/rip date, and
    guessing would file one uploader's convention under a token that never described the event --
    the same bias toward ``None`` the release-group extractor takes. A token REPEATED verbatim is
    one date, not two, and is read normally.
    """
    tokens = DATE_TOKEN_RE.findall(filename)
    if not tokens:
        return DateReading(verdict=DateVerdict.NONE_PRESENT)
    if len(set(tokens)) > 1:
        return DateReading(verdict=DateVerdict.MULTIPLE)

    first_text, second_text, year_text = tokens[0]
    raw = f"{first_text}-{second_text}-{year_text}"
    first, second, year = int(first_text), int(second_text), int(year_text)
    day_first_legal = _as_calendar_date(year, second, first) is not None
    month_first_legal = _as_calendar_date(year, first, second) is not None

    if day_first_legal and month_first_legal:
        verdict = DateVerdict.AMBIGUOUS
    elif day_first_legal:
        verdict = DateVerdict.DAY_FIRST
    elif month_first_legal:
        verdict = DateVerdict.MONTH_FIRST
    else:
        verdict = DateVerdict.NOT_A_DATE
    return DateReading(verdict=verdict, raw=raw, first=first, second=second, year=year)


@dataclass(slots=True)
class GroupTally:
    """Running evidence for one release group, before it is reduced to a convention row."""

    day_first: int = 0
    month_first: int = 0
    ambiguous: int = 0

    @property
    def self_resolving(self) -> int:
        """Files in this group that answered the question themselves."""
        return self.day_first + self.month_first

    def winner(self) -> DateOrder | None:
        """The majority self-resolving order, or ``None`` on a tie (including 0-0).

        A tie is a real outcome, not an error: the group's files disagree exactly evenly, or the
        group has only ambiguous files. Either way there is nothing to apply, and the model's
        nullable ``convention_value`` exists for precisely this row -- it still records that the
        question was asked and how large the payoff would be if a later run answered it.
        """
        if self.day_first > self.month_first:
            return DateOrder.DAY_FIRST
        if self.month_first > self.day_first:
            return DateOrder.MONTH_FIRST
        return None

    def evidence(self) -> tuple[DateOrder | None, int, int]:
        """``(convention_value, supporting_count, contradicting_count)`` for this group.

        On a tie NOTHING is supported, so every self-resolving file is counted as contradicting the
        (absent) convention. That keeps the DB-derived ``confidence`` at 0.0 while preserving the
        honest total -- a group with 40 evenly-split files is a very different object from a group
        with none, and collapsing both to all-zeros would hide the split.
        """
        winner = self.winner()
        if winner is None:
            return None, 0, self.self_resolving
        if winner is DateOrder.DAY_FIRST:
            return winner, self.day_first, self.month_first
        return winner, self.month_first, self.day_first


@dataclass(slots=True)
class LearnerReport:
    """What one full refresh read, wrote and declined to use. JSON-safe via :meth:`as_dict`."""

    files_scanned: int = 0
    files_with_date: int = 0
    self_resolving: int = 0
    ambiguous: int = 0
    skipped_no_date: int = 0
    skipped_not_a_date: int = 0
    skipped_multiple_dates: int = 0
    skipped_no_release_group: int = 0
    rejection_reasons: Counter[str] = field(default_factory=Counter)
    groups_written: int = 0
    contradictions: int = 0
    rows_deleted: int = 0
    computed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Render as a JSON-safe mapping -- the SAQ task's return value."""
        return {
            "files_scanned": self.files_scanned,
            "files_with_date": self.files_with_date,
            "self_resolving": self.self_resolving,
            "ambiguous": self.ambiguous,
            "skipped_no_date": self.skipped_no_date,
            "skipped_not_a_date": self.skipped_not_a_date,
            "skipped_multiple_dates": self.skipped_multiple_dates,
            "skipped_no_release_group": self.skipped_no_release_group,
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "groups_written": self.groups_written,
            "contradictions": self.contradictions,
            "rows_deleted": self.rows_deleted,
            "computed_at": self.computed_at.isoformat() if self.computed_at is not None else None,
        }


async def _iter_corpus_filenames(session_factory: SessionFactory, *, batch_size: int) -> AsyncIterator[list[tuple[uuid.UUID, str]]]:
    """Yield keyset-paged ``(file_id, best-known filename)`` batches over the whole corpus.

    Pages by ``files.id`` rather than OFFSET so page N+1 costs the same as page 1 on a
    quarter-million-row table, and opens a FRESH short session per page so the months-long-archive
    sweep never pins one connection (or one snapshot) for its whole duration. Rows inserted
    mid-sweep may or may not be seen; that is fine and inherent to a cache recomputed on demand.

    Reads ``COALESCE(original_filename_repaired, original_filename)`` -- the best-known-clean text
    (phaze-x4ux). Mojibake in the tail would otherwise change the release-group token and file a
    group's evidence under a garbled key.
    """
    filename = func.coalesce(FileRecord.original_filename_repaired, FileRecord.original_filename)
    after: uuid.UUID | None = None
    while True:
        async with session_factory() as session:
            stmt = select(FileRecord.id, filename).order_by(FileRecord.id).limit(batch_size)
            if after is not None:
                stmt = stmt.where(FileRecord.id > after)
            rows = [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
        if not rows:
            return
        yield rows
        after = rows[-1][0]


def _tally_filename(name: str, tallies: dict[str, GroupTally], report: LearnerReport) -> tuple[str, DateOrder] | None:
    """Fold ONE filename into *tallies* / *report*. Returns ``(group, order)`` when it was evidence.

    Every early return here is a documented skip that the report counts -- an out-of-scope date
    shape, a shape-matching non-date, an undecidable multi-date filename, or a filename whose
    release group the extractor declined to claim. None of them raise, which is what keeps a sweep
    over a heterogeneous quarter-million-file archive from dying on its first oddity.
    """
    report.files_scanned += 1

    reading = read_date_order(name)
    if reading.verdict is DateVerdict.NONE_PRESENT:
        # YYYY.MM.DD, bare years, month names, no date at all: out of scope for this learner
        # (epic phaze-5fta.3), and explicitly NOT an error.
        report.skipped_no_date += 1
        return None
    if reading.verdict is DateVerdict.NOT_A_DATE:
        report.skipped_not_a_date += 1
        return None
    if reading.verdict is DateVerdict.MULTIPLE:
        report.skipped_multiple_dates += 1
        return None

    report.files_with_date += 1

    extraction = extract_release_group_detail(name)
    if extraction.group is None:
        report.skipped_no_release_group += 1
        report.rejection_reasons[str(extraction.reason)] += 1
        return None

    tally = tallies.setdefault(extraction.group, GroupTally())
    order = reading.order
    if order is None:
        report.ambiguous += 1
        tally.ambiguous += 1
        return None

    report.self_resolving += 1
    if order is DateOrder.DAY_FIRST:
        tally.day_first += 1
    else:
        tally.month_first += 1
    return extraction.group, order


def _log_contradictions(contradictions: dict[str, tuple[DateOrder, list[str]]], report: LearnerReport) -> None:
    """Log EVERY self-resolving file that disagrees with its own group's convention.

    Per the epic's PRECEDENCE RULES these are never silently dropped: each one gets its own line
    carrying the filename, because the filename is the only thing that lets an operator tell drift
    from a shared tag from an extractor bug. There is deliberately NO per-group cap -- a cap would
    reintroduce exactly the silent drop this rule forbids, and on the measured corpus every group's
    contradicting count is 0, so the volume is a non-issue in the healthy case. A group that
    suddenly logs thousands of lines is the alarm working.
    """
    for group, (winner, losing_names) in sorted(contradictions.items()):
        for name in losing_names:
            report.contradictions += 1
            logger.warning(
                "release-group date-order contradiction -- a self-resolving filename disagrees with its group's convention",
                scope=CONVENTION_SCOPE,
                scope_value=group,
                convention_kind=CONVENTION_KIND,
                convention_value=str(winner),
                filename=name,
            )


async def _replace_conventions(
    session_factory: SessionFactory,
    tallies: dict[str, GroupTally],
    *,
    computed_at: datetime,
    report: LearnerReport,
) -> None:
    """Replace this learner's whole key space in ONE transaction (full-refresh cache semantics).

    Lock, delete, insert, commit -- and nothing else in between, so the window in which the store
    is empty is a single short transaction that either lands whole or rolls back whole. A caller
    that raises anywhere in the sweep never reaches this function at all, which is why "a bad run
    is discarded and regenerated" needs no compensation logic.

    ``confidence`` is NEVER named in the INSERT: it is a Postgres ``GENERATED ALWAYS AS (...)
    STORED`` column and naming it -- even as ``None`` -- makes Postgres reject the statement
    outright (``column "confidence" can only be updated to DEFAULT``). The counts are written and
    the database derives the rest.
    """
    rows: list[dict[str, Any]] = []
    for group, tally in sorted(tallies.items()):
        convention_value, supporting, contradicting = tally.evidence()
        rows.append(
            {
                "id": uuid.uuid4(),
                "scope": CONVENTION_SCOPE,
                "scope_value": group,
                "convention_kind": CONVENTION_KIND,
                "convention_value": str(convention_value) if convention_value is not None else None,
                "supporting_count": supporting,
                "contradicting_count": contradicting,
                # Payoff sizing, NOT evidence -- excluded from `confidence` by the DB itself.
                "ambiguous_count": tally.ambiguous,
                "computed_at": computed_at,
            }
        )

    async with session_factory() as session:
        # Serialize concurrent full refreshes. Transaction-scoped, so it is released by the commit
        # (or the rollback) below with no unlock call to leak.
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(_ADVISORY_LOCK_KEY))))
        # A DELETE returns a CursorResult at runtime (exposing rowcount); the async stubs type it as
        # the base Result, so cast to read the affected-row count (mirrors routers/agent_push.py).
        deleted = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(FilenameConvention).where(
                    FilenameConvention.scope == CONVENTION_SCOPE,
                    FilenameConvention.convention_kind == CONVENTION_KIND,
                )
            ),
        )
        report.rows_deleted = deleted.rowcount or 0
        if rows:
            await session.execute(insert(FilenameConvention), rows)
        await session.commit()

    report.groups_written = len(rows)


async def learn_release_group_date_conventions(
    session_factory: SessionFactory,
    *,
    batch_size: int = DEFAULT_SCAN_BATCH_SIZE,
    now: datetime | None = None,
) -> LearnerReport:
    """Sweep the corpus, tally per-group date-order evidence, and full-refresh the store.

    Args:
        session_factory: the short-session factory (SAQ's ``ctx["async_session"]``). The read
            phase opens one session per keyset page; the write phase opens exactly one more.
        batch_size: corpus rows per keyset page. Bounds memory on the read side only -- the tallies
            themselves are one small record per release group, not per file.
        now: injectable clock for ``computed_at``. Defaults to ``datetime.now(UTC)``.

    Returns:
        The :class:`LearnerReport` for this run -- what was scanned, what was skipped and why, how
        many groups were written, and how many contradictions were found.

    Re-running is idempotent by construction: the write phase deletes this learner's whole key
    space before inserting, so a second run over an unchanged corpus produces the same rows, never
    duplicates (and never trips the unique constraint on the key triple).
    """
    report = LearnerReport()
    tallies: dict[str, GroupTally] = {}
    # Self-resolving filenames kept per (group, order) so the losers can be named in the log. The
    # winner is not known until the sweep ends -- a file only becomes a "contradiction" relative to
    # a majority that does not exist yet -- so both sides must be retained until then. This is one
    # string per SELF-RESOLVING file (4,480 on the measured archive), not per corpus file.
    by_group_names: dict[str, dict[DateOrder, list[str]]] = {}

    async for page in _iter_corpus_filenames(session_factory, batch_size=batch_size):
        for _file_id, name in page:
            evidence = _tally_filename(name, tallies, report)
            if evidence is None:
                continue
            group, order = evidence
            by_group_names.setdefault(group, {}).setdefault(order, []).append(name)

    contradictions: dict[str, tuple[DateOrder, list[str]]] = {}
    for group, tally in tallies.items():
        winner = tally.winner()
        if winner is None:
            continue
        loser = DateOrder.MONTH_FIRST if winner is DateOrder.DAY_FIRST else DateOrder.DAY_FIRST
        losing_names = by_group_names.get(group, {}).get(loser, [])
        if losing_names:
            contradictions[group] = (winner, losing_names)
    _log_contradictions(contradictions, report)

    computed_at = now or datetime.now(UTC)
    report.computed_at = computed_at
    await _replace_conventions(session_factory, tallies, computed_at=computed_at, report=report)

    logger.info(
        "release-group date-order conventions refreshed",
        scope=CONVENTION_SCOPE,
        convention_kind=CONVENTION_KIND,
        **report.as_dict(),
    )
    return report
