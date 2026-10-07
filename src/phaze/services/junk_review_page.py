"""Read model and decision boundary for the junk review page (phaze-l1j35).

The page lists the ``companion_junk_review`` queue as CONTENT GROUPS -- every live row sharing one
SHA-256 -- and the operator decides a group at once. The transitions themselves are not here: they
are :func:`phaze.services.companion_junk_review.decide_content_group`, so the state machine has one
owner. This module adds what a page needs around it:

- **The group view** (:func:`load_groups`): copy counts by status, size, the junk signature, link
  state, a bounded member preview and the stored content features, built from a fixed number of
  queries per page of groups, never per row.
- **The review token** -- the tags review's optimistic-concurrency pattern (``routers/tags.py``):
  the page renders an opaque token naming exactly what the operator saw, the decision route
  recomputes it from the database and refuses a mismatch with a conflict. Here the "what was seen"
  is every live row of the group (id, status, reason, size, last change), folded into a SHA-256
  digest so the token stays bounded however many copies a group has. A copy detected since the
  render, another tab's decision, or a detector refresh all change the digest.
- **The content excerpt** (:func:`read_group_excerpt`): the stored features carry no text, and the
  controller is fileless (DIST-01), so the excerpt is a BOUNDED read on the owning agent through the
  existing ``read_companion_files`` request/response task -- at most :data:`EXCERPT_CHARS`
  characters and :data:`EXCERPT_LINES` lines ever reach the page. Empty and all-NUL files are
  described from their stored class without a read.

Nothing here commits; the router owns the transaction.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
import structlog

from phaze.enums.junk_review import TERMINAL_STATUSES, JunkReviewReason, JunkReviewStatus
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.schemas.agent_tasks import CompanionReadItem, ReadCompanionFilesPayload
from phaze.services.enqueue_router import lane_for_task
from phaze.services.pg_text import sanitize_pg_text


if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)

GROUP_PAGE_SIZE = 50
"""Content groups per page. Also the bulk cap: one bulk submit acts on at most one page."""

MEMBER_PREVIEW_LIMIT = 8
"""Copies listed by path on one card; the rest are counted, never rendered."""

REFERENCE_PREVIEW_LIMIT = 5
"""Media names shown from the stored features' references."""

EXCERPT_CHARS = 600
"""Characters of file content the excerpt can show -- the agent reads at most a small multiple of this."""

EXCERPT_LINES = 20
"""Lines of file content the excerpt can show."""

EXCERPT_READ_TIMEOUT_S = 15.0
"""How long the page waits on the owning agent for the excerpt before saying it is unavailable."""

SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SHA256_RE = re.compile(SHA256_PATTERN)

SIGNATURE_LABELS: dict[str, str] = {
    JunkReviewReason.EMPTY: "Empty file",
    JunkReviewReason.ALL_NUL: "Every byte is NUL",
    JunkReviewReason.KNOWN_STAMP: "Download-site stamp",
    JunkReviewReason.SITE_AD: "Site advert",
    JunkReviewReason.DUPLICATE: "Duplicate of a linked companion",
}
"""The junk signature each detector reason stands for, as the page names it."""


class JunkReviewTokenInvalid(ValueError):
    """The submitted review token is not one this page rendered (malformed, or for another group)."""


class JunkReviewTokenStale(Exception):
    """The group changed after the operator reviewed it; the decision was not applied."""

    def __init__(self, sha256_hash: str) -> None:
        super().__init__(f"junk review group {sha256_hash} changed after review")
        self.sha256_hash = sha256_hash


@dataclass(frozen=True)
class JunkMember:
    """One copy of a group's content, as the card lists it."""

    agent_id: str
    original_path: str
    file_type: str
    status: str
    linked: bool


@dataclass(frozen=True)
class JunkFeatures:
    """What the stored content features say about the group's bytes (one fresh copy's row)."""

    encoding: str
    byte_size: int
    truncated: bool
    is_tracklist: bool
    reference_count: int
    reference_names: tuple[str, ...]
    folder_media_count: int


@dataclass
class JunkGroup:
    """One content group: every live review row sharing a SHA-256, decided at once."""

    sha256_hash: str
    statuses: dict[str, int] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    file_size: int = 0
    pending_size: int = 0
    agents: tuple[str, ...] = ()
    members: tuple[JunkMember, ...] = ()
    linked_members: int = 0
    linked_elsewhere: int = 0
    features: JunkFeatures | None = None
    token: str = ""

    @property
    def copies(self) -> int:
        return sum(self.statuses.values())

    @property
    def pending(self) -> int:
        return self.statuses.get(JunkReviewStatus.PENDING, 0)

    @property
    def approved(self) -> int:
        return self.statuses.get(JunkReviewStatus.APPROVED, 0)

    @property
    def rejected(self) -> int:
        return self.statuses.get(JunkReviewStatus.REJECTED, 0)

    @property
    def executing(self) -> int:
        return self.statuses.get(JunkReviewStatus.EXECUTING, 0)

    @property
    def undoable(self) -> bool:
        """A decision still reversible: rows approved but not yet run, or rejected."""
        return self.approved + self.rejected > 0

    @property
    def open(self) -> bool:
        """Something on this card still needs the operator: a decision, or the quarantine run."""
        return self.pending + self.approved > 0

    @property
    def signature(self) -> str:
        return " / ".join(SIGNATURE_LABELS.get(reason, reason) for reason in self.reasons) or "No live copies"


@dataclass(frozen=True)
class JunkExcerpt:
    """A bounded look at one copy's bytes, or why there is none."""

    text: str | None
    note: str
    truncated: bool = False


# The token.


def _state_digest(rows: Sequence[tuple[uuid.UUID, str, str, int, datetime]]) -> str:
    canonical = [
        [str(row_id), status, reason, size, updated_at.isoformat()]
        for row_id, status, reason, size, updated_at in sorted(rows, key=lambda r: str(r[0]))
    ]
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def encode_group_token(sha256_hash: str, state: str) -> str:
    """The opaque token a card carries: the group and the digest of its live rows when rendered."""
    payload = json.dumps({"sha256_hash": sha256_hash, "state": state}, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_group_token(token: str, sha256_hash: str | None = None) -> tuple[str, str]:
    """``(sha256_hash, state)`` from a submitted token; :class:`JunkReviewTokenInvalid` on anything else.

    With ``sha256_hash`` the token must name that group -- a token for one card cannot decide another.
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        msg = "invalid junk review token"
        raise JunkReviewTokenInvalid(msg) from exc
    if not isinstance(payload, dict):
        msg = "invalid junk review token"
        raise JunkReviewTokenInvalid(msg)
    group, state = payload.get("sha256_hash"), payload.get("state")
    if not isinstance(group, str) or not _SHA256_RE.match(group) or not isinstance(state, str):
        msg = "invalid junk review token"
        raise JunkReviewTokenInvalid(msg)
    if sha256_hash is not None and group != sha256_hash:
        msg = "junk review token does not match the group"
        raise JunkReviewTokenInvalid(msg)
    return group, state


async def _live_states(session: AsyncSession, hashes: Sequence[str]) -> dict[str, str]:
    """The current digest of each group's live rows, with those rows LOCKED until the caller's transaction ends.

    ``FOR UPDATE`` closes the gap between this check and the decision's ``UPDATE``: a concurrent
    decision on the same group waits for this transaction rather than slipping in between.
    """
    rows: dict[str, list[tuple[uuid.UUID, str, str, int, datetime]]] = {sha: [] for sha in hashes}
    result = await session.execute(
        select(
            CompanionJunkReview.sha256_hash,
            CompanionJunkReview.id,
            CompanionJunkReview.status,
            CompanionJunkReview.reason,
            CompanionJunkReview.file_size,
            CompanionJunkReview.updated_at,
        )
        .where(CompanionJunkReview.sha256_hash.in_(hashes), CompanionJunkReview.status.not_in(TERMINAL_STATUSES))
        .order_by(CompanionJunkReview.id)
        .with_for_update()
    )
    for sha, row_id, status, reason, size, updated_at in result.all():
        rows[sha].append((row_id, status, reason, size, updated_at))
    return {sha: _state_digest(group_rows) for sha, group_rows in rows.items()}


async def validate_group_tokens(session: AsyncSession, tokens: Sequence[tuple[str, str]]) -> None:
    """Raise :class:`JunkReviewTokenStale` naming the first group whose live rows differ from its token.

    ``tokens`` is ``(sha256_hash, state)`` pairs from :func:`decode_group_token`. Read in the
    decision's own transaction, before any write, so a refused bulk changes nothing at all.
    """
    current = await _live_states(session, sorted({sha for sha, _ in tokens}))
    for sha, state in tokens:
        if current[sha] != state:
            raise JunkReviewTokenStale(sha)


# The listing.


OPEN_STATUSES: tuple[JunkReviewStatus, ...] = (JunkReviewStatus.PENDING, JunkReviewStatus.APPROVED)
"""A group is on the page while any copy awaits a decision (pending) or the run step (approved)."""


async def count_open_groups(session: AsyncSession) -> int:
    """How many content groups have a copy awaiting a decision or the quarantine run."""
    statement = select(func.count(func.distinct(CompanionJunkReview.sha256_hash))).where(CompanionJunkReview.status.in_(OPEN_STATUSES))
    return int((await session.execute(statement)).scalar_one())


async def list_open_groups(session: AsyncSession, *, offset: int = 0, limit: int = GROUP_PAGE_SIZE) -> list[JunkGroup]:
    """One page of open groups: most open copies first, then the hash, so paging is stable."""
    copies = func.count().label("copies")
    statement = (
        select(CompanionJunkReview.sha256_hash, copies)
        .where(CompanionJunkReview.status.in_(OPEN_STATUSES))
        .group_by(CompanionJunkReview.sha256_hash)
        .order_by(copies.desc(), CompanionJunkReview.sha256_hash)
        .offset(offset)
        .limit(limit)
    )
    hashes = [row.sha256_hash for row in (await session.execute(statement)).all()]
    groups = await load_groups(session, hashes)
    return [groups[sha] for sha in hashes]


async def load_groups(session: AsyncSession, hashes: Sequence[str]) -> dict[str, JunkGroup]:
    """The full card view of each group in ``hashes`` -- one entry per hash, empty when nothing is live."""
    groups = {sha: JunkGroup(sha256_hash=sha) for sha in hashes}
    if not hashes:
        return groups
    rows = (
        await session.execute(
            select(
                CompanionJunkReview.id,
                CompanionJunkReview.sha256_hash,
                CompanionJunkReview.agent_id,
                CompanionJunkReview.original_path,
                CompanionJunkReview.file_id,
                CompanionJunkReview.file_type,
                CompanionJunkReview.file_size,
                CompanionJunkReview.reason,
                CompanionJunkReview.status,
                CompanionJunkReview.updated_at,
            )
            .where(CompanionJunkReview.sha256_hash.in_(hashes), CompanionJunkReview.status.not_in(TERMINAL_STATUSES))
            .order_by(CompanionJunkReview.sha256_hash, CompanionJunkReview.agent_id, CompanionJunkReview.original_path)
        )
    ).all()
    file_ids = {row.file_id for row in rows if row.file_id is not None}
    linked_ids = await _linked_companion_ids(session, file_ids)
    linked_elsewhere = await _linked_copies_outside(session, hashes, file_ids)
    features = await _features(session, rows)

    by_hash: dict[str, list[Any]] = {sha: [] for sha in hashes}
    for row in rows:
        by_hash[row.sha256_hash].append(row)
    for sha, group_rows in by_hash.items():
        group = groups[sha]
        statuses: dict[str, int] = {}
        for row in group_rows:
            statuses[row.status] = statuses.get(row.status, 0) + 1
        group.statuses = statuses
        group.reasons = tuple(sorted({row.reason for row in group_rows}))
        group.file_size = max((row.file_size for row in group_rows), default=0)
        group.pending_size = sum(row.file_size for row in group_rows if row.status == JunkReviewStatus.PENDING)
        group.agents = tuple(sorted({row.agent_id for row in group_rows}))
        # Pending copies first: they are what an approval acts on.
        ordered = sorted(group_rows, key=lambda r: (r.status != JunkReviewStatus.PENDING, r.agent_id, r.original_path))
        group.members = tuple(
            JunkMember(agent_id=r.agent_id, original_path=r.original_path, file_type=r.file_type, status=r.status, linked=r.file_id in linked_ids)
            for r in ordered[:MEMBER_PREVIEW_LIMIT]
        )
        group.linked_members = sum(1 for r in group_rows if r.file_id in linked_ids)
        group.linked_elsewhere = linked_elsewhere.get(sha, 0)
        group.features = features.get(sha)
        group.token = encode_group_token(sha, _state_digest([(r.id, r.status, r.reason, r.file_size, r.updated_at) for r in group_rows]))
    return groups


async def _linked_companion_ids(session: AsyncSession, file_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    if not file_ids:
        return set()
    statement = select(FileCompanion.companion_id).where(FileCompanion.companion_id.in_(file_ids)).distinct()
    return set((await session.execute(statement)).scalars())


async def _linked_copies_outside(session: AsyncSession, hashes: Sequence[str], member_ids: set[uuid.UUID]) -> dict[str, int]:
    """Per hash: linked companion files with that content that are NOT a copy under review -- the copy that stays."""
    statement = (
        select(FileRecord.sha256_hash, func.count(func.distinct(FileRecord.id)))
        .join(FileCompanion, FileCompanion.companion_id == FileRecord.id)
        .where(FileRecord.sha256_hash.in_(hashes))
        .group_by(FileRecord.sha256_hash)
    )
    if member_ids:
        statement = statement.where(FileRecord.id.not_in(member_ids))
    return {sha: int(count) for sha, count in (await session.execute(statement)).all()}


async def _features(session: AsyncSession, rows: Sequence[Any]) -> dict[str, JunkFeatures]:
    """One fresh features row per hash (fingerprint equal to the hash), read by the members' file ids."""
    file_ids = {row.file_id for row in rows if row.file_id is not None}
    if not file_ids:
        return {}
    statement = (
        select(CompanionContentFeatures)
        .where(CompanionContentFeatures.file_id.in_(file_ids))
        .order_by(CompanionContentFeatures.fingerprint, CompanionContentFeatures.file_id)
    )
    out: dict[str, JunkFeatures] = {}
    for features in (await session.execute(statement)).scalars():
        if features.fingerprint in out:
            continue
        names = tuple(str(ref.get("name", "")) for ref in features.media_references[:REFERENCE_PREVIEW_LIMIT] if isinstance(ref, dict))
        out[features.fingerprint] = JunkFeatures(
            encoding=features.encoding,
            byte_size=features.byte_size,
            truncated=features.truncated,
            is_tracklist=features.is_tracklist,
            reference_count=features.reference_count,
            reference_names=names,
            folder_media_count=features.folder_media_count,
        )
    return out


async def approved_review_ids(session: AsyncSession, sha256_hash: str) -> list[uuid.UUID]:
    """The group's rows that are approved and not yet run -- what the quarantine run step dispatches."""
    statement = (
        select(CompanionJunkReview.id)
        .where(CompanionJunkReview.sha256_hash == sha256_hash, CompanionJunkReview.status == JunkReviewStatus.APPROVED)
        .order_by(CompanionJunkReview.id)
    )
    return list((await session.execute(statement)).scalars())


# The excerpt.


def bound_excerpt(text: str) -> tuple[str, bool]:
    """``text`` cut to :data:`EXCERPT_LINES` lines and :data:`EXCERPT_CHARS` characters; whether it was cut."""
    cleaned = sanitize_pg_text(text)
    lines = cleaned.splitlines()
    cut = len(lines) > EXCERPT_LINES
    bounded = "\n".join(lines[:EXCERPT_LINES])
    if len(bounded) > EXCERPT_CHARS:
        bounded, cut = bounded[:EXCERPT_CHARS], True
    return bounded, cut


async def read_group_excerpt(session: AsyncSession, task_router: AgentTaskRouter | None, sha256_hash: str) -> JunkExcerpt:
    """A bounded excerpt of one live copy of the group, read on the agent that holds it."""
    rows = (
        await session.execute(
            select(
                CompanionJunkReview.reason, CompanionJunkReview.file_size, FileRecord.agent_id, FileRecord.original_filename, FileRecord.current_path
            )
            .outerjoin(
                FileRecord,
                (FileRecord.id == CompanionJunkReview.file_id) & (FileRecord.sha256_hash == CompanionJunkReview.sha256_hash),
            )
            .where(CompanionJunkReview.sha256_hash == sha256_hash, CompanionJunkReview.status.not_in(TERMINAL_STATUSES))
            .order_by(CompanionJunkReview.status != JunkReviewStatus.PENDING, CompanionJunkReview.agent_id, CompanionJunkReview.original_path)
        )
    ).all()
    if not rows:
        return JunkExcerpt(text=None, note="No live copy of this content is left to show.")
    reasons = {row.reason for row in rows}
    if reasons == {JunkReviewReason.EMPTY} or all(row.file_size == 0 for row in rows):
        return JunkExcerpt(text=None, note="The file is empty (0 bytes); there is no content to show.")
    if reasons == {JunkReviewReason.ALL_NUL}:
        return JunkExcerpt(text=None, note="Every byte of this file is NUL; there is no readable text to show.")
    readable = next((row for row in rows if row.current_path is not None), None)
    if readable is None or task_router is None:
        return JunkExcerpt(text=None, note="No copy of this content is reachable to read an excerpt from.")
    payload = ReadCompanionFilesPayload(
        agent_id=readable.agent_id,
        companions=[CompanionReadItem(filename=readable.original_filename, path=readable.current_path)],
        max_chars=EXCERPT_CHARS,
    )
    try:
        queue = task_router.queue_for(readable.agent_id, lane_for_task("read_companion_files"))
        await queue.connect()
        result = await queue.apply("read_companion_files", timeout=EXCERPT_READ_TIMEOUT_S, **payload.model_dump(mode="json"))
    except Exception:
        logger.warning("junk_review_excerpt_unavailable", agent_id=readable.agent_id, sha256_hash=sha256_hash, exc_info=True)
        return JunkExcerpt(text=None, note=f"Agent {readable.agent_id} did not return an excerpt; it may be offline.")
    contents = (result or {}).get("contents", [])
    if not contents:
        return JunkExcerpt(text=None, note=f"Agent {readable.agent_id} could not read this copy.")
    text, cut = bound_excerpt(str(contents[0].get("content", "")))
    if not text.strip():
        return JunkExcerpt(text=None, note="This copy holds only whitespace or control characters.")
    return JunkExcerpt(
        text=text, note=f"First {EXCERPT_LINES} lines, at most {EXCERPT_CHARS} characters, read on agent {readable.agent_id}.", truncated=cut
    )
