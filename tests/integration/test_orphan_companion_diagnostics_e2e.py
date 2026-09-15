"""Cross-plane proof for scan-skipped orphan COMPANION diagnostics.

The fixture tree is the only source of file and diagnostic data in this module. The
core proof deliberately does not construct ``FileRecord``, ``FileCompanion``, or
``OrphanCompanionDiagnostic`` rows: the real scanner and authenticated HTTP routes
must create every row that reaches the operator surface.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import func, select

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.metadata import FileMetadata
from phaze.models.orphan_companion_diagnostic import OrphanCompanionDiagnostic
from phaze.models.proposal import RenameProposal
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.companion import associate_companions
from phaze.tasks.scan import scan_directory
from tests._queue_fakes import install_fake_queues


if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent
    from phaze.schemas.agent_orphan_companions import OrphanCompanionChunk, OrphanCompanionChunkResponse


class _ReplayOrphanChunksClient(PhazeAgentClient):
    """Replay each real diagnostic POST once while its scan is still RUNNING."""

    def __init__(self, *, base_url: str, token: str, client: AsyncClient) -> None:
        super().__init__(base_url=base_url, token=token, _client=client)
        self.chunk_sizes: list[int] = []
        self.responses: list[tuple[OrphanCompanionChunkResponse, OrphanCompanionChunkResponse]] = []

    async def post_orphan_companions(
        self,
        batch_id: uuid.UUID,
        payload: OrphanCompanionChunk,
    ) -> OrphanCompanionChunkResponse:
        self.chunk_sizes.append(len(payload.diagnostics))
        first = await super().post_orphan_companions(batch_id, payload)
        replay = await super().post_orphan_companions(batch_id, payload)
        self.responses.append((first, replay))
        return first


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _fixture_tree(root: Path) -> tuple[list[Path], list[Path], Path]:
    """Create normal ingest inputs, a paged orphan inventory, and an exclusion."""
    mixed = root / "mixed"
    media_only = root / "media-only"
    orphaned = root / "orphaned"
    mixed.mkdir()
    media_only.mkdir()
    orphaned.mkdir()

    ingestible = [
        _write(mixed / "song.mp3", b"synthetic music"),
        _write(mixed / "song.cue", b'TITLE "Synthetic"'),
        _write(media_only / "set.mp4", b"synthetic video"),
    ]
    orphaned_companions = [_write(orphaned / f"orphan-{index:03}.nfo", b"synthetic notes") for index in range(52)]
    orphaned_companions.extend(
        [
            _write(orphaned / "playlist.m3u", b"#EXTM3U"),
            _write(orphaned / "notes.txt", b"synthetic notes"),
        ]
    )
    excluded = _write(orphaned / "cover.jpg", b"synthetic image")
    return ingestible, orphaned_companions, excluded


async def test_orphan_companion_diagnostics_reach_bounded_operator_surface_without_ingestion(
    tmp_path: Path,
    client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """Real disk inputs reach persistence and UI while orphan files remain inventory-only."""
    ingestible, orphaned_companions, excluded = _fixture_tree(tmp_path)
    agent, raw_token = seed_test_agent
    batch = ScanBatch(
        id=uuid.uuid4(),
        agent_id=agent.id,
        scan_path=str(tmp_path),
        configured_root=str(tmp_path),
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
    )
    session.add(batch)
    await session.commit()

    controller_queue, task_router = install_fake_queues(client)
    client.headers["Authorization"] = f"Bearer {raw_token}"
    agent_api = _ReplayOrphanChunksClient(base_url=str(client.base_url), token=raw_token, client=client)

    scan_result = await scan_directory(
        {"api_client": agent_api},
        scan_path=str(tmp_path),
        batch_id=str(batch.id),
        agent_id=agent.id,
    )

    assert scan_result == {"status": "completed", "files_posted": 3}
    assert agent_api.chunk_sizes == [54]
    assert len(agent_api.responses) == 1
    first, replay = agent_api.responses[0]
    assert (first.inserted, first.existing) == (54, 0)
    assert (replay.inserted, replay.existing) == (0, 54)

    records = (await session.execute(select(FileRecord).order_by(FileRecord.original_path))).scalars().all()
    record_paths = {record.original_path for record in records}
    assert record_paths == {str(path) for path in ingestible}
    assert record_paths.isdisjoint(str(path) for path in orphaned_companions)
    assert str(excluded) not in record_paths

    diagnostics = (await session.execute(select(OrphanCompanionDiagnostic).order_by(OrphanCompanionDiagnostic.normalized_path))).scalars().all()
    diagnostic_paths = {diagnostic.normalized_path for diagnostic in diagnostics}
    assert diagnostic_paths == {str(path) for path in orphaned_companions}
    assert str(excluded) not in diagnostic_paths
    assert {diagnostic.configured_root for diagnostic in diagnostics} == {str(tmp_path)}

    assert await associate_companions(session) == 1
    link = (await session.execute(select(FileCompanion))).scalar_one()
    linked_companion = await session.get(FileRecord, link.companion_id)
    assert linked_companion is not None
    assert linked_companion.original_path == str(tmp_path / "mixed" / "song.cue")
    assert linked_companion.original_path not in diagnostic_paths

    assert controller_queue.captured == []
    assert task_router.captures == []
    assert (await session.execute(select(func.count()).select_from(FileMetadata))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(AnalysisResult))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(RenameProposal))).scalar_one() == 0

    recent = await client.get("/pipeline/scans/recent")
    assert recent.status_code == 200
    assert "54 orphan companions" in recent.text
    assert f"/pipeline/scans/{batch.id}/orphan-companions" in recent.text

    detail = await client.get(
        f"/pipeline/scans/{batch.id}/orphan-companions",
        params={"root": str(tmp_path), "type": ".nfo", "page": "2"},
    )
    assert detail.status_code == 200, detail.text
    assert "54 accepted companion files" in detail.text
    assert 'data-root-count="54"' in detail.text
    assert ".m3u (1)" in detail.text
    assert ".nfo (52)" in detail.text
    assert ".txt (1)" in detail.text
    assert "Showing 51-52 of 52 · Page 2" in detail.text
    assert str(tmp_path / "orphaned" / "orphan-050.nfo") in detail.text
    assert str(tmp_path / "orphaned" / "orphan-051.nfo") in detail.text
    assert str(tmp_path / "orphaned" / "orphan-049.nfo") not in detail.text

    download = await client.get(
        f"/pipeline/scans/{batch.id}/orphan-companions/download",
        params={"root": str(tmp_path), "type": ".nfo"},
    )
    assert download.status_code == 200
    csv_rows = list(csv.reader(io.StringIO(download.text)))
    assert csv_rows[0] == ["batch_id", "configured_root", "companion_extension", "normalized_path"]
    assert len(csv_rows) == 53
    assert [row[3] for row in csv_rows[1:]] == [str(tmp_path / "orphaned" / f"orphan-{index:03}.nfo") for index in range(52)]

    deleted = await client.delete(f"/pipeline/scans/{batch.id}", params={"poll": "0"})
    assert deleted.status_code == 200
    assert (await session.execute(select(func.count()).select_from(ScanBatch).where(ScanBatch.id == batch.id))).scalar_one() == 0
    assert (
        await session.execute(select(func.count()).select_from(OrphanCompanionDiagnostic).where(OrphanCompanionDiagnostic.batch_id == batch.id))
    ).scalar_one() == 0
