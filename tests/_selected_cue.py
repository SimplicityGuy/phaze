"""A reviewed, selected CUE source: the only fixture that can carry exact recording offsets.

Legacy ``tracklist_tracks`` timing is display-only since ADR-0027 (retire legacy timestamp qualification)
(docs/design/0027-retire-legacy-timestamp-qualification.md), so a test that needs real track
boundaries -- analysis segments, timeline ticks, CUE ``INDEX`` lines -- goes through the real
capture -> import -> decide path instead of hand-written evidence the product never writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
import uuid

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.schemas.agent_companion_capture import CaptureReport, CaptureTarget
from phaze.schemas.local_source_import import ImportLocalSource, SourceDecision
from phaze.services.companion_capture import store_capture_report
from phaze.services.local_source_import import decide_local_source, import_local_source
from phaze.tracklist_providers.domain import SourceRead


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def cue_sheet(*tracks: tuple[str | None, str, str | None], performer: str = "Synthetic Set") -> str:
    """One-FILE CUE text from ``(artist, title, "MM:SS:FF" or None)`` rows; ``None`` omits INDEX 01."""
    lines = [f'PERFORMER "{performer}"', 'TITLE "Synthetic Live Set"', 'FILE "recording.mp3" MP3']
    for position, (artist, title, index) in enumerate(tracks, start=1):
        lines.append(f" TRACK {position:02d} AUDIO")
        if artist is not None:
            lines.append(f'  PERFORMER "{artist}"')
        lines.append(f'  TITLE "{title}"')
        if index is not None:
            lines.append(f"  INDEX 01 {index}")
    return "\n".join(lines) + "\n"


async def select_cue(session: AsyncSession, media: FileRecord, text: str) -> None:
    """Capture ``text`` as ``media``'s companion CUE, import it, and select its FILE 1.

    ``media`` must already be flushed with a ``sha256_hash`` and ``agent_id``: the selection binds
    both content hashes, and the companion lives on the same agent.
    """
    directory = PurePosixPath(media.current_path).parent
    companion = FileRecord(
        id=uuid.uuid4(),
        agent_id=media.agent_id,
        original_path=str(directory / f"{media.id}.cue"),
        current_path=str(directory / f"{media.id}.cue"),
        original_filename=f"{media.id}.cue",
        file_type="cue",
        sha256_hash=hashlib.sha256(text.encode()).hexdigest(),
        file_size=len(text.encode()),
    )
    session.add(companion)
    await session.flush()
    session.add(FileCompanion(companion_id=companion.id, media_id=media.id))
    await session.flush()
    report = CaptureReport(
        target=CaptureTarget(
            file_id=companion.id,
            media_id=media.id,
            path=companion.current_path,
            expected_sha256=companion.sha256_hash,
            expected_size=companion.file_size,
        ),
        read=SourceRead(
            status="found",
            code="read",
            scope=str(companion.id),
            text=text,
            encoding="utf-8",
            revision=companion.sha256_hash,
            revision_scope="full",
            bytes_read=companion.file_size,
            evidence=("revision:sha256:full",),
            retrieved_at=datetime.now(UTC),
        ),
    )
    capture = await store_capture_report(session, media.agent_id, report)
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=capture.observation_id))
    assert imported.tracklist_observation_id is not None
    await decide_local_source(
        session,
        SourceDecision(
            media_id=media.id,
            observation_id=imported.tracklist_observation_id,
            kind="tracklist",
            decision_id=uuid.uuid4(),
            expected_selection_token=None,
            expected_revision=companion.sha256_hash,
            expected_source_sha256=companion.sha256_hash,
            expected_media_sha256=media.sha256_hash,
            cue_file_ordinal=1,
            action="select",
        ),
        actor="reviewer",
    )
    await session.flush()


async def selected_cue_media(session: AsyncSession, text: str, *, filename: str = "recording.mp3", agent_id: str = "test-fileserver") -> FileRecord:
    """A fresh mp3 media row with ``text`` selected as its reviewed CUE source."""
    media = FileRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        original_path=f"/synthetic/{filename}",
        current_path=f"/synthetic/{filename}",
        original_filename=filename,
        file_type="mp3",
        sha256_hash=hashlib.sha256(f"media:{uuid.uuid4()}".encode()).hexdigest(),
        file_size=1024,
    )
    session.add(media)
    await session.flush()
    await select_cue(session, media, text)
    return media
