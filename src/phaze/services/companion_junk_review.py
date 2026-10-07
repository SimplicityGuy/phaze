"""The junk-companion review queue: the detector, the group decisions and the state machine (phaze-bk5jp).

THE DETECTOR (:func:`detect_junk_reviews`) reads STORED facts only -- ``companion_content_features``,
``files`` and ``file_companions`` -- never the filesystem. For one agent it:

1. walks every companion row whose features are fresh (fingerprint equal to ``files.sha256_hash``;
   older features describe older bytes) by keyset page, and asks
   :func:`phaze.services.companion_content.never_link_reason` -- the one definition shared with the
   linking chain's veto -- whether it is junk or a duplicate orphan of an already-linked companion
   (operator decision 1 of 2026-10-07; every decision cited here is quoted, question and answer, in
   epic phaze-4x319's description), with the two deletion guards :func:`_reasons` names. Media rows
   are never read: the walk is restricted to companion file types, and the table's CHECK refuses
   anything else. A path under the quarantine directory is never proposed.
2. adds every current ``files`` row whose identity ``(agent_id, original_path, sha256_hash)`` was
   quarantined before: the file came back, so it goes back to review (decision 4, "Back to review"),
   as a NEW pending row beside the quarantined one, under its current reason or, if none, the reason
   it was quarantined for. It is never approved automatically, whatever its content group's state.
3. drops every candidate whose SHA-256 carries a rejection anywhere (decision 5, "Every identical
   copy"): a rejected content is not proposed again, at any path, on any agent.
4. reconciles against the live (non-terminal) rows: a new identity gets a pending row, a pending row
   gets its current reason, size and file id, a pending row that is no longer a candidate is
   withdrawn (deleted -- a pending row is no decision), and a decided row is left alone.

WHICH DUPLICATES. Only copies of an already-LINKED companion, in a folder with no media of their own.
Operator decision 2026-10-07 (dispatch session, AskUserQuestion; durable record: a comment on bead
phaze-bk5jp). Question as put: "Junk review, duplicates: your decision 1 was about 2,797 orphan
companions that are byte-identical copies of an IMPORTED companion (333 of them junk). phaze-bk5jp's
acceptance criterion narrowed that to copies of an already-LINKED companion, in a folder with no media
of their own. Replayed on the survey data, that queues 99 duplicates (4,760 review rows in total).
Which rule should the junk review use?" Answer as given (selected label): "Linked copies only
(Recommended)".

LINKS CHANGE UNDER THE QUEUE. "Duplicate" is a fact about the links stored when it is judged, and
the linking chain re-derives every link (phaze-rmhfr). So a duplicate is re-judged twice: on every
detector pass (a pending row whose reason no longer holds is withdrawn) and at the moment the operator
approves it (:func:`decide_content_group` withdraws, instead of approving, a pending duplicate that is
no longer one). Rows are keyed on ``(agent_id, original_path, sha256_hash)``, never on ``files.id``.

THE DECISIONS are by content group (:func:`decide_content_group`) -- one decision per identical
content, applied to every row with that SHA-256 -- and per row (:func:`transition_review`, for the
quarantine task). Both are single conditional ``UPDATE`` statements over :data:`TRANSITIONS`, so a
row can only move along an allowed edge, and nothing ever leaves a terminal status. A decision
records its time, ``decided_at``, and nothing about who made it (decision 7, "Timestamp only").

Nothing here commits; the caller owns the transaction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.constants import is_quarantined
from phaze.enums.junk_review import TERMINAL_STATUSES, JunkReviewStatus, allowed_from
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_junk_review import LIVE_IDENTITY_WHERE, CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.services.bulk_insert import chunk_rows
from phaze.services.companion_content import COMPANION_FILE_TYPES, linked_copy_fingerprints, never_link_reason


if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


DETECT_PAGE_SIZE = 1_000
"""Rows per keyset page and ids/hashes per ``IN`` list: one bind each, far under asyncpg's 32,767 cap."""

Identity = tuple[str, str]
"""``(original_path, sha256_hash)`` -- the natural identity within one agent."""


class JunkReviewTransitionRefused(Exception):
    """A row was asked to move along an edge :data:`TRANSITIONS` does not allow (including out of a terminal status)."""

    def __init__(self, review_id: uuid.UUID, current: str, target: JunkReviewStatus) -> None:
        super().__init__(f"junk review {review_id} is {current}; it cannot become {target.value}")
        self.review_id = review_id
        self.current = current
        self.target = target


@dataclass
class DetectionOutcome:
    """What one detector pass found for one agent, and (with ``apply``) wrote."""

    agent_id: str
    created: Counter[str] = field(default_factory=Counter)
    """New pending rows, by reason."""
    reappeared: int = 0
    """Of ``created``: identities that were quarantined before and came back (decision 4)."""
    refreshed: int = 0
    """Pending rows still candidates, kept (their reason, size and file id brought current)."""
    withdrawn: int = 0
    """Pending rows no longer candidates, deleted."""
    decided: int = 0
    """Candidates that already carry a live decision (approved, rejected, executing), left alone."""
    rejected_content: int = 0
    """Candidates skipped because their SHA-256 was rejected (decision 5)."""


def _pages(values: Sequence[Any]) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), DETECT_PAGE_SIZE):
        yield values[start : start + DETECT_PAGE_SIZE]


_PER_FILE_JUNK: frozenset[str] = frozenset({"empty", "all_nul", "site_ad"})
"""Classes the agent decides from one file's bytes; on a truncated read it saw only the file's head."""

DUPLICATE_REASON = "duplicate"
"""What :func:`phaze.services.companion_content.never_link_reason` returns for a duplicate orphan."""


def _features_columns() -> tuple[Any, ...]:
    return (
        FileRecord.id,
        FileRecord.original_path,
        FileRecord.sha256_hash,
        FileRecord.file_type,
        FileRecord.file_size,
        CompanionContentFeatures.junk_class,
        CompanionContentFeatures.truncated,
        CompanionContentFeatures.folder_media_count,
    )


def _fresh_companions(agent_id: str) -> Any:
    """The agent's companion rows joined to features that describe their CURRENT bytes."""
    return (
        select(*_features_columns())
        .join(CompanionContentFeatures, CompanionContentFeatures.file_id == FileRecord.id)
        .where(
            FileRecord.agent_id == agent_id,
            FileRecord.file_type.in_(COMPANION_FILE_TYPES),
            CompanionContentFeatures.fingerprint == FileRecord.sha256_hash,
        )
    )


async def _reasons(session: AsyncSession, agent_id: str, rows: Sequence[Any]) -> list[tuple[Any, str]]:
    """``(row, reason)`` for every row of one page that is a candidate, judged on the links stored NOW.

    The verdict is :func:`never_link_reason` -- THE definition the linking chain's veto shares. Two
    guards are added here because they are about DELETING a file, which linking never does:

    - **A per-file class on a truncated read is not trusted.** ``empty`` / ``all_nul`` / ``site_ad``
      describe only the first ``MAX_FEATURE_BYTES`` of a larger file, so the predicate is asked with
      no junk class. ``known_stamp`` is a whole-file fingerprint verdict and stands.
    - **A companion that is itself linked is never proposed as a duplicate.** Two linked copies in
      media-less folders each have "an identical copy linked"; proposing both would delete the content
      outright, where operator decision 1 (2026-10-07, epic phaze-4x319) is about copies OF a linked
      companion. The linked copy is the one that survives.
    """
    ids = [row.id for row in rows]
    linked = set((await session.execute(select(FileCompanion.companion_id).where(FileCompanion.companion_id.in_(ids)).distinct())).scalars())
    linked_fingerprints = await linked_copy_fingerprints(session, agent_id, {row.sha256_hash for row in rows})
    out: list[tuple[Any, str]] = []
    for row in rows:
        if is_quarantined(row.original_path):
            continue
        junk_class = None if row.truncated and row.junk_class in _PER_FILE_JUNK else row.junk_class
        reason = never_link_reason(
            junk_class,
            folder_has_media=row.folder_media_count > 0,
            # An unlinked row is never its own linked copy, so membership means ANOTHER copy is linked.
            identical_copy_linked=row.sha256_hash in linked_fingerprints,
        )
        if reason is None or (reason == DUPLICATE_REASON and row.id in linked):
            continue
        out.append((row, reason))
    return out


async def _content_candidates(session: AsyncSession, agent_id: str) -> dict[Identity, dict[str, Any]]:
    """Step 1: every fresh-featured companion row :func:`_reasons` names, keyed by identity."""
    candidates: dict[Identity, dict[str, Any]] = {}
    after: uuid.UUID | None = None
    while True:
        statement = _fresh_companions(agent_id).order_by(FileRecord.id).limit(DETECT_PAGE_SIZE)
        if after is not None:
            statement = statement.where(FileRecord.id > after)
        page = (await session.execute(statement)).all()
        if not page:
            return candidates
        after = page[-1].id
        for row, reason in await _reasons(session, agent_id, page):
            candidates[(row.original_path, row.sha256_hash)] = _row_values(agent_id, row, reason)


async def _reappeared(session: AsyncSession, agent_id: str, candidates: dict[Identity, dict[str, Any]]) -> set[Identity]:
    """Step 2: add every current files row whose identity was quarantined before; returns those identities."""
    identities: set[Identity] = set()
    after: uuid.UUID | None = None
    while True:
        statement = (
            select(
                CompanionJunkReview.id.label("review_id"),
                CompanionJunkReview.reason,
                FileRecord.id,
                FileRecord.original_path,
                FileRecord.sha256_hash,
                FileRecord.file_type,
                FileRecord.file_size,
            )
            .join(
                FileRecord,
                and_(
                    FileRecord.agent_id == CompanionJunkReview.agent_id,
                    FileRecord.original_path == CompanionJunkReview.original_path,
                    FileRecord.sha256_hash == CompanionJunkReview.sha256_hash,
                ),
            )
            .where(
                CompanionJunkReview.agent_id == agent_id,
                CompanionJunkReview.status == JunkReviewStatus.QUARANTINED,
                FileRecord.file_type.in_(COMPANION_FILE_TYPES),
            )
            .order_by(CompanionJunkReview.id)
            .limit(DETECT_PAGE_SIZE)
        )
        if after is not None:
            statement = statement.where(CompanionJunkReview.id > after)
        page = (await session.execute(statement)).all()
        if not page:
            return identities
        after = page[-1].review_id
        for row in page:
            if is_quarantined(row.original_path):
                continue
            identity = (row.original_path, row.sha256_hash)
            identities.add(identity)
            candidates.setdefault(identity, _row_values(agent_id, row, row.reason))


def _row_values(agent_id: str, row: Any, reason: str) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "original_path": row.original_path,
        "sha256_hash": row.sha256_hash,
        "file_id": row.id,
        "file_type": row.file_type,
        "file_size": row.file_size,
        "reason": reason,
        "content_group": row.sha256_hash,
    }


async def _rejected_hashes(session: AsyncSession, hashes: Collection[str]) -> set[str]:
    """The SHA-256 values among ``hashes`` that carry a rejection, on any agent (decision 5)."""
    rejected: set[str] = set()
    for page in _pages(sorted(hashes)):
        rejected.update(
            (
                await session.execute(
                    select(CompanionJunkReview.sha256_hash)
                    .where(CompanionJunkReview.sha256_hash.in_(page), CompanionJunkReview.status == JunkReviewStatus.REJECTED)
                    .distinct()
                )
            ).scalars()
        )
    return rejected


async def _live_rows(session: AsyncSession, agent_id: str) -> dict[Identity, tuple[uuid.UUID, str, tuple[Any, ...]]]:
    """The agent's non-terminal rows: ``identity -> (id, status, (reason, file_id, file_size, file_type))``."""
    live: dict[Identity, tuple[uuid.UUID, str, tuple[Any, ...]]] = {}
    after: uuid.UUID | None = None
    while True:
        statement = (
            select(
                CompanionJunkReview.id,
                CompanionJunkReview.original_path,
                CompanionJunkReview.sha256_hash,
                CompanionJunkReview.status,
                CompanionJunkReview.reason,
                CompanionJunkReview.file_id,
                CompanionJunkReview.file_size,
                CompanionJunkReview.file_type,
            )
            .where(CompanionJunkReview.agent_id == agent_id, CompanionJunkReview.status.not_in(TERMINAL_STATUSES))
            .order_by(CompanionJunkReview.id)
            .limit(DETECT_PAGE_SIZE)
        )
        if after is not None:
            statement = statement.where(CompanionJunkReview.id > after)
        page = (await session.execute(statement)).all()
        if not page:
            return live
        after = page[-1].id
        for row in page:
            live[(row.original_path, row.sha256_hash)] = (row.id, row.status, (row.reason, row.file_id, row.file_size, row.file_type))


async def detect_junk_reviews(session: AsyncSession, agent_id: str, *, apply: bool) -> DetectionOutcome:
    """Run the detector over one agent (see the module docstring); write only when ``apply``. Does NOT commit."""
    outcome = DetectionOutcome(agent_id=agent_id)
    candidates = await _content_candidates(session, agent_id)
    reappeared = await _reappeared(session, agent_id, candidates)

    rejected = await _rejected_hashes(session, {sha for _, sha in candidates})
    for identity in [identity for identity in candidates if identity[1] in rejected]:
        del candidates[identity]
        outcome.rejected_content += 1

    live = await _live_rows(session, agent_id)
    inserts: list[dict[str, Any]] = []
    refreshes: list[tuple[uuid.UUID, dict[str, Any]]] = []
    for identity, values in sorted(candidates.items()):
        current = live.get(identity)
        if current is None:
            inserts.append(values)
            outcome.created[values["reason"]] += 1
            outcome.reappeared += identity in reappeared
        elif current[1] == JunkReviewStatus.PENDING:
            outcome.refreshed += 1
            if current[2] != (values["reason"], values["file_id"], values["file_size"], values["file_type"]):
                refreshes.append((current[0], values))
        else:
            outcome.decided += 1
    withdraw = sorted(row_id for identity, (row_id, status, _) in live.items() if status == JunkReviewStatus.PENDING and identity not in candidates)
    outcome.withdrawn = len(withdraw)
    if not apply:
        return outcome

    for rows in chunk_rows(inserts):
        # A concurrent pass may have inserted the same identity: its row stands.
        statement = pg_insert(CompanionJunkReview).values(rows)
        await session.execute(
            statement.on_conflict_do_nothing(index_elements=["agent_id", "original_path", "sha256_hash"], index_where=LIVE_IDENTITY_WHERE)
        )
    for row_id, values in refreshes:
        await session.execute(
            update(CompanionJunkReview)
            .where(CompanionJunkReview.id == row_id, CompanionJunkReview.status == JunkReviewStatus.PENDING)
            .values(
                reason=values["reason"],
                file_id=values["file_id"],
                file_size=values["file_size"],
                file_type=values["file_type"],
                updated_at=func.now(),
            )
        )
    for page in _pages(withdraw):
        # Re-checked in the statement: a row decided since it was read is a decision, and stays.
        await session.execute(
            delete(CompanionJunkReview).where(CompanionJunkReview.id.in_(page), CompanionJunkReview.status == JunkReviewStatus.PENDING)
        )
    return outcome


def _decision_values(target: JunkReviewStatus, error_message: str | None) -> dict[str, Any]:
    """The columns a move to ``target`` writes: the decision time (decision 7), the execution time, the error."""
    values: dict[str, Any] = {"status": target.value, "updated_at": func.now()}
    if target is JunkReviewStatus.PENDING:
        values["decided_at"] = None
    elif target in (JunkReviewStatus.APPROVED, JunkReviewStatus.REJECTED):
        values["decided_at"] = func.now()
    if target in TERMINAL_STATUSES:
        values["executed_at"] = func.now()
        values["error_message"] = error_message
    return values


async def decide_content_group(session: AsyncSession, sha256_hash: str, target: JunkReviewStatus) -> int:
    """Move every row of one content group that may move to ``target``; returns how many moved. Does NOT commit.

    The operator's decisions: ``APPROVED`` (from pending; pending duplicates are first re-judged on the
    links stored now, and one that no longer holds is withdrawn, not approved), ``REJECTED`` (from pending or approved --
    a rejection covers every identical copy on every agent, decision 5, including one approved
    earlier and not yet dispatched) and ``PENDING`` (undo, from approved or rejected). Rows in any
    other status -- executing, and the terminal ones -- are not touched.
    """
    if target not in (JunkReviewStatus.APPROVED, JunkReviewStatus.REJECTED, JunkReviewStatus.PENDING):
        msg = f"{target.value} is not a review decision"
        raise ValueError(msg)
    if target is JunkReviewStatus.APPROVED:
        await _rejudge_pending_duplicates(session, sha256_hash)
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(CompanionJunkReview)
            .where(CompanionJunkReview.sha256_hash == sha256_hash, CompanionJunkReview.status.in_(allowed_from(target)))
            .values(**_decision_values(target, None))
        ),
    )
    return result.rowcount


async def _rejudge_pending_duplicates(session: AsyncSession, sha256_hash: str) -> None:
    """Before an approval: re-judge the group's pending duplicates on the links stored NOW.

    The links may have been re-derived since the detector proposed them (phaze-rmhfr), so a row that
    was a duplicate then may not be one now -- its linked copy lost its link, or it gained one itself.
    Such a row is withdrawn (deleted: a pending row is no decision) rather than approved; a row that is
    now junk for another reason keeps its place under that reason. Does NOT commit.
    """
    pending = (
        await session.execute(
            select(CompanionJunkReview.id, CompanionJunkReview.agent_id, CompanionJunkReview.original_path)
            .where(
                CompanionJunkReview.sha256_hash == sha256_hash,
                CompanionJunkReview.status == JunkReviewStatus.PENDING,
                CompanionJunkReview.reason == DUPLICATE_REASON,
            )
            .order_by(CompanionJunkReview.agent_id, CompanionJunkReview.original_path)
        )
    ).all()
    by_agent: dict[str, dict[str, uuid.UUID]] = {}
    for review_id, agent_id, original_path in pending:
        by_agent.setdefault(agent_id, {})[original_path] = review_id
    withdraw: list[uuid.UUID] = []
    for agent_id, reviews in sorted(by_agent.items()):
        for paths in _pages(sorted(reviews)):
            rows = (
                await session.execute(_fresh_companions(agent_id).where(FileRecord.original_path.in_(paths), FileRecord.sha256_hash == sha256_hash))
            ).all()
            current = {row.original_path: reason for row, reason in await _reasons(session, agent_id, rows)}
            for path in paths:
                reason = current.get(path)
                if reason is None:
                    withdraw.append(reviews[path])
                elif reason != DUPLICATE_REASON:
                    await session.execute(
                        update(CompanionJunkReview)
                        .where(CompanionJunkReview.id == reviews[path], CompanionJunkReview.status == JunkReviewStatus.PENDING)
                        .values(reason=reason, updated_at=func.now())
                    )
    for page in _pages(withdraw):
        await session.execute(
            delete(CompanionJunkReview).where(CompanionJunkReview.id.in_(page), CompanionJunkReview.status == JunkReviewStatus.PENDING)
        )


async def transition_review(session: AsyncSession, review_id: uuid.UUID, target: JunkReviewStatus, *, error_message: str | None = None) -> bool:
    """Move ONE row to ``target``. Does NOT commit.

    Returns ``True`` when it moved and ``False`` when it already was ``target`` (a replayed report is
    a no-op). Raises :class:`JunkReviewTransitionRefused` for any edge :data:`TRANSITIONS` lacks --
    including every edge out of a terminal status -- and ``LookupError`` for an unknown id. The guard
    is in the ``UPDATE``'s own ``WHERE``, so a concurrent move cannot slip between a read and a write.
    """
    moved = await session.execute(
        update(CompanionJunkReview)
        .where(CompanionJunkReview.id == review_id, CompanionJunkReview.status.in_(allowed_from(target)))
        .values(**_decision_values(target, error_message))
        .returning(CompanionJunkReview.id)
    )
    if moved.first() is not None:
        return True
    current = (await session.execute(select(CompanionJunkReview.status).where(CompanionJunkReview.id == review_id))).scalar_one_or_none()
    if current is None:
        msg = f"no junk review {review_id}"
        raise LookupError(msg)
    if current == target:
        return False
    raise JunkReviewTransitionRefused(review_id, current, target)
