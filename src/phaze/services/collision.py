"""Collision detection and directory tree builder for approved proposals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from phaze.models.proposal import ProposalStatus, RenameProposal


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class TreeNode:
    """Node in a directory tree of approved proposals."""

    name: str
    children: dict[str, TreeNode] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    file_count: int = 0


async def detect_collisions(session: AsyncSession) -> list[tuple[str, int]]:
    """Find approved proposals that would collide at the same destination.

    Returns list of (full_path, count) tuples where count > 1.
    Only considers proposals with a non-null proposed_path.
    """
    full_path = func.concat(RenameProposal.proposed_path, "/", RenameProposal.proposed_filename)
    stmt = (
        select(full_path.label("dest"), func.count().label("cnt"))
        .where(
            RenameProposal.status == ProposalStatus.APPROVED,
            RenameProposal.proposed_path.isnot(None),
        )
        .group_by(full_path)
        .having(func.count() > 1)
    )
    result = await session.execute(stmt)
    return [(row.dest, row.cnt) for row in result.all()]


async def get_collision_ids(session: AsyncSession) -> set[str]:
    """Return set of string UUIDs for proposals that participate in collisions.

    Used by template rendering to show collision badges on affected rows.
    """
    collisions = await detect_collisions(session)
    if not collisions:
        return set()

    collision_paths = [path for path, _ in collisions]
    full_path = func.concat(RenameProposal.proposed_path, "/", RenameProposal.proposed_filename)
    stmt = select(RenameProposal.id).where(
        RenameProposal.status == ProposalStatus.APPROVED,
        RenameProposal.proposed_path.isnot(None),
        full_path.in_(collision_paths),
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
