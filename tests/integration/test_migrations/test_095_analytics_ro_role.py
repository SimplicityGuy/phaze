"""Migration 095 creates the ``analytics_ro`` role and the stable ``analytics_*`` views (phaze-p22p8).

Every acceptance criterion of the bead is discharged here by a test that connects AS ``analytics_ro``:

* it can read every view (``test_the_role_reads_every_view``);
* it cannot read any base table (``test_the_role_cannot_read_any_base_table``) and cannot write
  (``test_the_role_cannot_write_or_create``);
* its grants are exactly SELECT on exactly the views (``test_the_role_holds_select_on_the_views_and_nothing_else``);
* the views' column contract is pinned (``test_the_view_column_contract_is_stable``) and documented
  (``tests/shared/test_analytics_read_only_docs.py``), so a column change cannot land without both moving;
* upgrade and downgrade round-trip (``test_downgrade_drops_the_views_and_the_grants_and_upgrade_is_repeatable``).

The role is cluster-wide while every seat's database is not, so the password this fixture sets is a fixed, test-only
string: production leaves the role passwordless until the operator sets one.
"""

import asyncio
from collections.abc import AsyncGenerator
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .analytics_contract import EXPECTED_COLUMNS
from .conftest import MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, _reset_schema, downgrade_to, upgrade_to


_ROLE = "analytics_ro"
_TEST_ONLY_PASSWORD = "analytics-ro-test-only"


def _ro_url() -> str:
    return make_url(MIGRATIONS_TEST_DATABASE_URL).set(username=_ROLE, password=_TEST_ONLY_PASSWORD).render_as_string(hide_password=False)


async def _seed(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """Insert one of everything the views project, plus rows each view's filter must exclude."""
    ids = {name: uuid.uuid4() for name in ("file", "tl", "tl_propagated", "v1", "v2", "obj", "obs", "other_obs", "other_file")}
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO agents (id, name, kind, created_at, updated_at) VALUES ('a1', 'a1', 'fileserver', NOW(), NOW())"))
        for key, name in (("file", "set"), ("other_file", "other")):
            await conn.execute(
                text(
                    "INSERT INTO files (id, sha256_hash, original_path, original_filename, current_path, file_type, file_size, agent_id) "
                    "VALUES (:id, :h, :p, :n, :p, 'mp3', 7, 'a1')"
                ),
                {"id": ids[key], "h": f"hash-{name}", "p": f"/x/{name}.mp3", "n": f"{name}.mp3"},
            )
        await conn.execute(
            text(
                "INSERT INTO analysis (id, file_id, bpm, musical_key, features, created_at, updated_at) VALUES (:id, :f, 128.5, 'Am', '{\"big\": 1}', NOW(), NOW())"
            ),
            {"id": uuid.uuid4(), "f": ids["file"]},
        )
        await conn.execute(
            text(
                "INSERT INTO analysis_window (id, file_id, tier, window_index, start_sec, end_sec, features, created_at, updated_at) "
                "VALUES (:id, :f, 'fine', 0, 0.0, 5.0, '{\"big\": 1}', NOW(), NOW())"
            ),
            {"id": uuid.uuid4(), "f": ids["file"]},
        )
        for key, propagated in (("tl", None), ("tl_propagated", "set-key")):
            await conn.execute(
                text(
                    "INSERT INTO tracklists (id, external_id, source_url, file_id, propagated_from_set_key, created_at, updated_at) "
                    "VALUES (:id, :e, 'u', :f, :p, NOW(), NOW())"
                ),
                {"id": ids[key], "e": str(ids[key])[:8], "f": ids["file"], "p": propagated},
            )
        for version_key, number in (("v1", 1), ("v2", 2)):
            await conn.execute(
                text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, created_at, updated_at) VALUES (:id, :t, :n, NOW(), NOW())"),
                {"id": ids[version_key], "t": ids["tl"], "n": number},
            )
            await conn.execute(
                text(
                    "INSERT INTO tracklist_tracks (id, version_id, position, title, timestamp, created_at, updated_at) "
                    "VALUES (:id, :v, 1, :title, '00:01:00', NOW(), NOW())"
                ),
                {"id": uuid.uuid4(), "v": ids[version_key], "title": f"track-v{number}"},
            )
        await conn.execute(
            text("INSERT INTO tracklist_versions (id, tracklist_id, version_number, created_at, updated_at) VALUES (:id, :t, 1, NOW(), NOW())"),
            {"id": uuid.uuid4(), "t": ids["tl_propagated"]},
        )
        await conn.execute(
            text("INSERT INTO provider_source_objects (id, provider_id, native_id, native_digest) VALUES (:id, 'local', 'cue-1', 'digest')"),
            {"id": ids["obj"]},
        )
        cue_payload = (
            '{"source_format": "cue", "tracks": ['
            '{"position": 1, "artist": "A", "title": "T1", "evidence": ["FILE:1:INDEX 01"],'
            ' "timestamp": {"original": "00:00:00", "kind": "offset", "precision": "cue_frame_75", "offset_usability": "qualified",'
            ' "offset": {"numerator": 0, "denominator": 1}}},'
            '{"position": 2, "artist": "A", "title": "T2", "evidence": ["FILE:1:INDEX 01"],'
            ' "timestamp": {"original": "03:30:15", "kind": "offset", "precision": "cue_frame_75", "offset_usability": "qualified",'
            ' "offset": {"numerator": 4201, "denominator": 20}}},'
            '{"position": 3, "artist": "A", "title": "T3", "evidence": ["FILE:2:INDEX 01"],'
            ' "timestamp": {"original": "00:00:00", "kind": "offset", "precision": "cue_frame_75", "offset_usability": "qualified",'
            ' "offset": {"numerator": 0, "denominator": 1}}},'
            '{"position": 4, "artist": "A", "title": "T4", "evidence": []}'
            "]}"
        )
        for key in ("obs", "other_obs"):
            await conn.execute(
                text(
                    "INSERT INTO provider_source_observations "
                    "(id, object_id, status, code, revision_scope, parser_version, content_digest, payload, truncated, retrieved_at) "
                    "VALUES (:id, :o, 'found', 'ok', 'full', 'local-tracklist-v4', :d, CAST(:p AS jsonb), false, NOW())"
                ),
                {"id": ids[key], "o": ids["obj"], "d": str(ids[key]), "p": cue_payload},
            )
        # Selected source for `file` (maps FILE 1); `other_file` has only a NOT-selected observation -> nothing visible.
        await conn.execute(
            text(
                "INSERT INTO provider_recording_selections (media_id, kind, observation_id, actor, target_mapping) "
                "VALUES (:m, 'tracklist', :o, 'op', CAST('{\"cue_file_ordinal\": 1}' AS jsonb))"
            ),
            {"m": ids["file"], "o": ids["obs"]},
        )
        await conn.execute(
            text("INSERT INTO provider_recording_candidates (id, media_id, observation_id, status, evidence) VALUES (:id, :m, :o, 'pending', '[]')"),
            {"id": uuid.uuid4(), "m": ids["other_file"], "o": ids["other_obs"]},
        )
    return ids


@pytest_asyncio.fixture
async def seeded() -> AsyncGenerator[tuple[AsyncEngine, AsyncEngine, dict[str, uuid.UUID]]]:
    """Upgrade to head, seed, and yield (owner engine, analytics_ro engine, ids)."""
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "head")
    owner = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    ro = create_async_engine(_ro_url())
    try:
        async with owner.begin() as conn:
            await conn.execute(text(f"ALTER ROLE {_ROLE} PASSWORD '{_TEST_ONLY_PASSWORD}'"))
        yield owner, ro, await _seed(owner)
    finally:
        await ro.dispose()
        await owner.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)


async def _denied(ro: AsyncEngine, sql: str) -> str:
    """Run ``sql`` as analytics_ro on its own connection and return the refusal text; fail if it was allowed."""
    async with ro.connect() as conn:
        with pytest.raises(DBAPIError) as excinfo:
            await conn.execute(text(sql))
    return str(excinfo.value)


async def test_the_role_reads_every_view(seeded: tuple[AsyncEngine, AsyncEngine, dict[str, uuid.UUID]]) -> None:
    _, ro, ids = seeded
    async with ro.connect() as conn:
        assert (await conn.execute(text("SELECT current_user"))).scalar_one() == _ROLE
        for view, columns in EXPECTED_COLUMNS.items():
            result = await conn.execute(text(f"SELECT * FROM {view}"))  # noqa: S608 -- view names come from EXPECTED_COLUMNS
            assert list(result.keys()) == columns, view
            rows = result.all()
            if view != "analytics_cue_recording_offsets":  # its seed has no sha binding; test_095_cue_recording_offsets.py covers it with real data
                assert len(rows) >= 1, f"{view} returned no seeded row"
        files = (await conn.execute(text("SELECT file_id, original_filename FROM analytics_files ORDER BY original_filename"))).all()
        assert [row.original_filename for row in files] == ["other.mp3", "set.mp3"]
        analysis = (await conn.execute(text("SELECT file_id, bpm, musical_key FROM analytics_file_analysis"))).one()
        assert (analysis.file_id, analysis.bpm, analysis.musical_key) == (ids["file"], 128.5, "Am")
        windows = (await conn.execute(text("SELECT tier, start_sec, end_sec FROM analytics_analysis_windows"))).one()
        assert tuple(windows) == ("fine", 0.0, 5.0)


async def test_canonical_tracklists_and_only_their_latest_version_are_exposed(seeded: tuple[AsyncEngine, AsyncEngine, dict[str, uuid.UUID]]) -> None:
    _, ro, ids = seeded
    async with ro.connect() as conn:
        tracklists = (await conn.execute(text("SELECT tracklist_id FROM analytics_tracklists"))).scalars().all()
        assert tracklists == [ids["tl"]], "the propagated projection must not be exposed as a tracklist"
        tracks = (await conn.execute(text("SELECT tracklist_id, version_number, title, timestamp FROM analytics_tracklist_tracks"))).all()
        assert [tuple(row) for row in tracks] == [(ids["tl"], 2, "track-v2", "00:01:00")]


async def test_cue_offsets_are_the_selected_observations_mapped_file_only(seeded: tuple[AsyncEngine, AsyncEngine, dict[str, uuid.UUID]]) -> None:
    _, ro, ids = seeded
    async with ro.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT file_id, observation_id, cue_file_ordinal, position, timestamp_original, timestamp_precision, offset_usability, "
                    "offset_numerator, offset_denominator, offset_seconds FROM analytics_cue_track_offsets ORDER BY position"
                )
            )
        ).all()
    assert [(row.file_id, row.observation_id, row.cue_file_ordinal, row.position) for row in rows] == [
        (ids["file"], ids["obs"], 1, 1),
        (ids["file"], ids["obs"], 1, 2),
    ], "only the mapped FILE's tracks of the SELECTED observation may appear (no FILE 2 track, no evidence-less track, no unselected candidate)"
    assert [float(row.offset_seconds) for row in rows] == [0.0, pytest.approx(210.05)]
    assert (rows[1].offset_numerator, rows[1].offset_denominator, rows[1].offset_usability, rows[1].timestamp_precision) == (
        4201,
        20,
        "qualified",
        "cue_frame_75",
    )


async def test_the_role_cannot_read_any_base_table(seeded: tuple[AsyncEngine, AsyncEngine, dict[str, uuid.UUID]]) -> None:
    owner, ro, _ = seeded
    async with owner.connect() as conn:
        tables = (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all()
    assert {"files", "analysis", "analysis_window", "tracklists", "provider_source_observations", "alembic_version"} <= set(tables)
    for table in tables:
        assert "permission denied" in await _denied(ro, f'SELECT * FROM "{table}" LIMIT 1'), table  # noqa: S608 -- names read from pg_tables


async def test_the_role_cannot_write_or_create(seeded: tuple[AsyncEngine, AsyncEngine, dict[str, uuid.UUID]]) -> None:
    """Refused by GRANTS, not merely by the read-only default: the second engine switches that default off."""
    owner, ro, _ = seeded
    read_write = create_async_engine(_ro_url(), connect_args={"server_settings": {"default_transaction_read_only": "off"}})
    try:
        async with read_write.connect() as conn:
            assert (await conn.execute(text("SHOW default_transaction_read_only"))).scalar_one() == "off"
        for statement in (
            "INSERT INTO files (id) VALUES (gen_random_uuid())",
            "UPDATE files SET file_size = 0",
            "DELETE FROM files",
            "UPDATE analytics_files SET file_size = 0",
            "DELETE FROM analytics_files",
            "INSERT INTO analytics_files (file_id) VALUES (gen_random_uuid())",
            "TRUNCATE files",
            "CREATE TABLE analytics_ro_scratch (x integer)",
            "CREATE VIEW analytics_ro_scratch AS SELECT 1",
        ):
            assert "permission denied" in await _denied(read_write, statement), statement
    finally:
        await read_write.dispose()
    # The role's own default is the second wall behind the missing grants.
    assert "read-only transaction" in await _denied(ro, "CREATE TEMP TABLE analytics_ro_scratch (x integer)")
    async with ro.connect() as conn:
        assert (await conn.execute(text("SHOW default_transaction_read_only"))).scalar_one() == "on"
    async with owner.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM files"))).scalar_one() == 2, "a refused write must change nothing"


async def test_the_role_holds_select_on_the_views_and_nothing_else(seeded: tuple[AsyncEngine, AsyncEngine, dict[str, uuid.UUID]]) -> None:
    owner, _, _ = seeded
    async with owner.connect() as conn:
        grants = (
            await conn.execute(text("SELECT table_name, privilege_type FROM information_schema.role_table_grants WHERE grantee = :r"), {"r": _ROLE})
        ).all()
        views = set((await conn.execute(text("SELECT viewname FROM pg_views WHERE schemaname = 'public'"))).scalars().all())
        role = (
            await conn.execute(
                text("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolcanlogin FROM pg_roles WHERE rolname = :r"), {"r": _ROLE}
            )
        ).one()
    assert sorted(grants) == sorted((view, "SELECT") for view in EXPECTED_COLUMNS)
    assert views == set(EXPECTED_COLUMNS), "a public view that is not in the documented contract (or a missing one)"
    assert tuple(role) == (False, False, False, False, True)


async def test_the_view_column_contract_is_stable(seeded: tuple[AsyncEngine, AsyncEngine, dict[str, uuid.UUID]]) -> None:
    owner, _, _ = seeded
    async with owner.connect() as conn:
        for view, columns in EXPECTED_COLUMNS.items():
            actual = (
                (
                    await conn.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = :v ORDER BY ordinal_position"), {"v": view}
                    )
                )
                .scalars()
                .all()
            )
            assert actual == columns, f"{view}: a column change needs the docs and EXPECTED_COLUMNS updated in the same change"


async def test_downgrade_drops_the_views_and_the_grants_and_upgrade_is_repeatable() -> None:
    cfg = _build_alembic_config(MIGRATIONS_TEST_DATABASE_URL)
    await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
    await asyncio.to_thread(upgrade_to, cfg, "094")
    engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
    grants = "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = :r"
    views = "SELECT count(*) FROM pg_views WHERE schemaname = 'public' AND viewname LIKE 'analytics\\_%'"
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text(views))).scalar_one() == 0
        for _ in range(2):  # the second pass proves the role surviving a downgrade (cluster-wide) does not break re-upgrade
            await asyncio.to_thread(upgrade_to, cfg, "095")
            async with engine.connect() as conn:
                assert (await conn.execute(text(views))).scalar_one() == len(EXPECTED_COLUMNS)
                assert (await conn.execute(text(grants), {"r": _ROLE})).scalar_one() == len(EXPECTED_COLUMNS)
            await asyncio.to_thread(downgrade_to, cfg, "094")
            async with engine.connect() as conn:
                assert (await conn.execute(text(views))).scalar_one() == 0
                assert (await conn.execute(text(grants), {"r": _ROLE})).scalar_one() == 0
    finally:
        await engine.dispose()
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
