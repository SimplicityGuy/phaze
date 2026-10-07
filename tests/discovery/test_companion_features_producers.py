"""Real-producer guard: the scan and the watcher report companion content features at ingest (phaze-osy6j).

Every companion starts as synthetic bytes in ``tmp_path`` and reaches ``companion_content_features``
through the production agent client and the authenticated HTTP routes -- the scan's
``scan_directory`` and the watcher's ``Poster.post_one``. No ``FileRecord`` or features row is
constructed here: the gap this guards is a producer that never reports, which a seeded row would hide.
"""

from __future__ import annotations

import codecs
import hashlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from structlog.testing import capture_logs

from phaze.agent_watcher.poster import Poster
from phaze.config import AgentSettings
from phaze.database import get_session
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.routers import agent_files, agent_scan_batches
from phaze.schemas.agent_tasks import CompanionFeaturesTarget, ExtractCompanionFeaturesPayload
from phaze.services.agent_client import PhazeAgentClient
from phaze.services.companion_content import count_backfill, select_backfill_page
from phaze.services.companion_features import read_companion
from phaze.tasks.companion_features import extract_companion_features
from phaze.tasks.scan import scan_directory


if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.agent import Agent


_ART = bytes([0xDB, 0xDC, 0xDF, 0xB0, 0xB1, 0xB2, 0xC4, 0xCD]) * 6
_STAMP = b"Downloaded from www.example-release-site.test\r\nVisit us for more free sets!\r\n"


def _release(root: Path) -> dict[str, Path]:
    """One release folder: a recording and one companion of every type, all beside it."""
    folder = root / "Example Artist - Live @ Example Festival - 2024-04-12"
    folder.mkdir(parents=True)
    files = {
        "media": folder / "Example Artist - Live @ Example Festival - 2024-04-12.mp3",
        "cue": folder / "Example Artist - Live @ Example Festival - 2024-04-12.cue",
        "m3u": folder / "00-example_artist-live-2024.m3u",
        "pls": folder / "set.pls",
        "nfo": folder / "00-example_artist-live-2024.nfo",
        "txt": folder / "Tracklist.txt",
        "stamp": folder / "site.nfo",
        "nul": folder / "broken.nfo",
    }
    files["media"].write_bytes(b"audio-bytes")
    files["cue"].write_bytes(
        b'FILE "Example Artist - Live @ Example Festival - 2024-04-12.mp3" MP3\r\n'
        b'  TRACK 01 AUDIO\r\n    TITLE "First"\r\n    INDEX 01 00:00:00\r\n'
        b'  TRACK 02 AUDIO\r\n    TITLE "Second"\r\n    INDEX 01 04:00:00\r\n'
    )
    files["m3u"].write_bytes(b"#EXTM3U\n" + files["media"].name.encode() + b"\n")
    files["pls"].write_bytes(b"[playlist]\nFile1=" + files["media"].name.encode() + b"\nNumberOfEntries=1\n")
    files["nfo"].write_bytes(_ART + b"\r\n\xba Artist ....: Example Artist\r\n\xba Genre .....: Trance\r\n\xba Source ....: FM\r\n" + _ART + b"\r\n")
    files["txt"].write_bytes(
        codecs.BOM_UTF16_LE + "01. Example Artist - First\r\n02. Other Artist - Second\r\n03. Third Artist - Third\r\n".encode("utf-16-le")
    )
    files["stamp"].write_bytes(_STAMP)
    files["nul"].write_bytes(b"\x00" * 256)
    return files


def _agent_api(client: AsyncClient) -> PhazeAgentClient:
    return PhazeAgentClient(base_url=str(client.base_url), token="unused-test-token", _client=client)  # noqa: S106 -- the client carries the fixture token


async def _features_by_name(session: AsyncSession) -> dict[str, CompanionContentFeatures]:
    rows = await session.execute(
        select(FileRecord.original_filename, CompanionContentFeatures).join(
            CompanionContentFeatures, CompanionContentFeatures.file_id == FileRecord.id
        )
    )
    return {row.original_filename: row.CompanionContentFeatures for row in rows}


def _assert_release_features(files: dict[str, Path], by_name: dict[str, CompanionContentFeatures]) -> None:
    media = files["media"].name
    assert set(by_name) == {path.name for key, path in files.items() if key != "media"}
    cue, m3u, pls, nfo, txt = (by_name[files[key].name] for key in ("cue", "m3u", "pls", "nfo", "txt"))
    assert cue.media_references == [{"name": media, "source": "cue_file"}]
    assert cue.is_tracklist is True
    assert m3u.media_references == [{"name": media, "source": "m3u"}]
    assert m3u.is_tracklist is False
    assert pls.media_references == [{"name": media, "source": "pls"}]
    assert nfo.encoding == "cp437"
    assert nfo.junk_class is None
    assert (txt.encoding, txt.is_tracklist) == ("utf-16", True)
    assert by_name[files["stamp"].name].junk_class == "site_ad"
    assert (by_name[files["nul"].name].encoding, by_name[files["nul"].name].junk_class) == ("all-nul", "all_nul")
    for key in ("cue", "m3u", "pls", "nfo", "txt", "stamp", "nul"):
        row = by_name[files[key].name]
        assert row.fingerprint == hashlib.sha256(files[key].read_bytes()).hexdigest()
        assert (row.folder_media, row.folder_media_count) == ([media], 1)


async def test_scan_directory_reports_features_for_every_companion_it_ingests(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    files = _release(tmp_path)
    agent, _token = seed_test_agent
    batch = ScanBatch(agent_id=agent.id, scan_path=str(tmp_path), status=ScanStatus.RUNNING.value, total_files=0, processed_files=0)
    session.add(batch)
    await session.commit()

    result = await scan_directory(
        {"api_client": _agent_api(authenticated_client)}, scan_path=str(tmp_path), batch_id=str(batch.id), agent_id=agent.id
    )

    assert result == {"status": "completed", "files_posted": len(files)}
    _assert_release_features(files, await _features_by_name(session))


async def test_the_watcher_reports_features_for_a_settled_companion(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    files = _release(tmp_path)
    agent, _token = seed_test_agent
    session.add(ScanBatch(agent_id=agent.id, scan_path="<watcher>", status=ScanStatus.LIVE.value, total_files=0, processed_files=0))
    await session.commit()
    poster = Poster(client=_agent_api(authenticated_client), agent_id=agent.id)

    for path in files.values():
        await poster.post_one(str(path))

    _assert_release_features(files, await _features_by_name(session))


async def test_an_older_control_plane_without_the_route_still_completes_the_scan(
    tmp_path: Path,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Agent and control plane deploy separately: a 404 on the features route costs the ingest nothing."""
    files = _release(tmp_path)
    agent, raw_token = seed_test_agent
    batch = ScanBatch(agent_id=agent.id, scan_path=str(tmp_path), status=ScanStatus.RUNNING.value, total_files=0, processed_files=0)
    session.add(batch)
    await session.commit()
    old_app = FastAPI()
    old_app.include_router(agent_files.router)
    old_app.include_router(agent_scan_batches.router)
    old_app.dependency_overrides[get_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=old_app), base_url="http://test", headers={"Authorization": f"Bearer {raw_token}"}) as client:
        result = await scan_directory({"api_client": _agent_api(client)}, scan_path=str(tmp_path), batch_id=str(batch.id), agent_id=agent.id)
        poster = Poster(client=_agent_api(client), agent_id=agent.id)
        await poster.post_one(str(files["cue"]))

    assert result == {"status": "completed", "files_posted": len(files)}
    assert len((await session.execute(select(FileRecord.id))).all()) == len(files)
    assert (await session.execute(select(CompanionContentFeatures.file_id))).all() == []


def _agent_settings(scan_roots: list[str]) -> AgentSettings:
    cfg = MagicMock(spec=AgentSettings)
    cfg.scan_roots = scan_roots
    return cfg


async def test_a_companion_skipped_at_ingest_is_logged_and_recovered_by_the_backfill(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """The 404 tolerance is a version-skew backstop: the skip is logged, and the backfill's own selection recovers it."""
    root = tmp_path / "root"
    files = _release(root)
    outside = tmp_path / "elsewhere" / "outside.cue"
    outside.parent.mkdir()
    outside.write_bytes(b'FILE "x.mp3" MP3\r\n')
    agent, _token = seed_test_agent
    old_app = FastAPI()
    old_app.include_router(agent_files.router)
    old_app.dependency_overrides[get_session] = lambda: session
    session.add(ScanBatch(agent_id=agent.id, scan_path="<watcher>", status=ScanStatus.LIVE.value, total_files=0, processed_files=0))
    await session.commit()
    # Ingest against a control plane without the features route: the rows land, the features do not.
    with capture_logs() as logs:
        async with AsyncClient(transport=ASGITransport(app=old_app), base_url="http://test", headers=dict(authenticated_client.headers)) as client:
            poster = Poster(client=_agent_api(client), agent_id=agent.id)
            for path in [*files.values(), outside]:
                await poster.post_one(str(path))
    skipped = [entry for entry in logs if entry["event"] == "companion features not reported; the backfill will cover them"]
    assert len(skipped) == len(files)  # one per companion post; never silent
    assert (await session.execute(select(CompanionContentFeatures.file_id))).all() == []

    # The backfill selects exactly the skipped companions (media never), then its task reports them.
    (before,) = await count_backfill(session)
    assert (before.companions, before.missing, before.current) == (len(files), len(files), 0)
    page = await select_backfill_page(session, agent.id, after=None, limit=100)
    with patch("phaze.tasks.companion_features.get_settings", return_value=_agent_settings([str(root)])):
        result = await extract_companion_features(
            {"api_client": _agent_api(authenticated_client)},
            **ExtractCompanionFeaturesPayload(
                agent_id=agent.id, targets=[CompanionFeaturesTarget(file_id=file_id, original_path=path) for file_id, path in page]
            ).model_dump(mode="json"),
        )

    assert result == {"requested": len(files), "escaped": 1, "unreadable": 0, "reported": len(files) - 1, "stored": len(files) - 1}
    _assert_release_features(files, await _features_by_name(session))
    (after,) = await count_backfill(session)
    assert (after.current, after.missing) == (len(files) - 1, 1)  # only the escaping path stays selected
    assert await select_backfill_page(session, agent.id, after=None, limit=100) == [(fid, path) for fid, path in page if path == str(outside)]


async def test_the_backfill_reads_through_the_resolved_path_and_never_follows_an_escape(
    tmp_path: Path,
    authenticated_client: AsyncClient,
    seed_test_agent: tuple[Agent, str],
    session: AsyncSession,
) -> None:
    """Containment resolves the control-plane string; the RESOLVED path is what gets opened."""
    root = tmp_path / "root"
    release = root / "rel"
    release.mkdir(parents=True)
    (release / "set.mp3").write_bytes(b"audio")
    secret = tmp_path / "outside" / "secret.nfo"
    secret.parent.mkdir()
    secret.write_bytes(b"Artist ....: NOT FOR READING\r\nGenre .....: x\r\nSource ....: y\r\n")
    real = release / "real.cue"
    real.write_bytes(b'FILE "set.mp3" MP3\r\n')
    (release / "alias.cue").symlink_to(real)  # contained: resolves inside the root
    (release / "escape.nfo").symlink_to(secret)  # escapes: resolves outside every root
    agent, _token = seed_test_agent
    session.add_all(
        FileRecord(
            agent_id=agent.id,
            sha256_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            original_path=str(path),
            original_filename=path.name,
            current_path=str(path),
            file_type=path.suffix.lstrip("."),
            file_size=path.stat().st_size,
        )
        for path in (release / "alias.cue", release / "escape.nfo")
    )
    await session.commit()
    rows = (await session.execute(select(FileRecord.id, FileRecord.original_path).order_by(FileRecord.original_path))).all()

    opened: list[str] = []
    real_read = read_companion

    def _spy(path: str, **kwargs: object) -> object:
        opened.append(path)
        return real_read(path, **kwargs)  # type: ignore[arg-type]

    with (
        patch("phaze.tasks.companion_features.get_settings", return_value=_agent_settings([str(root)])),
        patch("phaze.services.companion_features_report.read_companion", side_effect=_spy),
    ):
        result = await extract_companion_features(
            {"api_client": _agent_api(authenticated_client)},
            **ExtractCompanionFeaturesPayload(
                agent_id=agent.id, targets=[CompanionFeaturesTarget(file_id=file_id, original_path=path) for file_id, path in rows]
            ).model_dump(mode="json"),
        )

    assert result["escaped"] == 1
    assert opened == [str(real.resolve())]  # the resolved target, never the raw alias string or the escape
    by_name = await _features_by_name(session)
    assert set(by_name) == {"alias.cue"}  # reported under the ROW KEY, so it lands on the alias's row
    assert by_name["alias.cue"].media_references == [{"name": "set.mp3", "source": "cue_file"}]


def test_a_symlink_swapped_in_after_the_containment_check_is_refused(tmp_path: Path) -> None:
    """The window between resolving and opening: O_NOFOLLOW refuses a symlink planted at the resolved path."""
    from phaze.services.companion_features_report import read_companion_records
    from phaze.tasks.companion_features import _contained

    root = tmp_path / "root"
    root.mkdir()
    target = root / "notes.nfo"
    target.write_bytes(b"release notes long enough to be read\r\n")
    secret = tmp_path / "secret.nfo"
    secret.write_bytes(b"outside the roots, never to be read\r\n")

    targets = _contained([str(target)], [str(root)])
    assert targets == [(str(target), str(target.resolve()))]
    target.unlink()
    target.symlink_to(secret)  # the swap, after the check

    records, unreadable = read_companion_records(targets, follow_symlinks=False)

    assert (records, unreadable) == ([], 1)


async def test_the_backfill_task_posts_nothing_when_every_path_escapes(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    api = AsyncMock()
    with patch("phaze.tasks.companion_features.get_settings", return_value=_agent_settings([str(tmp_path / "root")])):
        result = await extract_companion_features(
            {"api_client": api},
            **ExtractCompanionFeaturesPayload(
                agent_id="test-agent-01", targets=[CompanionFeaturesTarget(file_id=uuid.uuid4(), original_path=str(tmp_path / "outside.cue"))]
            ).model_dump(mode="json"),
        )

    assert result == {"requested": 1, "escaped": 1, "unreadable": 0, "reported": 0, "stored": 0}
    api.post_companion_features.assert_not_awaited()


def test_an_nfd_twin_reached_through_a_symlinked_directory_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Containment applies to the path that gets OPENED: the twin, not the control-plane string.

    On a normalization-sensitive filesystem (Linux) a stored NFC path that does not exist byte-exact is
    handed to ``resolve_media_path``, which walks down from the longest existing ancestor and may
    pick a child that is a symlinked DIRECTORY. The twin is substituted here, deterministically, so the
    test does not depend on the machine's filesystem: checking the raw string (it resolves lexically,
    inside the root) and then opening the twin would read outside the roots.
    """
    from phaze.services.companion_features_report import read_companion_records
    from phaze.tasks import companion_features as task

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.nfo").write_bytes(b"outside the roots, never to be read\r\n")
    (root / "link").symlink_to(outside, target_is_directory=True)
    stored = str(root / "Cafe-nfc" / "notes.nfo")  # does not exist byte-exact
    twin = str(root / "link" / "notes.nfo")  # what the NFD walk would pick
    monkeypatch.setattr(task, "resolve_media_path", lambda path: twin if path == stored else path)

    targets = task._contained([stored], [str(root)])

    assert targets == []  # refused: the twin resolves outside every root
    assert read_companion_records(targets, follow_symlinks=False) == ([], 0)
    # Positive control: the same twin without the symlinked directory is read, from its resolved form.
    (root / "link").unlink()
    (root / "link").mkdir()
    (root / "link" / "notes.nfo").write_bytes(b"inside the root, fine to be read here\r\n")
    assert task._contained([stored], [str(root)]) == [(stored, str((root / "link" / "notes.nfo").resolve()))]
