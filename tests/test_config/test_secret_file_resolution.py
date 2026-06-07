"""Unit tests for the ``<VAR>_FILE`` secret-resolution convention (v4.0.1).

Every secret-bearing settings field accepts a ``<ALIAS>_FILE`` sibling for each
env name it already honors via ``validation_alias``. If the direct env var is
unset but ``<ALIAS>_FILE`` is set, the secret value is read from that file path
(trailing whitespace stripped). A direct env var always wins over its ``_FILE``
sibling; a ``_FILE`` var pointing at an unreadable path fails fast.

Covers ``anthropic_api_key`` (ControlSettings) and ``agent_token``
(AgentSettings) per the v4.0.1 spec, plus the shared ``database_url`` path.

No DB, no Redis required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import SecretStr, ValidationError
import pytest


if TYPE_CHECKING:
    from pathlib import Path


_VALID_URL = "http://app.test:8000"
_VALID_TOKEN = "phaze_agent_test-token-abc123"
_VALID_ROOTS = "/data/music,/data/concerts"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """lru_cache must be cleared so each test gets a fresh dispatch."""
    from phaze.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the non-secret required agent fields so AgentSettings can construct."""
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", _VALID_URL)
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", _VALID_ROOTS)


# --------------------------------------------------------------------------- #
# (a) _FILE reads the value
# --------------------------------------------------------------------------- #
def test_anthropic_key_read_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ANTHROPIC_API_KEY_FILE supplies the value when the direct var is unset."""
    from phaze.config import ControlSettings

    secret = tmp_path / "anthropic_key"
    secret.write_text("sk-ant-from-file", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(secret))

    settings = ControlSettings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-from-file"


def test_agent_token_read_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PHAZE_AGENT_TOKEN_FILE supplies the agent bearer token."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    secret = tmp_path / "agent_token"
    secret.write_text(_VALID_TOKEN, encoding="utf-8")
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN_FILE", str(secret))

    settings = AgentSettings()

    assert settings.agent_token.get_secret_value() == _VALID_TOKEN


# --------------------------------------------------------------------------- #
# (b) trailing newline is stripped
# --------------------------------------------------------------------------- #
def test_anthropic_key_file_strips_trailing_newline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A heredoc/echo-created secret file's trailing newline must be stripped."""
    from phaze.config import ControlSettings

    secret = tmp_path / "anthropic_key"
    secret.write_text("sk-ant-from-file\n", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(secret))

    settings = ControlSettings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-from-file"


def test_agent_token_file_strips_surrounding_whitespace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Surrounding whitespace/newlines are stripped so the hashed wire string matches."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    secret = tmp_path / "agent_token"
    secret.write_text(f"  {_VALID_TOKEN}\n", encoding="utf-8")
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN_FILE", str(secret))

    settings = AgentSettings()

    assert settings.agent_token.get_secret_value() == _VALID_TOKEN


# --------------------------------------------------------------------------- #
# (c) direct env var takes precedence over _FILE
# --------------------------------------------------------------------------- #
def test_direct_env_wins_over_file_anthropic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicitly-set ANTHROPIC_API_KEY beats ANTHROPIC_API_KEY_FILE."""
    from phaze.config import ControlSettings

    secret = tmp_path / "anthropic_key"
    secret.write_text("sk-ant-from-file", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-direct")
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(secret))

    settings = ControlSettings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-direct"


def test_direct_env_wins_over_file_agent_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicitly-set PHAZE_AGENT_TOKEN beats PHAZE_AGENT_TOKEN_FILE."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    secret = tmp_path / "agent_token"
    secret.write_text("phaze_agent_from-file", encoding="utf-8")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", _VALID_TOKEN)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN_FILE", str(secret))

    settings = AgentSettings()

    assert settings.agent_token.get_secret_value() == _VALID_TOKEN


# --------------------------------------------------------------------------- #
# (d) _FILE pointing at a nonexistent path raises a clear error
# --------------------------------------------------------------------------- #
def test_missing_file_path_raises_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A _FILE var whose path is missing fails fast, naming the var and path."""
    from phaze.config import ControlSettings

    missing = tmp_path / "does-not-exist"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(missing))

    with pytest.raises(ValidationError) as exc_info:
        ControlSettings()

    message = str(exc_info.value)
    assert "ANTHROPIC_API_KEY_FILE" in message
    assert str(missing) in message


def test_missing_agent_token_file_raises_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing PHAZE_AGENT_TOKEN_FILE path fails fast rather than 401-looping."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    missing = tmp_path / "nope"
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN_FILE", str(missing))

    with pytest.raises(ValidationError) as exc_info:
        AgentSettings()

    message = str(exc_info.value)
    assert "PHAZE_AGENT_TOKEN_FILE" in message
    assert str(missing) in message


# --------------------------------------------------------------------------- #
# (e) both PHAZE_-prefixed and bare alias _FILE forms work
# --------------------------------------------------------------------------- #
def test_bare_alias_file_form_agent_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AGENT_TOKEN_FILE (bare alias) resolves just like PHAZE_AGENT_TOKEN_FILE."""
    from phaze.config import AgentSettings

    _agent_env(monkeypatch)
    secret = tmp_path / "agent_token"
    secret.write_text(_VALID_TOKEN, encoding="utf-8")
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_TOKEN_FILE", str(secret))

    settings = AgentSettings()

    assert settings.agent_token.get_secret_value() == _VALID_TOKEN


def test_prefixed_and_bare_file_forms_database_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both PHAZE_DATABASE_URL_FILE and DATABASE_URL_FILE resolve database_url."""
    from phaze.config import ControlSettings

    url = "postgresql+asyncpg://u:p@db:5432/x"

    secret_prefixed = tmp_path / "db_prefixed"
    secret_prefixed.write_text(url, encoding="utf-8")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PHAZE_DATABASE_URL", raising=False)
    monkeypatch.setenv("PHAZE_DATABASE_URL_FILE", str(secret_prefixed))
    assert ControlSettings().database_url == url

    monkeypatch.delenv("PHAZE_DATABASE_URL_FILE", raising=False)
    secret_bare = tmp_path / "db_bare"
    secret_bare.write_text(url, encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL_FILE", str(secret_bare))
    assert ControlSettings().database_url == url


# --------------------------------------------------------------------------- #
# _FILE set in the .env file (not just the process env) must resolve too, since
# that is how every other documented var in .env.example is consumed.
# --------------------------------------------------------------------------- #
def _point_env_file(monkeypatch: pytest.MonkeyPatch, cls: type, env_path: Path) -> None:
    """Override the autouse `env_file=None` isolation so this test loads its own .env."""
    cfg = dict(cls.model_config)
    cfg["env_file"] = str(env_path)
    monkeypatch.setattr(cls, "model_config", cfg)


def test_file_var_from_dotenv_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ANTHROPIC_API_KEY_FILE declared in the loaded .env file resolves the secret."""
    from phaze.config import ControlSettings

    secret = tmp_path / "anthropic_key"
    secret.write_text("sk-ant-from-dotenv\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(f"ANTHROPIC_API_KEY_FILE={secret}\n", encoding="utf-8")
    _point_env_file(monkeypatch, ControlSettings, env_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_FILE", raising=False)

    settings = ControlSettings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-from-dotenv"


def test_process_env_file_var_wins_over_dotenv_file_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A _FILE var in the process env overrides the same _FILE var in .env (env > dotenv)."""
    from phaze.config import ControlSettings

    env_secret = tmp_path / "from_env"
    env_secret.write_text("sk-ant-process-env", encoding="utf-8")
    dotenv_secret = tmp_path / "from_dotenv"
    dotenv_secret.write_text("sk-ant-dotenv", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(f"ANTHROPIC_API_KEY_FILE={dotenv_secret}\n", encoding="utf-8")
    _point_env_file(monkeypatch, ControlSettings, env_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(env_secret))

    settings = ControlSettings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-process-env"


# --------------------------------------------------------------------------- #
# (f) agent_token from _FILE satisfies the required-field guard + hash matches
# --------------------------------------------------------------------------- #
def test_agent_token_file_satisfies_required_guard_and_hash_matches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A _FILE-sourced token satisfies _enforce_required_agent_fields and hashes
    to exactly hash_token(value) — proving no trailing newline leaks into the
    wire string that the server hashes."""
    from phaze.config import AgentSettings
    from phaze.routers.agent_auth import hash_token

    _agent_env(monkeypatch)
    secret = tmp_path / "agent_token"
    secret.write_text(f"{_VALID_TOKEN}\n", encoding="utf-8")  # trailing newline
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN_FILE", str(secret))

    settings = AgentSettings()  # would raise if the guard saw an empty token

    resolved = settings.agent_token.get_secret_value()
    assert hash_token(resolved) == hash_token(_VALID_TOKEN)


# --------------------------------------------------------------------------- #
# SecretStr preservation — resolved secrets must not leak in repr
# --------------------------------------------------------------------------- #
def test_file_resolved_secret_stays_secretstr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A _FILE-resolved SecretStr field stays SecretStr and is masked in repr."""
    from phaze.config import ControlSettings

    secret = tmp_path / "anthropic_key"
    secret.write_text("sk-ant-super-secret", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(secret))

    settings = ControlSettings()

    assert isinstance(settings.anthropic_api_key, SecretStr)
    assert "sk-ant-super-secret" not in repr(settings)
