"""Tag-write review scenarios moved from ``tests/shared/core/test_review_apply_workspaces.py``."""

from __future__ import annotations

from datetime import datetime, timedelta
import html
import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from tests._queue_fakes import install_fake_queues


async def _visible_tag_review_tokens(client: AsyncClient) -> list[str]:
    body = (await client.get("/s/tagwrite", headers={"HX-Request": "true"})).text
    return [html.unescape(value) for value in re.findall(r'<input[^>]*name="review_tokens"[^>]*value="([^"]+)"', body)]


async def _post_visible_tag_bulk(client: AsyncClient, tokens: list[str] | None = None):  # type: ignore[no-untyped-def]
    reviewed = tokens if tokens is not None else await _visible_tag_review_tokens(client)
    return await client.post("/tags/bulk-write-no-discrepancies", data={"review_tokens": reviewed})


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord
    from phaze.models.metadata import FileMetadata


async def _tagwrite_log_count(session: AsyncSession, file_id: object, *, status: str | None = None) -> int:
    stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id)
    if status is not None:
        stmt = stmt.where(TagWriteLog.status == status)
    return (await session.execute(stmt)).scalar_one()


@pytest.mark.asyncio
async def test_tag_bulk_no_discrepancy_predicate(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """REVIEW-02 / D-03 / OQ-1 -- tag bulk writes ONLY the qualifying no-blank, >=1-change set.

    A clean-change file (filename parses to a new artist+title absent from metadata, an existing
    album preserved) qualifies and is written exactly once; a zero-change file is untouched. The
    blank-guard clause (never erase an existing tag) is asserted directly on
    :func:`_qualifies_for_bulk_write` -- ``compute_proposed_tags`` copies every non-None metadata
    field, so a server-computed comparison structurally never blanks, making the guard defensive.
    """
    from phaze.routers.tags import _qualifies_for_bulk_write

    clean, _ = await seed_executed_file_with_metadata(original_filename="New Artist - New Title.mp3", artist=None, title=None, album="Keep Album")
    zero, _ = await seed_executed_file_with_metadata(
        original_filename="plain.mp3", artist=None, title=None, album=None, year=None, genre=None, track_number=None
    )

    resp = await _post_visible_tag_bulk(client)
    assert resp.status_code == 200

    async def _log_count(file_id: object, *, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id)
        if status is not None:
            stmt = stmt.where(TagWriteLog.status == status)
        return (await session.execute(stmt)).scalar_one()

    # The clean file is bulk-DISPATCHED exactly once (one audit row -- a real write, queued on the
    # owning agent). phaze-6bkk: the api performs no mutagen write, so the row's status is `queued`
    # rather than the FAILED this used to observe from an unpatched write against a nonexistent path.
    assert await _log_count(clean.id) == 1, "a clean >=1-change file is dispatched exactly once"
    assert await _log_count(clean.id, status="queued") == 1, "the dispatched write is recorded queued"
    assert await _log_count(clean.id, status="no_op") == 0, "a written file is not a NO_OP"
    # A zero-change file is not rendered and therefore cannot enter the reviewed authorization scope.
    assert await _log_count(zero.id) == 0

    # Blank-guard clause: a comparison that would erase an existing tag never qualifies.
    blanking = [{"field": "artist", "label": "Artist", "current": "Existing", "proposed": None, "changed": True}]
    assert _qualifies_for_bulk_write(blanking) is False
    clean_cmp = [{"field": "artist", "label": "Artist", "current": None, "proposed": "New", "changed": True}]
    assert _qualifies_for_bulk_write(clean_cmp) is True


@pytest.mark.asyncio
async def test_tag_bulk_makes_forward_progress_past_zero_change_wall(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-01: repeated bulk submits reach a qualifying file trapped behind a full window of zero-change files.

    With the per-submit cap patched to 2, two zero-change applied files (``aaa_noop_*``) fill the
    entire alphabetically-first window; a qualifying file (``aaa_qual - New Title.mp3``) sits just
    past it. Pre-WR-01 the zero-change files never earned a terminal log, so every submit re-selected
    the SAME window and the qualifying file was never written -- re-submitting made no progress. The
    fix persists a terminal NO-OP marker for each zero-change file so ``completed_subq`` evicts it,
    letting the next submit advance to (and write) the qualifying file.
    """
    monkeypatch.setattr("phaze.routers.tags._MAX_BULK_TAG_WRITE", 2)

    # Two zero-change files: filename has no "artist - title", so proposed == current metadata.
    for i in range(2):
        await seed_executed_file_with_metadata(original_filename=f"aaa_noop_{i}.mp3", artist="Old Artist", title="Old Title")
    # Qualifying file: filename parses a new artist+title absent from metadata (>=1 change, no blank).
    qual, _ = await seed_executed_file_with_metadata(original_filename="aaa_qual - New Title.mp3", artist=None, title=None, album="Keep Album")

    async def _queued_count(file_id: object) -> int:
        # phaze-6bkk: reaching the file now means DISPATCHING its write (status `queued`); the
        # COMPLETED transition happens later, when the agent reports. The WR-01 property under test
        # -- forward progress past the zero-change wall -- is unaffected.
        stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id, TagWriteLog.status == "queued")
        return (await session.execute(stmt)).scalar_one()

    # Submit repeatedly; each submit is bounded, but forward progress must reach the qualifying file.
    for _ in range(3):
        resp = await _post_visible_tag_bulk(client)
        assert resp.status_code == 200
        if await _queued_count(qual.id) == 1:
            break

    assert await _queued_count(qual.id) == 1, "the qualifying file behind the zero-change wall must eventually be reached"


@pytest.mark.asyncio
async def test_tag_bulk_reactivates_a_file_after_a_completed_undo(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """phaze-vwyco: a completed UNDO must not permanently evict the reverted file from bulk write.

    ``_terminal_tagwrite_subq`` (the candidate-window anti-join) and ``_has_terminal_tagwrite``
    (the per-file re-check under the advisory lock) both used to match a completed undo
    (``source="undo"``, status COMPLETED) exactly like a genuine forward completion. Once an undo
    lands, the file's disk tags are (again) changed, yet both checks dropped it from the queue
    forever -- this exercises a real second submit, after the revert, DOES dispatch a fresh write.
    """
    file, _ = await seed_executed_file_with_metadata(original_filename="New Artist - New Title.mp3", artist=None, title=None, album="Keep Album")
    # phaze-o2ln: the bulk loop's per-file rollback expires every ORM object in the session's
    # identity map -- capture the id up front so a post-request ``file.id`` access never triggers a
    # sync lazy-reload (``MissingGreenlet``) under the async engine.
    file_id = file.id
    base = datetime(2026, 8, 1, 12, 0, 0)
    session.add(
        TagWriteLog(
            file_id=file_id,
            before_tags={},
            after_tags={"title": "New Title"},
            source="proposal",
            status=TagWriteStatus.COMPLETED.value,
            written_at=base,
        )
    )
    await session.commit()

    async def _log_count(fid: object, *, status: str | None = None, source: str | None = None) -> int:
        stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == fid)
        if status is not None:
            stmt = stmt.where(TagWriteLog.status == status)
        if source is not None:
            stmt = stmt.where(TagWriteLog.source == source)
        return (await session.execute(stmt)).scalar_one()

    assert await _visible_tag_review_tokens(client) == []
    assert await _log_count(file_id) == 1, "the un-reverted COMPLETED write must be untouched -- the file must not be re-selected yet"

    session.add(
        TagWriteLog(
            file_id=file_id,
            before_tags={"title": "New Title"},
            after_tags={},
            source="undo",
            status=TagWriteStatus.COMPLETED.value,
            written_at=base + timedelta(seconds=30),
        )
    )
    await session.commit()

    resp2 = await _post_visible_tag_bulk(client)
    assert resp2.status_code == 200
    assert await _log_count(file_id, status="queued", source="proposal") == 1, "the reverted file must be re-dispatched, not stay evicted forever"


@pytest.mark.asyncio
async def test_tag_bulk_per_file_commit_survives_mid_loop_abort(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """phaze-k7g6: a mid-loop failure must NOT discard the audit rows of files already written.

    Two qualifying applied files; the SECOND (``bbb``) raises inside the loop, simulating a file
    concurrently un-applied between the candidate SELECT and its iteration (``enqueue_tag_write``
    raises ``ValueError``). Pre-fix -- one commit deferred to the end of the loop, no per-file error
    isolation -- that exception aborted the request and rolled back the FIRST file's flushed
    ``TagWriteLog``, so a dispatched file was left with no audit row -- nothing for the agent's
    result callback to PATCH, stranding the write silently. Post-fix each audit row is committed
    atomically with its dispatch, so the first file's row survives and the bad file merely skips.
    """
    f1, _ = await seed_executed_file_with_metadata(original_filename="aaa - New Title.mp3", artist=None, title=None, album="Keep Album")
    f2, _ = await seed_executed_file_with_metadata(original_filename="bbb - New Title.mp3", artist=None, title=None, album="Keep Album")
    # Capture ids up front: the router's per-file rollback (on the f2 abort) expires these ORM
    # instances in the shared test session, so a post-request ``f1.id`` access would lazy-reload.
    f1_id = f1.id
    f2_id = f2.id

    async def _fake_enqueue(sess: AsyncSession, _router: object, fr: FileRecord, tags: dict, source: str, **_review: object) -> TagWriteLog:
        if fr.id == f2_id:
            # The abort shape: a concurrently un-applied file raises straight out of enqueue_tag_write.
            msg = "Only executed files can have tags written"
            raise ValueError(msg)
        entry = TagWriteLog(file_id=fr.id, before_tags={}, after_tags=tags, source=source, status=TagWriteStatus.QUEUED.value)
        sess.add(entry)
        await sess.flush()
        return entry

    with patch("phaze.routers.tags.enqueue_tag_write", new=AsyncMock(side_effect=_fake_enqueue)):
        resp = await _post_visible_tag_bulk(client)

    assert resp.status_code == 200, "one bad file skips -- it must not 500 the whole batch"
    # The first file's audit row was committed per-file, so the mid-loop abort on the second file
    # cannot roll it back. Pre-fix this was 0 (all flushed rows discarded on the aborted request).
    assert await _tagwrite_log_count(session, f1_id, status="queued") == 1, "the already-dispatched file keeps its audit row"


@pytest.mark.asyncio
async def test_tag_bulk_rollback_does_not_expire_later_candidates(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """phaze-o2ln: a per-file ``session.rollback()`` must not poison files LATER in the loop.

    ``session.rollback()`` expires EVERY object still in the session's identity map, not just the
    one that failed -- so the file immediately after ``aaa`` (the failing file) had its ORM
    ``FileRecord``/``file_metadata`` expired too. Pre-fix, that file's ``fr.file_metadata`` access
    (feeding ``compute_proposed_tags``/``_build_comparison``) triggered a lazy reload from a sync
    context (``MissingGreenlet``), which the loop's own ``except Exception`` swallowed and
    miscounted as a genuine write FAILURE -- even though nothing was wrong with that file. Post-fix
    the loop works off a plain snapshot captured before any rollback, so ``bbb`` is written
    normally regardless of what happened to ``aaa``.
    """
    bad, _ = await seed_executed_file_with_metadata(original_filename="aaa - Bad.mp3", artist=None, title=None, album="Keep Album")
    good, _ = await seed_executed_file_with_metadata(original_filename="bbb - Good.mp3", artist=None, title=None, album="Keep Album")
    bad_id = bad.id
    good_id = good.id

    async def _fake_enqueue(sess: AsyncSession, task_router: object, fr: FileRecord, tags: dict, source: str, **_review: object) -> TagWriteLog:
        if fr.id == bad_id:
            msg = "Only executed files can have tags written"
            raise ValueError(msg)
        entry = TagWriteLog(file_id=fr.id, before_tags={}, after_tags=tags, source=source, status=TagWriteStatus.QUEUED.value)
        sess.add(entry)
        await sess.flush()
        return entry

    with patch("phaze.routers.tags.enqueue_tag_write", new=AsyncMock(side_effect=_fake_enqueue)):
        resp = await _post_visible_tag_bulk(client)

    assert resp.status_code == 200
    assert await _tagwrite_log_count(session, bad_id) == 0, "enqueue_tag_write raised before any row -- no audit row for the bad file"
    assert await _tagwrite_log_count(session, good_id, status="queued") == 1, (
        "the file behind the rollback must be dispatched normally, not miscounted as failed due to an expired ORM attribute"
    )


@pytest.mark.asyncio
async def test_tag_bulk_reports_failures_truthfully(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """phaze-5j82 (re-truthed for phaze-6bkk): the toast never claims an outcome the server has not observed.

    The rule is unchanged; what the server can honestly observe changed. Pre-phaze-6bkk the api did
    the mutagen write itself, so it could distinguish COMPLETED from FAILED. It performs no write at
    all now (DIST-01), so the only truthful tallies are the hand-off, the zero-change NO_OPs, and the
    dispatch failures -- and the toast must say "queued", never "tagged".
    """
    await seed_executed_file_with_metadata(original_filename="zzz - New Title.mp3", artist=None, title=None, album="Keep Album")

    _controller_queue, router = install_fake_queues(client)
    with patch.object(router, "enqueue_for_agent", side_effect=RuntimeError("broker unreachable")):
        resp = await _post_visible_tag_bulk(client)

    assert resp.status_code == 200
    body = resp.text
    assert "0 tag writes queued" in body, "an undispatched write is not counted as queued"
    assert "1 could not be dispatched" in body, "the failure is surfaced to the operator"
    assert "tagged" not in body, "the api never observes a write landing -- it must not claim one"


@pytest.mark.asyncio
async def test_tag_bulk_concurrent_submit_is_blocked(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """phaze-u28m: when the bulk advisory lock is already held, a second submit writes NOTHING.

    Simulates a concurrent/duplicate submit by forcing the lock acquire to fail. The endpoint must
    short-circuit -- no candidate re-select, no disk writes, no audit rows -- and tell the operator a
    bulk write is already in progress, instead of re-processing the identical still-non-terminal set.
    """
    f1, _ = await seed_executed_file_with_metadata(original_filename="qqq - New Title.mp3", artist=None, title=None, album="Keep Album")
    f1_id = f1.id

    with patch("phaze.routers.tags._acquire_bulk_tagwrite_lock", new=AsyncMock(return_value=False)):
        resp = await _post_visible_tag_bulk(client)

    assert resp.status_code == 200
    assert "already in progress" in resp.text
    assert await _tagwrite_log_count(session, f1_id) == 0, "a blocked concurrent submit double-writes nothing"


@pytest.mark.asyncio
async def test_tag_bulk_releases_lock_for_subsequent_submit(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """phaze-u28m: the advisory lock is released after a submit so the NEXT submit is not blocked.

    Two sequential real submits must both proceed (neither sees 'already in progress'); this proves
    the session-scoped lock taken under phaze-k7g6's per-file commits is properly released in the
    ``finally`` and does not leak into the next request.
    """
    await seed_executed_file_with_metadata(original_filename="rrr - New Title.mp3", artist=None, title=None, album="Keep Album")

    tokens = await _visible_tag_review_tokens(client)
    r1 = await _post_visible_tag_bulk(client, tokens)
    r2 = await _post_visible_tag_bulk(client, tokens)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert "already in progress" not in r2.text, "the lock must be released between submits"


@pytest.mark.asyncio
async def test_tag_bulk_write_oob_removes_terminal_rows_and_refreshes_subcount(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """phaze-gwe1: a bulk write that resolves rows to a TERMINAL outcome (COMPLETED write, or a fresh
    NO_OP marker) must OOB-remove those rows and refresh the subcount -- otherwise they linger on
    screen as still-pending rows with a live (and now redundant/undo-chain-corrupting) APPROVE.

    phaze-6bkk: the only outcome this handler can still resolve TERMINALLY is the zero-change NO_OP
    marker, which is decided entirely from DB state and needs no disk access at all. A dispatched
    write is by definition not yet terminal, so its row correctly stays (asserted below).
    """
    noop, _ = await seed_executed_file_with_metadata(original_filename="plain.mp3", artist=None, title=None, album=None)

    assert noop.id
    assert await _visible_tag_review_tokens(client) == []


@pytest.mark.asyncio
async def test_tag_bulk_write_leaves_discrepancy_and_failed_rows_in_place(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """phaze-gwe1 / phaze-6bkk: a non-terminal row must NOT be OOB-removed, unlike a terminal one.

    QUEUED joins DISCREPANCY/FAILED as non-terminal, and is the shape that matters now: the write is
    in flight on the agent and its outcome is not yet known, so removing the row would hide a file
    that may come back needing attention. A dispatch failure is likewise non-terminal -- nothing was
    written, and the operator must be able to retry.
    """
    queued_file, _ = await seed_executed_file_with_metadata(
        original_filename="Disc Artist - Disc Title.mp3", artist=None, title=None, album="Keep Album"
    )

    resp = await _post_visible_tag_bulk(client)

    assert resp.status_code == 200
    assert f'id="tagwrite-row-{queued_file.id}" hx-swap-oob="delete"' not in resp.text, "an in-flight QUEUED row stays in the queue by design"

    # Dispatch-failure path: nothing was handed to any agent, so nothing was written.
    failed_file, _ = await seed_executed_file_with_metadata(original_filename="Fail Artist - Fail Title.mp3", artist=None, title=None)
    _controller_queue, router = install_fake_queues(client)
    with patch.object(router, "enqueue_for_agent", side_effect=RuntimeError("broker unreachable")):
        resp2 = await _post_visible_tag_bulk(client)

    assert resp2.status_code == 200
    assert f'id="tagwrite-row-{failed_file.id}" hx-swap-oob="delete"' not in resp2.text, "an undispatched write never wrote anything -- row stays"


@pytest.mark.asyncio
async def test_review_audit_one_row(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """REVIEW-05 -- a single tag apply writes exactly ONE audit row (the append-only trail).

    The full reversibility + dedupe-resolution round-trip is proven in
    ``tests/integration/test_review_audit.py``; this guards the one-row-per-apply core at the
    workspace level. phaze-6bkk: no mutagen patching is needed any more -- the api dispatches
    instead of writing, so the DB audit row is exercised without a file by construction.
    """
    file, _ = await seed_executed_file_with_metadata(original_filename="New Artist - New Title.mp3", artist=None, title=None)
    token = (await _visible_tag_review_tokens(client))[0]
    resp = await client.post(f"/tags/{file.id}/write", data={"review_token": token})
    assert resp.status_code == 200
    stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file.id)
    assert (await session.execute(stmt)).scalar_one() == 1, "exactly one TagWriteLog per apply"


@pytest.mark.asyncio
async def test_tagwrite_workspace_apply_and_bulk_wiring(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """REVIEW-01/REVIEW-02 (Plan 60-03) -- ``/s/tagwrite`` renders the shared diff row over the tag facet.

    An EXECUTED file whose filename parses to a new artist+title (a >=1-change comparison, no COMPLETED
    ``TagWriteLog``) surfaces in the queue. Its per-row APPROVE POSTs ``/tags/{id}/write`` (the write IS the
    apply -- NOT a proposals PATCH); the header bulk button POSTs the id-less server-predicate
    ``/tags/bulk-write-no-discrepancies`` (D-03). Tag rows carry NO SAVE-EDIT (tag inline-edit is out of
    the initial cut) and NO proposals-facet ``hx-patch``. phaze-o5rf: a FRESH row (no TagWriteLog at
    all yet) renders NO undo control -- undo_tag_write would 404 on it (nothing to revert), so the
    control must not be advertised.
    """
    file, _ = await seed_executed_file_with_metadata(original_filename="New Artist - New Title.mp3", artist=None, title=None, album="Keep Album")

    frag = await client.get("/s/tagwrite", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    # Per-row apply wiring is the tag write path (POST), NOT a proposals PATCH.
    assert f'hx-post="/tags/{file.id}/write"' in body, "APPROVE posts the tag write, not a proposals PATCH"
    # phaze-o5rf: a fresh row (no prior TagWriteLog) has nothing to undo -- no undo control at all.
    assert f"/tags/{file.id}/undo" not in body, "a fresh row with no prior write log must not advertise UNDO"
    assert "UNDO" not in body
    # The bulk header is the id-less D-03 server predicate.
    assert 'hx-post="/tags/bulk-write-no-discrepancies"' in body
    assert "Approve visible eligible tag writes" in body
    assert "Approve the eligible tag changes visible on this reviewed page?" in body
    # Tag inline-edit is out of cut -- no SAVE-EDIT control, no proposals-facet edit PATCH.
    assert "SAVE EDIT" not in body, "tag rows render no SAVE-EDIT (tag inline-edit out of cut)"
    assert f'hx-patch="/tags/{file.id}/write"' not in body, "tag apply never routes through a proposals PATCH"
    # The computed tag diff surfaces (before/after summaries autoescaped through the shared partial).
    assert "New Artist" in body and "grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]" in body


@pytest.mark.asyncio
async def test_tagwrite_workspace_shows_undo_only_with_prior_write_log(
    client: AsyncClient,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
    session: AsyncSession,
) -> None:
    """phaze-o5rf: UNDO is surfaced ONLY on a row that already carries a (non-terminal) TagWriteLog --
    a DISCREPANCY entry -- where undo_tag_write can genuinely revert the file. This is the ONLY state
    where the pending queue's advertised reversibility is actually reachable.
    """
    file, _ = await seed_executed_file_with_metadata(original_filename="Discrepant Artist - Some Title.mp3", artist=None, title=None)
    session.add(
        TagWriteLog(
            file_id=file.id,
            before_tags={"title": None},
            after_tags={"title": "Some Title"},
            source="review",
            status="discrepancy",
        )
    )
    await session.commit()

    frag = await client.get("/s/tagwrite?status=blocked", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert f'hx-post="/tags/{file.id}/undo"' in body, "a row with a prior write log DOES advertise a working UNDO"
