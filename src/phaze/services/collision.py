"""Collision detection and directory tree builder for approved proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, func, select

from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# The effective destination directory of an approved proposal, mirroring the executor
# (tasks/execution.py:_resolve_destination): a non-null proposed_path is the relative
# destination dir; a NULL/in-place proposed_path resolves to the file's own directory,
# i.e. dirname(original_path). ``regexp_replace(..., '/[^/]*$', '')`` strips the trailing
# ``/filename`` to yield that directory. Using COALESCE here is what lets rename-in-place
# proposals (proposed_path IS NULL) be compared by their true on-disk destination instead
# of collapsing to ``/filename`` (Postgres ``concat`` ignores NULLs) -- phaze-7czn.
def _dest_path_expr() -> ColumnElement[str]:
    """Build the ``<effective-dir>/<proposed_filename>`` key expression for collision grouping."""
    effective_dir = func.coalesce(
        RenameProposal.proposed_path,
        func.regexp_replace(FileRecord.original_path, "/[^/]*$", ""),
    )
    return func.concat(effective_dir, "/", RenameProposal.proposed_filename)


@dataclass
class TreeNode:
    """Node in a directory tree of approved proposals."""

    name: str
    children: dict[str, TreeNode] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    file_count: int = 0


async def detect_collisions(session: AsyncSession) -> list[tuple[str, int]]:
    """Find approved proposals that would collide at the same destination.

    Returns list of (full_path, count) tuples where count > 1. Covers BOTH
    path-relative renames and NULL-path (rename-in-place) proposals, keying each
    on its effective on-disk destination so two in-place renames in the same
    source directory that target the same filename are flagged (phaze-7czn).
    """
    full_path = _dest_path_expr()
    stmt = (
        select(full_path.label("dest"), func.count().label("cnt"))
        .select_from(RenameProposal)
        .join(FileRecord, RenameProposal.file_id == FileRecord.id)
        .where(RenameProposal.status == ProposalStatus.APPROVED)
        .group_by(full_path)
        .having(func.count() > 1)
    )
    result = await session.execute(stmt)
    return [(row.dest, row.cnt) for row in result.all()]


async def get_collision_ids(session: AsyncSession) -> set[str]:
    """Return set of string UUIDs for proposals that participate in collisions.

    Used by template rendering to show collision badges on affected rows. Mirrors
    :func:`detect_collisions`, including NULL-path (in-place) proposals so those
    rows also render a collision badge (phaze-7czn).
    """
    collisions = await detect_collisions(session)
    if not collisions:
        return set()

    collision_paths = [path for path, _ in collisions]
    full_path = _dest_path_expr()
    stmt = (
        select(RenameProposal.id)
        .select_from(RenameProposal)
        .join(FileRecord, RenameProposal.file_id == FileRecord.id)
        .where(
            RenameProposal.status == ProposalStatus.APPROVED,
            full_path.in_(collision_paths),
        )
    )
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
