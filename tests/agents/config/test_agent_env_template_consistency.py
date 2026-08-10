"""Regression for phaze-6goty: `.env.example.agent` must never instruct an operator to set
`PHAZE_AGENT_ENV` to a value `AgentSettings.agent_env` rejects.

`agent_env` is `Literal["dev", "production"]` (config.py). The template's own prose used to say
"Use `development` only on a local test rig" -- a value the Literal does not accept, so following
the template's instruction crash-loops every lane worker + the watcher at settings construction.

No DB, no Redis required -- pure text/typing assertions against the repo-root template file.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import get_args

from phaze.config import AgentSettings


_TEMPLATE_PATH = Path(__file__).resolve().parents[3] / ".env.example.agent"


def _agent_env_literal_values() -> tuple[str, ...]:
    field = AgentSettings.model_fields["agent_env"]
    return get_args(field.annotation)


def test_agent_env_literal_values_are_dev_and_production() -> None:
    """Pin the accepted vocabulary so this test fails loudly if the Literal itself ever changes."""
    assert _agent_env_literal_values() == ("dev", "production")


def test_env_example_agent_assigns_a_legal_agent_env_value() -> None:
    """The template's live `PHAZE_AGENT_ENV=...` assignment must be a value the Literal accepts."""
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    match = re.search(r"^PHAZE_AGENT_ENV=(\S+)$", text, flags=re.MULTILINE)
    assert match is not None, "expected a PHAZE_AGENT_ENV= assignment in .env.example.agent"
    assert match.group(1) in _agent_env_literal_values()


def test_env_example_agent_does_not_recommend_development_alias() -> None:
    """phaze-6goty: the surrounding comment must not tell operators to use the rejected
    "development" alias -- it must use "dev", the actual accepted short form.
    """
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "`development`" not in text, "template must not recommend the rejected 'development' alias for PHAZE_AGENT_ENV"
