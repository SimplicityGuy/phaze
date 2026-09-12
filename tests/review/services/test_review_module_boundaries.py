"""Contract and dependency-direction checks for the review read-model modules."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from phaze.services import review
from phaze.services.proposal_queries import Pagination


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.services.review_changes import ChangesReviewPage


_MODULE_PATHS = (
    Path("src/phaze/services/review_changes.py"),
    Path("src/phaze/services/review_tagwrite.py"),
    Path("src/phaze/services/review_dedupe.py"),
    Path("src/phaze/services/review_cue.py"),
)


@pytest.mark.parametrize("module_path", _MODULE_PATHS)
def test_capability_modules_do_not_depend_back_on_facade_or_routers(module_path: Path) -> None:
    tree = ast.parse(module_path.read_text())
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "phaze.services.review" not in imports
    assert not any(name == "phaze.routers" or name.startswith("phaze.routers.") for name in imports)


class _FakeChangesReader:
    def __init__(self, result: ChangesReviewPage) -> None:
        self.result = result
        self.call: tuple[AsyncSession, str, int, int] | None = None

    async def get_changes_review_page(
        self,
        session: AsyncSession,
        *,
        status: str,
        page: int,
        page_size: int,
    ) -> ChangesReviewPage:
        self.call = (session, status, page, page_size)
        return self.result


@pytest.mark.asyncio
async def test_facade_can_substitute_changes_review_port(monkeypatch: pytest.MonkeyPatch) -> None:
    result = review.ChangesReviewPage(
        rows=[],
        pagination=Pagination(page=1, page_size=25, total=0),
        stats=review.ChangesReviewStats(all=0, needs_review=0, approved=0, blocked=0, rejected=0),
    )
    fake = _FakeChangesReader(result)
    session = object()
    monkeypatch.setattr(review, "_changes_reader", lambda: fake)

    actual = await review.get_changes_review_page(session, status="approved", page=2, page_size=25)  # type: ignore[arg-type]

    assert actual is result
    assert fake.call == (session, "approved", 2, 25)


class _FailingNested:
    async def __aenter__(self) -> None:
        raise RuntimeError("synthetic adapter failure")

    async def __aexit__(self, *_args: object) -> None:
        return None


class _AdapterSession:
    def begin_nested(self) -> _FailingNested:
        return _FailingNested()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "method", "kwargs", "expected"),
    [
        ("_changes_reader", "get_pending_proposal_rows", {}, review.PendingProposalRows([], 0, 0)),
        ("_tagwrite_reader", "get_tagwrite_review_page", {}, review.TagwriteReviewPage([], False)),
        ("_dedupe_reader", "get_dedupe_groups", {"limit": review.GROUP_PAGE_SIZE}, []),
        ("_cue_reader", "get_cue_review_cards", {}, []),
    ],
)
async def test_real_adapters_own_their_degrade_safe_nested_transaction(
    factory: str,
    method: str,
    kwargs: dict[str, Any],
    expected: object,
) -> None:
    adapter = getattr(review, factory)()

    actual = await getattr(adapter, method)(_AdapterSession(), **kwargs)

    assert actual == expected
