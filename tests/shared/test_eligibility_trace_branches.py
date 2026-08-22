"""Branch coverage for ``routers/pipeline/skip.py`` — the trace arms and the two degrade paths.

WHY THIS FILE EXISTS. ``phaze-oau1o`` split the 2,217-NLOC ``routers/pipeline.py`` into a package.
That split did not change any behaviour, but it did change what coverage MEASURES: this cluster of
code was always this thinly covered, and the god-file's 2,217-statement denominator diluted it out
of sight. Measured on its own, ``skip.py`` came in at 84.67% while every other file in the repo sat
at or above 90%.

The gaps were not cosmetic. They were the per-stage arms of :func:`_eligibility_trace_context` that
the existing ``test_eligibility_trace.py`` never reaches (it exercises ``metadata`` and ``propose``
only), plus BOTH degrade paths — the ones whose entire job is to keep a poll from 500ing, and which
therefore only ever run when something is already wrong.

Covered here:

* the ``tracklist`` arm and the ``apply`` arm (the latter joins ``execution_log`` through proposals,
  because ``execution_log`` carries no ``file_id``)
* :func:`_has_approved_proposal` — apply's ELIG-02 gate, both verdicts
* the blocker labels ``done`` / ``inflight`` and the done-phrase split between genuinely done and
  force-skipped (⊘), which the trace deliberately reports as *skipped, not done*
* ``eligibility_trace``'s except arm, which must render "unavailable" rather than propagate
* ``force_skip_stage``'s ``IntegrityError`` arm — the file-deleted-between-pre-check-and-insert race,
  which must degrade to the no-op toast rather than 500

Must pass in the ``shared`` bucket in isolation (consumes the DB fixtures -> auto-marked integration).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.enums.stage import Stage, Status
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.routers.pipeline import skip as skip_mod


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Seed a committed FileRecord (FK anchor for the per-stage marker rows)."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


# --------------------------------------------------------------------------------------------------
# Direct unit coverage for the three pure conjunct helpers extracted from
# ``_eligibility_trace_context`` (phaze-bk9el.11, CCN 22 -> below 15). Each one is DB-free, so these
# run as plain sync tests rather than through the router -- the async/DB tests above and below still
# exercise the composed function end-to-end; these pin down every branch of the extraction itself.
# --------------------------------------------------------------------------------------------------


def test_upstream_verdict_apply_gated_on_approved_proposal() -> None:
    """apply's upstream conjunct is ELIG-02 (an APPROVED proposal), not a DAG walk, in both verdicts."""
    assert skip_mod._eligibility_upstream_verdict(Stage.APPLY, (), {}, has_approved=True) == (True, "approved proposal exists")
    assert skip_mod._eligibility_upstream_verdict(Stage.APPLY, (), {}, has_approved=False) == (False, "no approved proposal")


def test_upstream_verdict_enrich_stage_is_vacuously_met() -> None:
    """A stage with no ``ELIGIBILITY_DAG`` entry (enrich) is vacuously met (ELIG-01 independence)."""
    assert skip_mod._eligibility_upstream_verdict(Stage.METADATA, (), {}, has_approved=False) == (True, "no upstream (enrich stage)")


def test_upstream_verdict_all_upstream_done() -> None:
    statuses = {Stage.METADATA: Status.DONE, Stage.ANALYZE: Status.DONE}
    verdict = skip_mod._eligibility_upstream_verdict(Stage.PROPOSE, (Stage.METADATA, Stage.ANALYZE), statuses, has_approved=False)
    assert verdict == (True, "all upstream done")


def test_upstream_verdict_names_the_skipped_upstream_as_still_gating() -> None:
    """OQ-1 SCOPE-MINIMAL: a SKIPPED upstream stays gating, named honestly (not reported as met)."""
    statuses = {Stage.METADATA: Status.SKIPPED, Stage.ANALYZE: Status.DONE}
    verdict = skip_mod._eligibility_upstream_verdict(Stage.PROPOSE, (Stage.METADATA, Stage.ANALYZE), statuses, has_approved=False)
    assert verdict == (False, "metadata skipped — downstream stays gated (Phase 90)")


def test_upstream_verdict_names_the_unfinished_upstream() -> None:
    statuses = {Stage.METADATA: Status.NOT_STARTED, Stage.ANALYZE: Status.DONE}
    verdict = skip_mod._eligibility_upstream_verdict(Stage.PROPOSE, (Stage.METADATA, Stage.ANALYZE), statuses, has_approved=False)
    assert verdict == (False, "metadata not done")


def test_blocker_is_empty_when_eligible() -> None:
    """No conjunct is highlighted once the stage is eligible -- ``is_eligible`` short-circuits first."""
    blocker = skip_mod._eligibility_blocker(
        is_eligible=True, is_done=True, is_skipped=True, is_in_flight=True, is_terminal_fail=True, upstream_met=False
    )
    assert blocker == ""


def test_blocker_folds_skipped_onto_done() -> None:
    """A force-skip is a settled state, so it reports as the ``done`` blocker line, not its own."""
    blocker = skip_mod._eligibility_blocker(
        is_eligible=False, is_done=False, is_skipped=True, is_in_flight=False, is_terminal_fail=False, upstream_met=True
    )
    assert blocker == "done"


def test_blocker_orders_inflight_before_terminal_and_upstream() -> None:
    """Mirrors ``eligible()``'s own short-circuit order: in-flight wins over a terminal failure."""
    blocker = skip_mod._eligibility_blocker(
        is_eligible=False, is_done=False, is_skipped=False, is_in_flight=True, is_terminal_fail=True, upstream_met=False
    )
    assert blocker == "inflight"


def test_blocker_names_terminal_failure() -> None:
    blocker = skip_mod._eligibility_blocker(
        is_eligible=False, is_done=False, is_skipped=False, is_in_flight=False, is_terminal_fail=True, upstream_met=False
    )
    assert blocker == "terminal"


def test_blocker_names_upstream_last() -> None:
    blocker = skip_mod._eligibility_blocker(
        is_eligible=False, is_done=False, is_skipped=False, is_in_flight=False, is_terminal_fail=False, upstream_met=False
    )
    assert blocker == "upstream"


def test_done_verdict_distinguishes_done_skipped_and_neither() -> None:
    assert skip_mod._eligibility_done_verdict(is_done=True, is_skipped=False) == (True, "already done")
    assert skip_mod._eligibility_done_verdict(is_done=False, is_skipped=True) == (
        True,
        "force-skipped (⊘) — recorded as skipped, not done",
    )
    assert skip_mod._eligibility_done_verdict(is_done=False, is_skipped=False) == (False, "not done")


# --------------------------------------------------------------------------- per-stage trace arms


@pytest.mark.asyncio
async def test_tracklist_arm_renders_without_a_tracklist_row(client: AsyncClient, session: AsyncSession) -> None:
    """The ``tracklist`` arm probes ``Tracklist.file_id`` and renders rather than raising when absent."""
    file_id = await _seed_file(session)

    response = await client.get(f"/pipeline/files/{file_id}/trace/tracklist")

    assert response.status_code == 200
    assert "Trace unavailable this tick." not in response.text


@pytest.mark.asyncio
async def test_apply_arm_joins_execution_log_through_proposals(client: AsyncClient, session: AsyncSession) -> None:
    """``execution_log`` has NO ``file_id``, so apply's presence/failure probes join through proposals."""
    file_id = await _seed_file(session)

    response = await client.get(f"/pipeline/files/{file_id}/trace/apply")

    assert response.status_code == 200
    assert "Trace unavailable this tick." not in response.text


@pytest.mark.asyncio
async def test_apply_without_an_approved_proposal_names_that_as_the_gate(client: AsyncClient, session: AsyncSession) -> None:
    """apply is gated on ELIG-02 (an APPROVED proposal), NOT on bare done(review)."""
    file_id = await _seed_file(session)

    response = await client.get(f"/pipeline/files/{file_id}/trace/apply")

    assert response.status_code == 200
    assert "no approved proposal" in response.text


@pytest.mark.asyncio
async def test_apply_with_an_approved_proposal_reports_the_gate_satisfied(client: AsyncClient, session: AsyncSession) -> None:
    """The other verdict of :func:`_has_approved_proposal` — the ELIG-02 gate met."""
    file_id = await _seed_file(session)
    session.add(
        RenameProposal(
            file_id=file_id,
            status=ProposalStatus.APPROVED.value,
            proposed_filename="Artist - Event - Title (2024).mp3",
            proposed_path="/music/Artist/Artist - Event - Title (2024).mp3",
        )
    )
    await session.commit()

    response = await client.get(f"/pipeline/files/{file_id}/trace/apply")

    assert response.status_code == 200
    assert "approved proposal exists" in response.text


# --------------------------------------------------------------------------- blocker / done phrasing


@pytest.mark.asyncio
async def test_a_done_stage_is_reported_done_not_eligible(client: AsyncClient, session: AsyncSession) -> None:
    """A finished stage is NOT eligible, and the blocker is ``done`` — not a missing upstream."""
    file_id = await _seed_file(session)
    session.add(AnalysisResult(file_id=file_id, analysis_completed_at=datetime.now(UTC)))
    await session.commit()

    response = await client.get(f"/pipeline/files/{file_id}/trace/analyze")

    assert response.status_code == 200
    assert "already done" in response.text


# --------------------------------------------------------------------------- the two degrade paths


@pytest.mark.asyncio
async def test_trace_degrades_to_unavailable_when_the_context_read_raises(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The except arm exists so a POLL never 500s -- it must render, not propagate.

    Patched on ``routers.pipeline.skip`` rather than the package facade: the route resolves
    ``_eligibility_trace_context`` out of its OWN module namespace, so patching
    ``phaze.routers.pipeline`` would set an attribute nothing reads and silently no-op this test.
    """
    file_id = await _seed_file(session)

    async def _boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("simulated trace read failure")

    monkeypatch.setattr(skip_mod, "_eligibility_trace_context", _boom)

    response = await client.get(f"/pipeline/files/{file_id}/trace/metadata")

    assert response.status_code == 200, "a degraded trace must still render -- a poll never 500s"
    assert "Trace unavailable this tick." in response.text


@pytest.mark.asyncio
async def test_force_skip_degrades_to_a_no_op_toast_when_the_file_races_away(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file can be deleted between the pre-check and the marker insert (FK violation on write).

    That race must render the no-op toast, not a 500: the operator clicked skip on a row that no
    longer exists, which is a no-op, not an error.

    Reproduced HONESTLY rather than by patching the driver: the file is never seeded, and only the
    TOCTOU pre-check is forced to report it present. The INSERT then violates the real foreign key,
    so the `IntegrityError` this asserts on is the genuine article -- the same one
    `delete_scan_cascade` would produce by removing the row between the SELECT and the INSERT.
    (An earlier draft patched `AsyncSession.execute` at CLASS level; that is global, leaked across
    the async teardown of sibling tests, and hard-crashed the interpreter.)
    """
    file_id = uuid.uuid4()  # deliberately NOT seeded -- the row the operator clicked is already gone

    async def _pretend_it_still_exists(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(skip_mod, "_force_skip_file_exists", _pretend_it_still_exists)

    response = await client.post(f"/pipeline/files/{file_id}/skip/metadata", data={"reason": "operator skipped a vanished row"})

    assert response.status_code == 200, "a lost race is a no-op, not a 500"
    assert "Trace unavailable" not in response.text
