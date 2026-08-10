"""phaze-cvn6.3: resizable columns on the pipeline files table (``GET /pipeline/files``).

The drag itself is client-side Alpine (``$store.colResize`` + ``colResizer()``, registered in
base.html/shell.html next to the theme store) and cannot be exercised from a server-rendered
response -- there is no browser here. What CAN and MUST be pinned server-side is the contract the
JS depends on:

* every one of the seven headers (the two real ``FileRecord`` columns plus the five stage-matrix
  columns, phaze-cvn6.1's single header loop) carries exactly one resize handle, keyed to its own
  column label via ``data-col-resize-handle`` -- "a resize handle goes in one place" (cvn6.1's
  header-loop rule) extended to resizability: it is decided ONCE, after both the sortable and
  plain ``<th>`` branches, not per-branch;
* the handle never lands between a sortable header's label and its decorative caret span --
  ``tests/integration/test_files_sort.py::test_stage_header_labels_match_the_stage_matrix_cells``
  anchors on that exact adjacency, so a regression here would silently break sorting's own test
  rather than this one;
* a ``<colgroup>`` precedes ``<thead>`` with one ``<col data-col="...">`` per header, in the same
  order -- the single source of truth the JS store's ``:style`` bindings and every ``<td>`` in that
  column share, so a header and its cells can never drift to different widths;
* the FILE cell keeps ``truncate`` (the ellipsis-on-overflow behavior) and its ``title`` attribute
  (the full, un-truncated path, always present so a screen reader / hover always has it) but no
  longer carries a fixed ``max-w-md`` -- that Tailwind cap would fight the operator's drag instead
  of enabling it, since the column's rendered width now comes from the resizable ``<colgroup>``;
* the five stage ``<td>``s keep cvn6.2's ``whitespace-nowrap`` no-wrap contract -- resizing must not
  reopen the two-line wrapping bug that bead fixed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.routers.pipeline import FILES_SORT


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration

_ALL_COLUMNS = ["File", "Type", "Meta", "Analyze", "Prop", "Appr", "Exec"]


def _make_file(current_path: str) -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=current_path,
        original_filename=current_path.rsplit("/", 1)[-1],
        current_path=current_path,
        file_type="mp3",
        file_size=1000,
    )


@pytest.mark.asyncio
async def test_every_header_carries_exactly_one_resize_handle(client: AsyncClient, session: AsyncSession) -> None:
    """Every header -- sortable and (if ever unsortable) plain alike -- gets one handle each.

    Asserted against ``FILES_SORT`` rather than a hardcoded list so this test and
    ``test_every_rendered_header_label_is_whitelisted_and_vice_versa`` share the same anti-drift
    property: a column the contract does not know about would fail THAT test first.
    """
    session.add(_make_file("/music/a.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    head = body[body.index("<thead") : body.index("<tbody")]

    handles = re.findall(r'data-col-resize-handle="([^"]+)"', head)
    assert handles == [column.label for column in FILES_SORT.columns]
    assert len(handles) == len(set(handles)) == 7


@pytest.mark.asyncio
async def test_resize_handle_never_splits_a_sortable_label_from_its_caret(client: AsyncClient, session: AsyncSession) -> None:
    """The handle sits AFTER ``</button>``, never between ``{{ col }}`` and the caret span.

    Guards the exact adjacency ``test_stage_header_labels_match_the_stage_matrix_cells`` (sort
    suite) relies on -- if a future edit moved the handle inside the button or between the label
    and its caret, that sort test would start failing for a reason that has nothing to do with
    sorting, which is the wrong test to fail for a resize regression.
    """
    session.add(_make_file("/music/a.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    head = body[body.index("<thead") : body.index("<tbody")]

    for column in FILES_SORT.columns:
        label_pos = head.index(f"{column.label}<span")
        handle_pos = head.index(f'data-col-resize-handle="{column.label}"')
        assert handle_pos > label_pos, f"{column.label}'s resize handle must follow its label+caret, not precede it"


@pytest.mark.asyncio
async def test_colgroup_precedes_thead_with_one_col_per_column_in_order(client: AsyncClient, session: AsyncSession) -> None:
    """The ``<colgroup>`` is the single width source: one ``<col data-col>`` per header, same order."""
    session.add(_make_file("/music/a.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    colgroup_start = body.index("<colgroup")
    colgroup_end = body.index("</colgroup>")
    thead_start = body.index("<thead")
    assert colgroup_start < colgroup_end < thead_start, "colgroup must precede thead so the browser sizes columns from it"

    colgroup = body[colgroup_start:colgroup_end]
    cols = re.findall(r'<col data-col="([^"]+)"', colgroup)
    assert cols == _ALL_COLUMNS


@pytest.mark.asyncio
async def test_file_cell_keeps_truncate_and_title_but_sheds_the_fixed_max_width(client: AsyncClient, session: AsyncSession) -> None:
    """FILE stays truncate+title (the ellipsis/full-path contract); the OLD fixed cap is gone.

    A resizable column and a Tailwind ``max-w-md`` cap on the same cell fight each other -- the cap
    would clamp the cell at 28rem regardless of how wide the operator drags the header, defeating
    the whole point of this bead. Retiring it is the change, not an incidental cleanup.
    """
    long_path = "/music/staging/Artist Name - A Rather Long Concert Recording Title (2024-01-01).flac"
    session.add(_make_file(long_path))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    row = body[body.index("<tbody") :]

    assert f'title="{long_path}"' in row
    file_cell_match = re.search(r'<td class="([^"]*)" title="' + re.escape(long_path) + '">', row)
    assert file_cell_match is not None, "could not locate the FILE cell for the seeded row"
    file_cell_class = file_cell_match.group(1)
    assert "truncate" in file_cell_class
    assert "max-w-md" not in file_cell_class


@pytest.mark.asyncio
async def test_stage_cells_keep_the_no_wrap_contract_after_resize_wiring(client: AsyncClient, session: AsyncSession) -> None:
    """cvn6.2's ``whitespace-nowrap`` on every stage ``<td>`` must survive the colgroup/handle wiring."""
    session.add(_make_file("/music/a.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    row = body[body.index("<tbody") :]

    # Five stage cells, each still carrying the no-wrap contract cvn6.2 introduced.
    assert row.count("px-6 py-3 whitespace-nowrap") == 5
