"""Catalog refusal and privacy contracts without replacing the settings singleton."""

import pytest

from phaze.config import AgentSettings, ControlSettings
from phaze.runtime_config import RESTART_ONLY_KEYS
from phaze.runtime_config_catalog import RESTART_HELP, restart_only_groups


def test_missing_restart_description_refuses_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "database_url"
    with monkeypatch.context() as patch:
        patch.delitem(RESTART_HELP, key)
        patch.setattr(ControlSettings.model_fields[key], "description", None)
        if key in AgentSettings.model_fields:
            patch.setattr(AgentSettings.model_fields[key], "description", None)
        with pytest.raises(AssertionError, match=f"Missing Runtime Config description: {key}"):
            restart_only_groups()
    assert restart_only_groups()


def test_catalog_contains_only_names_and_descriptions_without_resolving_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "synthetic-catalog-secret-never-read"
    monkeypatch.setenv("PHAZE_DATABASE_URL", f"postgresql://user:{secret}@localhost/example")
    monkeypatch.setenv("PHAZE_OPENAI_API_KEY", secret)

    def forbid_settings(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("catalog must not instantiate or resolve settings")

    monkeypatch.setattr(ControlSettings, "__init__", forbid_settings)
    monkeypatch.setattr(AgentSettings, "__init__", forbid_settings)
    groups = restart_only_groups()
    assert {item["key"] for group in groups for item in group["items"]} == RESTART_ONLY_KEYS
    for group in groups:
        assert set(group) == {"name", "items"}
        assert group["name"]
        for item in group["items"]:
            assert set(item) == {"key", "description"}
            assert item["description"]
    assert secret not in repr(groups)
