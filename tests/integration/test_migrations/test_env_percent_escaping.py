"""Regression coverage for the '%' -> '%%' ConfigParser escaping fix (phaze-7oya).

Both ``alembic/env.py`` and ``phaze.database._run_upgrade_head_sync`` stored
``settings.database_url`` verbatim into an Alembic ``Config`` (ConfigParser-backed) via
``set_main_option``. ConfigParser applies %-interpolation to every value it stores, so any
literal ``%`` in the URL -- which is MANDATORY when a Postgres username/password contains a
URL-reserved character (``p@ss`` -> ``p%40ss``) -- crashes ``set_main_option`` itself with
``ValueError: invalid interpolation syntax`` before any migration ever runs. The fix escapes
``%`` as ``%%`` before handing the URL to ConfigParser at both call sites.

``test_unit_configparser_interpolation_contract`` demonstrates the underlying ConfigParser
behavior directly (no Alembic machinery): unescaped crashes, escaped round-trips losslessly.

``test_env_py_upgrade_survives_percent_in_database_url`` drives the REAL ``alembic/env.py``
end-to-end (via ``alembic.command.upgrade``) with a database URL containing a redundant,
semantically-inert percent-encoding of the test harness's own credentials (``phaze`` ->
``ph%61ze``, which decodes back to the exact same string), so the connection succeeds while
still exercising env.py's ``set_main_option`` call with a literal ``%`` in the URL -- the
regression this bead fixes. Before the fix this raises ``ValueError`` and the upgrade never
starts.
"""

from __future__ import annotations

import asyncio
from configparser import ConfigParser

from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

from .conftest import ALEMBIC_INI_PATH, MIGRATIONS_TEST_DATABASE_URL, _patched_settings_database_url, _reset_schema


def test_unit_configparser_interpolation_contract() -> None:
    """ConfigParser rejects a bare '%' and accepts '%%' -- the exact mechanism env.py/database.py escape around."""
    dsn_with_percent = "postgresql+asyncpg://phaze:s3cret%40home@db:5432/phaze"

    with pytest.raises(ValueError, match="interpolation"):
        Config().set_main_option("sqlalchemy.url", dsn_with_percent)

    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", dsn_with_percent.replace("%", "%%"))
    assert cfg.get_main_option("sqlalchemy.url") == dsn_with_percent, "escaped round-trip must deinterpolate back to the original literal URL"

    # Cross-check directly against a bare ConfigParser (Alembic's Config is a thin wrapper
    # around one) so the assertion isn't tied to Alembic's specific wrapper behavior.
    parser = ConfigParser()
    parser.add_section("s")
    with pytest.raises(ValueError, match="interpolation"):
        parser.set("s", "url", dsn_with_percent)
    parser.set("s", "url", dsn_with_percent.replace("%", "%%"))
    assert parser.get("s", "url") == dsn_with_percent


@pytest.mark.asyncio
async def test_env_py_upgrade_survives_percent_in_database_url() -> None:
    """A percent-encoded (but semantically identical) credential must not crash ``alembic upgrade head``.

    Redundantly percent-encodes one letter of the migrations-test harness's own username and
    password (``phaze`` -> ``ph%61ze``; ``%61`` is ``'a'``, so the decoded value is identical),
    producing a URL that is 100% valid for the real connection but contains a literal ``%`` --
    exactly the shape that crashed ``config.set_main_option`` pre-fix. Drives the REAL
    ``alembic/env.py`` (via ``command.upgrade``, never our own ``set_main_option`` call in test
    code) so the assertion is against production code, not a re-implementation of it.
    """
    percent_url = MIGRATIONS_TEST_DATABASE_URL.replace("phaze:phaze@", "ph%61ze:ph%61ze@")
    assert "%" in percent_url and percent_url != MIGRATIONS_TEST_DATABASE_URL

    cfg = Config(str(ALEMBIC_INI_PATH))  # no set_main_option call here -- env.py sets it from settings
    try:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
        with _patched_settings_database_url(percent_url):
            # The regression: pre-fix, env.py's module-level set_main_option raises ValueError
            # here before a single migration runs.
            await asyncio.to_thread(command.upgrade, cfg, "head")

        engine = create_async_engine(MIGRATIONS_TEST_DATABASE_URL)
        try:
            async with engine.connect() as conn:
                version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            assert version == "044"
        finally:
            await engine.dispose()
    finally:
        await _reset_schema(MIGRATIONS_TEST_DATABASE_URL)
