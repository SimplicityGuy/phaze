"""Phase 1 gap-fill tests: app factory, core settings, DB session factory.

The Gap-4 structural checks on ``001_initial_schema.py`` died with the Phase 102
flatten; their durable value lives in
``tests/integration/test_migrations/test_baseline_schema.py``.
"""

import pytest

from phaze.config import Settings
from phaze.main import create_app
from tests._route_introspection import effective_route_paths


# --- Gap 1: Core Settings defaults ---


def test_settings_database_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings.database_url defaults to the Docker Compose postgres address."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+asyncpg://phaze:phaze@postgres:5432/phaze"


def test_settings_redis_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings.redis_url defaults to the Docker Compose redis address."""
    monkeypatch.delenv("PHAZE_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("redis_url", raising=False)
    s = Settings(_env_file=None)
    assert s.redis_url == "redis://redis:6379/0"


def test_settings_debug_default_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings.debug defaults to False (not verbose in production)."""
    monkeypatch.delenv("DEBUG", raising=False)
    s = Settings(_env_file=None)
    assert s.debug is False


def test_settings_api_port_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings.api_port defaults to 8000."""
    monkeypatch.delenv("API_PORT", raising=False)
    s = Settings(_env_file=None)
    assert s.api_port == 8000


def test_settings_openai_api_key_default_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings.openai_api_key defaults to None (optional credential)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = Settings(_env_file=None)
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
    assert "/health" in effective_route_paths(app)


# --- Gap 3: Database session factory ---


def test_get_session_is_async_generator_function() -> None:
    """get_session is an async generator function (not a coroutine)."""
    import inspect

    from phaze.database import get_session

    assert inspect.isasyncgenfunction(get_session)
