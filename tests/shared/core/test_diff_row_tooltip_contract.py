"""The shared ``_diff_row.html`` tooltip contract, read out of the RENDERED HTML (phaze-n8o9p).

Every assertion here parses the ``title=`` attribute the browser would actually receive. That is
deliberate and it is the point of the module: ADR-0012 rule 3 says an artifact is verified with its
REAL consumer, not with the tool that produced it, and the consumer of ``file_tooltip`` is the
rendered attribute -- never the Python dict a router returns. A sibling bead in this wave proved the
cost of the other approach concretely: it renamed a serialized field, 56 tests passed, and the wire
key had silently reverted, because every one of those tests exchanged Python objects and none named
the serialized form.

Two properties make a naive test here structurally unable to fail, so both are defended:

* **No template in this repo runs under ``StrictUndefined``.** Every ``Jinja2Templates`` instance
  takes the default ``Undefined``, so a binding that still passes the OLD key name renders
  ``title=""`` -- no exception, no log line, no failing assertion anywhere else. The empty-tooltip
  check below is what turns that silent revert into a red test.
* **``make_file`` seeds ``original_path`` and ``current_path`` to DIFFERENT values** (they differ by
  a uuid path segment). Asserting the tooltip is the filename is therefore decisive in both
  directions: it can distinguish the filename from either column. A fixture that set the two columns
  equal could not, and the test would pass against a tooltip carrying the wrong one.

The contract itself, per the operator's 2026-08-25 decision recorded on phaze-n8o9p: the tooltip
UN-TRUNCATES the visible display name, so it carries the FILENAME at every binding and never a path.
A path belongs in the Destination facet.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord
    from phaze.models.proposal import RenameProposal


# The one line in _diff_row.html that renders the tooltip, matched as the browser receives it.
_TOOLTIP_DIV = re.compile(r'<div class="min-w-0 truncate[^"]*" title="([^"]*)">([^<]*)</div>')


def _tooltips(body: str) -> list[tuple[str, str]]:
    """Every (tooltip, visible-text) pair the response actually rendered."""
    return [(m.group(1), m.group(2).strip()) for m in _TOOLTIP_DIV.finditer(body)]


def _assert_tooltip_is_the_filename(body: str, file: FileRecord) -> None:
    """The rendered tooltip un-truncates the filename and leaks neither path column."""
    pairs = _tooltips(body)
    assert pairs, "no _diff_row.html tooltip rendered at all -- the partial did not reach the response"

    # The silent-revert guard. A producer still emitting the pre-phaze-n8o9p key name leaves
    # `file_tooltip` undefined, and default Jinja renders that as the empty string.
    assert all(tooltip for tooltip, _ in pairs), f"empty tooltip rendered (a binding still passes the old key name): {pairs}"

    assert any(tooltip == file.original_filename for tooltip, _ in pairs), (
        f"filename {file.original_filename!r} is not the tooltip on any row: {pairs}"
    )

    # Decisive because make_file seeds these to values that differ from the filename AND each other.
    rendered = {tooltip for tooltip, _ in pairs}
    assert file.current_path not in rendered, f"tooltip leaked current_path {file.current_path!r}: {pairs}"
    assert file.original_path not in rendered, f"tooltip leaked original_path {file.original_path!r}: {pairs}"


@pytest.mark.asyncio
async def test_changes_review_rename_row_tooltip_is_the_filename(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """_changes_list.html's Filename/Destination section (the `rename-row` binding)."""
    proposal = await seed_pending_proposal(0.88, original_filename="rename-tooltip.mp3", proposed_filename="After.mp3")

    body = (await client.get("/s/rename?status=all")).text

    assert f'id="rename-row-{proposal.id}"' in body
    _assert_tooltip_is_the_filename(body, proposal.file)


@pytest.mark.asyncio
async def test_changes_review_tagwrite_row_tooltip_is_the_filename(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, object]]],
) -> None:
    """_changes_list.html's Tag Changes section -- the binding phaze-n8o9p found as tags.py's twin.

    The seed shape is load-bearing: ``get_tagwrite_review_page`` surfaces a row only when the
    server-computed comparison has >= 1 change, so the filename must derive tags the stored
    metadata lacks. A file whose tags already match renders no row and the test would pass
    vacuously.
    """
    file, _md = await seed_executed_file_with_metadata(
        original_filename="Tooltip Artist - Tooltip Title.mp3", artist=None, title=None, album="Keep Album"
    )

    body = (await client.get("/s/rename?status=all")).text

    assert f'id="tagwrite-row-{file.id}"' in body
    _assert_tooltip_is_the_filename(body, file)


@pytest.mark.asyncio
async def test_proposal_row_swap_response_tooltip_is_the_filename(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """routers/proposals.py's _diff_row_response -- the partial rendered as a whole response.

    This is the binding that carried ``FileRecord.current_path`` before phaze-n8o9p, so it is the
    one an incomplete change would most plausibly leave on the old value.
    """
    proposal = await seed_pending_proposal(0.95, original_filename="swap-tooltip.mp3")

    response = await client.patch(f"/proposals/{proposal.id}/approve", data={"expected_updated_at": proposal.updated_at.isoformat()})

    assert response.status_code == 200
    assert f'id="rename-row-{proposal.id}"' in response.text
    _assert_tooltip_is_the_filename(response.text, proposal.file)


@pytest.mark.asyncio
async def test_tagwrite_row_swap_response_tooltip_is_the_filename(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, object]]],
) -> None:
    """routers/tags.py's _tagwrite_diff_row_response, reached via the no-prior-write undo branch."""
    file, _md = await seed_executed_file_with_metadata(original_filename="undo-tooltip.mp3")

    response = await client.post(f"/tags/{file.id}/undo")

    assert response.status_code == 200
    assert f'id="tagwrite-row-{file.id}"' in response.text
    _assert_tooltip_is_the_filename(response.text, file)


@pytest.mark.asyncio
async def test_propose_workspace_filename_tooltip_is_the_filename(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """_propose_list.html's own tooltip -- a different partial fed by the same review.py row shape.

    Not a ``_diff_row.html`` binding, so it is asserted on its own rendered ``title=`` rather than
    through the shared helper: this list renders through ``_file_table.html``.
    """
    proposal = await seed_pending_proposal(0.91, original_filename="propose-tooltip.mp3")
    file = proposal.file

    body = (await client.get("/s/propose?status=pending")).text

    assert f'title="{file.original_filename}"' in body
    assert f'title="{file.current_path}"' not in body
    assert f'title="{file.original_path}"' not in body


@pytest.mark.asyncio
async def test_the_destination_facet_still_shows_the_current_path(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """The other half of the rename, and the one a careless edit would break.

    ``services/review.py``'s row key was renamed ``original_path`` -> ``current_path``, and that key
    has TWO readers in _changes_list.html: the tooltip (now the filename) and the Destination
    facet's before/after, which genuinely needs the path. Moving the tooltip to the filename must
    not have taken the Destination with it.
    """
    proposal = await seed_pending_proposal(0.88, original_filename="dest.mp3", proposed_filename="After.mp3", proposed_path="Artist/Event")
    file = proposal.file

    body = (await client.get("/s/rename?status=all")).text

    assert "Destination" in body
    assert file.current_path in body, "the Destination facet lost the current_path it renders as `before`"
    assert "Artist/Event/After.mp3" in body
