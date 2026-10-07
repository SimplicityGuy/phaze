"""Cross-plane proof that a scan's media-less COMPANIONS are file rows, not diagnostics (phaze-gafl9).

Until phaze-gafl9 the scan withheld an approved companion with no media beside it and POSTed its path
to the orphan-companion diagnostic inventory, which the scans page surfaced. Operator decision 6 (2026-10-07,
"Yes, include them (Recommended)"; epic phaze-4x319) admits every approved companion, so the scan reports nothing there any more and the scans
page reads nothing from it. The fixture tree is the only source of file data in this module: the real
scanner and authenticated HTTP routes create every row that reaches the operator surface.
"""

from __future__ import annotations

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


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _fixture_tree(root: Path) -> tuple[list[Path], list[Path], Path]:
    """Create normal ingest inputs, a folder of media-less companions, and an exclusion."""
    mixed = root / "mixed"
    media_only = root / "media-only"
    orphaned = root / "orphaned"
    mixed.mkdir()
    media_only.mkdir()
    orphaned.mkdir()

    ingestible = [
        _write(mixed / "song.mp3", b"synthetic music"),
        _write(mixed / "song.cue", b'TITLE "Synthetic Set"\nFILE "song.mp3" MP3\n  TRACK 01 AUDIO\n    TITLE "Opening"\n'),
        _write(media_only / "set.mp4", b"synthetic video"),
    ]
    orphaned_companions = [_write(orphaned / f"orphan-{index:03}.nfo", f"synthetic notes {index}".encode()) for index in range(52)]
    orphaned_companions.extend(
        [
            _write(orphaned / "playlist.m3u", b"#EXTM3U"),
            _write(orphaned / "notes.txt", b"synthetic notes"),
        ]
    )
    excluded = _write(orphaned / "cover.jpg", b"synthetic image")
    return ingestible, orphaned_companions, excluded


async def test_media_less_companions_become_file_rows_and_the_scans_page_shows_no_orphans(
    tmp_path: Path,
    client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """Real disk inputs: every approved companion is a file row, nothing is a diagnostic, nothing is enqueued."""
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
    agent_api = PhazeAgentClient(base_url=str(client.base_url), token=raw_token, _client=client)

    scan_result = await scan_directory(
        {"api_client": agent_api},
        scan_path=str(tmp_path),
        batch_id=str(batch.id),
        agent_id=agent.id,
    )

    assert scan_result == {"status": "completed", "files_posted": 57}
    records = (await session.execute(select(FileRecord))).scalars().all()
    record_paths = {record.original_path for record in records}
    assert record_paths == {str(path) for path in ingestible + orphaned_companions}
    assert str(excluded) not in record_paths
    assert (await session.execute(select(func.count()).select_from(OrphanCompanionDiagnostic))).scalar_one() == 0

    # The linking chain (phaze-rmhfr) links song.cue by its FILE reference. The former orphans name no
    # media and have none beside them, so they stay unlinked file rows (their few bytes are also junk).
    assert (await associate_companions(session)).links_created == 1
    link = (await session.execute(select(FileCompanion))).scalar_one()
    linked_companion = await session.get(FileRecord, link.companion_id)
    assert linked_companion is not None
    assert linked_companion.original_path == str(tmp_path / "mixed" / "song.cue")

    # Admitting companions enqueues nothing: they are never metadata, analysis or proposal work.
    assert controller_queue.captured == []
    assert task_router.captures == []
    assert (await session.execute(select(func.count()).select_from(FileMetadata))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(AnalysisResult))).scalar_one() == 0
    assert (await session.execute(select(func.count()).select_from(RenameProposal))).scalar_one() == 0

    recent = await client.get("/pipeline/scans/recent")
    assert recent.status_code == 200
    assert "57" in recent.text  # Positive control: this scan's row rendered with its file count.
    assert "orphan companion" not in recent.text
    assert (await client.get(f"/pipeline/scans/{batch.id}/orphan-companions")).status_code == 404


async def test_diagnostics_left_by_an_older_agent_never_reach_the_scans_page(
    tmp_path: Path,
    client: AsyncClient,
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
) -> None:
    """An agent still on a pre-phaze-gafl9 image may POST diagnostics; they persist but are never surfaced.

    The agent endpoint is kept until the table is dropped so such an agent's scan still completes (a 404
    there would abort it after every file was already upserted). Its rows go in through that real route.
    """
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
    client.headers["Authorization"] = f"Bearer {raw_token}"

    posted = await client.post(
        f"/api/internal/agent/scan-batches/{batch.id}/orphan-companions",
        json={"diagnostics": [{"normalized_path": str(tmp_path / "orphaned" / "notes.nfo"), "companion_extension": ".nfo"}]},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["inserted"] == 1
    assert (await session.execute(select(func.count()).select_from(OrphanCompanionDiagnostic))).scalar_one() == 1

    recent = await client.get("/pipeline/scans/recent")
    assert recent.status_code == 200
    assert str(tmp_path) in recent.text  # Positive control: the batch holding the diagnostic rendered.
    assert "orphan companion" not in recent.text
    assert "orphan-companions" not in recent.text

    batch.status = ScanStatus.COMPLETED.value  # Only a terminal scan is deletable.
    await session.commit()
    deleted_scan = await client.delete(f"/pipeline/scans/{batch.id}", params={"poll": "0"})
    assert deleted_scan.status_code == 200
    # Deleting the scan still cascades its leftover diagnostics.
    assert (await session.execute(select(func.count()).select_from(OrphanCompanionDiagnostic))).scalar_one() == 0
