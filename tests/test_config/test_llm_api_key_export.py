"""Regression tests for :func:`phaze.config.export_llm_api_keys`.

Bug A (live incident, June 2026): the control worker loaded the Anthropic key from
the ``<VAR>_FILE`` secret convention into ``ControlSettings.anthropic_api_key`` but
never handed it to litellm, so every ``generate_proposals`` call raised
``litellm.AuthenticationError: Missing Anthropic API Key``. litellm reads the bare
provider env var (``ANTHROPIC_API_KEY``) from ``os.environ``; phaze only ever set
``ANTHROPIC_API_KEY_FILE``. ``export_llm_api_keys`` bridges the gap at startup.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pydantic import SecretStr
import pytest

from phaze.config import export_llm_api_keys


if TYPE_CHECKING:
    from collections.abc import Iterator

_PROVIDER_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@pytest.fixture(autouse=True)
def _isolate_provider_env() -> Iterator[None]:
    """Guarantee the provider env vars start unset and are restored afterwards.

    The function under test mutates ``os.environ`` directly (litellm reads it there),
    so monkeypatch cannot be relied on to track keys it never set. Snapshot/restore
    explicitly to prevent leakage into ``ControlSettings()`` in unrelated tests.
    """
    saved = {var: os.environ.get(var) for var in _PROVIDER_VARS}
    for var in _PROVIDER_VARS:
        os.environ.pop(var, None)
    try:
        yield
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def test_exports_anthropic_key_when_set() -> None:
    export_llm_api_keys(anthropic_api_key=SecretStr("sk-ant-secret"), openai_api_key=None)

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-secret"


def test_exports_openai_key_when_set() -> None:
    export_llm_api_keys(anthropic_api_key=None, openai_api_key=SecretStr("sk-openai-secret"))

    assert os.environ["OPENAI_API_KEY"] == "sk-openai-secret"


def test_does_not_override_operator_set_env() -> None:
    """An explicitly-set provider env var wins over the file-loaded secret."""
    os.environ["ANTHROPIC_API_KEY"] = "operator-wins"

    export_llm_api_keys(anthropic_api_key=SecretStr("file-secret"), openai_api_key=None)

    assert os.environ["ANTHROPIC_API_KEY"] == "operator-wins"


def test_none_keys_are_noop() -> None:
    export_llm_api_keys(anthropic_api_key=None, openai_api_key=None)

    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
