"""Preview UI router -- serves the directory tree preview page."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from phaze.database import get_session
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.services.collision import TreeNode, build_tree


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["preview"])


def _count_dirs(node: TreeNode) -> int:
    """Recursively count the number of directory nodes in the tree."""
    count = len(node.children)
    for child in node.children.values():
        count += _count_dirs(child)
    return count


@router.get("/preview/", response_class=HTMLResponse)
async def tree_preview(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the directory tree preview page for approved proposals."""
    stmt = select(RenameProposal).where(RenameProposal.status == ProposalStatus.APPROVED).options(selectinload(RenameProposal.file))
    result = await session.execute(stmt)
    proposals = list(result.scalars().all())

    root = build_tree(proposals)
    total_dirs = _count_dirs(root)

    return templates.TemplateResponse(
        request=request,
        name="preview/tree.html",
        context={
            "request": request,
            "tree": root,
            "total_files": root.file_count,
            "total_dirs": total_dirs,
            "current_page": "preview",
        },
    )
