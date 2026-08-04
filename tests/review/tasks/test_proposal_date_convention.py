"""End-to-end coverage of the gated date fallback inside ``generate_proposals`` (phaze-5fta.4).

``tests/review/services/test_date_convention.py`` covers the resolution rule and its gates. What is
asserted HERE is the wiring: that the flag really does reach the proposal path, that the provenance
survives all the way into the persisted ``context_used`` JSONB, and -- the acceptance criterion that
is easiest to break and hardest to notice -- that with the flag off BOTH the prompt sent to the LLM
and the stored row are byte-identical to the pre-phaze-5fta.4 behavior.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from phaze.models.filename_convention import FilenameConvention
from phaze.models.proposal import RenameProposal
from phaze.services.date_convention import CONTEXT_KEY, SOURCE_CONVENTION, SOURCE_FILENAME
from phaze.services.filename_convention_learner import CONVENTION_KIND, CONVENTION_SCOPE
from phaze.services.proposal import BatchProposalResponse, FileProposalResponse, ProposalService, load_prompt_template


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


AMBIGUOUS = "Artist - Event - 04-05-2014 - sbd-alphagrp.mp3"
SELF_RESOLVING = "Artist - Event - 04-25-2014 - sbd-alphagrp.mp3"


class _CapturingProposalService:
    """Stands in for the LLM: records the contexts it was handed, answers with a fixed proposal."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def generate_batch(self, files_context: list[dict[str, Any]]) -> BatchProposalResponse:
        self.seen = files_context
        return BatchProposalResponse(
            proposals=[
                FileProposalResponse(
                    file_index=index,
                    proposed_filename=f"Artist - Event ({index}).mp3",
                    confidence=0.9,
                    reasoning="fixture",
                )
                for index in range(len(files_context))
            ]
        )


def _ctx_for(session: AsyncSession, service: _CapturingProposalService) -> dict[str, Any]:
    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    class _NoOpRedis:
        """A redis double for the LLM rate limiter. ``eval`` here is redis-py's EVAL (run a Lua
        script server-side), NOT Python's builtin -- it executes nothing and always grants a slot."""

        async def eval(self, *_args: Any, **_kwargs: Any) -> int:
            return 1

    return {"async_session": _factory, "redis": _NoOpRedis(), "proposal_service": service, "task_router": None}


@pytest.fixture
def enable_fallback(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Flip ``convention_date_fallback_enabled`` on for one test, with thresholds supplied."""
    from phaze.tasks import proposal as proposal_task

    def _enable(*, min_supporting: int = 50, min_purity: float = 0.99) -> None:
        monkeypatch.setattr(proposal_task.settings, "convention_date_fallback_enabled", True)
        monkeypatch.setattr(proposal_task.settings, "convention_date_min_supporting", min_supporting)
        monkeypatch.setattr(proposal_task.settings, "convention_date_min_purity", min_purity)

    return _enable


@pytest.fixture
def disable_fallback(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Flip ``convention_date_fallback_enabled`` OFF for one test.

    Set explicitly rather than leaning on the field default: the default is an operator decision
    that has already moved once (off -> on, 2026-08-04, on phaze-5fta.5's validation). A flag-off
    test that reads the default is really asserting the default, and silently inverts its own
    meaning the next time that decision changes -- which is exactly what happened to these tests.
    """
    from phaze.tasks import proposal as proposal_task

    def _disable() -> None:
        monkeypatch.setattr(proposal_task.settings, "convention_date_fallback_enabled", False)

    return _disable


async def _seed_convention(session: AsyncSession, *, supporting: int = 500, contradicting: int = 0) -> FilenameConvention:
    row = FilenameConvention(
        scope=CONVENTION_SCOPE,
        scope_value="alphagrp",
        convention_kind=CONVENTION_KIND,
        convention_value="DD-MM",
        supporting_count=supporting,
        contradicting_count=contradicting,
        ambiguous_count=120,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _run(session: AsyncSession, file_ids: list[uuid.UUID]) -> tuple[_CapturingProposalService, dict[str, Any]]:
    from phaze.tasks.proposal import generate_proposals

    service = _CapturingProposalService()
    result = await generate_proposals(_ctx_for(session, service), file_ids=[str(fid) for fid in file_ids], batch_index=0)
    return service, result


async def _context_used(session: AsyncSession, file_id: uuid.UUID) -> dict[str, Any]:
    proposal = (await session.execute(select(RenameProposal).where(RenameProposal.file_id == file_id))).scalar_one()
    assert proposal.context_used is not None
    return proposal.context_used


class TestFlagOff:
    async def test_no_provenance_reaches_the_llm_or_the_stored_row(self, session: AsyncSession, make_file: Any, disable_fallback: Any) -> None:
        """The opt-out path. Even with a perfectly qualifying convention present, nothing changes."""
        disable_fallback()
        await _seed_convention(session)
        record = await make_file(original_filename=AMBIGUOUS)

        service, result = await _run(session, [record.id])

        assert result["status"] == "ok"
        assert CONTEXT_KEY not in service.seen[0]
        assert CONTEXT_KEY not in await _context_used(session, record.id)

    async def test_a_self_resolving_date_is_also_left_alone(self, session: AsyncSession, make_file: Any, disable_fallback: Any) -> None:
        """The flag gates the WHOLE capability, not merely the store lookup."""
        disable_fallback()
        record = await make_file(original_filename=SELF_RESOLVING)

        service, _ = await _run(session, [record.id])

        assert CONTEXT_KEY not in service.seen[0]


class TestFlagOn:
    async def test_an_ambiguous_date_is_resolved_and_its_provenance_persisted(
        self, session: AsyncSession, make_file: Any, enable_fallback: Any
    ) -> None:
        row = await _seed_convention(session)
        enable_fallback()
        record = await make_file(original_filename=AMBIGUOUS)

        service, _ = await _run(session, [record.id])

        assert service.seen[0][CONTEXT_KEY]["date"] == "2014-05-04"
        provenance = (await _context_used(session, record.id))[CONTEXT_KEY]
        assert provenance["source"] == SOURCE_CONVENTION
        assert provenance["convention_id"] == str(row.id)
        assert (provenance["supporting_count"], provenance["contradicting_count"]) == (500, 0)
        assert provenance["confidence"] == pytest.approx(1.0)

    async def test_a_self_resolving_date_is_never_overridden(self, session: AsyncSession, make_file: Any, enable_fallback: Any) -> None:
        """The stored convention says DD-MM; the filename says MM-DD and the filename wins."""
        await _seed_convention(session)
        enable_fallback()
        record = await make_file(original_filename=SELF_RESOLVING)

        _, _ = await _run(session, [record.id])

        provenance = (await _context_used(session, record.id))[CONTEXT_KEY]
        assert provenance["source"] == SOURCE_FILENAME
        assert provenance["date"] == "2014-04-25"
        assert provenance["scope_value"] is None

    async def test_a_below_bar_group_leaves_the_date_unresolved(self, session: AsyncSession, make_file: Any, enable_fallback: Any) -> None:
        await _seed_convention(session, supporting=4)
        enable_fallback()
        record = await make_file(original_filename=AMBIGUOUS)

        service, _ = await _run(session, [record.id])

        assert CONTEXT_KEY not in service.seen[0]
        assert CONTEXT_KEY not in await _context_used(session, record.id)

    async def test_an_impure_group_leaves_the_date_unresolved(self, session: AsyncSession, make_file: Any, enable_fallback: Any) -> None:
        await _seed_convention(session, supporting=900, contradicting=100)
        enable_fallback()
        record = await make_file(original_filename=AMBIGUOUS)

        service, _ = await _run(session, [record.id])

        assert CONTEXT_KEY not in service.seen[0]

    async def test_the_provenance_survives_a_re_run_that_overwrites_the_pending_proposal(
        self, session: AsyncSession, make_file: Any, enable_fallback: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``store_proposals`` upserts the one PENDING row -- the new provenance must land with it."""
        from phaze.tasks import proposal as proposal_task

        await _seed_convention(session)
        record = await make_file(original_filename=AMBIGUOUS)
        # First pass with the capability off, so the stored row starts WITHOUT provenance and the
        # assertion below is about the upsert rather than about the flag's default.
        monkeypatch.setattr(proposal_task.settings, "convention_date_fallback_enabled", False)
        await _run(session, [record.id])
        assert CONTEXT_KEY not in await _context_used(session, record.id)

        enable_fallback()
        await _run(session, [record.id])

        assert (await _context_used(session, record.id))[CONTEXT_KEY]["source"] == SOURCE_CONVENTION


class TestPromptGuidance:
    """The prompt is part of "byte-identical", because it is what the LLM actually receives."""

    def test_the_guidance_line_is_removed_entirely_when_no_file_carries_provenance(self) -> None:
        template = load_prompt_template()
        service = ProposalService(model="test", prompt_template=template, max_rpm=1)
        contexts = [{"index": 0, "original_filename": "track.mp3"}]

        prompt = service.prompt_template.replace("{date_convention_guidance}\n", "").replace("{files_json}", json.dumps(contexts, indent=2))

        assert "{date_convention_guidance}" not in prompt
        assert "date_convention" not in prompt
        # Nothing is left behind where the placeholder line was -- not even a blank line.
        assert "with their text content\n\n" in prompt

    def test_the_guidance_line_appears_when_a_file_carries_provenance(self) -> None:
        from phaze.services.proposal import _date_convention_guidance

        assert _date_convention_guidance([{"index": 0}]) == ""
        rendered = _date_convention_guidance([{"index": 0}, {"index": 1, CONTEXT_KEY: {"date": "2014-05-04"}}])
        assert rendered.startswith("- `date_convention`:")
        assert rendered.endswith("\n")

    def test_the_template_still_carries_the_placeholder(self) -> None:
        """A template edit that dropped the placeholder would silently disable the guidance."""
        assert "{date_convention_guidance}\n" in load_prompt_template()
