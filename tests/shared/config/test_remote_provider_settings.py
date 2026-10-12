"""Per-provider remote settings (phaze-c2zke), exercised through a real ProviderRegistry.

The provider is the fictional ``example_catalog``/``other_catalog``; nothing is sent anywhere.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from phaze.config_remote_providers import (
    RemoteProviderSettings,
    RemoteProviderSpec,
    disabled_provider_ids,
    load_remote_provider_settings,
)
from phaze.tracklist_providers import ProviderRegistry
from phaze.tracklist_providers.domain import (
    Capability,
    Completeness,
    DiscoveryBudget,
    DiscoveryOutcome,
    Fact,
    LoadBudget,
    LoadOutcome,
    OpaqueCursor,
    ProviderCandidate,
    ProviderDescriptor,
    RecordingContext,
    SourceRead,
    SourceReference,
)
from phaze.tracklist_providers.host_profile import RemoteHostProfile, RemoteSearchRoute
from phaze.tracklist_providers.port import AuthorizedEffects


SPECS = (
    RemoteProviderSpec(provider_id="example_catalog", credential_names=("client_secret",)),
    RemoteProviderSpec(provider_id="other_catalog"),
)
SECRET = "s3cr3t-value-do-not-leak"
CONTEXT = RecordingContext(recording_id="recording-01", artist=Fact(value="Example Artist", certainty="known"))
BUDGET = DiscoveryBudget()


class FakeRemote:
    def __init__(self, provider_id: str) -> None:
        self.descriptor = ProviderDescriptor(
            id=provider_id,
            display_name="Fictional remote",
            capabilities=frozenset({Capability.LOAD, Capability.DISCOVERY, Capability.REMOTE_TRANSPORT}),
        )
        self.calls = 0

    async def discover(
        self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: AuthorizedEffects
    ) -> DiscoveryOutcome:
        del cursor
        self.calls += 1
        await effects.read(effects.minted[0], budget)
        return DiscoveryOutcome(status="found", code="ok", scope=context.recording_id, completeness=Completeness(state="complete", reason="done"))

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: AuthorizedEffects) -> LoadOutcome:  # pragma: no cover
        del candidate, budget, effects
        raise AssertionError("not used")


class CountingEffects:
    def __init__(self) -> None:
        self.reads: list[SourceReference] = []

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        del budget
        self.reads.append(source)
        return SourceRead(status="found", code="http_200", scope="x", text="{}", bytes_read=2, retrieved_at=datetime(2026, 1, 1, tzinfo=UTC))


def build(env: dict[str, str]) -> tuple[ProviderRegistry, dict[str, FakeRemote], dict[str, RemoteProviderSettings]]:
    settings = load_remote_provider_settings(SPECS, env)
    providers = {spec.provider_id: FakeRemote(spec.provider_id) for spec in SPECS}
    profiles = [
        RemoteHostProfile(provider_id=pid, search=RemoteSearchRoute(fields=frozenset({"artist"}), format="example_json")) for pid in providers
    ]
    disabled = disabled_provider_ids((p.descriptor for p in providers.values()), settings)
    return ProviderRegistry(providers.values(), disabled=disabled, remote_profiles=profiles), providers, settings


def test_every_remote_provider_is_disabled_by_default() -> None:
    registry, providers, settings = build({})
    assert all(not s.enabled for s in settings.values())
    assert disabled_provider_ids((p.descriptor for p in providers.values()), settings) == {"example_catalog", "other_catalog"}
    # Fail closed: a remote provider with no settings entry at all is disabled too.
    assert disabled_provider_ids((providers["example_catalog"].descriptor,), {}) == {"example_catalog"}
    assert len(registry.descriptors()) == 2


def test_a_directly_constructed_settings_object_is_off() -> None:
    assert RemoteProviderSettings(provider_id="example_catalog").enabled is False


def test_disabled_provider_is_listed_but_invocation_returns_unavailable() -> None:
    registry, providers, _ = build({})
    effects = CountingEffects()
    assert [d.id for d in registry.descriptors()] == ["example_catalog", "other_catalog"]
    outcome = asyncio.run(registry.discover("example_catalog", CONTEXT, BUDGET, effects))
    assert (outcome.status, outcome.code) == ("unavailable", "disabled_provider")
    assert providers["example_catalog"].calls == 0
    assert effects.reads == []


def test_enabled_provider_is_invoked() -> None:
    registry, providers, _ = build({"PHAZE_PROVIDER_EXAMPLE_CATALOG_ENABLED": "true"})
    effects = CountingEffects()
    outcome = asyncio.run(registry.discover("example_catalog", CONTEXT, BUDGET, effects))
    assert outcome.code == "ok"
    assert providers["example_catalog"].calls == 1
    assert len(effects.reads) == 1


def test_enabling_one_provider_does_not_enable_another() -> None:
    registry, providers, settings = build({"PHAZE_PROVIDER_EXAMPLE_CATALOG_ENABLED": "1"})
    assert settings["example_catalog"].enabled is True
    assert settings["other_catalog"].enabled is False
    effects = CountingEffects()
    other = asyncio.run(registry.discover("other_catalog", CONTEXT, BUDGET, effects))
    assert (other.status, other.code) == ("unavailable", "disabled_provider")
    assert providers["other_catalog"].calls == 0


def test_secret_settings_are_masked(tmp_path: Path) -> None:
    secret_file = tmp_path / "client_secret"
    secret_file.write_text(f"{SECRET}\n")
    for env in (
        {"PHAZE_PROVIDER_EXAMPLE_CATALOG_CLIENT_SECRET": SECRET},
        {"PHAZE_PROVIDER_EXAMPLE_CATALOG_CLIENT_SECRET_FILE": str(secret_file)},
    ):
        settings = load_remote_provider_settings(SPECS, env)
        assert settings["example_catalog"].credential("client_secret") == SECRET
        for text in (str(settings), repr(settings), str(settings["example_catalog"]), repr(settings["example_catalog"])):
            assert SECRET not in text
            assert "**********" in text


def test_direct_credential_wins_over_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "client_secret"
    secret_file.write_text("from-file")
    env = {"PHAZE_PROVIDER_EXAMPLE_CATALOG_CLIENT_SECRET": "direct", "PHAZE_PROVIDER_EXAMPLE_CATALOG_CLIENT_SECRET_FILE": str(secret_file)}
    assert load_remote_provider_settings(SPECS, env)["example_catalog"].credential("client_secret") == "direct"


def test_pacing_and_budget_overrides_default_unset_and_parse() -> None:
    default = load_remote_provider_settings(SPECS, {})["example_catalog"]
    assert (default.min_interval_seconds, default.max_requests_per_run) == (None, None)
    env = {"PHAZE_PROVIDER_EXAMPLE_CATALOG_MIN_INTERVAL_SECONDS": "2.5", "PHAZE_PROVIDER_EXAMPLE_CATALOG_MAX_REQUESTS_PER_RUN": "7"}
    tuned = load_remote_provider_settings(SPECS, env)["example_catalog"]
    assert (tuned.min_interval_seconds, tuned.max_requests_per_run) == (2.5, 7)


@pytest.mark.parametrize("suffix,value", [("MIN_INTERVAL_SECONDS", "0"), ("MAX_REQUESTS_PER_RUN", "-1"), ("ENABLED", "maybe")])
def test_invalid_values_fail_at_load(suffix: str, value: str) -> None:
    with pytest.raises(ValueError):
        load_remote_provider_settings(SPECS, {f"PHAZE_PROVIDER_EXAMPLE_CATALOG_{suffix}": value})


@pytest.mark.parametrize("name", ["enabled", "x_file", "Bad", "min_interval_seconds"])
def test_credential_names_cannot_shadow_reserved_keys(name: str) -> None:
    with pytest.raises(ValueError, match="Invalid credential name"):
        RemoteProviderSpec(provider_id="example_catalog", credential_names=(name,))


def test_local_provider_is_never_disabled_by_settings() -> None:
    local = ProviderDescriptor(id="local", display_name="Local")
    assert disabled_provider_ids((local,), {}) == frozenset()


def test_remote_provider_keys_are_not_runtime_reloadable() -> None:
    from phaze.runtime_config import RELOADABLE_KEYS

    assert not [key for key in RELOADABLE_KEYS if "provider" in key]
