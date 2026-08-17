"""Preflight manifest for the Execute workspace -- what a dispatch WOULD do, before it does it.

Execute is the product's final safety boundary: the one control that moves bytes on disk. Before
this module the workspace offered four aggregate proposal counters and a native ``hx-confirm``
prompt naming a single number, which is not enough to answer the question an operator actually has
at that boundary -- *which* operations run, against *what*, what is excluded and why, and what can
be undone afterwards.

D-10 DECISION RECORD -- the manifest reuses the dispatch path's own reads
=========================================================================

Every number here is produced by the SAME function ``POST /execution/start`` calls to do the work:

===========================================  ==========================================
manifest field                               dispatch-path source
===========================================  ==========================================
``operations`` / ``agents`` / ``total``      :func:`get_approved_proposals_grouped_by_agent`
``conflicts``                                :func:`detect_collisions`
``skipped_revoked``                          :func:`count_revoked_skipped_proposals`
===========================================  ==========================================

That is the whole point, and it is not incidental convenience. A manifest assembled from
independent queries is a *second* definition of "what will run", free to drift from the first one
silently -- and a preflight that disagrees with the dispatch it precedes is worse than no preflight,
because it is trusted. This mirrors the ``build_propose_list_context`` discipline already used where
two producers of one view would otherwise diverge. **If you add a step to ``start_execution``, add
its read here too.**

The reads are ordered to match ``start_execution`` exactly: collisions first (they short-circuit
dispatch there, so they must render as BLOCKING here), then the agent grouping, then the revoked
count. Read-only throughout -- no enqueue, no commit, no write, no Redis.

What actually participates (and what does not)
==============================================

``start_execution`` dispatches **approved RenameProposal rows only**. It does not dispatch tag
writes, duplicate resolution, or cue-sheet generation. Those are real operations with their own
queues and their own authorization records, and an operator who assumes EXECUTE APPROVED flushes
them is wrong in a way that costs a debugging session.

So the manifest reports them as *explicit exclusions with live counts*, never by omission. An
operation type absent from a manifest reads as "there is none of it"; the honest statement is "there
are 12 of these and this control will not run them, here is what will".

Per ADR-0008 one RenameProposal carries both the filename and destination decision, so the two
participating operation types are facets of one row, split on ``proposed_path``:

* ``""``   -> rename in place (keep the directory, apply the new filename)
* non-empty -> move to a new destination directory (and apply the new filename)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from phaze.models.agent import Agent
from phaze.services.collision import detect_collisions
from phaze.services.execution_dispatch import (
    count_revoked_skipped_proposals,
    get_approved_proposals_grouped_by_agent,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Copy-verify-delete is the executor's contract: the original is preserved on disk until the copy
# has been verified, and only then unlinked. That is what makes the reversibility text below a
# statement of fact rather than reassurance, and it is why the two participating operations carry
# DIFFERENT reversibility: an in-place rename has no second location to fall back to once renamed,
# while a move leaves the source intact until verification succeeds.
_RENAME_CONSEQUENCE = "Renames the file where it already sits. The directory is unchanged."
_MOVE_CONSEQUENCE = "Copies the file to its proposed directory, verifies the copy, then deletes the original."

_RENAME_REVERSIBILITY = "Reversible from the audit log: the previous filename is recorded before the rename."
_MOVE_REVERSIBILITY = "Reversible until the original is deleted; the delete happens only after the copy verifies."


@dataclass(frozen=True, slots=True)
class PreflightOperation:
    """One operation type that WILL run in this batch, with its real count and consequences."""

    key: str
    label: str
    count: int
    consequence: str
    reversibility: str


@dataclass(frozen=True, slots=True)
class PreflightAgent:
    """Per-agent affected-file count. Dispatch is grouped by agent, so this is the real unit of work."""

    agent_id: str
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class PreflightExclusion:
    """Work that exists but will NOT run in this batch, with the reason and the operator's next action.

    ``blocking`` distinguishes the two very different kinds. A blocking exclusion (a destination
    collision) stops the whole dispatch -- ``start_execution`` short-circuits on it before grouping.
    A non-blocking exclusion (pending review, a revoked agent, a tag write) simply is not part of
    this batch and does not prevent the rest from running.
    """

    key: str
    label: str
    count: int
    reason: str
    next_action: str
    blocking: bool
    # phaze-tzy6s.17: True when ``count`` is a FLOOR, not a total -- the producing read is capped and
    # saturated, so the real number is "count or more". The template renders "N+" and says so.
    #
    # This exists for exactly one row today, ``tagwrite``, and cannot be designed away: tag-write
    # eligibility is a Python predicate (``compute_proposed_tags`` over metadata + tracklist +
    # accepted Discogs link per candidate), so unlike ``dedupe`` and ``cue`` it has no SQL COUNT to
    # fall back on. The dishonest alternative was reporting the cap as a total, which on this screen
    # is a wrong number on the last thing an operator reads before an irreversible batch.
    at_least: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionPreflight:
    """The complete manifest rendered by the Execute workspace before an operator commits a batch."""

    total: int
    operations: list[PreflightOperation]
    agents: list[PreflightAgent]
    conflicts: list[tuple[str, int]]
    exclusions: list[PreflightExclusion]
    can_execute: bool
    blocked_reason: str | None
    blocked_next_action: str | None

    @property
    def excluded_total(self) -> int:
        """Every unit of work this batch will not touch, blocking or otherwise."""
        return sum(exclusion.count for exclusion in self.exclusions)

    @property
    def excluded_total_at_least(self) -> bool:
        """True when :attr:`excluded_total` is a floor because some contributing row is (phaze-tzy6s.17).

        A sum containing one saturated term is itself saturated, so the heading that renders
        ``excluded_total`` has to carry the same "+" the row does. Omitting it would put an exact-looking
        total directly above a row openly labelled approximate.
        """
        return any(exclusion.at_least for exclusion in self.exclusions)


async def get_execution_preflight(
    session: AsyncSession,
    *,
    pending: int,
    rejected: int,
    tagwrite_pending: int = 0,
    tagwrite_pending_at_least: bool = False,
    dedupe_pending: int = 0,
    cue_pending: int = 0,
) -> ExecutionPreflight:
    """Assemble the Execute preflight manifest. Read-only; no enqueue, no commit, no Redis.

    ``pending`` and ``rejected`` come from the caller's existing ``get_proposal_stats`` read rather
    than being re-queried here -- the Execute stage already loads it for the counter row, and one
    aggregate read serving both keeps the two counts on the page consistent by construction.

    The three ``*_pending`` counts describe adjacent work that this control does NOT dispatch. They
    default to 0 so a caller that cannot cheaply obtain them degrades to omitting the row rather
    than reporting a confident zero it did not measure.

    ``tagwrite_pending_at_least`` extends that same honesty rule to the other end of the range
    (phaze-tzy6s.17). A caller whose count is a saturated cap rather than a total passes True, and the
    row renders "N+". A caller that reported a capped read's length as a total would satisfy the letter
    of the paragraph above -- it measured something -- while breaking its intent, which is that no
    number on this manifest asserts more precision than the read behind it actually has.
    """
    # Ordered to match start_execution: collisions gate everything, so they are read first.
    conflicts = await detect_collisions(session)
    groups = await get_approved_proposals_grouped_by_agent(session)
    skipped_revoked = await count_revoked_skipped_proposals(session)

    agent_names = await _resolve_agent_names(session, list(groups))

    rename_in_place = 0
    moves = 0
    agents: list[PreflightAgent] = []
    for agent_id, items in groups.items():
        for item in items:
            if item.proposed_path:
                moves += 1
            else:
                rename_in_place += 1
        agents.append(PreflightAgent(agent_id=agent_id, name=agent_names.get(agent_id, agent_id), count=len(items)))
    agents.sort(key=lambda agent: (-agent.count, agent.name))

    total = rename_in_place + moves

    operations: list[PreflightOperation] = []
    if moves:
        operations.append(
            PreflightOperation(
                key="move",
                label="Move to new destination",
                count=moves,
                consequence=_MOVE_CONSEQUENCE,
                reversibility=_MOVE_REVERSIBILITY,
            )
        )
    if rename_in_place:
        operations.append(
            PreflightOperation(
                key="rename",
                label="Rename in place",
                count=rename_in_place,
                consequence=_RENAME_CONSEQUENCE,
                reversibility=_RENAME_REVERSIBILITY,
            )
        )

    exclusions = _build_exclusions(
        conflicts=conflicts,
        skipped_revoked=skipped_revoked,
        pending=pending,
        rejected=rejected,
        tagwrite_pending=tagwrite_pending,
        tagwrite_pending_at_least=tagwrite_pending_at_least,
        dedupe_pending=dedupe_pending,
        cue_pending=cue_pending,
    )

    # Two distinct disabled states, and they need different words. A collision means "you must fix
    # something"; an empty approved set means "there is nothing to do yet". Collapsing them into one
    # greyed-out button with no explanation is the defect this bead exists to close.
    blocked_reason: str | None = None
    blocked_next_action: str | None = None
    if conflicts:
        blocked_reason = (
            f"{len(conflicts)} destination{'' if len(conflicts) == 1 else 's'} would receive more than one file. "
            "Execution is blocked for the whole batch, not just the conflicting rows."
        )
        blocked_next_action = "Resolve the conflicting destinations in Changes Review, then return here."
    elif total == 0:
        blocked_reason = "No approved proposals are ready to execute."
        blocked_next_action = "Approve filename and destination changes in Changes Review first."

    return ExecutionPreflight(
        total=total,
        operations=operations,
        agents=agents,
        conflicts=conflicts,
        exclusions=exclusions,
        can_execute=total > 0 and not conflicts,
        blocked_reason=blocked_reason,
        blocked_next_action=blocked_next_action,
    )


def _build_exclusions(
    *,
    conflicts: list[tuple[str, int]],
    skipped_revoked: int,
    pending: int,
    rejected: int,
    tagwrite_pending: int,
    dedupe_pending: int,
    cue_pending: int,
    tagwrite_pending_at_least: bool = False,
) -> list[PreflightExclusion]:
    """Everything the batch will not touch, blocking first, then zero-count rows dropped.

    Zero-count rows are dropped rather than rendered as "0 excluded": a manifest's job is to make
    the exceptions visible, and a wall of zeroes buries the one row that is non-zero.
    """
    candidates = [
        PreflightExclusion(
            key="conflict",
            label="Destination conflicts",
            count=len(conflicts),
            reason="Two or more approved files target the same destination path.",
            next_action="Resolve in Changes Review — execution is blocked until none remain.",
            blocking=True,
        ),
        PreflightExclusion(
            key="revoked",
            label="Owned by a revoked agent",
            count=skipped_revoked,
            reason="The agent that owns these files has been revoked, so no executor can reach them.",
            next_action="Re-register the agent in Operations, then execute again.",
            blocking=False,
        ),
        PreflightExclusion(
            key="pending",
            label="Still needs review",
            count=pending,
            reason="Not yet approved. Only approved proposals are executed.",
            next_action="Review them in Changes Review.",
            blocking=False,
        ),
        PreflightExclusion(
            key="rejected",
            label="Rejected",
            count=rejected,
            reason="Explicitly rejected. These are never executed.",
            next_action="No action needed.",
            blocking=False,
        ),
        PreflightExclusion(
            key="tagwrite",
            label="Tag writes",
            count=tagwrite_pending,
            reason=(
                "Tag writes are authorized and dispatched separately (ADR-0008); this control does not run them."
                + (" At least this many — the queue scan is capped, so the real number may be higher." if tagwrite_pending_at_least else "")
            ),
            next_action="Dispatch them from the Tag Changes section of Changes Review.",
            blocking=False,
            at_least=tagwrite_pending_at_least,
        ),
        PreflightExclusion(
            key="dedupe",
            label="Duplicate groups",
            count=dedupe_pending,
            reason="Duplicate resolution is its own decision and is not part of an execute batch.",
            next_action="Resolve them in Duplicates.",
            blocking=False,
        ),
        PreflightExclusion(
            key="cue",
            label="Cue sheets",
            count=cue_pending,
            reason="Cue sheets are generated artifacts, not file operations, and are written on their own stage.",
            next_action="Generate them in Cue sheets.",
            blocking=False,
        ),
    ]
    return [exclusion for exclusion in candidates if exclusion.count > 0]


async def _resolve_agent_names(session: AsyncSession, agent_ids: list[str]) -> dict[str, str]:
    """Map agent id -> display name, mirroring ``start_execution``'s own name resolution."""
    if not agent_ids:
        return {}

    result = await session.execute(select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids)))
    return {row.id: row.name for row in result.all()}
