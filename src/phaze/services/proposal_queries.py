"""Query service for proposal list page -- pagination, filtering, stats."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any

from sqlalchemy import ARRAY, DateTime, Update, bindparam, case, column, func, or_, select, update, values
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.like_escape import LIKE_ESCAPE_CHAR, like_wildcard


if TYPE_CHECKING:
    from collections.abc import Iterable
    import datetime as datetime_mod
    import uuid as uuid_mod

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.routers.column_sort import SortState

from phaze.services.collision import get_review_collision_ids


class ProposalTransitionError(Exception):
    """A proposal status write blocked by the documented state machine (phaze-uu17).

    Carries the current and attempted statuses so routers can translate it into a
    409 without re-querying. Terminal EXECUTED/FAILED rows (the authoritative record
    that a rename was applied) must never be flipped back to pending/approved/rejected.
    """

    def __init__(self, current_status: str, attempted_status: str) -> None:
        super().__init__(f"illegal transition {current_status} -> {attempted_status}")
        self.current_status = current_status
        self.attempted_status = attempted_status


class ProposalPendingConflictError(Exception):
    """Reverting a proposal to PENDING would violate the one-pending-per-file index (phaze-uu17)."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"file already has a pending proposal (proposal {proposal_id})")
        self.proposal_id = proposal_id


class ProposalStaleWriteError(Exception):
    """A single-proposal status write blocked because the row changed since it was rendered (phaze-exivg).

    ``update_proposal_status`` used to fold only the from-status into its conditional UPDATE's WHERE
    clause. ``store_proposals`` is an idempotent upsert that overwrites an existing PENDING row's
    content in place on a replayed ``generate_proposals`` (a SAQ retry, or ``recover_orphaned_work``'s
    verbatim ledger replay) -- same id, same status, new ``proposed_filename`` / ``proposed_path`` /
    ``confidence``. A row rewritten between the operator's page render and their Approve click still
    satisfied the status-only WHERE, so the write could commit content the operator never saw --
    exactly the TOCTOU class phaze-p35v closed for the confidence half of the bulk-approve predicate.

    Callers that pass ``expected_updated_at`` fold it into the SAME conditional UPDATE (one
    statement, no snapshot-then-write gap): the write only ever commits against the row's CURRENT
    ``updated_at`` at the moment it runs. This is raised when the row exists, is in an allowed
    from-state, but its live ``updated_at`` no longer matches the caller's token -- the router
    translates it into a 409 telling the operator to reload and re-review, instead of silently
    approving the swapped-in content.
    """

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"proposal {proposal_id} changed since it was loaded -- reload to re-review")
        self.proposal_id = proposal_id


class ProposalEditRefusedError(Exception):
    """A field-edit write blocked because the row is no longer editable (phaze-3mru).

    :func:`update_proposal_fields` has no "attempted status" -- an edit doesn't transition
    ``status`` at all -- so reusing :class:`ProposalTransitionError` there meant passing the row's
    CURRENT status as both the current and the attempted value, producing a self-contradictory 409
    detail like "illegal transition approved -> approved". This carries only the current status and
    names the refusal for what it is.
    """

    def __init__(self, current_status: str) -> None:
        super().__init__(f"edit refused: proposal is {current_status}, only PENDING rows are editable")
        self.current_status = current_status


@dataclass
class Pagination:
    """Pagination metadata for a page of results."""

    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        """Total number of pages."""
        if self.total == 0:
            return 1
        return math.ceil(self.total / self.page_size)

    @property
    def has_prev(self) -> bool:
        """Whether a previous page exists."""
        return self.page > 1

    @property
    def has_next(self) -> bool:
        """Whether a next page exists."""
        return self.page < self.total_pages

    @property
    def start(self) -> int:
        """1-based index of the first item on this page."""
        if self.total == 0:
            return 0
        return (self.page - 1) * self.page_size + 1

    @property
    def end(self) -> int:
        """1-based index of the last item on this page."""
        return min(self.page * self.page_size, self.total)


@dataclass
class ProposalStats:
    """Aggregate statistics for proposals.

    ``executed`` is its OWN count, not folded into ``approved`` (phaze-te2g3, ADR-0008 amendment).

    ADR-0008 maps the operator state *Approved* onto persisted ``approved`` OR ``executed``, and
    the obvious reading of that table is that ``approved`` here should be the union. It is
    deliberately not, and the reason is that this dataclass feeds the Execute stage, where the
    number an operator wants is "still to dispatch" -- persisted ``approved`` alone. Folding
    ``executed`` in would make the Execute card count work it has already done as work it is about
    to do, on the one screen that moves bytes.

    The defect that opened the bead was not the split, it was that ``executed`` was counted by
    ``total`` and by nothing else, so the per-status counts silently did not account for every row
    in ``total``. Carrying ``executed`` as a separate field is the presentation-only fix: no
    consumer's existing number changes, and every surface can show a set of counts that adds up.

    ``failed`` (operator vocabulary: *Blocked*) rides the SAME aggregate as its siblings
    (phaze-5uh4u), closing the accounting identity this type exists to support:
    ``pending + approved + executed + rejected + failed == total``. It was the last missing term --
    a ``failed`` proposal used to inflate ``total`` while appearing under no visible status, the same
    shape of gap ``executed`` had before phaze-te2g3.

    This is a presentation contract only. ADR-0008's requirement that ``approved`` and ``executed``
    stay DISTINCT in the persisted data is untouched -- nothing here migrates or collapses a status.
    """

    total: int
    pending: int
    approved: int
    executed: int
    rejected: int
    failed: int
    avg_confidence: float | None


@dataclass(frozen=True, slots=True)
class ProposalReviewToken:
    """The exact proposal snapshot an operator selected for bulk authorization."""

    proposal_id: uuid_mod.UUID
    updated_at: datetime_mod.datetime
    content_digest: str


def proposal_review_digest(proposal: RenameProposal) -> str:
    """Digest every displayed/authorized filename and destination value."""
    payload = {
        "id": str(proposal.id),
        "before_filename": proposal.file.original_filename,
        "before_destination": proposal.file.current_path,
        "proposed_filename": proposal.proposed_filename,
        "proposed_path": proposal.proposed_path,
        "confidence": proposal.confidence,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


async def count_pending_above_confidence(session: AsyncSession, threshold: float = 0.9) -> int:
    """Count PENDING proposals with confidence >= threshold -- read-only (phaze-rw14).

    phaze-7tiqp: this used to mirror the predicate of ``approve_pending_above_confidence``, the
    server-evaluated bulk approve behind ``PATCH /proposals/bulk-approve-high-confidence``. That
    route lost its last UI caller at ADR-0008 and was retired with its service function, so the
    predicate below is now stated in its own right rather than as a mirror of a sibling's.

    The Rename/Move workspaces' bulk-approve confirm dialog used to quote the RENDERED row count
    (``rename_proposals | length``, capped at 200 and unfiltered by confidence) as the number of
    proposals the action was about to approve. That is wrong on two axes at once: it is the
    render cap, not the true match count, and the render is ordered confidence-ASC, so the visible
    200 can hold zero rows meeting the threshold while the server-side predicate approves
    thousands. This gives the confirm dialog the real number instead.
    """
    stmt = (
        select(func.count())
        .select_from(RenameProposal)
        .where(
            RenameProposal.status == ProposalStatus.PENDING.value,
            RenameProposal.confidence >= threshold,
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one()


def _still_approvable(proposal: RenameProposal, token: ProposalReviewToken, collision_ids: set[str], threshold: float) -> bool:
    """Every precondition a selected row must STILL satisfy to be bulk-approved.

    Extracted verbatim from :func:`bulk_approve_selected_above_confidence`'s filter so the six
    predicates read as a named rule rather than a six-clause comprehension. All six must hold, so
    their order carries no meaning; it is kept as written to keep a diff against the shipped filter
    trivial to check.

    This is only the PYTHON-side pre-filter over rows already read. It does NOT replace the
    status / confidence / updated_at predicates carried inside the UPDATE itself -- those are what
    make the write safe under concurrency (see that function's own comment on why the statement
    cannot be collapsed to ``WHERE id = ANY(:ids)``). This filter is a cheap pre-pass, not the guard.
    """
    return (
        str(proposal.id) not in collision_ids
        and proposal.status == ProposalStatus.PENDING.value
        and proposal.confidence is not None
        and proposal.confidence >= threshold
        and proposal.updated_at == token.updated_at
        and proposal_review_digest(proposal) == token.content_digest
    )


async def bulk_approve_selected_above_confidence(
    session: AsyncSession,
    review_tokens: Iterable[ProposalReviewToken],
    *,
    threshold: float = 0.9,
) -> int:
    """Approve selected rows only when they are still pending and still meet ``threshold``.

    Changes Review keeps selection in browser/history state, so every submitted id is stale by
    definition. The status and confidence predicates therefore live in the UPDATE itself. A replay,
    concurrent action, confidence rewrite, malformed selection, or forged low-confidence id simply
    does not match and is reported through the returned transition count.
    """
    tokens = list(review_tokens)
    if not tokens:
        return 0
    token_by_id = {token.proposal_id: token for token in tokens}
    ids = list(token_by_id)
    proposals = (
        (
            await session.execute(
                select(RenameProposal)
                .options(selectinload(RenameProposal.file))
                .where(RenameProposal.id == func.any(bindparam("reviewed_proposal_ids", value=ids, type_=ARRAY(PGUUID(as_uuid=True)))))
            )
        )
        .scalars()
        .all()
    )
    collision_ids = await get_review_collision_ids(session)
    reviewed = [p for p in proposals if _still_approvable(p, token_by_id[p.id], collision_ids, threshold)]
    if not reviewed:
        await session.commit()
        return 0
    # ONE round trip regardless of selection size (phaze-r4am1). This used to be a
    # `for proposal in reviewed:` loop issuing one UPDATE ... RETURNING per row, which cost up to
    # ~50 sequential round trips inside a single web request (Changes Review paginates at 50).
    #
    # It could NOT be collapsed to `WHERE id = ANY(:ids)`: `updated_at` is the row's OWN
    # optimistic-concurrency token, so a shared-id predicate would silently drop the per-row check
    # that is the entire point of this function. Joining a VALUES list keeps every predicate
    # per-row -- each candidate id is matched against its own token -- so a stale token neither
    # fails the batch nor rides along inside it; it simply matches no row and is reported through
    # the returned transition count, exactly as when each row had its own statement.
    review_tokens_values = values(
        column("id", PGUUID(as_uuid=True)),
        column("updated_at", DateTime(timezone=True)),
        name="review_token",
    ).data([(proposal.id, token_by_id[proposal.id].updated_at) for proposal in reviewed])
    result = await session.execute(
        update(RenameProposal)
        .where(
            RenameProposal.id == review_tokens_values.c.id,
            RenameProposal.status == ProposalStatus.PENDING.value,
            RenameProposal.confidence >= threshold,
            RenameProposal.updated_at == review_tokens_values.c.updated_at,
        )
        .values(status=ProposalStatus.APPROVED.value)
        .returning(RenameProposal.id)
    )
    # The len-all-count rule has misread this. It rewrites `len(QUERY.all())` into
    # `QUERY.count()` to keep the count server-side, which assumes a SELECT. This is an
    # UPDATE ... RETURNING: the rows are the proposals this statement actually transitioned
    # under the optimistic-concurrency predicate, so there is no query object to call .count()
    # on and no second round trip to save -- RETURNING is already how the affected-row set
    # comes back. Rewriting it to satisfy the rule would mean dropping RETURNING for
    # `result.rowcount`, which is a behavioural change, not a cleanup.
    #
    # The marker below must stay on the line IMMEDIATELY above the statement: semgrep reads
    # nosemgrep from the preceding line only, so folding it into the paragraph above silently
    # stops suppressing.
    # nosemgrep: python.sqlalchemy.performance.performance-improvements.len-all-count
    applied = len(result.scalars().all())
    await session.commit()
    return applied


async def get_proposal_stats(session: AsyncSession) -> ProposalStats:
    """Get aggregate proposal statistics in a single query.

    ``executed`` and ``failed`` ride the SAME aggregate as their siblings (phaze-te2g3,
    phaze-5uh4u). Each is one more ``count(case(...))`` term over the scan this function already
    performs, so the new numbers cost no extra round trip and -- more importantly -- cannot
    disagree with ``total``. Reading either separately would reintroduce, between two reads, exactly
    the arithmetic the fields exist to fix.

    Every existing term is unchanged, deliberately: ``approved`` still counts persisted ``approved``
    ONLY. See :class:`ProposalStats` for why the ADR-0008 union is a presentation choice made per
    surface rather than baked in here.
    """
    stmt = select(
        func.count().label("total"),
        func.count(case((RenameProposal.status == ProposalStatus.PENDING, 1))).label("pending"),
        func.count(case((RenameProposal.status == ProposalStatus.APPROVED, 1))).label("approved"),
        func.count(case((RenameProposal.status == ProposalStatus.EXECUTED, 1))).label("executed"),
        func.count(case((RenameProposal.status == ProposalStatus.REJECTED, 1))).label("rejected"),
        func.count(case((RenameProposal.status == ProposalStatus.FAILED, 1))).label("failed"),
        func.avg(RenameProposal.confidence).label("avg_confidence"),
    ).select_from(RenameProposal)

    result = await session.execute(stmt)
    row = result.one()
    return ProposalStats(
        total=row.total,
        pending=row.pending,
        approved=row.approved,
        executed=row.executed,
        rejected=row.rejected,
        failed=row.failed,
        avg_confidence=float(row.avg_confidence) if row.avg_confidence is not None else None,
    )


async def get_proposals_page(
    session: AsyncSession,
    *,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    sort: SortState | None = None,
) -> tuple[list[RenameProposal], Pagination]:
    """Get a paginated, filtered, sorted page of proposals with eager-loaded file data.

    ``sort`` is a RESOLVED :class:`~phaze.routers.column_sort.SortState`, never a raw wire string
    (phaze-a6hm.10). This function therefore holds NO whitelist of its own: it used to map a
    ``sort_by`` string to a column through an ``if``/``elif`` ladder, which was a second, drifting
    implementation of the shared sortable-column contract and the exact defect that contract's rule
    2 exists to close. Resolution -- and with it the guarantee that an untrusted string never
    reaches a column -- now happens once, in the caller, against
    :data:`~phaze.routers.proposal_sort.PROPOSAL_SORT_COLUMNS`.

    ``None`` means "no operator preference", answered with the default confidence ordering rather
    than an unordered read: an OFFSET page over an unordered query is not a stable page.
    """
    base = select(RenameProposal).options(selectinload(RenameProposal.file))
    count_base = select(func.count()).select_from(RenameProposal)

    # Status filter
    if status is not None and status != "all":
        base = base.where(RenameProposal.status == status)
        count_base = count_base.where(RenameProposal.status == status)

    # Search filter. `like_wildcard` escapes LIKE metacharacters (`%`, `_`, `\`) in the operator's
    # raw text before wrapping it in `%...%`, so `_`-dense filenames (e.g. `set_live_2024.mp3`)
    # match literally instead of treating each `_` as a single-char wildcard, and a literal `\` in
    # `original_filename` round-trips instead of being silently consumed as an escape character
    # (phaze-0dd2).
    if search and search.strip():
        search_term = like_wildcard(search.strip())
        search_filter = or_(
            RenameProposal.proposed_filename.ilike(search_term, escape=LIKE_ESCAPE_CHAR),
            RenameProposal.file_id.in_(select(FileRecord.id).where(FileRecord.original_filename.ilike(search_term, escape=LIKE_ESCAPE_CHAR))),
        )
        base = base.where(search_filter)
        count_base = count_base.where(search_filter)

    # Sorting. The join is UNCONDITIONAL now that the ORDER BY is opaque: the whitelist may hand
    # back a FileRecord column (`original_filename`) and this function no longer knows which key it
    # resolved, so it can no longer decide per-key whether the join is needed. Joining always is
    # safe rather than merely convenient -- `RenameProposal.file_id` is a NOT NULL foreign key to
    # `files.id`, so this INNER join is row-preserving by schema constraint and cannot silently
    # drop proposals the old conditional join kept.
    base = base.join(RenameProposal.file)

    # `sort.order_by()` is the ONLY place a direction becomes SQL, and it can only ever yield an
    # expression some developer enumerated at import time. The trailing `RenameProposal.id` is a
    # TIEBREAKER, not display order (paging contract rule 4): the operator-chosen key ties often --
    # `confidence` is nullable and coarse, `proposed_path` repeats across a whole album -- and
    # OFFSET paging over a non-total order lets a row appear on two pages or none.
    order_by = sort.order_by() if sort is not None else (RenameProposal.confidence.asc(),)
    base = base.order_by(*order_by, RenameProposal.id.asc())

    # Count total
    count_result = await session.execute(count_base)
    total = count_result.scalar_one()

    # Clamp the requested page to [1, total_pages] BEFORE it reaches OFFSET (phaze-33sx). An
    # out-of-range page (e.g. a stale bookmark after a bulk approve/reject shrank the pending
    # set) must render the last real page instead of an empty one with an inverted
    # `Pagination.start > Pagination.end` range -- mirrors the `total_pages` math `Pagination`
    # itself uses so the clamp and the property agree.
    total_pages = math.ceil(total / page_size) if total > 0 else 1
    page = min(max(page, 1), total_pages)

    # Paginate
    base = base.offset((page - 1) * page_size).limit(page_size)

    result = await session.execute(base)
    proposals = list(result.scalars().all())

    pagination = Pagination(page=page, page_size=page_size, total=total)
    return proposals, pagination


def _guarded_status_update(
    proposal_id: uuid_mod.UUID,
    new_status: ProposalStatus,
    allowed_from: Iterable[ProposalStatus] | None,
    expected_updated_at: datetime_mod.datetime | None,
) -> Update:
    """Build :func:`update_proposal_status`'s conditional UPDATE, guards folded into the WHERE.

    Both guards are OPTIONAL and independent: ``None`` omits that predicate entirely, preserving
    the unconditional write for callers that pre-filter. Keeping them in the statement rather than
    as a Python check on a prior unlocked SELECT is the whole point (phaze-upnj / phaze-exivg) --
    see the caller's docstring for the two TOCTOUs that shape closes.
    """
    stmt = update(RenameProposal).where(RenameProposal.id == proposal_id)
    if allowed_from is not None:
        stmt = stmt.where(RenameProposal.status.in_([s.value for s in allowed_from]))
    if expected_updated_at is not None:
        stmt = stmt.where(RenameProposal.updated_at == expected_updated_at)
    return stmt.values(status=new_status.value)


async def _explain_unmatched_status_update(
    session: AsyncSession,
    proposal_id: uuid_mod.UUID,
    new_status: ProposalStatus,
    allowed_from: Iterable[ProposalStatus] | None,
    expected_updated_at: datetime_mod.datetime | None,
) -> RenameProposal | None:
    """Diagnose a conditional status UPDATE that matched ZERO rows, and raise the right error.

    The UPDATE matching nothing is ambiguous: the proposal may not exist, its current status may be
    outside ``allowed_from``, or its ``updated_at`` may no longer match the caller's token. Re-read
    to distinguish -- returning ``None`` for a genuinely absent row, and otherwise raising the
    specific error the router maps to a 409.
    """
    current = await session.execute(select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id))
    proposal = current.scalar_one_or_none()
    if proposal is None:
        return None
    if allowed_from is not None and proposal.status not in {s.value for s in allowed_from}:
        raise ProposalTransitionError(proposal.status, new_status.value)
    if expected_updated_at is not None and proposal.updated_at != expected_updated_at:
        raise ProposalStaleWriteError(str(proposal_id))
    if allowed_from is not None:
        # allowed_from was satisfied and (if checked) the token matched, yet the guarded
        # UPDATE still matched nothing -- a fresh row swap between the UPDATE and this
        # re-SELECT. Keep refusing rather than silently reporting success on a re-read that
        # is already stale itself.
        raise ProposalTransitionError(proposal.status, new_status.value)
    return proposal


async def update_proposal_status(
    session: AsyncSession,
    proposal_id: uuid_mod.UUID,
    new_status: ProposalStatus,
    allowed_from: Iterable[ProposalStatus] | None = None,
    expected_updated_at: datetime_mod.datetime | None = None,
) -> RenameProposal | None:
    """Update a single proposal's status and return it with eagerly loaded file.

    ``allowed_from`` gates the write to the documented state machine (phaze-uu17):
    when provided, a proposal whose current status is not in the set raises
    :class:`ProposalTransitionError` (routers translate it to 409) instead of
    silently overwriting a terminal EXECUTED/FAILED row. ``None`` preserves the
    legacy unconditional write for internal callers that already pre-filter.

    phaze-upnj: the guard is evaluated INSIDE the UPDATE's WHERE clause, in one
    statement (mirroring :func:`bulk_update_status`), NOT as a Python check on a
    prior unlocked SELECT. The former select-check-then-blind-write shape was a
    TOCTOU: under READ COMMITTED an operator Undo and a concurrent agent
    EXECUTED report both read APPROVED, both passed the Python guard, and the
    last committer silently reverted the terminal record. Folding ``allowed_from``
    into the UPDATE predicate makes the write itself conditional, so a row that
    left the allowed set between any read and this write matches zero rows and
    the transition is refused rather than clobbered.

    ``expected_updated_at`` (phaze-exivg) closes the SIBLING TOCTOU the status-only guard above
    leaves open: ``store_proposals`` can overwrite a still-PENDING row's content in place (same id,
    same status, new ``proposed_filename`` / ``proposed_path`` / ``confidence``) between the
    operator's page render and their Approve click, and the status-only WHERE still matches that
    swapped-in row. When provided, this is folded into the SAME conditional UPDATE, so the write
    only ever commits against the row's CURRENT ``updated_at``; a mismatch (row exists, is in an
    allowed from-state, but was touched since the caller's token was minted) raises
    :class:`ProposalStaleWriteError` instead of approving content the operator never reviewed.
    ``None`` (the default) skips the check entirely, preserving the pre-phaze-exivg behavior for
    every caller that does not carry a render-time token.
    """
    stmt = _guarded_status_update(proposal_id, new_status, allowed_from, expected_updated_at)
    try:
        cursor_result: Any = await session.execute(stmt)
        await session.commit()
    except IntegrityError as exc:
        # Reverting to PENDING can collide with the one-pending-per-file partial unique
        # index (uq_proposals_file_id_pending) if the file already has a pending proposal.
        await session.rollback()
        raise ProposalPendingConflictError(str(proposal_id)) from exc

    if int(cursor_result.rowcount) == 0:
        return await _explain_unmatched_status_update(session, proposal_id, new_status, allowed_from, expected_updated_at)

    # Re-fetch with selectinload to ensure file relationship is available
    # (session.refresh does not honor selectinload on lazy='raise' relationships)
    stmt2 = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result2 = await session.execute(stmt2)
    return result2.scalar_one_or_none()


async def bulk_update_status(
    session: AsyncSession,
    proposal_ids: list[uuid_mod.UUID],
    new_status: ProposalStatus,
    allowed_from: Iterable[ProposalStatus] | None = None,
) -> int:
    """Bulk-update status for multiple proposals. Returns number of rows updated.

    ``allowed_from`` constrains the UPDATE to rows in one of those from-states
    (phaze-uu17): terminal EXECUTED/FAILED rows selected by an "All"-tab bulk
    action are skipped rather than rewritten, and the returned count reflects only
    the rows that actually transitioned.
    """
    stmt = update(RenameProposal).where(RenameProposal.id.in_(proposal_ids))
    if allowed_from is not None:
        stmt = stmt.where(RenameProposal.status.in_([s.value for s in allowed_from]))
    stmt = stmt.values(status=new_status.value)
    cursor_result: Any = await session.execute(stmt)
    await session.commit()
    return int(cursor_result.rowcount)


async def update_proposal_fields(
    session: AsyncSession,
    proposal_id: uuid_mod.UUID,
    *,
    proposed_filename: str | None = None,
    proposed_path: str | None = None,
    allowed_from: Iterable[ProposalStatus] | None = None,
) -> RenameProposal | None:
    """Persist an operator edit to a proposal's ``proposed_filename`` / ``proposed_path`` (D-05).

    Mirrors :func:`update_proposal_status` -- an atomic conditional UPDATE -- but mutates the
    provided Text field(s) instead of ``.status``. The row keeps its status (edit is pre-approve --
    no status change) and the LLM is NOT re-run. Keeps the re-select-with-``selectinload(file)``
    tail so the returned row can render its diff. Returns ``None`` if the proposal does not exist.

    phaze-3tj4: ``allowed_from`` gates the edit to the given from-states, evaluated INSIDE the
    UPDATE's WHERE clause. The docstring's long-claimed "the row stays PENDING (edit is pre-approve)"
    invariant used to be prose-only: the write was unconditional, so an edit that landed after a
    concurrent approval silently rewrote the ``proposed_path`` an APPROVED row feeds straight into
    ``execution_dispatch`` -- redirecting a reviewed move to an unreviewed destination -- and edits
    to terminal EXECUTED/FAILED rows corrupted the historical record. With ``allowed_from`` the
    edit refuses (:class:`ProposalEditRefusedError` -> 409) rather than mutating a non-editable row.

    phaze-3mru: the refusal raises :class:`ProposalEditRefusedError`, not
    :class:`ProposalTransitionError`. An edit has no "attempted status" to name -- reusing the
    status-transition error meant passing the row's current status as both arguments, producing a
    409 detail like "illegal transition approved -> approved".
    """
    values: dict[str, Any] = {}
    if proposed_filename is not None:
        values["proposed_filename"] = proposed_filename
    if proposed_path is not None:
        values["proposed_path"] = proposed_path

    stmt = update(RenameProposal).where(RenameProposal.id == proposal_id)
    if allowed_from is not None:
        stmt = stmt.where(RenameProposal.status.in_([s.value for s in allowed_from]))
    stmt = stmt.values(**values)
    cursor_result: Any = await session.execute(stmt)
    await session.commit()

    if int(cursor_result.rowcount) == 0:
        # No row matched: 404 (proposal gone) vs 409 (status outside allowed_from).
        current = await session.execute(select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id))
        proposal = current.scalar_one_or_none()
        if proposal is None:
            return None
        if allowed_from is not None:
            raise ProposalEditRefusedError(proposal.status)
        return proposal

    # Re-fetch with selectinload to ensure the file relationship is available for the row render
    # (session.refresh does not honor selectinload on lazy='raise' relationships).
    stmt2 = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result2 = await session.execute(stmt2)
    return result2.scalar_one_or_none()


async def get_proposal_with_file(
    session: AsyncSession,
    proposal_id: uuid_mod.UUID,
) -> RenameProposal | None:
    """Get a single proposal with its associated file record."""
    stmt = select(RenameProposal).options(selectinload(RenameProposal.file)).where(RenameProposal.id == proposal_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
