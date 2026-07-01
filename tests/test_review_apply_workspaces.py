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

import pytest

from phaze.models.proposal import ProposalStatus


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

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


@pytest.mark.xfail(reason="converted to real assertions by Plan 60-01 Task 3 (D-03/OQ-1 tag predicate)", strict=False)
@pytest.mark.asyncio
async def test_tag_bulk_no_discrepancy_predicate(client: AsyncClient) -> None:
    """REVIEW-02 / D-03 / OQ-1 -- tag bulk writes only the qualifying no-blank, >=1-change set."""
    resp = await client.post("/tags/bulk-write-no-discrepancies")
    assert resp.status_code == 200


@pytest.mark.xfail(reason="converted to real assertions by Plan 60-01 Task 3 (REVIEW-05 audit)", strict=False)
@pytest.mark.asyncio
async def test_review_audit_one_row(client: AsyncClient) -> None:
    """REVIEW-05 -- each apply writes exactly one audit row and is reversible (integration-level)."""
    raise AssertionError("audit reversibility asserted in tests/integration/test_review_audit.py")


@pytest.mark.xfail(reason="converted to real assertions by Plan 60-02 (shared _diff_row.html)", strict=False)
@pytest.mark.asyncio
async def test_diff_row_before_after(client: AsyncClient) -> None:
    """REVIEW-01 -- the shared diff row renders before->after with an inline-edit ``name="proposed"``."""
    resp = await client.get("/s/rename", headers={"HX-Request": "true"})
    assert 'name="proposed"' in resp.text


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
