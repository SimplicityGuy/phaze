"""A remote adapter's binding: provider, core's host profile for it, and its settings declaration."""

import pytest

from phaze.config_remote_providers import disabled_provider_ids, load_remote_provider_settings
from phaze.services.remote_providers import REMOTE_PROVIDERS, RemoteProviderBinding
from phaze.tracklist_providers.domain import Capability, ProviderDescriptor
from phaze.tracklist_providers.host_profile import CredentialSlot, RemoteFetchRoute, RemoteHostProfile


class Catalog:
    descriptor = ProviderDescriptor(
        id="example_catalog", display_name="Fictional catalog", capabilities=frozenset({Capability.LOAD, Capability.REMOTE_TRANSPORT})
    )


PROFILE = RemoteHostProfile(
    provider_id="example_catalog",
    host="catalog.example.invalid",
    user_agent="phaze-test/1 (+https://example.invalid)",
    fetch=RemoteFetchRoute(native_id_pattern=r"set-[0-9]{2}", format="example_set_json", path_template="/sets/{native_id}"),
)


def test_binding_derives_its_settings_spec() -> None:
    assert RemoteProviderBinding(Catalog(), PROFILE).spec.credential_names == ()
    keyed = RemoteProviderBinding(Catalog(), PROFILE.model_copy(update={"credential": CredentialSlot(header="X-Api-Key")}), credential_name="api_key")
    assert keyed.spec.provider_id == "example_catalog" and keyed.spec.credential_names == ("api_key",)


def test_binding_refuses_a_profile_for_another_provider_or_a_mismatched_credential() -> None:
    with pytest.raises(ValueError, match="not for provider"):
        RemoteProviderBinding(Catalog(), PROFILE.model_copy(update={"provider_id": "other_catalog"}))
    with pytest.raises(ValueError, match="credential slot"):
        RemoteProviderBinding(Catalog(), PROFILE, credential_name="api_key")
    with pytest.raises(ValueError, match="credential slot"):
        RemoteProviderBinding(Catalog(), PROFILE.model_copy(update={"credential": CredentialSlot(header="X-Api-Key")}))


def test_the_shipped_remote_adapters_are_exactly_the_registered_bindings() -> None:
    ids = [binding.profile.provider_id for binding in REMOTE_PROVIDERS]
    assert ids == ["trackid", "mixesdb"]
    # Each bound once, and every one is off under empty settings.
    descriptors = [ProviderDescriptor.model_validate(binding.provider.descriptor) for binding in REMOTE_PROVIDERS]
    settings = load_remote_provider_settings((binding.spec for binding in REMOTE_PROVIDERS), environ={})
    assert disabled_provider_ids(descriptors, settings) == frozenset(ids)
