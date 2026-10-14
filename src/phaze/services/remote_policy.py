"""Durable global provider policy, validated atomically before each outbound boundary.

Credentials remain startup-only. Policy reload never writes transport history or clears stops.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from phaze.config_remote_providers import RemoteProviderSettings
from phaze.models.runtime_config_override import RuntimeConfigOverride
from phaze.services.remote_providers import RemoteProviderBinding
from phaze.tracklist_providers.host_profile import RemoteHostProfile


logger = structlog.get_logger(__name__)


class ProviderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    enabled: bool
    min_interval_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    max_requests_per_run: int = Field(gt=0, le=256)


@dataclass(frozen=True)
class EffectiveProviderPolicy:
    configured: ProviderPolicy
    profile: RemoteHostProfile
    available: bool
    unavailable_reason: str | None


def policy_key(provider_id: str) -> str:
    return f"provider_{provider_id}_policy"


class RemotePolicy:
    def __init__(
        self, bindings: Iterable[RemoteProviderBinding], settings: Mapping[str, RemoteProviderSettings], sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        self.bindings = {binding.profile.provider_id: binding for binding in bindings}
        self.settings = dict(settings)
        self.sessions = sessions
        self.snapshot = self.validate({})

    def validate(self, overrides: Mapping[str, object]) -> dict[str, EffectiveProviderPolicy]:
        known = {policy_key(provider_id) for provider_id in self.bindings}
        if any(key.startswith("provider_") and key.endswith("_policy") and key not in known for key in overrides):
            raise ValueError("Unknown provider policy")
        result = {}
        for provider_id, binding in self.bindings.items():
            base = self.settings.get(provider_id, RemoteProviderSettings(provider_id=provider_id))
            profile = binding.profile
            defaults = ProviderPolicy(
                enabled=base.enabled,
                min_interval_seconds=max(profile.min_gap_seconds, base.min_interval_seconds or profile.min_gap_seconds),
                max_requests_per_run=min(profile.per_run_budget, base.max_requests_per_run or profile.per_run_budget),
            )
            config = ProviderPolicy.model_validate(overrides.get(policy_key(provider_id), defaults))
            if config.min_interval_seconds < profile.min_gap_seconds or config.max_requests_per_run > profile.per_run_budget:
                raise ValueError(
                    f"{provider_id}: interval must be at least {profile.min_gap_seconds:g}s and per-run budget at most {profile.per_run_budget}"
                )
            missing = bool(binding.credential_name and not base.credentials.get(binding.credential_name))
            reason = "credential_missing" if missing else ("disabled_provider" if not config.enabled else None)
            result[provider_id] = EffectiveProviderPolicy(
                config,
                profile.model_copy(update={"min_gap_seconds": config.min_interval_seconds, "per_run_budget": config.max_requests_per_run}),
                config.enabled and not missing,
                reason,
            )
        return result

    async def refresh(self) -> dict[str, EffectiveProviderPolicy]:
        async with self.sessions() as session:
            rows = await session.scalars(select(RuntimeConfigOverride).where(RuntimeConfigOverride.key.like("provider_%_policy")))
            overrides = {row.key: row.value for row in rows}
        try:
            candidate = self.validate(overrides)
        except ValueError:
            logger.warning("remote_policy.snapshot_rejected")
            return self.snapshot
        self.snapshot = candidate
        return candidate

    async def resolve(self, provider_id: str) -> EffectiveProviderPolicy:
        return (await self.refresh())[provider_id]

    async def set(self, provider_id: str, value: ProviderPolicy, *, actor: str) -> None:
        if provider_id not in self.bindings:
            raise ValueError("Unknown provider")
        async with self.sessions() as session, session.begin():
            # Serializes read/validate/write so concurrent edits cannot publish a mixed snapshot.
            await session.execute(text("SELECT pg_advisory_xact_lock(728513928)"))
            rows = await session.scalars(select(RuntimeConfigOverride).where(RuntimeConfigOverride.key.like("provider_%_policy")))
            overrides = {row.key: row.value for row in rows}
            overrides[policy_key(provider_id)] = value.model_dump(mode="json")
            self.validate(overrides)
            await session.execute(
                insert(RuntimeConfigOverride)
                .values(key=policy_key(provider_id), value=value.model_dump(mode="json"))
                .on_conflict_do_update(index_elements=[RuntimeConfigOverride.key], set_={"value": value.model_dump(mode="json")})
            )
            await session.execute(text("SELECT pg_notify('phaze_runtime_config', '')"))
        await self.refresh()
        logger.info("remote_policy.changed", provider_id=provider_id, actor=actor, **value.model_dump())
