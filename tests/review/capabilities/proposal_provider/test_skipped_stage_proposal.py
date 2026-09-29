"""phaze-iyqhg: a file whose enrich stage was force-SKIPPED flows through proposal generation, end to end.

Skipping now satisfies the propose gate (D-08's downstream half), so a file can reach
``generate_proposals`` with NO completed analysis -- typically carrying the FAILED ``analysis`` row
that made the operator skip it (the skip writer is additive and never clears ``failed_at``), or a
failure-only ``metadata`` row. This exercises the real path rather than a mocked session: the real
convergence gate picks the file, the real task reads it from Postgres, the real ``load_prompt_template``
prompt is rendered by the real ``ProposalService``, and ``store_proposals`` writes the row. Only the LLM
network call is replaced, and it captures the prompt the model would have been sent.

The property that matters beyond "no exception": a stage that did not complete is sent as ``null`` --
what ``prompts/naming.md`` documents for "not analyzed" / "no tags were read" -- not as a dict of
NULL aggregates copied off the failed row, which would read to the model as "analyzed, found nothing".
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from sqlalchemy import select

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.stage_skip import StageSkip
from phaze.services.pipeline.proposals import get_proposal_pending_batches
from phaze.services.proposal import BatchProposalResponse, FileProposalResponse, ProposalService, load_prompt_template


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed(session: AsyncSession, *, skip: str) -> uuid.UUID:
    """A file whose ``skip`` stage failed and was then force-skipped; the other stage completed."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            original_path=f"/test/music/{file_id}.mp3",
            original_filename="Artist - Live at Somewhere 2024.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.flush()
    now = datetime.now(UTC)
    if skip == "analyze":
        session.add(AnalysisResult(file_id=file_id, failed_at=now, error_message="essentia crashed"))
        session.add(FileMetadata(file_id=file_id, artist="Artist", title="Live at Somewhere"))
    else:
        session.add(FileMetadata(file_id=file_id, failed_at=now, error_message="unreadable header"))
        session.add(AnalysisResult(file_id=file_id, bpm=124.0, musical_key="Am", analysis_completed_at=now))
    session.add(StageSkip(file_id=file_id, stage=skip, reason="unprocessable source"))
    await session.commit()
    return file_id


def _completion(file_count: int) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = BatchProposalResponse(
        proposals=[
            FileProposalResponse(file_index=i, proposed_filename=f"Artist - Live {i}.mp3", confidence=0.5, reasoning="synthetic")
            for i in range(file_count)
        ]
    ).model_dump_json()
    response.choices[0].finish_reason = "stop"
    return response


def _files_json(prompt: str) -> list[dict[str, Any]]:
    """Pull the rendered ``{files_json}`` array back out of the prompt the model was sent."""
    start = prompt.index("[\n  {")
    decoded, _end = json.JSONDecoder().raw_decode(prompt, start)
    return list(decoded)


@pytest.mark.asyncio
@pytest.mark.parametrize(("skip", "null_key", "present_key"), [("analyze", "analysis", "tags"), ("metadata", "tags", "analysis")])
async def test_a_skipped_stage_is_proposed_with_that_stage_sent_as_null(session: AsyncSession, skip: str, null_key: str, present_key: str) -> None:
    import phaze.database
    from phaze.tasks.proposal import generate_proposals

    file_id = await _seed(session, skip=skip)

    batches = await get_proposal_pending_batches(session, 10)
    assert [str(file_id)] in batches, f"a {skip}-skipped file must clear the propose gate"

    ctx = {
        "async_session": phaze.database.async_session,  # the per-test connection (tests/conftest.py _route_stats_fanout)
        "redis": AsyncMock(),
        "proposal_service": ProposalService(model="test-model", prompt_template=load_prompt_template(), max_rpm=60),
        "task_router": None,
    }
    with (
        patch("phaze.services.proposal.acompletion", new_callable=AsyncMock, return_value=_completion(1)) as llm,
        patch("phaze.tasks.proposal.check_rate_limit", new_callable=AsyncMock),
    ):
        result = await generate_proposals(ctx, file_ids=[str(file_id)], batch_index=0)

    assert result == {"batch": 0, "count": 1, "status": "ok"}
    sent = _files_json(llm.call_args.kwargs["messages"][0]["content"])
    assert len(sent) == 1
    assert sent[0][null_key] is None, f"the skipped {skip} stage must reach the model as null, got {sent[0][null_key]!r}"
    assert sent[0][present_key] is not None, "the completed stage's data must still be sent"
    stored = (await session.execute(select(RenameProposal).where(RenameProposal.file_id == file_id))).scalar_one()
    assert stored.proposed_filename == "Artist - Live 0.mp3"
