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

phaze-cvn6.1 extended the file to the SIX DERIVED stage-matrix columns (Meta / FP / Analyze / Prop /
Appr / Exec), which phaze-a6hm shipped un-sortable -- a GAP in that epic's "every data table
sortable" claim, not a regression of it (nothing about this table ever sorted and then stopped). The
block at the bottom covers them, including the ordering-is-not-alphabetical obligation and the
"sorts the corpus, not the loaded page" one the bounded-render rule (phaze-1wvb) requires.
"""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.enums.stage import Stage, Status
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.routers.pipeline import FILES_SORT
from phaze.services.pipeline import _FILES_PAGE_STAGES
from phaze.services.stage_status import STAGE_STATUS_DISPLAY_ORDER
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _header_labels(head: str) -> set[str]:
    """Return the bare label of every ``<th>`` in ``head``, sortable or not.

    Drops the decorative ``aria-hidden`` caret span BEFORE stripping tags -- otherwise the ACTIVE
    column's label comes back as ``"File▲"`` and the set comparison fails for a reason that has
    nothing to do with what is being asserted.
    """
    cells = re.findall(r"<th\b[^>]*>(.*?)</th>", head, flags=re.DOTALL)
    without_carets = [re.sub(r'<span aria-hidden="true">.*?</span>', "", cell, flags=re.DOTALL) for cell in cells]
    return {re.sub(r"<[^>]+>", "", cell).strip() for cell in without_carets}


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
    """EVERY rendered header is a server-side sort control; the active one announces itself (rules 1/5).

    phaze-cvn6.1 REPLACED this test's original inverse assertion. It used to assert
    ``<th …>Meta</th>`` rendered as LITERAL plain markup, encoding phaze-a6hm.3's decision that the
    six derived stage columns "must NEVER grow a sort button". That was a gap, not a law: the same
    ``stage_status_case`` expression had been used in the WHERE clause since Phase 87, so it was
    always addressable by ORDER BY -- what was missing was a defined order for an enum-like status.
    The old assertion is kept in the git history rather than as a skip, because a test that pins a
    superseded decision fails the NEXT change for the wrong reason.
    """
    session.add(_make_file("/music/a.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    head = body[body.index("<thead") : body.index("<tbody")]

    # Every whitelisted header is a real server-side sort control aimed at this table's own endpoint.
    assert 'hx-get="/pipeline/files?' in head
    for key in ("file", "type", "metadata", "analyze", "propose", "review", "apply"):
        assert f"sort={key}" in head, f"header for sort={key} is not a sort control"
    # Rule 5: the ACTIVE column (default_key="file") announces its direction; the caret is decorative.
    assert 'aria-sort="ascending"' in head
    assert 'aria-hidden="true"' in head
    # The complement, and the assertion that actually holds the acceptance criterion: NOT ONE header
    # is left as the plain non-sortable markup. Asserting only the presence of eight sort buttons
    # would pass against a ninth header that quietly stayed plain.
    assert '<th scope="col" class="px-6 py-2.5 font-medium">' not in head


@pytest.mark.asyncio
async def test_every_rendered_header_label_is_whitelisted_and_vice_versa(client: AsyncClient, session: AsyncSession) -> None:
    """The anti-drift guard: FILES_SORT's labels and the rendered ``<th>`` labels are the SAME set.

    A ``SortableColumn.label`` is matched against the header string by EQUALITY, so a typo on either
    side degrades that header to plain text with no error anywhere -- exactly the silent shape this
    bead exists to fix. Checked in BOTH directions: a label the template never renders is dead
    whitelist entry (and a URL that sorts by an invisible column), and a header the contract does not
    know is an un-sortable column.
    """
    session.add(_make_file("/music/a.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    head = body[body.index("<thead") : body.index("<tbody")]

    labels = _header_labels(head)
    assert {column.label for column in FILES_SORT.columns}.issubset(labels)
    assert labels - {column.label for column in FILES_SORT.columns} == {"Current state", "File details"}


@pytest.mark.asyncio
async def test_stage_header_labels_match_the_stage_matrix_cells(client: AsyncClient, session: AsyncSession) -> None:
    """The six stage headers name the SAME six stages the pill cells render, in the same order.

    The 7-stage -> 6-pill remap is a documented landmine (``Appr`` reads ``review``, ``Exec`` reads
    ``apply``, ``tracklist`` is never shown). A header whose sort key pointed at a different stage
    than the cell beneath it would reorder the table by a column the operator cannot see, and would
    look correct in every screenshot.
    """
    session.add(_make_file("/music/a.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    head = body[body.index("<thead") : body.index("<tbody")]

    stage_columns = [column for column in FILES_SORT.columns if column.key not in {"file", "type"}]
    assert [column.key for column in stage_columns] == [stage.value for stage in _FILES_PAGE_STAGES]
    assert [column.label for column in stage_columns] == ["Metadata", "Analyze", "Propose", "Review", "Execute"]
    # And the header row actually rendered them in that order (each label immediately precedes its
    # own decorative caret span, which is how a label is anchored rather than matched loosely).
    positions = [head.index(f"{column.label}<span") for column in stage_columns]
    assert positions == sorted(positions)


@pytest.mark.asyncio
async def test_files_sort_reorders_the_set_server_side(client: AsyncClient, session: AsyncSession) -> None:
    """Rule 1: the ORDER BY lands in SQL, so asc and desc return genuinely different row orders.

    Seeded out of alphabetical order so a handler that ignored ``sort`` (falling back to
    ``FileRecord.id`` insertion order) fails at least one of the two direction assertions.
    """
    for path in ("/music/banana.mp3", "/music/apple.mp3", "/music/cherry.mp3"):
        session.add(_make_file(path))
    await session.commit()

    asc = (await client.get("/pipeline/files?sort=file&order=asc", headers={"HX-Request": "true"})).text
    desc = (await client.get("/pipeline/files?sort=file&order=desc", headers={"HX-Request": "true"})).text

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

    asc = (await client.get("/pipeline/files?sort=type&order=asc", headers={"HX-Request": "true"})).text
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

    resp = await client.get(f"/pipeline/files?sort={hostile}&order=asc", headers={"HX-Request": "true"})
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

    body = (await client.get("/pipeline/files?sort=file&order=desc&page_size=10", headers={"HX-Request": "true"})).text
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

    body = (await client.get("/pipeline/files?stage=metadata&bucket=failed&sort=file&order=asc", headers={"HX-Request": "true"})).text
    head = body[body.index("<thead") : body.index("<tbody")]

    assert "stage=metadata" in head
    assert "bucket=failed" in head


# --------------------------------------------------------------------------------------------------
# phaze-cvn6.1: the six DERIVED stage-matrix columns.
#
# These are the columns phaze-a6hm.3 left out. They do not sort on a stored value: each one sorts on
# the display RANK of the bucket `stage_status_case` derives for that row
# (`stage_status_sort_case` / STAGE_STATUS_DISPLAY_ORDER), so the assertions below are about an
# ORDER OF BUCKETS, not an order of strings.
# --------------------------------------------------------------------------------------------------


async def _place_in_metadata_bucket(session: AsyncSession, file: FileRecord, bucket: Status) -> None:
    """Put ``file`` in the named derived ``metadata`` bucket, honouring the precedence ladder.

    ``metadata`` is used for every bucket assertion because it is one of the three ENRICH stages and
    therefore the only kind that can reach all FIVE buckets -- the four downstream stages have no
    force-skip affordance (D-10) and no per-file ledger key, so they can never be ``skipped`` and
    (propose/review/apply) never ``in_flight``. Testing the ordering on a 3-bucket column would leave
    the ``skipped`` and ``in_flight`` ranks unexercised.
    """
    match bucket:
        case Status.NOT_STARTED:
            pass  # the absence of every marker IS not_started
        case Status.DONE:
            session.add(FileMetadata(file_id=file.id))
        case Status.FAILED:
            session.add(FileMetadata(file_id=file.id, failed_at=datetime.now(UTC), error_message="boom"))
        case Status.SKIPPED:
            session.add(StageSkip(file_id=file.id, stage=Stage.METADATA.value, reason="operator force-skip"))
        case Status.IN_FLIGHT:
            func_name = STAGE_TO_FUNCTION[Stage.METADATA.value]
            session.add(SchedulingLedger(key=f"{func_name}:{file.id}", function=func_name, routing="agent", payload={"file_id": str(file.id)}))
        case _:  # pragma: no cover - a new Status member should fail loudly here, not sort silently
            raise AssertionError(f"no seeding recipe for {bucket!r}")
    await session.flush()


def test_display_order_covers_every_status() -> None:
    """STAGE_STATUS_DISPLAY_ORDER ranks EVERY ``Status`` member -- nothing falls through to ``else_``.

    ``stage_status_sort_case``'s ``else_`` exists so a newly added status sorts to the end instead of
    NULL-ordering, but a status that reaches it has no DEFINED position relative to the others and
    would silently share a rank with any other unranked one. This is the test that turns "we thought
    about all five buckets" into an obligation the next status has to satisfy.
    """
    assert set(STAGE_STATUS_DISPLAY_ORDER) == set(Status)
    assert len(STAGE_STATUS_DISPLAY_ORDER) == len(set(STAGE_STATUS_DISPLAY_ORDER))


@pytest.mark.asyncio
async def test_stage_column_sorts_by_the_documented_operational_order(client: AsyncClient, session: AsyncSession) -> None:
    """Ascending a stage column walks STAGE_STATUS_DISPLAY_ORDER; descending walks it backwards.

    The seeding is deliberately shuffled against BOTH the display order and the alphabet, so a
    handler that ignored ``sort`` (falling back to insertion order) and one that ordered the raw
    status STRING both fail. The alphabetical order is ``done, failed, in_flight, not_started,
    skipped``; ours is ``done, in_flight, not_started, failed, skipped``, so the ``in_flight`` vs
    ``failed`` pair is the discriminator between the two -- and it is the pair that matters, because
    alphabetical would file a terminal error between two healthy states.
    """
    # One file per bucket, named so the DEFAULT (file asc) order is the exact reverse of the expected
    # sorted order -- an unsorted response therefore cannot accidentally satisfy the assertion.
    by_bucket = {
        Status.DONE: "/music/track-05.mp3",
        Status.IN_FLIGHT: "/music/track-04.mp3",
        Status.NOT_STARTED: "/music/track-03.mp3",
        Status.FAILED: "/music/track-02.mp3",
        Status.SKIPPED: "/music/track-01.mp3",
    }
    for bucket, path in by_bucket.items():
        record = _make_file(path)
        session.add(record)
        await session.flush()
        await _place_in_metadata_bucket(session, record, bucket)
    await session.commit()

    expected = [by_bucket[status] for status in STAGE_STATUS_DISPLAY_ORDER]

    def order_of(body: str) -> list[str]:
        rows = body[body.index("<tbody") :]
        return sorted(by_bucket.values(), key=rows.index)

    asc = (await client.get("/pipeline/files?sort=metadata&order=asc", headers={"HX-Request": "true"})).text
    desc = (await client.get("/pipeline/files?sort=metadata&order=desc", headers={"HX-Request": "true"})).text

    assert order_of(asc) == expected
    assert order_of(desc) == list(reversed(expected))


@pytest.mark.asyncio
async def test_stage_sort_orders_the_whole_set_not_the_loaded_page(client: AsyncClient, session: AsyncSession) -> None:
    """The bounded-render rule (phaze-1wvb): the ORDER BY is applied ACROSS the corpus, before the LIMIT.

    THE test the acceptance criterion names, and the one a client-side sort cannot pass. The single
    ``done`` file is seeded so that under the default order it sits on the LAST page; a browser-side
    sort of page 1 could never surface it, because page 1 never contained it. Sorting ascending by
    Meta must bring it to the top of page 1.
    """
    # MIN_PAGE_SIZE is 10 (the paging contract clamps below it), so the corpus must be > 2x that for
    # "page 1 cannot contain it" to mean anything.
    page_size = 10
    paths = [f"/music/track-{index:02d}.mp3" for index in range(1, 25)]
    for path in paths:
        session.add(_make_file(path))
    await session.commit()

    last_by_default = paths[-1]
    record = (await session.execute(select(FileRecord).where(FileRecord.current_path == last_by_default))).scalar_one()
    await _place_in_metadata_bucket(session, record, Status.DONE)
    await session.commit()

    # Sanity: under the DEFAULT order that file is genuinely NOT on page 1, so the assertion below
    # cannot be satisfied by a page-local reordering.
    default_page_one = (await client.get(f"/pipeline/files?page_size={page_size}", headers={"HX-Request": "true"})).text
    assert last_by_default not in default_page_one[default_page_one.index("<tbody") :]

    sorted_page_one = (await client.get(f"/pipeline/files?sort=metadata&order=asc&page_size={page_size}", headers={"HX-Request": "true"})).text
    rows = sorted_page_one[sorted_page_one.index("<tbody") :]
    assert last_by_default in rows
    # And it is the FIRST row: `done` is rank 0, every other seeded file is `not_started` (rank 2).
    # Compared against the rows actually on this page -- the 23 tied rows fall in FileRecord.id
    # (UUID) tiebreaker order, so which of them made page 1 is deliberately not asserted.
    present = [path for path in paths if path in rows]
    assert min(present, key=rows.index) == last_by_default


@pytest.mark.asyncio
async def test_stage_sort_survives_the_pager_and_keeps_the_filter(client: AsyncClient, session: AsyncSession) -> None:
    """Contract rule 4, inverse direction, for the NEW keys: Prev/Next carries a stage sort forward.

    Covered separately from the File-column case because the pager builds its query string by
    concatenation and a stage key is the first sort value on this table that is not also a column
    name -- a pager that special-cased the old two keys would pass the older test and fail here.
    """
    for index in range(12):
        session.add(_make_file(f"/music/track-{index:02d}.mp3"))
    await session.commit()

    body = (await client.get("/pipeline/files?sort=apply&order=desc&page_size=10", headers={"HX-Request": "true"})).text
    pager = body[body.index("</table>") :]

    assert "sort=apply" in pager
    assert "order=desc" in pager
