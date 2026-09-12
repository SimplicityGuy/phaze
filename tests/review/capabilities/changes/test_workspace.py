"""Changes-review scenarios moved from ``tests/shared/core/test_review_apply_workspaces.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phaze.models.proposal import ProposalStatus


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.proposal import RenameProposal

_WORKSPACE_STAGES = ["propose", "rename", "tagwrite", "move", "dedupe", "cue", "apply"]


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


@pytest.mark.asyncio
async def test_no_corpus_wide_bulk_approve_survives_anywhere(
    client: AsyncClient,
    session: AsyncSession,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    """REVIEW-02 / D-02's corpus-wide bulk approve is retired -- no surface and no route (phaze-7tiqp).

    This test used to exercise ``PATCH /proposals/bulk-approve-high-confidence`` and assert its
    server-side predicate drove the result (>=0.9 PENDING approved, a forged client id-list ignored,
    NULL confidence excluded). ADR-0008 then made Changes Review the only surface that authorizes
    anything, and its bulk action is selection-driven: an operator approves rows they have SEEN.
    That deleted the route's two callers, and phaze-7tiqp deleted the route.

    So the property is now the stronger one -- there is no way, from any surface or by hand, to
    authorize a corpus-wide set nobody rendered. Neither the seeded 0.95 row nor anything else may
    transition, and the two Review dimensions must not link at the retired endpoint.
    """
    p_high = await seed_pending_proposal(0.95, original_filename="high.mp3")
    p_mid = await seed_pending_proposal(0.50, original_filename="mid.mp3")
    p_null = await seed_pending_proposal(None, original_filename="null.mp3")

    resp = await client.patch(
        "/proposals/bulk-approve-high-confidence",
        data={"proposal_ids": str(p_mid.id)},
    )
    assert not resp.is_success, f"the retired route answered {resp.status_code}"

    for proposal in (p_high, p_mid, p_null):
        await session.refresh(proposal)
        assert proposal.status == ProposalStatus.PENDING.value, "nothing may be approved without being rendered and selected"

    rename = await client.get("/s/rename", headers={"HX-Request": "true"})
    move = await client.get("/s/move", headers={"HX-Request": "true"})
    assert "/proposals/bulk-approve-high-confidence" not in rename.text
    assert "/proposals/bulk-approve-high-confidence" not in move.text


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
    move_row = move_review.split(f'id="rename-row-{proposal.id}"', 1)[1].split('id="rename-row-', 1)[0]
    assert "Destination" in move_row and "Artist/Event/Reviewed Name.mp3" in move_row
    assert "Filename" in move_row and "Reviewed Name.mp3" in move_row
    assert f'hx-patch="/proposals/{proposal.id}/approve"' in move_row
    assert "/proposals/bulk-approve-high-confidence" not in move_review
    assert move_row.count("whitespace-pre-wrap break-all") == 4, "the compatibility alias renders the same complete canonical decision"


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
    assert f'hx-post="/proposals/{p.id}/approve"' not in body
    assert "x-data='{ editing" in body
    assert 'name="proposed"' in body
    assert f'id="rename-row-{p.id}"' in body
    assert 'value="filename"' in body

    mv = await client.get("/s/move", headers={"HX-Request": "true"})
    assert mv.status_code == 200
    mbody = mv.text
    assert "Artist/Album/Renamed.mp3" in mbody, "move renders the proposed_path facet (after value)"
    assert f'id="rename-row-{p.id}"' in mbody
    assert 'value="filename"' in mbody
    assert "Changes Review" in mbody


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

    # phaze-tzy6s.12: R-4 confirmation moved OFF the native browser prompt onto the shared
    # ui.confirmation <dialog> primitive. hx-confirm renders one unstyled string, which cannot carry
    # a manifest -- the whole point of the preflight. Assert the product's own pattern is used, and
    # that the native one has not crept back.
    assert "hx-confirm" not in body, "R-4 confirmation must use the shared dialog, not the native browser prompt"
    assert 'id="apply-confirm"' in body, "the shared confirmation dialog is missing"
    assert "<dialog" in body
    assert "showModal()" in body, "the execute button must open the confirmation dialog"

    # The dispatching element is a SIBLING of the sink, never inside it (phaze-thd6) -- otherwise
    # dispatching would delete the control that dispatched. The pre-.12 form of this check compared
    # string indices, which only worked while the trigger preceded the sink; the dialog now renders
    # last, so assert the invariant directly instead: the sink is emitted EMPTY, so nothing that
    # posts can be a descendant of it.
    assert '<div id="apply-execute-response" class="px-6 pt-4 empty:hidden"></div>' in body, (
        "the response sink must be emitted empty so no trigger can live inside it"
    )


@pytest.mark.asyncio
async def test_apply_counter_row_accounts_for_executed_proposals(
    client: AsyncClient, session: AsyncSession, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]
) -> None:
    """Regression (phaze-te2g3): the Execute counter row shows every proposal it totals.

    ``Total`` is a plain ``count()``. ``Approved`` is ``count(case(status == APPROVED))`` and stays
    that way deliberately -- on THIS card the operator's question is "what is still to dispatch", so
    folding ADR-0008's ``approved OR executed`` union into it would count already-done work as
    pending work on the one control that moves bytes. The defect was the other half: ``executed``
    was counted by ``Total`` and by no visible status, so the row silently did not account for a
    proposal it had already included in its own total.

    Asserted as arithmetic over the rendered numbers rather than as the presence of a label, so it
    fails on the old query regardless of what the new card happens to be called.
    """
    approved = await seed_pending_proposal(0.95, original_filename="counter-approved.mp3")
    approved.status = ProposalStatus.APPROVED.value
    executed = await seed_pending_proposal(0.95, original_filename="counter-executed.mp3")
    executed.status = ProposalStatus.EXECUTED.value
    rejected = await seed_pending_proposal(0.40, original_filename="counter-rejected.mp3")
    rejected.status = ProposalStatus.REJECTED.value
    await seed_pending_proposal(0.95, original_filename="counter-pending.mp3")
    await session.commit()

    body = (await client.get("/s/apply", headers={"HX-Request": "true"})).text

    def metric(label: str) -> int:
        after = body.split(f">{label}</span>", 1)
        assert len(after) == 2, f"the Execute counter row has no {label!r} metric"
        return int(after[1].split(">", 2)[1].split("<")[0].strip())

    assert metric("Executed") == 1, "an executed proposal must have its own visible count"
    assert metric("Approved") == 1, "Approved must NOT absorb executed rows -- Execute reads it as 'still to dispatch'"
    assert metric("Needs Review") + metric("Approved") + metric("Executed") + metric("Rejected") == metric("Total"), (
        "the per-status metrics must account for every proposal Total counts"
    )


@pytest.mark.asyncio
async def test_apply_counter_row_accounts_for_failed_proposals(
    client: AsyncClient, session: AsyncSession, seed_pending_proposal: Callable[..., Awaitable[RenameProposal]]
) -> None:
    """phaze-5uh4u: the Execute counter row shows a Blocked metric for `failed` proposals.

    Closes the last accounting gap phaze-te2g3 left open (see the test above): before this bead
    `failed` had no visible term at all, so the five metrics summed to Total only when no proposal
    had failed. This corpus includes one, so the arithmetic assertion fails on the old card
    regardless of label wording.
    """
    approved = await seed_pending_proposal(0.95, original_filename="counter-approved-2.mp3")
    approved.status = ProposalStatus.APPROVED.value
    executed = await seed_pending_proposal(0.95, original_filename="counter-executed-2.mp3")
    executed.status = ProposalStatus.EXECUTED.value
    rejected = await seed_pending_proposal(0.40, original_filename="counter-rejected-2.mp3")
    rejected.status = ProposalStatus.REJECTED.value
    blocked = await seed_pending_proposal(0.95, original_filename="counter-blocked.mp3")
    blocked.status = ProposalStatus.FAILED.value
    await seed_pending_proposal(0.95, original_filename="counter-pending-2.mp3")
    await session.commit()

    body = (await client.get("/s/apply", headers={"HX-Request": "true"})).text

    def metric(label: str) -> int:
        after = body.split(f">{label}</span>", 1)
        assert len(after) == 2, f"the Execute counter row has no {label!r} metric"
        return int(after[1].split(">", 2)[1].split("<")[0].strip())

    assert metric("Blocked") == 1, "a failed proposal must have its own visible count"
    assert (metric("Needs Review") + metric("Approved") + metric("Executed") + metric("Blocked") + metric("Rejected")) == metric("Total"), (
        "the per-status metrics must account for every proposal Total counts, including Blocked"
    )


@pytest.mark.asyncio
async def test_apply_workspace_disables_execute_with_nothing_approved(client: AsyncClient) -> None:
    """With zero approved proposals the trigger is inert and says why -- it does not post an empty batch.

    phaze-tzy6s.12 raised the bar from "says why" to "says why AND what to do next", in visible text.
    A disabled control whose explanation lives only in a ``title=`` attribute is unreachable by
    keyboard and by touch, so the reason and the next action are both asserted as body text here.
    """
    fragment = await client.get("/s/apply", headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    body = fragment.text

    assert "disabled" in body
    assert 'hx-post="/execution/start"' not in body, "an empty batch must not be dispatchable"
    assert 'id="apply-confirm"' not in body, "no confirmation dialog should be rendered when nothing can execute"

    assert "No approved proposals are ready to execute." in body, "the disabled reason must be visible text"
    assert "Approve filename and destination changes in Changes Review first." in body, "the disabled state must name the next action"
    # The reason is wired to the button for assistive tech, not left as a floating paragraph.
    assert 'id="apply-blocked-reason"' in body
    assert 'aria-describedby="apply-blocked-reason"' in body


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
