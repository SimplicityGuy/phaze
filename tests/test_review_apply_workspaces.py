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

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from phaze.models.proposal import ProposalStatus
from phaze.models.tag_write_log import TagWriteLog


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord
    from phaze.models.metadata import FileMetadata
    from phaze.models.proposal import RenameProposal


# The six redesigned Review & Apply workspace stages whose HX fragments must ride the ONE chrome
# poll (no per-fragment ``hx-trigger="every"`` / ``setInterval``).
_WORKSPACE_STAGES = ["propose", "rename", "tagwrite", "move", "dedupe", "cue"]


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

    # The Rename workspace header wires this id-less server predicate -- no client id-list markup (D-02).
    frag = await client.get("/s/rename", headers={"HX-Request": "true"})
    assert 'hx-patch="/proposals/bulk-approve-high-confidence"' in frag.text
    assert "proposal_ids" not in frag.text, "the bulk button carries no client-built id-list"


@pytest.mark.asyncio
async def test_edit_patch_targets_own_row(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """REVIEW-01 / D-05 -- inline Edit PATCH updates the persisted field and returns only the row.

    The happy path persists the submitted ``proposed`` value to ``proposed_filename``, leaves the
    row PENDING (no LLM re-run, no FileState transition) and returns only the row markup (R-6).
    Rejected inputs -- a ``..`` traversal segment, a leading ``/``, or a NUL byte -- 400 and leave
    the row unchanged (T-60-02).
    """
    proposal = await seed_pending_proposal(0.8, proposed_filename="Original.mp3", original_filename="orig.mp3")

    resp = await client.patch(
        f"/proposals/{proposal.id}/edit",
        data={"proposed": "Edited Name.mp3", "facet": "filename"},
    )
    assert resp.status_code == 200
    assert f'id="proposal-{proposal.id}"' in resp.text, "returns the targeted row"
    assert "<html" not in resp.text, "returns only the row, not a full page"
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

    async def _log_count(file_id: object) -> int:
        stmt = select(func.count()).select_from(TagWriteLog).where(TagWriteLog.file_id == file_id)
        return (await session.execute(stmt)).scalar_one()

    assert await _log_count(clean.id) == 1, "a clean >=1-change file is written exactly once"
    assert await _log_count(zero.id) == 0, "a zero-change file is not written"

    # Blank-guard clause: a comparison that would erase an existing tag never qualifies.
    blanking = [{"field": "artist", "label": "Artist", "current": "Existing", "proposed": None, "changed": True}]
    assert _qualifies_for_bulk_write(blanking) is False
    clean_cmp = [{"field": "artist", "label": "Artist", "current": None, "proposed": "New", "changed": True}]
    assert _qualifies_for_bulk_write(clean_cmp) is True


@pytest.mark.asyncio
async def test_review_audit_one_row(
    client: AsyncClient,
    session: AsyncSession,
    seed_executed_file_with_metadata: Callable[..., Awaitable[tuple[FileRecord, FileMetadata]]],
) -> None:
    """REVIEW-05 -- a single tag apply writes exactly ONE audit row (the append-only trail).

    The full reversibility + dedupe-resolution round-trip is proven in
    ``tests/integration/test_review_audit.py``; this guards the one-row-per-apply core at the
    workspace level. The mutagen write is patched so the DB audit row is exercised without a file.
    """
    file, _ = await seed_executed_file_with_metadata(artist="Original Artist")
    with (
        patch("phaze.services.tag_writer._extract_before_tags", return_value={"artist": "Original Artist"}),
        patch("phaze.services.tag_writer.write_tags"),
        patch("phaze.services.tag_writer.verify_write", return_value={}),
    ):
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
    assert "grid-cols-[1fr_auto_1fr]" in body
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
    apply -- NOT a proposals PATCH) and its per-row UNDO POSTs ``/tags/{id}/undo``; the header bulk button
    POSTs the id-less server-predicate ``/tags/bulk-write-no-discrepancies`` (D-03). Tag rows carry NO
    SAVE-EDIT (tag inline-edit is out of the initial cut) and NO proposals-facet ``hx-patch``.
    """
    file, _ = await seed_executed_file_with_metadata(original_filename="New Artist - New Title.mp3", artist=None, title=None, album="Keep Album")

    frag = await client.get("/s/tagwrite", headers={"HX-Request": "true"})
    assert frag.status_code == 200
    body = frag.text

    # Per-row apply wiring is the tag write path (POST), NOT a proposals PATCH.
    assert f'hx-post="/tags/{file.id}/write"' in body, "APPROVE posts the tag write, not a proposals PATCH"
    assert f'hx-post="/tags/{file.id}/undo"' in body, "UNDO posts the tag undo route"
    # The bulk header is the id-less D-03 server predicate.
    assert 'hx-post="/tags/bulk-write-no-discrepancies"' in body
    assert "APPROVE ALL WITH NO DISCREPANCIES" in body
    # Tag inline-edit is out of cut -- no SAVE-EDIT control, no proposals-facet edit PATCH.
    assert "SAVE EDIT" not in body, "tag rows render no SAVE-EDIT (tag inline-edit out of cut)"
    assert "/proposals/" not in body, "tag apply never routes through a proposals PATCH"
    # The computed tag diff surfaces (before/after summaries autoescaped through the shared partial).
    assert "New Artist" in body and "grid-cols-[1fr_auto_1fr]" in body


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
    assert settings.llm_model in body, "the Model column renders the configured llm_model (A1)"
    # The generation view lists the proposal + is not a per-row diff-approve surface.
    assert "messy.mp3" in body and "Renamed.mp3" in body
    assert f"/proposals/{p.id}/approve" not in body, "Propose is a generation view -- no per-row approve here"


@pytest.mark.xfail(reason="converted to real assertions by the dedupe workspace plan (REVIEW-03)", strict=False)
@pytest.mark.asyncio
async def test_dedupe_keeper_resolve_wiring(client: AsyncClient) -> None:
    """REVIEW-03 -- keeper radio posts /duplicates/{sha256}/resolve with canonical_id; UNDO round-trips."""
    resp = await client.get("/s/dedupe", headers={"HX-Request": "true"})
    assert 'name="canonical_id"' in resp.text


@pytest.mark.xfail(reason="converted to real assertions by the cue workspace plan (REVIEW-04)", strict=False)
@pytest.mark.asyncio
async def test_cue_gate_and_preview(client: AsyncClient) -> None:
    """REVIEW-04 -- eligible sets render a preview + APPROVE->/cue/{id}/generate; ineligible are gated."""
    resp = await client.get("/s/cue", headers={"HX-Request": "true"})
    assert "/generate" in resp.text
