"""phaze-227l5: a row with ``files.missing_at`` is its own state ("missing"), never counted or listed as "failed".

Every row here carries BOTH a terminal enrich failure and ``missing_at`` -- the shape the 2026-10-10
production archive held (44 rows). Each surface that used to call it a failure is asserted to call it
missing instead: the stage buckets / Summary + Metadata counts, the Files ``failed`` filter (and the new
``missing`` one), the record page stage pill, the Analyze listing, and the analysis-failed count.
The control row (failed, NOT missing) pins that real failures are untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.routers.shell.summary import _summary_stage_status
from phaze.services.pipeline import (
    get_analysis_failed_count,
    get_analysis_failed_files,
    get_analyze_files_page,
    get_file_stage_buckets,
    get_files_page,
    get_metadata_status_snapshot,
    get_stage_progress,
)
from phaze.services.stage_status import MISSING_BUCKET, counted_failed_clause, missing_clause


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _file(name: str, *, missing: bool) -> FileRecord:
    uid = uuid.uuid4()
    path = f"/music/{name}-{uid.hex}.mp3"
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex * 2,
        original_path=path,
        original_filename=path.rsplit("/", 1)[-1],
        current_path=path,
        file_type="mp3",
        file_size=1000,
        missing_at=datetime(2026, 10, 8, tzinfo=UTC) if missing else None,
    )


async def _seed(session: AsyncSession) -> tuple[FileRecord, FileRecord]:
    """One file that failed both enrich stages and is missing, and one that failed both and is present."""
    gone, here = _file("gone", missing=True), _file("here", missing=False)
    session.add_all([gone, here])
    await session.flush()
    now = datetime.now(UTC)
    for f in (gone, here):
        session.add_all([FileMetadata(file_id=f.id, failed_at=now), AnalysisResult(file_id=f.id, failed_at=now)])
    await session.commit()
    return gone, here


async def test_the_shared_predicates_split_failed_from_missing(session: AsyncSession) -> None:
    gone, here = await _seed(session)
    failed = set((await session.execute(select(FileRecord.id).where(counted_failed_clause(Stage.METADATA)))).scalars())
    missing = set((await session.execute(select(FileRecord.id).where(missing_clause()))).scalars())
    assert gone.id not in failed
    assert here.id in failed
    assert gone.id in missing
    assert here.id not in missing


async def test_stage_buckets_count_the_missing_row_apart_from_failed(session: AsyncSession) -> None:
    await _seed(session)
    progress = await get_stage_progress(session)
    for node in ("metadata", "analyze"):
        assert progress[node]["failed"] == 1, f"{node}: only the present row is a failure"
        assert progress[node][MISSING_BUCKET] == 1, f"{node}: the missing row is counted as missing"
        assert (
            progress[node]["failed"]
            + progress[node][MISSING_BUCKET]
            + sum(int(progress[node][k] or 0) for k in ("not_started", "in_flight", "done", "skipped", "orphaned"))
            == progress[node]["total"]
        )
    snapshot = await get_metadata_status_snapshot(session)
    assert (snapshot.failed, snapshot.missing) == (1, 1)


async def test_summary_flow_card_shows_a_missing_badge_not_a_failed_one(session: AsyncSession) -> None:
    await _seed(session)
    progress = await get_stage_progress(session)
    status = _summary_stage_status(progress["metadata"])
    assert status["failed"] == 1
    assert status["missing"] == 1


async def test_the_files_failed_filter_excludes_missing_and_the_missing_filter_lists_it(session: AsyncSession) -> None:
    gone, here = await _seed(session)
    failed_page = await get_files_page(session, stage=Stage.METADATA, bucket="failed")
    assert [r.file.id for r in failed_page.rows] == [here.id]
    any_failed = await get_files_page(session, bucket="failed")
    assert [r.file.id for r in any_failed.rows] == [here.id]
    missing_page = await get_files_page(session, stage=Stage.METADATA, bucket=MISSING_BUCKET)
    assert [r.file.id for r in missing_page.rows] == [gone.id]
    assert missing_page.rows[0].buckets["metadata"] == MISSING_BUCKET


async def test_the_record_page_stage_buckets_and_pill_say_missing(client: AsyncClient, session: AsyncSession) -> None:
    gone, here = await _seed(session)
    assert (await get_file_stage_buckets(session, gone.id))["metadata"] == MISSING_BUCKET
    assert (await get_file_stage_buckets(session, here.id))["metadata"] == "failed"

    body = (await client.get(f"/record/{gone.id}", headers={"HX-Request": "true"})).text
    assert re.search(r"<span>missing</span>", body), "the record pane renders a 'missing' stage pill"
    assert "<span>failed</span>" not in body


async def test_files_filter_bar_offers_the_missing_option(client: AsyncClient, session: AsyncSession) -> None:
    gone, _ = await _seed(session)
    body = (await client.get("/pipeline/files?stage=metadata&bucket=missing", headers={"HX-Request": "true"})).text
    assert '<option value="missing" selected' in body
    assert str(gone.id) in body


async def test_analysis_failure_counts_and_listing_exclude_missing(session: AsyncSession) -> None:
    gone, here = await _seed(session)
    assert await get_analysis_failed_count(session) == 1
    assert [f.id for f in await get_analysis_failed_files(session)] == [here.id]

    failed = await get_analyze_files_page(session, status="failed")
    assert [r["file_id"] for r in failed.rows] == [str(here.id)]
    missing = await get_analyze_files_page(session, status="missing")
    assert [r["file_id"] for r in missing.rows] == [str(gone.id)]
    assert missing.rows[0]["analysis_missing"] is True
    assert missing.rows[0]["analysis_failed"] is False
