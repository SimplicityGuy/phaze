"""Collision detection and directory tree builder for approved proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, ScalarSelect, String, and_, case, func, or_, select, type_coerce

from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _owning_root_expr() -> ScalarSelect[str]:
    """Scalar subquery: the owning scan_root of a file's ``original_path``.

    Mirrors the executor's ``_resolve_and_check_containment`` (tasks/execution.py):
    a file's destination is resolved under whichever of its agent's ``scan_roots``
    contains its ``original_path``. ``Agent.scan_roots`` is a JSONB array, so we
    unnest it and pick the entry that is a prefix of ``original_path``. When
    scan_roots overlap we take the LONGEST (most specific) match, which is
    order-independent and deterministic. Returns NULL when no scan_root matches
    (an unconfigured/legacy agent) -- callers keep the absolute dirname in that
    case rather than fabricating a root-relative path.
    """
    roots = func.jsonb_array_elements_text(Agent.scan_roots).table_valued("value").lateral()
    return (
        select(roots.c.value)
        .select_from(Agent, roots)
        .where(Agent.id == FileRecord.agent_id)
        .where(
            or_(
                FileRecord.original_path == roots.c.value,
                func.starts_with(FileRecord.original_path, roots.c.value.op("||")("/")),
            ),
        )
        .order_by(func.length(roots.c.value).desc())
        .limit(1)
        .correlate(FileRecord)
        .scalar_subquery()
    )


# A collision means two approved proposals would land the SAME physical file at the SAME
# destination. Pre-fix, the grouping key mixed namespaces -- a RELATIVE proposed_path in one
# COALESCE arm and an ABSOLUTE dirname(original_path) in the other -- and ignored the owning
# agent and scan_root entirely. That both MISSED real collisions (an in-place rename and a
# path proposal resolving to the same on-disk file keyed differently) and INVENTED phantom
# ones (two agents, or two scan_roots of one agent, sharing a relative key though their real
# destinations are unrelated), permanently blocking dispatch (phaze-dqx8). The key now
# identifies a REAL destination with three parts compared like-with-like:
#   * FileRecord.agent_id        -- a file server owns its own filesystem namespace;
#   * the owning scan_root       -- distinguishes distinct roots of a single agent;
#   * a root-relative dest path  -- proposed_path is already relative; the in-place arm's
#                                   absolute dirname(original_path) is stripped of the owning
#                                   scan_root prefix so both arms share one namespace.
def _dest_key_columns() -> tuple[ColumnElement[str], ScalarSelect[str], ColumnElement[str]]:
    """Return ``(agent_id, owning_root, dest_path)`` -- the composite collision-grouping key."""
    owning_root = _owning_root_expr()
    dirname = func.regexp_replace(FileRecord.original_path, "/[^/]*$", "")
    # In-place (proposed_path IS NULL) dir, normalized into proposed_path's relative
    # namespace: strip the owning scan_root prefix when known, else keep it absolute.
    inplace_dir = case(
        (
            and_(owning_root.isnot(None), func.starts_with(dirname, owning_root)),
            func.ltrim(func.substr(dirname, func.length(owning_root) + 1), "/"),
        ),
        else_=dirname,
    )
    effective_dir = func.coalesce(RenameProposal.proposed_path, inplace_dir)
    dest_path = type_coerce(effective_dir.op("||")("/").op("||")(RenameProposal.proposed_filename), String)
    return type_coerce(FileRecord.agent_id, String), owning_root, dest_path


@dataclass
class TreeNode:
    """Node in a directory tree of approved proposals."""

    name: str
    children: dict[str, TreeNode] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    file_count: int = 0


async def detect_collisions(session: AsyncSession) -> list[tuple[str, int]]:
    """Find approved proposals that would collide at the same destination.

    Returns list of (dest_path, count) tuples where count > 1. Two proposals
    collide only when they share the same agent, the same owning scan_root, AND
    the same root-relative destination path (:func:`_dest_key_columns`), so
    cross-form collisions are caught (an in-place rename and a path proposal
    resolving to one on-disk file) while cross-agent / cross-scan-root phantoms
    are not (phaze-dqx8). Covers both path-relative renames and NULL-path
    (rename-in-place) proposals (phaze-7czn).
    """
    agent_id, owning_root, dest_path = _dest_key_columns()
    stmt = (
        select(dest_path.label("dest"), func.count().label("cnt"))
        .select_from(RenameProposal)
        .join(FileRecord, RenameProposal.file_id == FileRecord.id)
        .where(RenameProposal.status == ProposalStatus.APPROVED)
        .group_by(agent_id, owning_root, dest_path)
        .having(func.count() > 1)
    )
    result = await session.execute(stmt)
    return [(row.dest, row.cnt) for row in result.all()]


async def get_collision_ids(session: AsyncSession) -> set[str]:
    """Return set of string UUIDs for proposals that participate in collisions.

    Used by template rendering to show collision badges on affected rows. Uses
    the SAME composite key as :func:`detect_collisions` via a window count, so a
    proposal is flagged iff another approved proposal shares its agent, owning
    scan_root, and root-relative destination (phaze-dqx8) -- including NULL-path
    (in-place) proposals (phaze-7czn).
    """
    agent_id, owning_root, dest_path = _dest_key_columns()
    cnt = func.count().over(partition_by=[agent_id, owning_root, dest_path])
    scoped = (
        select(RenameProposal.id.label("pid"), cnt.label("cnt"))
        .select_from(RenameProposal)
        .join(FileRecord, RenameProposal.file_id == FileRecord.id)
        .where(RenameProposal.status == ProposalStatus.APPROVED)
        .subquery()
    )
    stmt = select(scoped.c.pid).where(scoped.c.cnt > 1)
    result = await session.execute(stmt)
    return {str(row[0]) for row in result.all()}


def build_tree(proposals: list[RenameProposal]) -> TreeNode:
    """Build a directory tree from approved proposals.

    Each proposal's proposed_path is split into directory segments.
    Files with null proposed_path are placed in the root node.
    """
    root = TreeNode(name="output")
    for p in proposals:
        if p.proposed_path is None:
            root.files.append(p.proposed_filename)
            continue
        parts = p.proposed_path.strip("/").split("/")
        node = root
        for part in parts:
            if part not in node.children:
                node.children[part] = TreeNode(name=part)
            node = node.children[part]
        node.files.append(p.proposed_filename)
    _count_files(root)
    return root


def _count_files(node: TreeNode) -> int:
    """Recursively compute file_count for each node."""
    count = len(node.files)
    for child in node.children.values():
        count += _count_files(child)
    node.file_count = count
    return count
