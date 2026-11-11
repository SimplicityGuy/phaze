"""``analytics_cue_recording_offsets`` agrees with the application's read-time projection (phaze-p22p8).

This discharges the analytics half of phaze-zimli's criterion 1: the target-bound qualified CUE offsets are computed in
SQL from ``provider_recording_selections.target_mapping`` and must equal what the application itself uses. The oracle is
not a re-implementation: a real CUE is captured, imported and selected through ``import_local_source`` /
``decide_local_source``; ``get_selected_recording_source`` supplies the availability verdict; and
``cue_tracks_from_selected`` -- the exact input of ``cue_generator.generate_cue_content`` -- supplies the offsets the CUE
writer would emit. The view, read AS ``analytics_ro``, must return those offsets while the selection is current and none
once the application stops treating it as current (sha drift on either file, a missing recording, an unlinked CUE) or when
nothing was selected (a rejected candidate).

An availability condition SQL deliberately does not express: a selection whose source channel is ``embedded`` is NOT exposed
(its revision check hashes processed tag text in Python), so the view can under-report, never over-report.
"""

import asyncio
from collections.abc import AsyncGenerator
from fractions import Fraction
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.schemas.local_source_import import ImportLocalSource
from phaze.services.cue_generator import qualified_legacy_offset
from phaze.services.cue_review import cue_tracks_from_selected
from phaze.services.local_source_import import decide_local_source, get_selected_recording_source, import_local_source
from tests.integration.test_local_source_import import decision, inventory
from tests.integration.test_selected_source_consumers import CUE

from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, upgrade_to


_PASSWORD = "analytics-ro-test-only"
_VIEW = (
    "SELECT position, offset_numerator, offset_denominator, offset_seconds, origin, file_id FROM analytics_cue_recording_offsets ORDER BY position"
)


@pytest_asyncio.fixture
async def world() -> AsyncGenerator[tuple[AsyncSession, AsyncEngine]]:
    """A migrated database, an application session on it, and an ``analytics_ro`` engine."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "head")
    owner = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    ro_url = make_url(MIGRATIONS_TEST_DATABASE_URL).set(username="analytics_ro", password=_PASSWORD).render_as_string(hide_password=False)
    ro = create_async_engine(ro_url)
    try:
        async with owner.begin() as conn:
            await conn.execute(text(f"ALTER ROLE analytics_ro PASSWORD '{_PASSWORD}'"))
            await conn.execute(
                text(
                    "INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES ('test-fileserver', 'test-fileserver', 'fileserver', NOW(), NOW())"
                )
            )
        async with async_sessionmaker(owner, expire_on_commit=False)() as session:
            yield session, ro
    finally:
        await ro.dispose()
        await owner.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


async def _selected_cue(session: AsyncSession, *, ordinal: int = 1):
    media, companion, raw = await inventory(session, CUE, "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    selected = await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, ordinal=ordinal), actor="reviewer")
    await session.commit()
    return media, companion, selected


async def _view(ro: AsyncEngine) -> list[tuple[int, Fraction | None, str, uuid.UUID]]:
    async with ro.connect() as conn:
        rows = (await conn.execute(text(_VIEW))).all()
    return [(r.position, Fraction(r.offset_numerator, r.offset_denominator), r.origin, r.file_id) for r in rows]


async def _application_offsets(session: AsyncSession, media_id: uuid.UUID) -> tuple[str, dict[int, Fraction]]:
    """What the application itself says: (availability, {position: exact offset the CUE writer would use})."""
    session.expire_all()
    selected = await get_selected_recording_source(session, media_id)
    assert selected is not None
    return selected.availability, {
        track.position: Fraction(track.timestamp_seconds) for track in cue_tracks_from_selected(selected) if track.timestamp_seconds is not None
    }


async def test_a_current_selection_yields_exactly_the_offsets_the_cue_writer_uses(world: tuple[AsyncSession, AsyncEngine]) -> None:
    session, ro = world
    media, _, selected = await _selected_cue(session)
    availability, offsets = await _application_offsets(session, media.id)
    assert availability == "current"
    assert offsets == {1: Fraction(76, 75), 2: Fraction(152, 75)}, "precondition: the real consumer produced two exact frame-precision offsets"
    rows = await _view(ro)
    assert {position: offset for position, offset, _, _ in rows} == offsets
    assert {origin for _, _, origin, _ in rows} == {f"recording:{media.id}"}, "origin is the recording, not the CUE file"
    assert {file_id for *_, file_id in rows} == {media.id}
    # The cue_generator's own gate accepts each row's evidence for this recording and reads the same exact offset.
    for track in cue_tracks_from_selected(selected):
        evidence = next(t.timestamp for t in selected.tracks if t.position == track.position)
        assert qualified_legacy_offset(evidence.original, evidence.model_dump(mode="json"), media.id) == offsets[track.position]


async def test_a_selection_without_a_reviewed_file_mapping_yields_nothing_and_so_does_a_mapping_of_another_file(
    world: tuple[AsyncSession, AsyncEngine],
) -> None:
    session, ro = world
    media, _, _ = await _selected_cue(session)
    assert await _view(ro)
    await session.execute(text("UPDATE provider_recording_selections SET target_mapping = target_mapping || '{\"cue_file_ordinal\": 2}'::jsonb"))
    await session.commit()
    assert await _view(ro) == [], "mapped FILE only: FILE 2 holds none of this CUE's tracks"
    await session.execute(text("UPDATE provider_recording_selections SET target_mapping = target_mapping - 'media_sha256'"))
    await session.commit()
    assert await _view(ro) == []
    assert (await _application_offsets(session, media.id))[0] != "current"


@pytest.mark.parametrize("drift", ["media_sha", "source_sha", "media_missing", "source_missing", "unlinked"])
async def test_the_view_empties_exactly_when_the_application_stops_treating_the_selection_as_current(
    world: tuple[AsyncSession, AsyncEngine], drift: str
) -> None:
    session, ro = world
    media, companion, _ = await _selected_cue(session)
    assert await _view(ro)
    if drift == "media_sha":
        await session.execute(update(FileRecord).where(FileRecord.id == media.id).values(sha256_hash="b" * 64))
    elif drift == "source_sha":
        await session.execute(update(FileRecord).where(FileRecord.id == companion.id).values(sha256_hash="c" * 64))
    elif drift == "media_missing":
        await session.execute(text("UPDATE files SET missing_at = NOW() WHERE id = :i"), {"i": media.id})
    elif drift == "source_missing":
        await session.execute(text("UPDATE files SET missing_at = NOW() WHERE id = :i"), {"i": companion.id})
    else:
        await session.execute(FileCompanion.__table__.delete())
    await session.commit()
    availability, offsets = await _application_offsets(session, media.id)
    assert availability != "current", f"precondition: the application no longer treats a {drift} drift as current"
    assert offsets == {}, "and the real CUE writer input then carries no qualified offset"
    assert await _view(ro) == []


async def test_a_rejected_candidate_exposes_nothing(world: tuple[AsyncSession, AsyncEngine]) -> None:
    session, ro = world
    media, companion, raw = await inventory(session, CUE, "cue")
    imported = await import_local_source(session, ImportLocalSource(media_id=media.id, raw_observation_id=raw))
    await decide_local_source(session, decision(media, companion, imported.tracklist_observation_id, action="reject"), actor="reviewer")
    await session.commit()
    assert await get_selected_recording_source(session, media.id) is None
    assert await _view(ro) == []
    async with ro.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM analytics_cue_track_offsets"))).scalar_one() == 0
