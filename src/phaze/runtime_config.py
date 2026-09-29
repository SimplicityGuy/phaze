"""Hot-reloadable runtime configuration: the RELOADABLE key set, layered, validated, swapped.

The design is ``docs/design/0019-runtime-config-hot-reload.md``; this module is its core
(``phaze-mvq8z.4``). Everything outside :data:`RELOADABLE_KEYS` keeps today's static path
through :func:`phaze.config.get_settings` and is read once at process start.

**Layers, highest wins, per key** (ADR §3):

1. ``override`` -- the DB override table, through an injected async provider
   (:meth:`RuntimeConfigStore.set_override_provider`; the table itself is ``phaze-mvq8z.6``);
2. ``file`` -- ``<runtime_config_dir>/runtime.toml``, flat top-level ``key = value`` pairs;
3. ``env`` -- the process's settings as resolved at start (env / ``*_FILE`` / ``.env``), or for
   the two thread keys the ``TF_NUM_INTRAOP_THREADS`` / ``OMP_NUM_THREADS`` env vars;
4. ``default`` -- the settings defaults, and :func:`~phaze.services.analysis_sizing.derive_sizing`
   for the sizing defaults.

The ``env`` layer is fixed for the life of the process -- a container's environment does not
change under it -- so a reload re-reads only the ``file`` and ``override`` layers.

**A reload** (:meth:`RuntimeConfigStore.reload`) reads the override provider on the loop, then
builds and validates the candidate OFF the loop (file IO, TOML parse, pydantic, the core
detection behind the sizing check), then runs registered validators, and only then swaps the
snapshot reference -- one attribute assignment, so a reader sees the old snapshot or the new
one, never a mixture. Anything that fails before the swap REJECTS the reload and keeps the
last-good snapshot. Every attempt emits one structured log line and the
``phaze.config.*`` metrics.

A key offered through the ``file`` or ``override`` layer that is not reloadable rejects the
WHOLE reload -- fail closed, one clear error, nothing partially applied:

* a restart-only settings key (a DB URL, a token, the agent lane, ...) -> ``requires restart``.
  Only its NAME is ever reported: several of these carry credentials;
* ``backends`` / ``buckets`` -> configured in ``backends.toml`` (``phaze-mvq8z.8``), not here;
* anything else -> an unknown key, so a typo is an error rather than a silent no-op.

**Appliers** (:meth:`RuntimeConfigStore.register_applier`) are how a subsystem adopts a new
snapshot: ``applier(old, new)``, sync or async. Each applier remembers the snapshot it last
applied successfully, and is called only when the store's snapshot differs from that one:

* a reload whose snapshot is value-identical to every applier's last one calls nothing;
* each applier runs once per real change, in registration order, with ``old`` being the
  snapshot IT last adopted -- not necessarily the store's previous one;
* an applier that raises is logged and reported (outcome ``partial``, the success gauge goes
  to 0) and does NOT stop the appliers after it, and does NOT un-swap the snapshot: the
  snapshot is the operator's validated intent, and rolling it back would leave every applier
  that DID succeed on a config the store no longer reports. The failed applier keeps its old
  snapshot and is retried with it on the next reload, even one where nothing else changed.

**Validators** (:meth:`RuntimeConfigStore.register_validator`) are the pre-swap veto for checks
this module cannot make itself -- ``phaze-mvq8z.8``'s "a backend with in-flight ``cloud_job``
rows cannot be removed" is the motivating one. Sync or async; raising rejects the reload.

**Registry reload hooks** (:meth:`RuntimeConfigStore.register_registry_reload_hook`) are for a
subsystem whose reloadable state lives OUTSIDE this snapshot entirely and therefore never moves
its digest -- ``backends``/``buckets`` (``phaze-mvq8z.8``): TOML-only, deliberately excluded from
:class:`RuntimeConfig` (see :data:`_BACKENDS_TOML_KEYS`), so a backends.toml-only edit changes no
value an applier/validator would ever see. A registry hook runs on EVERY :meth:`reload` attempt,
regardless of whether the candidate's digest differs from the last one, and owns its own change
detection and error handling -- ``reload()`` awaits each hook but does not let one affect the
:class:`ReloadResult` or another hook; see :mod:`phaze.runtime_config_backends`.

Registering against the process-wide store::

    store = get_runtime_config_store()
    store.register_applier("analysis_semaphore", lambda old, new: limiter.resize(new.worker_process_pool_size))
    result = await store.reload("sighup")  # what phaze-mvq8z.5's trigger calls
    current().analysis_stall_timeout_sec     # what a per-job reader calls
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import inspect
import json
import os
from pathlib import Path
import time
import tomllib
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
import structlog

from phaze.config import AgentSettings, BaseSettings, ControlSettings, get_settings
from phaze.config_base import derive_analysis_job_heartbeat_sec
from phaze.logging_config import KNOWN_LOG_LEVELS
from phaze.services.analysis_sizing import INTRA_OP_ENV, OMP_ENV, derive_sizing
from phaze.telemetry.instruments import add, set_gauge


logger = structlog.get_logger(__name__)

ReloadSource = Literal["startup", "sighup", "file", "api", "poll"]
Layer = Literal["override", "file", "env", "default"]
ReloadOutcome = Literal["applied", "unchanged", "rejected", "partial"]

RUNTIME_TOML_NAME: Final = "runtime.toml"

#: Applier / validator signatures. Either may return an awaitable, which is awaited.
Applier = Callable[["RuntimeConfig", "RuntimeConfig"], Awaitable[None] | None]
Validator = Callable[["RuntimeConfig"], Awaitable[None] | None]
#: The DB-override layer: key -> value for every override currently set.
OverrideProvider = Callable[[], Awaitable[Mapping[str, Any]]]
#: A hook for state that lives outside RuntimeConfig (backends.toml, phaze-mvq8z.8) and must
#: still be driven by the same triggers. Always async; runs on every reload() attempt.
RegistryReloadHook = Callable[["ReloadSource"], Awaitable[None]]


class RuntimeConfig(BaseModel):
    """One immutable snapshot of every reloadable key.

    ``strict`` so a layer cannot smuggle ``"4"`` or ``True`` into an int field: TOML and a
    JSON override both carry real ints, and a string there is an operator mistake to report.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    # Validated against phaze.logging_config.KNOWN_LOG_LEVELS -- a fixed set, not the
    # process-wide logging registry, so the accepted set is identical in every process type
    # (phaze-mvq8z.16). configure_logging resolves every accepted name to its real stdlib level.
    log_level: str
    worker_max_jobs: int = Field(ge=1)
    lane_analyze_concurrency: int = Field(ge=1)
    lane_meta_concurrency: int = Field(ge=1)
    lane_io_concurrency: int = Field(ge=1)
    worker_process_pool_size: int = Field(ge=1)
    # Thread caps for analysis children started AFTER a change (ADR §7). The inter-op pool is
    # deliberately absent: it is pinned at 1 as the memory term (analysis_sizing._INTER_OP_THREADS).
    analysis_intra_op_threads: int = Field(ge=1)
    analysis_omp_threads: int = Field(ge=1)
    analysis_stall_timeout_sec: int = Field(gt=0, lt=86400)
    cloud_route_threshold_sec: int = Field(gt=0, lt=86400)

    @field_validator("log_level", mode="before")
    @classmethod
    def _known_level(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        name = value.strip().upper()
        if name not in KNOWN_LOG_LEVELS:
            raise ValueError(f"unknown log level {value!r}")
        return name

    @property
    def analysis_job_heartbeat_sec(self) -> int:
        """The SAQ ``process_file`` heartbeat deadline derived from THIS snapshot's stall threshold.

        The live twin of ``BaseSettings.analysis_job_heartbeat_sec`` (phaze-mvq8z.21): the outer
        deadline must follow the stall threshold a job's watchdog is actually armed with, so every
        reader that arms or backstops that watchdog derives it from the same snapshot. A property,
        not a field: it is not a key an operator sets, and it stays out of ``digest()``.
        """
        return derive_analysis_job_heartbeat_sec(self.analysis_stall_timeout_sec)

    def digest(self) -> str:
        """Content hash of the VALUES -- equal digests mean nothing an applier reads changed."""
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


RELOADABLE_KEYS: Final[frozenset[str]] = frozenset(RuntimeConfig.model_fields)

#: Registry keys that ARE reloadable, but from backends.toml (phaze-mvq8z.8), never runtime.toml.
_BACKENDS_TOML_KEYS: Final[frozenset[str]] = frozenset({"backends", "buckets"})

#: Every settings field, on either role, that a reload may not touch.
RESTART_ONLY_KEYS: Final[frozenset[str]] = frozenset(
    (set(ControlSettings.model_fields) | set(AgentSettings.model_fields)) - RELOADABLE_KEYS - _BACKENDS_TOML_KEYS
)

#: The keys that are not settings fields, and where their env layer and default come from.
_THREAD_ENV: Final[Mapping[str, str]] = MappingProxyType({"analysis_intra_op_threads": INTRA_OP_ENV, "analysis_omp_threads": OMP_ENV})

#: Keys whose sizing is checked for core oversubscription.
_SIZING_KEYS: Final[frozenset[str]] = frozenset(
    {"worker_max_jobs", "lane_analyze_concurrency", "worker_process_pool_size", "analysis_intra_op_threads", "analysis_omp_threads"}
)


def _thread_demand(config: RuntimeConfig) -> int:
    """Analysis threads this config can run at once on one agent: concurrent children x threads each.

    Concurrent children are bounded by the analyze lane's SAQ concurrency (itself capped by
    ``worker_max_jobs``, agent_worker's ``min(lane knob, worker_max_jobs)``) and by the
    ``worker_process_pool_size`` semaphore. A child's threads are the larger of its TF intra-op
    and OpenMP pools. This is the lane-mode figure; an unlaned all-mode worker's concurrency is
    ``worker_max_jobs`` alone, which this can under-count.
    """
    children = min(config.lane_analyze_concurrency, config.worker_max_jobs, config.worker_process_pool_size)
    return children * max(config.analysis_intra_op_threads, config.analysis_omp_threads)


class ReloadRejectedError(Exception):
    """A candidate snapshot failed before the swap. The last-good snapshot stays in force."""

    def __init__(self, message: str, *, restart_required: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.restart_required = restart_required


@dataclass(frozen=True)
class ConfigSnapshot:
    """What a reader sees: the values, which layer each came from, and their digest -- swapped as one."""

    config: RuntimeConfig
    sources: Mapping[str, Layer]
    digest: str


@dataclass(frozen=True)
class ReloadResult:
    """One reload attempt, as logged and as reported to phaze-mvq8z.9's effective-config field."""

    source: ReloadSource
    outcome: ReloadOutcome
    #: key -> (old, new), for every key whose VALUE changed. Empty on a rejection.
    changes: Mapping[str, tuple[Any, Any]] = field(default_factory=dict)
    error: str | None = None
    #: Restart-only keys the rejected reload offered -- names only, never values.
    restart_required: tuple[str, ...] = ()
    #: applier name -> error, for every applier that raised.
    applier_errors: Mapping[str, str] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    @property
    def successful(self) -> bool:
        return self.outcome in ("applied", "unchanged")


@dataclass
class _Registration:
    fn: Applier
    last_applied: RuntimeConfig


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']} (got {error['input']!r})" for error in exc.errors())


class RuntimeConfigStore:
    """The process's reloadable config: one current snapshot, the reload pipeline, and its hooks.

    ``check_sizing`` gates :meth:`_check_sizing`, the core-oversubscription check. It is only
    meaningful on a host that RUNS analysis children -- an agent; see :func:`get_runtime_config_store`.

    Construction resolves the ``env`` / ``default`` layers only and raises ``ValueError`` if
    they fail validation -- a start-time fault, like any other settings error. Call
    ``await store.reload("startup")`` once the process can reach its override source to fold
    in the ``file`` and ``override`` layers; a rejection there leaves the process running on
    env/defaults, reported, rather than crashing it.
    """

    def __init__(
        self,
        settings: BaseSettings,
        *,
        runtime_toml: Path | None,
        override_provider: OverrideProvider | None = None,
        env: Mapping[str, str] | None = None,
        physical_cores: Callable[[], int] | None = None,
        check_sizing: bool = True,
    ) -> None:
        self._runtime_toml = runtime_toml
        self._check_sizing_enabled = check_sizing
        self._override_provider = override_provider
        self._physical_cores = physical_cores or (lambda: derive_sizing().physical_cores)
        base_values, base_sources = self._resolve_base(settings, os.environ if env is None else env)
        try:
            self._base_config = RuntimeConfig.model_validate(base_values)
        except ValidationError as exc:
            raise ValueError(f"reloadable settings are invalid at start: {_format_validation_error(exc)}") from exc
        self._base_values = self._base_config.model_dump()
        self._base_sources = base_sources
        self._state = ConfigSnapshot(self._base_config, MappingProxyType(dict(base_sources)), self._base_config.digest())
        self._appliers: dict[str, _Registration] = {}
        self._validators: dict[str, Validator] = {}
        self._registry_hooks: dict[str, RegistryReloadHook] = {}
        self._lock = asyncio.Lock()
        self._last_result: ReloadResult | None = None

    # Reading

    def current(self) -> RuntimeConfig:
        return self._state.config

    def snapshot(self) -> ConfigSnapshot:
        return self._state

    @property
    def last_result(self) -> ReloadResult | None:
        return self._last_result

    # Hooks

    def register_applier(self, name: str, applier: Applier) -> None:
        """Adopt future snapshots via ``applier(old, new)``.

        The applier is assumed to have been built from the CURRENT snapshot, so it is not
        called now -- only on the next reload that changes a value.
        """
        if name in self._appliers:
            raise ValueError(f"applier {name!r} is already registered")
        self._appliers[name] = _Registration(applier, self._state.config)

    def register_validator(self, name: str, validator: Validator) -> None:
        """Veto a candidate before the swap by raising. Runs only when the candidate's values differ."""
        if name in self._validators:
            raise ValueError(f"validator {name!r} is already registered")
        self._validators[name] = validator

    def set_override_provider(self, provider: OverrideProvider | None) -> None:
        self._override_provider = provider

    async def preview(self, overrides: Mapping[str, Any]) -> RuntimeConfig:
        """Validate a candidate override set OFF the loop, WITHOUT swapping (phaze-mvq8z.6).

        ``overrides`` is the FULL override-layer map the caller intends to persist -- e.g. the
        current DB overrides with one key added, changed, or removed -- merged over the ``file``
        / ``env`` / ``default`` layers exactly the way :meth:`reload` merges the override layer,
        so the verdict here is the SAME one a real ``reload("api")`` would reach for that exact
        override set. Raises :class:`ReloadRejectedError` on an invalid candidate (unknown key,
        restart-only key, wrong type, or oversubscribed sizing); never swaps the live snapshot.

        The admin API (phaze-mvq8z.6) calls this BEFORE writing to the DB override table, so an
        invalid override is rejected with nothing stored -- writing first and rolling back on
        failure would leave a window where a bad value was live, or require a second write to
        undo it.
        """
        candidate = await asyncio.to_thread(self._build, overrides)
        return candidate.config

    def register_registry_reload_hook(self, name: str, hook: RegistryReloadHook) -> None:
        """Run ``hook(source)`` on every :meth:`reload` attempt, unconditionally.

        For state that lives OUTSIDE :class:`RuntimeConfig` and so never moves its digest --
        ``backends.toml`` (``phaze-mvq8z.8``) is the motivating case; see the module docstring's
        "Registry reload hooks" section. The hook manages its own change detection and its own
        error handling: a hook that raises is caught and logged here, never allowed to affect
        this reload's :class:`ReloadResult` or stop a later hook from running.
        """
        if name in self._registry_hooks:
            raise ValueError(f"registry reload hook {name!r} is already registered")
        self._registry_hooks[name] = hook

    # The pipeline

    async def reload(self, source: ReloadSource) -> ReloadResult:
        """Rebuild from every layer, validate, swap, apply. Never raises; the result says what happened."""
        async with self._lock:
            old = self._state
            try:
                overrides = dict(await self._override_provider()) if self._override_provider is not None else {}
                candidate = await asyncio.to_thread(self._build, overrides)
                if candidate.digest != old.digest:
                    await self._run_validators(candidate.config)
            except ReloadRejectedError as exc:
                result = ReloadResult(source=source, outcome="rejected", error=str(exc), restart_required=exc.restart_required)
            except Exception as exc:
                # The override read, a validator or an unexpected build fault: still a rejection
                # with the last-good snapshot kept, never an exception into a signal handler.
                result = ReloadResult(source=source, outcome="rejected", error=f"{type(exc).__name__}: {exc}")
            else:
                self._state = candidate  # THE swap: one reference assignment.
                changes = {
                    key: (old_value, new_value)
                    for key, old_value in old.config.model_dump().items()
                    if (new_value := getattr(candidate.config, key)) != old_value
                }
                retried, applier_errors = await self._run_appliers(candidate.config)
                if applier_errors:
                    outcome: ReloadOutcome = "partial"
                elif changes or retried:
                    outcome = "applied"
                else:
                    outcome = "unchanged"
                result = ReloadResult(source=source, outcome=outcome, changes=changes, applier_errors=applier_errors)
            self._last_result = result
            _emit(result, self._state.digest)
            await self._run_registry_hooks(source)
            return result

    async def _run_registry_hooks(self, source: ReloadSource) -> None:
        """Every registry hook, unconditionally, isolated from each other and from ``result`` above."""
        for name, hook in self._registry_hooks.items():
            try:
                await hook(source)
            except Exception:
                logger.exception("phaze.runtime_config registry reload hook failed", hook=name)

    def _build(self, overrides: Mapping[str, Any]) -> ConfigSnapshot:
        """Resolve every layer into a validated candidate. Blocking: runs off the event loop."""
        file_values = self._read_file()
        _check_keys({**file_values, **overrides})
        values = dict(self._base_values)
        sources: dict[str, Layer] = dict(self._base_sources)
        for key, value in file_values.items():
            values[key] = value
            sources[key] = "file"
        for key, value in overrides.items():
            values[key] = value
            sources[key] = "override"
        try:
            config = RuntimeConfig.model_validate(values)
        except ValidationError as exc:
            raise ReloadRejectedError(_format_validation_error(exc)) from exc
        self._check_sizing(config, sources)
        return ConfigSnapshot(config, MappingProxyType(sources), config.digest())

    def _read_file(self) -> dict[str, Any]:
        if self._runtime_toml is None or not self._runtime_toml.is_file():
            return {}
        try:
            with self._runtime_toml.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ReloadRejectedError(f"{self._runtime_toml}: {exc}") from exc

    def _check_sizing(self, config: RuntimeConfig, sources: Mapping[str, Layer]) -> None:
        """Reject a file/override sizing that oversubscribes the host's physical cores (ADR §6).

        Judged against the start-time snapshot, not an absolute: a deployment whose env ALREADY
        oversubscribes keeps running, and a reload may move it toward coherence, but may not
        move it further away. Env/default sizing alone is never checked here -- it is what the
        process was started with, and rejecting it would reject every reload.
        """
        if not self._check_sizing_enabled or not any(sources[key] in ("file", "override") for key in _SIZING_KEYS):
            return
        demand = _thread_demand(config)
        cores = self._physical_cores()
        if demand > cores and demand > _thread_demand(self._base_config):
            raise ReloadRejectedError(
                f"sizing oversubscribes {cores} physical cores: {demand} analysis threads "
                f"(min(lane_analyze_concurrency, worker_max_jobs, worker_process_pool_size) x max(intra-op, omp) threads); "
                f"derive_sizing's policy is threads x concurrency <= physical cores"
            )

    async def _run_validators(self, config: RuntimeConfig) -> None:
        for name, validator in self._validators.items():
            try:
                outcome = validator(config)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:
                raise ReloadRejectedError(f"validator {name!r} rejected the reload: {exc}") from exc

    async def _run_appliers(self, new: RuntimeConfig) -> tuple[bool, dict[str, str]]:
        """Bring every applier up to ``new``. Returns (any applier ran, errors by applier name)."""
        ran = False
        errors: dict[str, str] = {}
        new_digest = new.digest()
        for name, registration in self._appliers.items():
            if registration.last_applied.digest() == new_digest:
                continue
            ran = True
            try:
                outcome = registration.fn(registration.last_applied, new)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
                logger.exception("phaze.runtime_config applier failed", applier=name)
            else:
                registration.last_applied = new
        return ran, errors

    def _resolve_base(self, settings: BaseSettings, env: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Layer]]:
        """The ``env`` / ``default`` layers, per key."""
        values: dict[str, Any] = {}
        sources: dict[str, Layer] = {}
        explicitly_set = settings.model_fields_set
        for key in RuntimeConfig.model_fields:
            if key in _THREAD_ENV:
                continue
            if key in type(settings).model_fields:
                values[key] = getattr(settings, key)
                sources[key] = "env" if key in explicitly_set else "default"
            else:
                # A control-only key on an agent process: the control default, reported as such.
                values[key] = ControlSettings.model_fields[key].default
                sources[key] = "default"
        # configure_logging falls back to INFO on an unknown name rather than raising, so the
        # base layer reports the level actually in force instead of refusing to start. Checked
        # against the fixed KNOWN_LOG_LEVELS set (phaze-mvq8z.16), not the process-wide logging
        # registry, so a value like PHAZE_LOG_LEVEL=TRACE falls back to INFO identically in
        # every process type, regardless of whether that process happens to have uvicorn's
        # TRACE level registered.
        if str(values["log_level"]).strip().upper() not in KNOWN_LOG_LEVELS:
            logger.warning("phaze.runtime_config unknown log level at start; INFO is in force", log_level=values["log_level"])
            values["log_level"] = "INFO"
        sizing = derive_sizing(self._physical_cores())
        derived = {"analysis_intra_op_threads": sizing.intra_op_threads, "analysis_omp_threads": sizing.omp_threads}
        for key, env_name in _THREAD_ENV.items():
            raw = env.get(env_name, "").strip()
            if raw:
                try:
                    values[key] = int(raw)
                except ValueError:
                    logger.warning("phaze.runtime_config ignoring a non-integer thread env var", env=env_name, value=raw)
                else:
                    sources[key] = "env"
                    continue
            values[key] = derived[key]
            sources[key] = "default"
        return values, sources


def _check_keys(offered: Mapping[str, Any]) -> None:
    """Refuse any non-reloadable key a file/override layer offers. Reports NAMES only."""
    restart_only = tuple(sorted(key for key in offered if key in RESTART_ONLY_KEYS))
    backends = sorted(key for key in offered if key in _BACKENDS_TOML_KEYS)
    unknown = sorted(key for key in offered if key not in RELOADABLE_KEYS and key not in RESTART_ONLY_KEYS and key not in _BACKENDS_TOML_KEYS)
    problems: list[str] = []
    if restart_only:
        problems.append(f"requires restart: {', '.join(restart_only)}")
    if backends:
        problems.append(f"configured in backends.toml, not runtime config: {', '.join(backends)}")
    if unknown:
        problems.append(f"unknown key(s): {', '.join(unknown)}")
    if problems:
        raise ReloadRejectedError("; ".join(problems), restart_required=restart_only)


def _emit(result: ReloadResult, digest: str) -> None:
    """The audit line and the metrics, for every attempt (ADR §6, and the §10 compensating control)."""
    fields: dict[str, Any] = {
        "source": result.source,
        "outcome": result.outcome,
        "changes": {key: {"old": old, "new": new} for key, (old, new) in result.changes.items()},
        "digest": digest,
    }
    if result.error is not None:
        fields["error"] = result.error
    if result.restart_required:
        fields["restart_required"] = list(result.restart_required)
    if result.applier_errors:
        fields["applier_errors"] = dict(result.applier_errors)
    if result.successful:
        logger.info("phaze.runtime_config reload", **fields)
    else:
        logger.warning("phaze.runtime_config reload", **fields)
    add("phaze.config.reloads", 1, source=result.source, outcome=result.outcome)
    set_gauge("phaze.config.last_reload.successful", 1.0 if result.successful else 0.0)
    if result.successful:
        set_gauge("phaze.config.last_reload.success_timestamp", result.at)


@lru_cache(maxsize=1)
def get_runtime_config_store() -> RuntimeConfigStore:
    """The process-wide store, built from :func:`get_settings` on first use.

    Sizing coherence is checked only in an AGENT process (phaze-mvq8z.19, an IMPLEMENTER decision,
    not an operator one). The sizing keys bound analysis children, and only an agent worker runs
    those (``phaze.tasks.agent_worker``), so only an agent's own cores can judge them. The api and
    control processes skip the check: judged against the API host's cores, the admin preview
    rejected values that were valid on every agent and accepted values a smaller agent then refused.
    Each agent still rejects an oversubscribing value on its own reload, keeps last-good, and
    reports the rejection back through ``effective_config.last_reload`` on its heartbeat. Validating
    against each agent's reported cores up front was the alternative, rejected here because the
    override table is fleet-wide (not per-agent) and agents differ, so there is no single verdict.
    """
    settings = get_settings()
    return RuntimeConfigStore(
        settings, runtime_toml=Path(settings.runtime_config_dir) / RUNTIME_TOML_NAME, check_sizing=isinstance(settings, AgentSettings)
    )


def current() -> RuntimeConfig:
    """The process's current reloadable config. Read it per use; never cache the result."""
    return get_runtime_config_store().current()
