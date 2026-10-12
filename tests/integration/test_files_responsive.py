"""Responsive and accessible interaction contracts for the Files workspace."""

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


def _make_file(path: str = "/music/example.mp3") -> FileRecord:
    file_id = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=file_id,
        sha256_hash=file_id.hex,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="mp3",
        file_size=1000,
    )


@pytest.mark.asyncio
async def test_rows_and_details_controls_share_one_guarded_record_activation_contract(client: AsyncClient, session: AsyncSession) -> None:
    file = _make_file()
    session.add(file)
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text
    row_start = body[body.index('<tr id="files-row-1"') : body.index(">", body.index('<tr id="files-row-1"'))]

    assert f'hx-get="/record/{file.id}"' in row_start
    assert 'hx-target="#record-body"' in row_start
    assert "data-record-row" in row_start
    assert 'tabindex="0"' in row_start
    assert "cursor-pointer" in row_start
    assert "event.target.closest" in row_start
    assert "event.target===this" in row_start
    # Desktop row + its Details button + compact card + its Details button all use the same URL.
    assert body.count(f'hx-get="/record/{file.id}"') == 4
    assert body.count('aria-label="Open details for example.mp3"') == 4
    assert "$dispatch(&#39;record:open&#39;, { el: $el })" in body or "$dispatch('record:open', { el: $el })" in body


@pytest.mark.asyncio
async def test_compact_layout_uses_cards_and_progressive_stage_disclosure(client: AsyncClient, session: AsyncSession) -> None:
    session.add(_make_file())
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text

    assert 'class="hidden md:block"' in body
    assert 'class="grid gap-3 px-4 py-3 md:hidden"' in body
    assert 'class="min-w-0 cursor-pointer rounded-lg border' in body
    assert "Stage details</summary>" in body
    assert "hidden whitespace-nowrap px-3 py-2 text-center xl:table-cell" in body
    for label in ("Metadata", "Analyze", "Propose", "Review", "Execute"):
        assert f">{label}<" in body
    for abbreviation in (">Meta<", ">Prop<", ">Appr<", ">Exec<"):
        assert abbreviation not in body


@pytest.mark.asyncio
async def test_current_actionable_state_is_visible_in_table_and_card(client: AsyncClient, session: AsyncSession) -> None:
    session.add(_make_file())
    await session.commit()

    body = (await client.get("/pipeline/files", headers={"HX-Request": "true"})).text

    assert body.count("Current state") >= 3
    assert body.count('aria-label="Metadata: not started"') >= 2


@pytest.mark.asyncio
async def test_compact_sort_controls_preserve_filters_and_push_history(client: AsyncClient, session: AsyncSession) -> None:
    session.add(_make_file())
    await session.commit()

    body = (
        await client.get(
            "/pipeline/files?stage=metadata&bucket=not_started&sort=review&order=desc&page_size=10",
            headers={"HX-Request": "true"},
        )
    ).text
    form = body[body.index('aria-label="Sort files"') : body.index("</form>", body.index('aria-label="Sort files"'))]

    assert 'hx-target="#files-table-view"' in form
    assert 'hx-push-url="true"' in form
    assert 'name="stage" value="metadata"' in form
    assert 'name="bucket" value="not_started"' in form
    assert '<option value="review" selected>Review</option>' in form
    assert '<option value="desc" selected>Descending</option>' in form


@pytest.mark.asyncio
async def test_canonical_files_route_serves_live_fragment_and_restorable_filtered_shell(client: AsyncClient, session: AsyncSession) -> None:
    failed = _make_file("/music/failed.mp3")
    healthy = _make_file("/music/healthy.mp3")
    session.add_all([failed, healthy])
    await session.commit()
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    await session.commit()
    url = "/s/files?stage=metadata&bucket=failed&sort=file&order=desc&page_size=10"

    fragment = await client.get(url, headers={"HX-Request": "true", "HX-Target": "files-table-view"})
    assert fragment.status_code == 200
    assert "<html" not in fragment.text.lower()
    assert "/music/failed.mp3" in fragment.text
    assert "/music/healthy.mp3" not in fragment.text
    assert 'hx-get="/s/files' in fragment.text

    restored = await client.get(url, headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert restored.status_code == 200
    assert "<html" in restored.text.lower()
    assert 'aria-label="Pipeline navigation"' in restored.text
    assert "/music/failed.mp3" in restored.text
    assert "/music/healthy.mp3" not in restored.text
    assert '<option value="metadata" selected>' in restored.text
    assert '<option value="failed" selected>' in restored.text


@pytest.mark.asyncio
async def test_canonical_files_route_restores_sorted_page(client: AsyncClient, session: AsyncSession) -> None:
    for index in range(12):
        session.add(_make_file(f"/music/file-{index:02d}.mp3"))
    await session.commit()
    url = "/s/files?page=2&page_size=10&sort=file&order=asc"

    fragment = await client.get(url, headers={"HX-Request": "true", "HX-Target": "files-table-view"})
    restored = await client.get(url, headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})

    for body in (fragment.text, restored.text):
        assert "/music/file-10.mp3" in body
        assert "/music/file-11.mp3" in body
        assert "/music/file-00.mp3" not in body
        assert "Page 2" in body
    assert "<html" not in fragment.text.lower()
    assert "<html" in restored.text.lower()


@pytest.mark.parametrize("file_type", ["txt", "nfo", "jpg", "pdf", "mp3", "mp4"])
async def test_untouched_stage_scope_matches_files_and_record_without_hiding_skips(
    client: AsyncClient, session: AsyncSession, file_type: str
) -> None:
    from bs4 import BeautifulSoup

    from phaze.services.pipeline.files import get_file_stage_buckets, get_files_page

    file = _make_file(f"/synthetic/example.{file_type}")
    file.file_type = file_type
    session.add(file)
    await session.commit()
    buckets = await get_file_stage_buckets(session, file.id)
    assert set(buckets.values()) == {"not_started"}
    row = next(row for row in (await get_files_page(session)).rows if row.file.id == file.id)
    scoped = file_type not in ("mp3", "mp4")
    assert row.stage_history == dict.fromkeys(("metadata", "analyze", "tracklist"), False)
    assert row.stage_applicability == {
        "metadata": not scoped,
        "analyze": not scoped,
        "tracklist": not scoped,
        "propose": True,
        "review": True,
        "apply": True,
    }
    files = BeautifulSoup((await client.get("/pipeline/files", headers={"HX-Request": "true"})).text, "html.parser")
    record = BeautifulSoup((await client.get(f"/record/{file.id}")).text, "html.parser")
    for stage, label in [("metadata", "Metadata"), ("analyze", "Analyze"), ("propose", "Propose"), ("review", "Review"), ("apply", "Execute")]:
        suffix = "not applicable" if scoped and stage in ("metadata", "analyze") else "not started"
        for prefix, glyph in [("files", True), ("files-mobile", False)]:
            pill = files.select_one(f"#{prefix}-stage-pill-{stage}-{file.id} [aria-label]")
            assert pill["aria-label"] == f"{label}: {suffix}"
            assert (pill.get("data-status-variant") == "glyph") is glyph
        assert record.select_one(f"#stage-pill-{stage}-{file.id} [aria-label]")["aria-label"] == f"{label}: {suffix}"
    assert (
        record.select_one(f"#stage-pill-tracklist-{file.id} [aria-label]")["aria-label"]
        == f"Tracklist: {'not applicable' if scoped else 'no stored tracklist'}"
    )
    for stage in ("metadata", "analyze"):
        assert record.select_one(f'form[hx-post="/pipeline/files/{file.id}/skip/{stage}"]') is not None
    summary = files.select_one(f"#files-current-status-{file.id}").get_text(" ", strip=True)
    assert "complete" not in summary.lower()
    assert ("workflow awaits prerequisites" in summary) is scoped
    assert (await get_file_stage_buckets(session, file.id)) == buckets


@pytest.mark.parametrize("history_kind", ["partial_result", "window", "stored_tracklist"])
async def test_nonmedia_output_history_is_not_erased_by_not_started(client: AsyncClient, session: AsyncSession, history_kind: str) -> None:
    from bs4 import BeautifulSoup

    from phaze.models.analysis import AnalysisResult, AnalysisWindow
    from phaze.models.tracklist import Tracklist
    from phaze.services.pipeline.files import get_file_stage_buckets, get_files_page

    file = _make_file("/synthetic/history.txt")
    file.file_type = "txt"
    session.add(file)
    await session.commit()
    if history_kind == "partial_result":
        session.add(AnalysisResult(file_id=file.id, bpm=120))
    elif history_kind == "window":
        session.add(AnalysisWindow(file_id=file.id, tier="fine", window_index=0, start_sec=0, end_sec=60))
    else:
        session.add(Tracklist(file_id=file.id, external_id=uuid.uuid4().hex, source_url="https://example.invalid/source"))
    await session.commit()
    history = {}
    buckets = await get_file_stage_buckets(session, file.id, history=history)
    key = "tracklist" if history_kind == "stored_tracklist" else "analyze"
    row = next(row for row in (await get_files_page(session)).rows if row.file.id == file.id)
    assert history == row.stage_history
    assert history[key] is True and row.stage_applicability[key] is True
    files = BeautifulSoup((await client.get("/pipeline/files", headers={"HX-Request": "true"})).text, "html.parser")
    record = BeautifulSoup((await client.get(f"/record/{file.id}")).text, "html.parser")
    label = "Tracklist" if key == "tracklist" else "Analyze"
    pill = record.select_one(f"#stage-pill-{key}-{file.id} [aria-label]")
    assert "not applicable" not in pill["aria-label"]
    if key == "analyze":
        assert buckets[key] == "not_started"
        assert files.select_one(f"#files-stage-pill-{key}-{file.id} [aria-label]")["aria-label"] == f"{label}: not started"


async def test_reasoned_nonmedia_dual_skip_preserves_real_proposal_candidate(client: AsyncClient, session: AsyncSession) -> None:
    from bs4 import BeautifulSoup
    from sqlalchemy import select

    from phaze.services.pipeline.files import get_file_stage_buckets
    from phaze.services.pipeline.proposals import _proposal_pending_clauses

    file = _make_file("/synthetic/skip.nfo")
    file.file_type = "nfo"
    session.add(file)
    await session.commit()

    async def candidate() -> bool:
        return (
            await session.execute(select(FileRecord.id).where(FileRecord.id == file.id, *_proposal_pending_clauses()))
        ).scalar_one_or_none() == file.id

    assert not await candidate()
    blank = await client.post(f"/pipeline/files/{file.id}/skip/metadata", data={"reason": "   "})
    assert blank.status_code == 422 and not await candidate()
    for stage in ("metadata", "analyze"):
        response = await client.post(f"/pipeline/files/{file.id}/skip/{stage}", data={"reason": "Synthetic source does not require enrichment"})
        assert response.status_code == 200
        pushed = BeautifulSoup(response.text, "html.parser").select_one(f"#stage-pill-{stage}-{file.id} [aria-label]")
        assert "skipped" in pushed["aria-label"] and "not applicable" not in pushed["aria-label"]
        if stage == "metadata":
            assert not await candidate()
    assert await candidate()
    buckets = await get_file_stage_buckets(session, file.id)
    assert buckets["metadata"] == buckets["analyze"] == "skipped"
    files = BeautifulSoup((await client.get("/pipeline/files", headers={"HX-Request": "true"})).text, "html.parser")
    assert files.select_one(f"#files-current-status-{file.id}").get_text(strip=True) == "Awaiting proposed changes"
    assert (await client.post(f"/pipeline/files/{file.id}/skip/propose", data={"reason": "Not permitted"})).status_code == 422


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("stage", ["metadata", "analyze"])
async def test_nonmedia_retry_oob_preserves_history_missing_and_fresh_summary(
    client: AsyncClient, session: AsyncSession, stage: str, missing: bool
) -> None:
    from bs4 import BeautifulSoup

    from phaze.models.analysis import AnalysisResult
    from tests._queue_fakes import install_fake_queues, make_agent_live

    file = _make_file("/synthetic/retry.txt")
    file.file_type = "txt"
    file.missing_at = datetime.now(UTC) if missing else None
    session.add(file)
    await session.commit()
    output = (
        FileMetadata(file_id=file.id, failed_at=datetime.now(UTC))
        if stage == "metadata"
        else AnalysisResult(file_id=file.id, failed_at=datetime.now(UTC))
    )
    session.add(output)
    await session.commit()
    await make_agent_live(session)
    install_fake_queues(client)
    endpoint = "metadata-failed" if stage == "metadata" else "analysis-failed"
    response = await client.post(f"/pipeline/files/{file.id}/{endpoint}/retry")
    assert response.status_code == 200
    fresh = BeautifulSoup((await client.get("/pipeline/files", headers={"HX-Request": "true"})).text, "html.parser")
    pushed = BeautifulSoup(response.text, "html.parser")
    for prefix in ("files", "files-mobile"):
        token_id = f"{prefix}-stage-pill-{stage}-{file.id}"
        assert str(fresh.select_one(f"#{token_id} [aria-label]")) == str(pushed.select_one(f"#{token_id} [aria-label]"))
        assert "not applicable" not in pushed.select_one(f"#{token_id} [aria-label]")["aria-label"]
        summary_id = f"{prefix}-current-status-{file.id}"
        assert fresh.select_one(f"#{summary_id}").get_text(" ", strip=True) == pushed.select_one(f"#{summary_id}").get_text(" ", strip=True)
        summary = pushed.select_one(f"#{summary_id}").get_text(" ", strip=True)
        assert ("missing" in summary) is missing
        assert f"{stage.capitalize()}: failed" not in summary


@pytest.mark.parametrize("proposal_status", ["pending", "approved", "executed", "failed"])
async def test_nonmedia_workflow_history_never_means_type_denial_or_inferred_approval(
    client: AsyncClient, session: AsyncSession, proposal_status: str
) -> None:
    from bs4 import BeautifulSoup

    from phaze.models.execution import ExecutionLog
    from phaze.models.proposal import RenameProposal
    from phaze.services.pipeline.files import get_file_stage_buckets

    file = _make_file("/synthetic/proposed.txt")
    file.file_type = "txt"
    session.add(file)
    await session.commit()
    proposal = RenameProposal(file_id=file.id, proposed_filename="organized.txt", confidence=95, status=proposal_status)
    session.add(proposal)
    await session.commit()
    if proposal_status in ("executed", "failed"):
        session.add(
            ExecutionLog(
                proposal_id=proposal.id,
                operation="rename",
                source_path=file.current_path,
                destination_path="/synthetic/organized.txt",
                sha256_verified=True,
                status="completed" if proposal_status == "executed" else "failed",
            )
        )
        await session.commit()
    buckets = await get_file_stage_buckets(session, file.id)
    assert buckets["propose"] == buckets["review"] == "done"
    body = BeautifulSoup((await client.get("/pipeline/files", headers={"HX-Request": "true"})).text, "html.parser")
    for prefix in ("files", "files-mobile"):
        assert body.select_one(f"#{prefix}-stage-pill-review-{file.id} [aria-label]")["aria-label"] == "Review: done"
        summary = body.select_one(f"#{prefix}-current-status-{file.id}").get_text(" ", strip=True)
        assert "approved" not in summary.lower()
        if proposal_status in ("pending", "approved"):
            assert body.select_one(f"#{prefix}-stage-pill-apply-{file.id} [aria-label]")["aria-label"] == "Execute: not started"
            assert summary == "Check Changes Review and execution readiness"
        elif proposal_status == "failed":
            assert summary == "Inspect Execute error and recovery options"
            assert body.select_one(f"#{prefix}-stage-pill-apply-{file.id} [aria-label]")["aria-label"] == "Execute: failed"
        else:
            assert body.select_one(f"#{prefix}-stage-pill-apply-{file.id} [aria-label]")["aria-label"] == "Execute: done"
