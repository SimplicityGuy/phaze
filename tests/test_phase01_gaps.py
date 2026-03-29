"""Phase 1 gap-fill tests: app factory, core settings, DB session factory, Alembic migration."""

from pathlib import Path
import re

from phaze.config import Settings
from phaze.main import create_app


# --- Gap 1: Core Settings defaults ---


def test_settings_database_url_default() -> None:
    """Settings.database_url defaults to the Docker Compose postgres address."""
    s = Settings()
    assert s.database_url == "postgresql+asyncpg://phaze:phaze@postgres:5432/phaze"


def test_settings_redis_url_default() -> None:
    """Settings.redis_url defaults to the Docker Compose redis address."""
    s = Settings()
    assert s.redis_url == "redis://redis:6379/0"


def test_settings_debug_default_is_false() -> None:
    """Settings.debug defaults to False (not verbose in production)."""
    s = Settings()
    assert s.debug is False


def test_settings_api_port_default() -> None:
    """Settings.api_port defaults to 8000."""
    s = Settings()
    assert s.api_port == 8000


def test_settings_openai_api_key_default_is_none() -> None:
    """Settings.openai_api_key defaults to None (optional credential)."""
    s = Settings()
    assert s.openai_api_key is None


# --- Gap 2: App factory structure ---


def test_create_app_returns_fastapi_instance() -> None:
    """create_app() returns a FastAPI application instance."""
    from fastapi import FastAPI

    app = create_app()
    assert isinstance(app, FastAPI)


def test_create_app_title_is_phaze() -> None:
    """create_app() sets title to 'Phaze'."""
    app = create_app()
    assert app.title == "Phaze"


def test_create_app_has_health_route() -> None:
    """create_app() registers the /health route."""
    app = create_app()
    route_paths = [route.path for route in app.routes]  # type: ignore[union-attr]
    assert "/health" in route_paths


# --- Gap 3: Database session factory ---


def test_get_session_is_async_generator_function() -> None:
    """get_session is an async generator function (not a coroutine)."""
    import inspect

    from phaze.database import get_session

    assert inspect.isasyncgenfunction(get_session)


# --- Gap 4: Alembic migration file structural verification ---


def test_initial_migration_creates_five_tables() -> None:
    """Initial Alembic migration creates all 5 core tables."""
    migration_path = Path("alembic/versions/001_initial_schema.py")
    assert migration_path.exists(), "001_initial_schema.py must exist"
    content = migration_path.read_text()
    # op.create_table( is followed by a newline then the quoted table name
    create_table_calls = re.findall(r'op\.create_table\(\s*["\'](\w+)["\']', content)
    expected_tables = {"files", "metadata", "analysis", "proposals", "execution_log"}
    assert expected_tables == set(create_table_calls), f"Expected {expected_tables}, got {set(create_table_calls)}"


def test_initial_migration_has_downgrade() -> None:
    """Initial Alembic migration includes downgrade that drops all 5 tables."""
    migration_path = Path("alembic/versions/001_initial_schema.py")
    content = migration_path.read_text()
    drop_table_calls = re.findall(r'op\.drop_table\(["\'](\w+)["\']', content)
    expected_tables = {"files", "metadata", "analysis", "proposals", "execution_log"}
    assert expected_tables == set(drop_table_calls), f"Downgrade should drop {expected_tables}, got {set(drop_table_calls)}"


def test_initial_migration_has_down_revision_none() -> None:
    """Initial Alembic migration has down_revision = None (it is the root)."""
    migration_path = Path("alembic/versions/001_initial_schema.py")
    content = migration_path.read_text()
    # Alembic generates: down_revision: str | Sequence[str] | None = None
    # or the simpler: down_revision = None
    # Use a simple substring check after splitting on '=' to find None assignment
    assert re.search(r"down_revision[^=]+=\s*None", content) is not None
