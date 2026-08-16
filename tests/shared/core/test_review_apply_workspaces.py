"""Behavioral tests for the v7.0 Review & Apply workspaces (Phase 60, REVIEW-01..05 + R-2/R-5).

This is the single Phase-60 test file (Wave 0, Plan 60-01 / 60-VALIDATION.md). It mirrors the
Phase-58/59 ``tests/test_enrich_analyze_workspaces.py`` / ``tests/test_identify_workspaces.py``
model and defines the full Phase-60 test surface up front:

* The two **foundation** tests are FILLED here (they pass against the six current
  ``_STAGE_PLACEHOLDER`` fragments TODAY and guard the R-2/R-5 contract every later plan must
  preserve):
    - ``test_review_fragments_are_bare``      -> R-5  (every ``/s/{stage}`` HX response is a bare
      fragment: no ``<html>``/``<head>``/``<header>``/``{% extends %}`` document wrapper and no
      ``id="stage-workspace"`` -- the fragment is the workspace body, not the full shell).
    - ``test_review_single_poll_discipline``  -> R-2  (the shell fires EXACTLY ONE
      ``/pipeline/stats`` poll; no Review fragment starts a second ``hx-trigger="every"`` /
      ``setInterval`` loop).

* The seven **behavior** tests are ``xfail`` stubs that COLLECT cleanly now and are converted to
  real assertions by their owning plan/task:
    - ``test_bulk_approve_high_confidence_server_predicate`` -> REVIEW-02 (Plan 60-01 Task 2)
    - ``test_edit_patch_targets_own_row``                    -> REVIEW-01 (Plan 60-01 Task 2)
    - ``test_tag_bulk_no_discrepancy_predicate``             -> REVIEW-02/OQ-1 (Plan 60-01 Task 3)
    - ``test_review_audit_one_row``                          -> REVIEW-05 (Plan 60-01 Task 3)
    - ``test_diff_row_before_after``                         -> REVIEW-01 (Plan 60-02)
    - ``test_dedupe_keeper_resolve_wiring``                  -> REVIEW-03 (Plan 60-03/dedupe)
    - ``test_cue_gate_and_preview``                          -> REVIEW-04 (Plan 60-04/cue)

The per-shape ORM seed factories live in ``tests/conftest.py`` (``make_file``,
``seed_pending_proposal``, ``seed_executed_file_with_metadata``, ``seed_duplicate_group``,
``seed_cue_set``) because they are reused across Review plans -- they are test fixtures only
(ORM inserts, never a backend change).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from phaze.models.proposal import ProposalStatus
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from tests._queue_fakes import install_fake_queues


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord
    from phaze.models.metadata import FileMetadata
    from phaze.models.proposal import RenameProposal


# The Review & Apply workspace stages whose HX fragments must ride the ONE chrome poll (no
# per-fragment ``hx-trigger="every"`` / ``setInterval``). phaze-vvmh appended "apply" -- the group's
# terminal node, and the only in-product trigger for POST /execution/start -- so it inherits the
# bare-fragment and single-poll contracts every sibling is held to.
_WORKSPACE_STAGES = ["propose", "rename", "tagwrite", "move", "dedupe", "cue", "apply"]


# ---------------------------------------------------------------------------
# Foundation tests (FILLED in Plan 60-01 Task 1 -- green against the placeholders today).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_fragments_are_bare(client: AsyncClient) -> None:
    """R-5 -- every ``/s/{stage}`` HX response is a bare workspace fragment.

    Mirrors ``test_identify_workspaces.py::test_identify_fragments_are_bare``: a swapped
    workspace fragment NEVER carries the document wrapper (``<html>``/``<head>``/``<header>``/
    ``{% extends %}``) nor the shell's ``#stage-workspace`` host (that lives only in the full
    ``shell.html`` chrome, which persists across swaps). Passes against the ``_STAGE_PLACEHOLDER``
    fragments today and must stay green once later plans supersede them.
    """
    for stage in _WORKSPACE_STAGES:
        hx = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert hx.status_code == 200, f"{stage} fragment must render 200"
        assert "<html" not in hx.text, f"{stage} fragment must not carry <html>"
        assert "<head" not in hx.text, f"{stage} fragment must not carry <head>"
        assert "<header" not in hx.text, f"{stage} fragment must not carry a <header> landmark"
        assert "{% extends" not in hx.text, f"{stage} fragment must not extend a base template"
        assert 'id="stage-workspace"' not in hx.text, f"{stage} fragment is the body, not the shell host"


@pytest.mark.asyncio
async def test_review_single_poll_discipline(client: AsyncClient) -> None:
    """R-2 -- exactly one chrome poll; no second loop in any Review fragment.

    The full shell (``GET /``) fires the live refresh from persistent chrome: EXACTLY ONE
    ``hx-get="/pipeline/stats"`` element. No swappable Review workspace fragment may carry its own
    ``hx-trigger="every"`` poll or a ``setInterval`` loop -- every workspace's live values ride the
    one chrome poll via ``hx-swap-oob`` against the existing ``stats_bar.html`` seeds. A poll that
    re-renders a diff row / keeper card / cue card would clobber an in-progress operator selection.
    """
    shell = await client.get("/")
    assert shell.status_code == 200
    assert shell.text.count('hx-get="/pipeline/stats"') == 1, "shell must fire exactly one /pipeline/stats poll"

    for stage in _WORKSPACE_STAGES:
        frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert frag.status_code == 200
        assert 'hx-trigger="every' not in frag.text, f"{stage} fragment must not start a second poll loop"
        assert "setInterval" not in frag.text, f"{stage} fragment must not use setInterval"


# ---------------------------------------------------------------------------
# Behavior tests -- xfail stubs converted to real assertions by their owning plan/task.
# (names + reasons per 60-RESEARCH.md Test Map)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_approve_high_confidence_server_predicate(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """REVIEW-02 / D-02 -- bulk approve re-queries confidence>=0.9 and ignores any client id-list.

    Seeds a 0.95 + a 0.50 + a NULL-confidence pending proposal, then submits a client
    ``proposal_ids`` form field naming the 0.50 row (the REVIEW-02 anti-pattern). The server
    re-query MUST drive the result: exactly the 0.95 row is approved; the 0.50 row is untouched
    (the client id-list has NO effect); the NULL-confidence row is excluded by the SQL predicate
    (Pitfall 2), never approved.
    """
    p_high = await seed_pending_proposal(0.95, original_filename="high.mp3")
    p_mid = await seed_pending_proposal(0.50, original_filename="mid.mp3")
    p_null = await seed_pending_proposal(None, original_filename="null.mp3")

    resp = await client.patch(
        "/proposals/bulk-approve-high-confidence",
        data={"proposal_ids": str(p_mid.id)},  # forged selection -- must be ignored
    )
    assert resp.status_code == 200

    await session.refresh(p_high)
    await session.refresh(p_mid)
    await session.refresh(p_null)
    assert p_high.status == ProposalStatus.APPROVED.value, "only the >=0.9 pending row is approved"
    assert p_mid.status == ProposalStatus.PENDING.value, "the client id-list must not approve the 0.50 row"
    assert p_null.status == ProposalStatus.PENDING.value, "NULL confidence is excluded by the SQL predicate"

    # The compatibility endpoint remains live, but neither Review dimension can expose a corpus-wide
    # action that authorizes values outside its rendered window.
    rename = await client.get("/s/rename", headers={"HX-Request": "true"})
    move = await client.get("/s/move", headers={"HX-Request": "true"})
    assert 'hx-patch="/proposals/bulk-approve-high-confidence"' not in rename.text
    assert 'hx-patch="/proposals/bulk-approve-high-confidence"' not in move.text


@pytest.mark.asyncio
async def test_rename_move_headers_report_real_counts_without_unseen_bulk_actions(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """Rename/Move report the real pending total without offering actions beyond rendered rows.

    The sub-count remains corpus-wide rather than ``rename_proposals | length`` (the 200-row render
    cap), while the removed trigger hosts ensure neither surface can approve the hidden remainder.
    """
    await seed_pending_proposal(0.95, original_filename="high1.mp3")
    await seed_pending_proposal(0.95, original_filename="high2.mp3")
    await seed_pending_proposal(0.5, original_filename="low1.mp3")
    await seed_pending_proposal(0.5, original_filename="low2.mp3")
    await seed_pending_proposal(0.5, original_filename="low3.mp3")

    rename = await client.get("/s/rename", headers={"HX-Request": "true"})
    move = await client.get("/s/move", headers={"HX-Request": "true"})
    assert "5 awaiting approval" in rename.text
    assert "5 awaiting approval" in move.text
    assert "rename-trigger-response" not in rename.text
    assert "move-trigger-response" not in move.text
    assert "match now" not in rename.text and "match now" not in move.text


@pytest.mark.asyncio
async def test_canonical_review_discloses_destination_before_whole_proposal_approval(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """A Propose-to-Review transition cannot offer approval without rendering its path facet."""
    proposal = await seed_pending_proposal(
        0.85,
        original_filename="unreviewed.mp3",
        proposed_filename="Reviewed Name.mp3",
        proposed_path="Artist/Event/Reviewed Name.mp3",
    )

    propose = (await client.get("/s/propose")).text
    assert 'href="/s/rename"' in propose and 'hx-get="/s/rename"' in propose

    review = (await client.get("/s/rename", headers={"HX-Request": "true"})).text
    row = review.split(f'id="rename-row-{proposal.id}"', 1)[1].split('id="rename-row-', 1)[0]
    assert "Filename" in row and "Reviewed Name.mp3" in row
    assert "Destination" in row and "Artist/Event/Reviewed Name.mp3" in row
    assert f'hx-patch="/proposals/{proposal.id}/approve"' in row
    assert "/proposals/bulk-approve-high-confidence" not in review

    move_review = (await client.get("/s/move", headers={"HX-Request": "true"})).text
    move_row = move_review.split(f'id="move-row-{proposal.id}"', 1)[1].split('id="move-row-', 1)[0]
    assert "Destination" in move_row and "Artist/Event/Reviewed Name.mp3" in move_row
    assert "Filename" in move_row and "Reviewed Name.mp3" in move_row
    assert f'hx-patch="/proposals/{proposal.id}/approve"' in move_row
    assert "/proposals/bulk-approve-high-confidence" not in move_review
    assert move_row.count("whitespace-pre-wrap break-all") == 4, "all before/after values must be fully inspectable without ellipsis"


@pytest.mark.asyncio
async def test_edit_patch_targets_own_row(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """REVIEW-01 / D-05 -- inline Edit PATCH updates the persisted field and returns only the row.

    The happy path persists the submitted ``proposed`` value to ``proposed_filename``, leaves the
    row PENDING (no LLM re-run, no scalar-state transition) and returns only the row markup (R-6).
    Rejected inputs -- a ``..`` traversal segment, a leading ``/``, or a NUL byte -- 400 and leave
    the row unchanged (T-60-02).
    """
    proposal = await seed_pending_proposal(
        0.8,
        proposed_filename="Original.mp3",
        proposed_path="Artist/Event/Original.mp3",
        original_filename="orig.mp3",
    )

    resp = await client.patch(
        f"/proposals/{proposal.id}/edit",
        data={"proposed": "Edited Name.mp3", "facet": "filename"},
    )
    assert resp.status_code == 200
    # phaze-vvmh: the row comes back as the SHARED pipeline/partials/_diff_row.html under the
    # default rename/filename shape. It used to be the legacy <tr> proposals/partials/proposal_row.html
    # (id="proposal-<id>"), which the v7 workspaces mount into a <div> list -- a swap that dropped
    # table-row markup into a div and threw Alpine ReferenceErrors. That template is deleted; every
    # mutation route now has exactly one response shape.
    assert f'id="rename-row-{proposal.id}"' in resp.text, "returns the targeted row"
    assert "<html" not in resp.text, "returns only the row, not a full page"
    assert "Destination" in resp.text and "Artist/Event/Original.mp3" in resp.text, "row swaps must retain the path being authorized"
    await session.refresh(proposal)
    assert proposal.proposed_filename == "Edited Name.mp3"
    assert proposal.status == ProposalStatus.PENDING.value, "edit is pre-approve -- row stays PENDING"

    for bad in ("../escape.mp3", "/leading.mp3", "na\x00me.mp3"):
        bad_resp = await client.patch(
            f"/proposals/{proposal.id}/edit",
            data={"proposed": bad, "facet": "filename"},
        )
        assert bad_resp.status_code == 400, f"{bad!r} must be rejected"
    await session.refresh(proposal)
    assert proposal.proposed_filename == "Edited Name.mp3", "rejected edits leave the row unchanged"

    # The workspace SAVE EDIT targets ONLY its own row (R-6): the diff-row id + an outerHTML swap.
    frag = await client.get("/s/rename", headers={"HX-Request": "true"})
    assert f'hx-patch="/proposals/{proposal.id}/edit"' in frag.text
    assert f'hx-target="#rename-row-{proposal.id}"' in frag.text
    assert 'hx-swap="outerHTML"' in frag.text


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

    resp = await client.post("/tags/bulk-write-no-discrepancies")
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
    # WR-01: a zero-change file is NOT tag-written (no write attempt), but earns exactly one terminal
    # NO_OP marker so it is evicted from the candidate window and never re-starves the queue.
    assert await _log_count(zero.id) == 1, "a zero-change file gets exactly one log -- the NO_OP marker"
    assert await _log_count(zero.id, status="no_op") == 1, "a zero-change file earns one terminal NO_OP marker (WR-01)"

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
        resp = await client.post("/tags/bulk-write-no-discrepancies")
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

    resp = await client.post("/tags/bulk-write-no-discrepancies")
    assert resp.status_code == 200
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

    resp2 = await client.post("/tags/bulk-write-no-discrepancies")
    assert resp2.status_code == 200
    assert await _log_count(file_id, status="queued", source="proposal") == 1, "the reverted file must be re-dispatched, not stay evicted forever"


async def _tagwrite_log_count(session: AsyncSession, file_id: object, *, status: str | None = None) -> int:
    stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id)
    if status is not None:
        stmt = stmt.where(TagWriteLog.status == status)
    return (await session.execute(stmt)).scalar_one()


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

    async def _fake_enqueue(sess: AsyncSession, _router: object, fr: FileRecord, tags: dict, source: str) -> TagWriteLog:
        if fr.id == f2_id:
            # The abort shape: a concurrently un-applied file raises straight out of enqueue_tag_write.
            msg = "Only executed files can have tags written"
            raise ValueError(msg)
        entry = TagWriteLog(file_id=fr.id, before_tags={}, after_tags=tags, source=source, status=TagWriteStatus.QUEUED.value)
        sess.add(entry)
        await sess.flush()
        return entry

    with patch("phaze.routers.tags.enqueue_tag_write", new=AsyncMock(side_effect=_fake_enqueue)):
        resp = await client.post("/tags/bulk-write-no-discrepancies")

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

    async def _fake_enqueue(sess: AsyncSession, task_router: object, fr: FileRecord, tags: dict, source: str) -> TagWriteLog:
        if fr.id == bad_id:
            msg = "Only executed files can have tags written"
            raise ValueError(msg)
        entry = TagWriteLog(file_id=fr.id, before_tags={}, after_tags=tags, source=source, status=TagWriteStatus.QUEUED.value)
        sess.add(entry)
        await sess.flush()
        return entry

    with patch("phaze.routers.tags.enqueue_tag_write", new=AsyncMock(side_effect=_fake_enqueue)):
        resp = await client.post("/tags/bulk-write-no-discrepancies")

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
        resp = await client.post("/tags/bulk-write-no-discrepancies")

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
        resp = await client.post("/tags/bulk-write-no-discrepancies")

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

    r1 = await client.post("/tags/bulk-write-no-discrepancies")
    r2 = await client.post("/tags/bulk-write-no-discrepancies")

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

    resp = await client.post("/tags/bulk-write-no-discrepancies")

    assert resp.status_code == 200
    body = resp.text
    assert f'id="tagwrite-row-{noop.id}" hx-swap-oob="delete"' in body, "a terminal NO_OP row must be OOB-removed"
    assert 'id="stage-workspace-subcount" hx-swap-oob="true"' in body, "the subcount must be OOB-refreshed"
    assert "0 awaiting approval" in body, "the resolved file is gone from the queue -- subcount reflects it"


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

    resp = await client.post("/tags/bulk-write-no-discrepancies")

    assert resp.status_code == 200
    assert f'id="tagwrite-row-{queued_file.id}" hx-swap-oob="delete"' not in resp.text, "an in-flight QUEUED row stays in the queue by design"

    # Dispatch-failure path: nothing was handed to any agent, so nothing was written.
    failed_file, _ = await seed_executed_file_with_metadata(original_filename="Fail Artist - Fail Title.mp3", artist=None, title=None)
    _controller_queue, router = install_fake_queues(client)
    with patch.object(router, "enqueue_for_agent", side_effect=RuntimeError("broker unreachable")):
        resp2 = await client.post("/tags/bulk-write-no-discrepancies")

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
    file, _ = await seed_executed_file_with_metadata(artist="Original Artist")
    resp = await client.post(f"/tags/{file.id}/write", data={"artist": "New Artist"})
    assert resp.status_code == 200
    stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file.id)
    assert (await session.execute(stmt)).scalar_one() == 1, "exactly one TagWriteLog per apply"


@pytest.mark.asyncio
async def test_diff_row_before_after(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """REVIEW-01 / D-06 -- ``/s/rename`` and ``/s/move`` render the ONE shared diff row over both facets.

    One pending proposal seeds both queues (same ``RenameProposal`` source). ``/s/rename`` renders the
    filename facet: the rose-struck BEFORE + emerald AFTER over the fixed ``1fr_auto_1fr`` grid, the
    APPROVE ``hx-patch`` (never ``hx-post``), the Alpine inline-edit island with its ``name="proposed"``
    input, the stable ``rename-row-{id}`` id, and the ``facet=filename`` hidden field. ``/s/move`` renders
    the SAME partial over the ``proposed_path`` facet (``facet=path``) -- proving D-06's single partial.
    """
    p = await seed_pending_proposal(
        0.95,
        proposed_filename="Renamed.mp3",
        proposed_path="Artist/Album/Renamed.mp3",
        original_filename="messy.mp3",
    )

    rn = await client.get("/s/rename", headers={"HX-Request": "true"})
    assert rn.status_code == 200
    body = rn.text
    assert "line-through" in body and "rose" in body and "emerald" in body
    assert "grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]" in body
    assert "messy.mp3" in body and "Renamed.mp3" in body
    assert f'hx-patch="/proposals/{p.id}/approve"' in body
    assert "hx-post" not in body
    assert "x-data='{ editing" in body
    assert 'name="proposed"' in body
    assert f'id="rename-row-{p.id}"' in body
    assert 'value="filename"' in body

    mv = await client.get("/s/move", headers={"HX-Request": "true"})
    assert mv.status_code == 200
    mbody = mv.text
    assert "Artist/Album/Renamed.mp3" in mbody, "move renders the proposed_path facet (after value)"
    assert f'id="move-row-{p.id}"' in mbody
    assert 'value="path"' in mbody
    assert "hx-post" not in mbody


@pytest.mark.asyncio
async def test_diff_row_edit_island_is_js_context_safe(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """REVIEW-01 security -- a proposed value with an apostrophe (e.g. "Guns N' Roses") must NOT
    break out of the Alpine ``x-data``/``@click`` JS string. ``|e`` is HTML-context escaping and is
    unsafe here (the browser HTML-decodes the attribute before Alpine evaluates it as JS); the row
    uses ``|tojson`` with a single-quoted attribute delimiter so ``'`` serializes to ``\\u0027``.
    """
    await seed_pending_proposal(
        0.95,
        proposed_filename="Guns N' Roses - Don't Cry.mp3",
        proposed_path="Guns N' Roses/Album/Don't Cry.mp3",
        original_filename="messy.mp3",
    )

    body = (await client.get("/s/rename", headers={"HX-Request": "true"})).text

    # The vulnerable single-quote-delimited JS-string pattern must be gone entirely.
    assert "val:'" not in body, "|e-in-JS breakout pattern (val:'...') must not be present"
    # The tojson-safe island delimiter is in use, and the apostrophe is unicode-escaped.
    assert "x-data='{ editing" in body
    assert "\\u0027" in body, "apostrophe must be JS-escaped by |tojson, not left raw in the attribute"


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
    assert "APPROVE ALL WITH NO DISCREPANCIES" in body
    # Tag inline-edit is out of cut -- no SAVE-EDIT control, no proposals-facet edit PATCH.
    assert "SAVE EDIT" not in body, "tag rows render no SAVE-EDIT (tag inline-edit out of cut)"
    assert "/proposals/" not in body, "tag apply never routes through a proposals PATCH"
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

    frag = await client.get("/s/tagwrite", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert f'hx-post="/tags/{file.id}/undo"' in body, "a row with a prior write log DOES advertise a working UNDO"


@pytest.mark.asyncio
async def test_propose_workspace_generate_and_model(
    client: AsyncClient,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """D-01 (Plan 60-03) -- ``/s/propose`` is the generation view: GENERATE ALL + the configured Model.

    Propose is a thin generation view over the SAME pending ``RenameProposal`` source (NOT a diff): the
    header GENERATE ALL button POSTs the EXISTING batch trigger ``/pipeline/proposals`` and the table's
    Model column renders the CONFIGURED ``settings.llm_model`` (A1 -- one model per run, not a per-row
    field). It carries NO per-row Approve/Edit/Skip (approval lives on Rename/Move).
    """
    from phaze.config import settings

    p = await seed_pending_proposal(0.95, proposed_filename="Renamed.mp3", original_filename="messy.mp3")

    frag = await client.get("/s/propose", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert 'hx-post="/pipeline/proposals"' in body, "GENERATE ALL wires to the existing batch trigger"
    assert "GENERATE ALL" in body
    # R-4 bulk-enqueue guard (phaze-dyvt): the busy-gate binds the SEEDED controllerBusy key -- NOT the
    # never-seeded proposalsBusy (undefined > 0 is permanently false, so the button never disabled) -- and
    # carries hx-disabled-elt="this" so a mid-flight re-click enqueues nothing (matching move/rename siblings).
    assert ':disabled="$store.pipeline.controllerBusy > 0"' in body, "busy-gate binds the seeded controllerBusy key"
    assert "proposalsBusy" not in body, "the never-seeded proposalsBusy key must not gate GENERATE ALL"
    assert 'hx-disabled-elt="this"' in body, "GENERATE ALL disables itself in-flight like its bulk-enqueue siblings"
    assert settings.llm_model in body, "the Model column renders the configured llm_model (A1)"
    # The generation view lists the proposal + is not a per-row diff-approve surface.
    assert "messy.mp3" in body and "Renamed.mp3" in body
    assert f"/proposals/{p.id}/approve" not in body, "Propose is a generation view -- no per-row approve here"
    assert 'href="/s/rename"' in body and 'hx-get="/s/rename"' in body, "candidate decisions route to canonical Review"
    assert 'hx-target="#stage-workspace"' in body and 'hx-push-url="true"' in body
    assert "Approval and rejection happen only in Review" in body
    assert 'name="proposal_ids"' not in body, "Propose must not expose decision selection controls"
    assert 'hx-patch="/proposals/bulk' not in body, "the shared bulk endpoint remains backend-compatible but is not a Propose affordance"


@pytest.mark.asyncio
async def test_dedupe_keeper_resolve_wiring(
    client: AsyncClient,
    seed_duplicate_group: Callable[..., Awaitable[list[FileRecord]]],
) -> None:
    """REVIEW-03/REVIEW-05 -- keeper selection is staged; explicit confirmation resolves and remains undoable.

    A seeded duplicate group (two EXECUTED files sharing one sha256) surfaces as a keeper-select card. The
    radio is a form-local ``canonical_id`` selection with no request behavior. The enclosing form posts the
    VERIFIED ``/duplicates/{sha256}/resolve`` contract only from an explicit confirmation control. POSTing
    the form then returns the resolved state whose UNDO round-trips ``file_states`` to
    ``/duplicates/{sha256}/undo`` (REVIEW-05 -- undo reconstructs prior state FROM that blob).
    """
    files = await seed_duplicate_group(count=2)
    sha = files[0].sha256_hash

    frag = await client.get("/s/dedupe", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert f'<form id="dupe-group-{sha}"' in body
    assert f'hx-post="/duplicates/{sha}/review"' in body, "the choice posts only to the non-mutating review route"
    assert 'type="radio" name="canonical_id"' in body
    radio = re.search(r'<input type="radio"[^>]+>', body)
    assert radio is not None and "hx-post" not in radio.group(), "selection alone must not invoke the resolve endpoint"
    assert "group_id" not in body and "keeper_id" not in body, "the UI-SPEC sketch's group_id/keeper_id must NOT appear"
    assert "pending keeper" in body and "will archive" in body
    assert "Review decision" in body and "Selecting a copy only stages this decision" in body
    assert "bitrate first" in body and "tag completeness" in body and "shortest path" in body
    assert body.count("checked") == 1, "exactly one keeper radio is pre-selected per group"

    # Resolving round-trips file_states on UNDO over the existing resolve_response.html toast (REVIEW-05).
    review = await client.post(f"/duplicates/{sha}/review", data={"canonical_id": str(files[0].id)})
    plan_id = re.search(r'name="plan_id" value="([^"]+)"', review.text)
    assert plan_id is not None
    resolved = await client.post(f"/duplicates/{sha}/resolve", data={"plan_id": plan_id.group(1)})
    assert resolved.status_code == 200
    assert f'hx-post="/duplicates/{sha}/undo"' in resolved.text, "the resolved state's UNDO posts the undo route"
    assert 'name="file_states"' in resolved.text, "UNDO carries the file_states blob for a stateful reversal"


@pytest.mark.asyncio
async def test_dedupe_auto_keep_submits_rendered_group_hashes(
    client: AsyncClient,
    seed_duplicate_group: Callable[..., Awaitable[list[FileRecord]]],
) -> None:
    """AUTO-SELECTION submits rendered hashes to a read-only review boundary before bulk resolution.

    Regression for a page/page_size-derived bulk resolve that could silently act on groups the operator
    was never shown. The button now carries NO ``page``/``page_size`` hx-vals; instead it ``hx-include``s
    a hidden ``name="group_hashes"`` input per rendered group, so the write set matches the display set.
    """
    files = await seed_duplicate_group(count=2)
    sha = files[0].sha256_hash

    frag = await client.get("/s/dedupe", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert 'hx-post="/duplicates/review-all"' in body
    assert 'hx-post="/duplicates/resolve-all"' not in body, "the workspace action must not commit before review"
    assert '"page"' not in body, "AUTO-KEEP must not carry a page/page_size re-derivation"
    assert '"page_size"' not in body
    assert 'hx-include="#dedupe-group-hash-inputs"' in body, "AUTO-KEEP must pull the rendered group hashes via hx-include"
    assert f'<input type="hidden" name="group_hashes" value="{sha}">' in body, "the rendered group's hash must be submittable"

    review = await client.post("/duplicates/review-all", data={"group_hashes": [sha]})
    assert review.status_code == 200
    assert "No files have been archived yet" in review.text
    assert 'hx-post="/duplicates/resolve-all"' in review.text
    assert 'name="plan_ids"' in review.text
    assert f'value="{sha}"' not in review.text, "the commit carries opaque plans, not caller-controlled hashes"
    assert "Confirm 1 resolutions" in review.text


@pytest.mark.asyncio
async def test_cue_gate_and_preview(
    client: AsyncClient,
    seed_cue_set: Callable[..., Awaitable[object]],
) -> None:
    """REVIEW-04 -- Cue Sheets is an artifact workspace with prerequisites, preview, generation, and source actions.

    One eligible set (approved tracklist, EXECUTED file, a timestamped track) and one ineligible set (no
    timestamped track) are seeded. The eligible card renders the in-memory ``.cue`` preview ``<pre>`` and an
    APPROVE that POSTs ``/cue/{id}/generate`` (generate IS approve/write -- there is NO ``/approve`` route);
    the ineligible card is ``opacity-60`` with "awaiting tracklist match…" and NO approve control.
    """
    eligible = await seed_cue_set(eligible=True)
    _gated = await seed_cue_set(eligible=False)
    eligible_tracklist_id = eligible[1].id  # (file, tracklist, version)

    frag = await client.get("/s/cue", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    assert "Artifacts · Cue sheets" in body and "Generated artifact" in body
    assert "Applied file:" in body and "Tracklist:" in body and "Timestamps:" in body
    assert "<pre" in body, "the eligible card renders the in-memory .cue preview block"
    assert f'hx-post="/cue/{eligible_tracklist_id}/generate"' in body
    assert "/approve" not in body, "there is no /cue/{id}/approve route -- generate IS the write"
    assert "Generate cue sheet" in body and "Open source record" in body and "Tracklist workspace" in body
    assert "Preview and generation are unavailable" in body and "! required" in body
    assert body.count("Generate cue sheet") == 1, "only the eligible card carries a generation control"


@pytest.mark.asyncio
async def test_cue_preview_failure_is_not_reported_as_missing_timestamps(
    client: AsyncClient,
    seed_cue_set: Callable[..., Awaitable[object]],
) -> None:
    """A renderer failure is a red preview error, not an amber prerequisite diagnosis."""
    await seed_cue_set(eligible=True)
    with patch("phaze.services.review.generate_cue_content", side_effect=ValueError("synthetic preview failure")):
        response = await client.get("/s/cue", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "preview failed" in response.text
    assert "timestamps were not classified as missing" in response.text
    assert "Preview and generation are unavailable until" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["dedupe", "cue"])
async def test_workspace_declares_no_second_toast_container(client: AsyncClient, stage: str) -> None:
    """phaze-gzrd -- the rendered document carries EXACTLY ONE ``id="toast-container"``.

    ``shell.html`` already declares the single toast container, and every stage workspace mounts
    inside that same document (full page render, or an HX fragment swapped into ``#stage-workspace``
    of the live shell). A workspace that declares its own ``id="toast-container"`` therefore creates
    a DUPLICATE id: htmx resolves an ``hx-swap-oob`` selector via ``querySelectorAll`` (ALL matches,
    unlike ``getElementById``) and appends an independent ``cloneNode(true)`` into EACH one, so every
    OOB toast renders twice with separate Alpine state and separate dismissal timers.

    Same hazard class the shared scaffold already calls out ("don't create duplicate ids that would
    steal the live OOB swap", ``_workspace_scaffold.html``). Asserted on the FULL-PAGE render, which
    is the composed document the browser actually holds.
    """
    page = await client.get(f"/s/{stage}")
    assert page.status_code == 200
    declared = page.text.count('id="toast-container"')
    assert declared == 1, f'/s/{stage} renders {declared} elements with id="toast-container"; the shell owns the only one.'

    # And the bare fragment must not smuggle one back in -- it swaps into a shell that already has it.
    frag = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    assert 'id="toast-container"' not in frag.text, f"the /s/{stage} fragment must not declare its own toast container"


# ---------------------------------------------------------------------------
# phaze-vvmh: the Apply (Execute) workspace -- the terminal step of "nothing moves without review,
# then execute", which had no in-product trigger at all between the Phase-62 cutover and this bead.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_workspace_hosts_the_execute_trigger(
    client: AsyncClient, session: AsyncSession, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]
) -> None:
    """/s/apply serves a document that can dispatch approved proposals.

    The regression this pins: POST /execution/start had exactly one caller in the template tree,
    inside a fragment addressed to the deleted ``#stats-bar`` id, so no served document contained an
    execute control. An operator could approve any number of proposals and never apply them.
    """
    proposal = await seed_pending_proposal(0.95)
    proposal.status = ProposalStatus.APPROVED.value
    await session.commit()

    fragment = await client.get("/s/apply", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    body = fragment.text

    assert 'hx-post="/execution/start"' in body, "the Apply workspace no longer triggers execution"
    assert 'hx-target="#apply-execute-response"' in body
    assert 'id="apply-execute-response"' in body, "the dispatch response has no sink to land in"
    assert "hx-confirm" in body, "R-4: a mass action must confirm"
    # The button's sink is a SIBLING, not an ancestor (phaze-thd6) -- otherwise dispatching would
    # delete the control that dispatched.
    sink_index = body.index('id="apply-execute-response"')
    trigger_index = body.index('hx-post="/execution/start"')
    assert trigger_index < sink_index, "the execute trigger must not live inside its own response sink"


@pytest.mark.asyncio
async def test_apply_workspace_disables_execute_with_nothing_approved(client: AsyncClient) -> None:
    """With zero approved proposals the trigger is inert and says why -- it does not post an empty batch."""
    fragment = await client.get("/s/apply", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    body = fragment.text

    assert "disabled" in body
    assert 'hx-post="/execution/start"' not in body, "an empty batch must not be dispatchable"
    assert "Nothing approved yet" in body


@pytest.mark.asyncio
async def test_apply_rail_node_is_navigable(client: AsyncClient) -> None:
    """The rail offers the Execute node, so the workspace is reachable without knowing the URL.

    A stage partial with no rail entry is only marginally better than no stage at all -- the missing
    rail node is half of why the previous execute affordance went unnoticed.
    """
    shell = await client.get("/s/apply")
    assert shell.status_code == 200
    assert 'data-rail-stage="apply"' in shell.text
    assert 'hx-get="/s/apply"' in shell.text
    assert 'aria-current="page"' in shell.text
