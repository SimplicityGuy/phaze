"""phaze-a6hm.3: the sortable-column contract wired onto the Files matrix (``GET /pipeline/files``).

Mirrors ``tests/shared/core/test_identify_workspaces.py``'s Track-ID sort block (phaze-a6hm.1) --
those tests prove the WIRING pattern for one table; these prove the same pattern holds for
``files_table_view.html``'s hand-rolled ``<thead>`` (it cannot include ``_file_table.html``, see that
template's module docstring, so the sortable-header markup is duplicated by hand and needs its own
end-to-end coverage rather than inheriting the shared partial's).

``tests/shared/routers/test_column_sort.py`` already proves the ``SortContract``/``SortState``
mechanism in isolation; these prove the SET actually reorders server-side through a real handler, that
the whitelist holds at the HTTP boundary, and that the pager/header keep the rest of the view state
(contract rule 4, both directions).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _make_file(current_path: str, *, file_type: str = "mp3") -> FileRecord:
    """A FileRecord with an EXACT ``current_path`` (the File cell's displayed + sorted value)."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=current_path,
        original_filename=current_path.rsplit("/", 1)[-1],
        current_path=current_path,
        file_type=file_type,
        file_size=1000,
    )


@pytest.mark.asyncio
async def test_files_headers_are_sortable_and_announce_state(client: AsyncClient, session: AsyncSession) -> None:
    """File / Type are whitelisted (FILES_SORT); a stage-matrix header (Meta) stays plain (rules 1/5).

    Asserting both a sortable AND a non-sortable header is what proves the label-recognition gate
    rather than "the template emits buttons somewhere" -- the six stage cells are per-page DERIVED
    ``stage_status_case`` buckets (see ``_files_page_stmt``), not columns a SQL ORDER BY can address,
    so they must NEVER grow a sort button.
    """
    session.add(_make_file("/music/a.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files")).text
    head = body[body.index("<thead") : body.index("<tbody")]

    # The whitelisted "File" header is a real server-side sort control aimed at its own endpoint.
    assert 'hx-get="/pipeline/files?' in head
    assert "sort=file" in head
    assert "sort=type" in head
    # Rule 5: the ACTIVE column (default_key="file") announces its direction; the caret is decorative.
    assert 'aria-sort="ascending"' in head
    assert 'aria-hidden="true"' in head
    # A derived stage-matrix header stays plain text -- no button, no aria-sort, ever. Matched as the
    # LITERAL plain-header markup (rather than a proximity window) so a longer sortable File/Type
    # hx-get URL upstream cannot bleed a false negative into this assertion.
    assert '<th scope="col" class="px-6 py-2.5 font-medium">Meta</th>' in head


@pytest.mark.asyncio
async def test_files_sort_reorders_the_set_server_side(client: AsyncClient, session: AsyncSession) -> None:
    """Rule 1: the ORDER BY lands in SQL, so asc and desc return genuinely different row orders.

    Seeded out of alphabetical order so a handler that ignored ``sort`` (falling back to
    ``FileRecord.id`` insertion order) fails at least one of the two direction assertions.
    """
    for path in ("/music/banana.mp3", "/music/apple.mp3", "/music/cherry.mp3"):
        session.add(_make_file(path))
    await session.commit()

    asc = (await client.get("/pipeline/files?sort=file&order=asc")).text
    desc = (await client.get("/pipeline/files?sort=file&order=desc")).text

    def order_of(body: str) -> list[str]:
        rows = body[body.index("<tbody") :]
        return sorted(("/music/apple.mp3", "/music/banana.mp3", "/music/cherry.mp3"), key=rows.index)

    assert order_of(asc) == ["/music/apple.mp3", "/music/banana.mp3", "/music/cherry.mp3"]
    assert order_of(desc) == ["/music/cherry.mp3", "/music/banana.mp3", "/music/apple.mp3"]


@pytest.mark.asyncio
async def test_type_sort_reorders_the_set_server_side(client: AsyncClient, session: AsyncSession) -> None:
    """The second whitelisted column (Type / ``FileRecord.file_type``) also reaches its own expression."""
    session.add(_make_file("/music/one.flac", file_type="flac"))
    session.add(_make_file("/music/two.mp3", file_type="mp3"))
    session.add(_make_file("/music/three.ogg", file_type="ogg"))
    await session.commit()

    asc = (await client.get("/pipeline/files?sort=type&order=asc")).text
    rows = asc[asc.index("<tbody") :]
    assert rows.index("flac") < rows.index("mp3") < rows.index("ogg")


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", ["original_path", "__class__", "id", "file_size; DROP TABLE files", "1) OR 1=1 --"])
async def test_unwhitelisted_sort_is_rejected_at_the_http_boundary(client: AsyncClient, session: AsyncSession, hostile: str) -> None:
    """THE regression the bead requires, at the boundary: an unwhitelisted sort cannot reach a column.

    Three assertions, because any one alone is too weak: the request does not 500 (a ``getattr``-based
    implementation would, on ``__class__``), the response falls back to the DEFAULT order (the hostile
    value was discarded, not honoured), and the hostile value is never echoed back as a ``sort=``
    parameter, which would let the operator's next click carry it further.
    """
    session.add(_make_file("/music/banana.mp3"))
    session.add(_make_file("/music/apple.mp3"))
    await session.commit()

    resp = await client.get(f"/pipeline/files?sort={hostile}&order=asc")
    assert resp.status_code == 200
    rows = resp.text[resp.text.index("<tbody") :]
    assert rows.index("/music/apple.mp3") < rows.index("/music/banana.mp3")  # the default (file asc) order
    assert f"sort={hostile}" not in resp.text


@pytest.mark.asyncio
async def test_sorting_preserves_view_state_and_the_pager_preserves_the_sort(client: AsyncClient, session: AsyncSession) -> None:
    """Rule 4, both directions: a sort keeps the other view state, and a pager keeps the sort.

    The second half is the one that rots silently -- Prev/Next dropping the sort looks fine on page 1
    and only misbehaves once the operator scrolls, which is exactly when they are relying on it.
    """
    for index in range(12):
        session.add(_make_file(f"/music/file-{index:02d}.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files?sort=file&order=desc&page_size=10")).text
    head = body[body.index("<thead") : body.index("<tbody")]

    # A header click re-emits page_size, and resets to page 1 rather than holding a stale offset.
    assert "page_size=10" in head
    assert "page=" not in head

    # The pager carries the ACTIVE sort forward, so Next stays inside the chosen order.
    pager = body[body.index("</table>") :]
    assert "sort=file" in pager
    assert "order=desc" in pager


@pytest.mark.asyncio
async def test_sort_preserves_the_active_stage_and_bucket_filter(client: AsyncClient, session: AsyncSession) -> None:
    """A header click under an active ``stage``/``bucket`` filter must not silently clear the filter."""
    failed = _make_file("/music/apple.mp3")
    session.add(failed)
    await session.commit()
    # A metadata failed_at row so the filtered set is non-empty and the <thead>/pagination actually render
    # (an empty filtered result renders the failed-filter empty state instead, per files_table_view.html).
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    await session.commit()

    body = (await client.get("/pipeline/files?stage=metadata&bucket=failed&sort=file&order=asc")).text
    head = body[body.index("<thead") : body.index("<tbody")]

    assert "stage=metadata" in head
    assert "bucket=failed" in head
