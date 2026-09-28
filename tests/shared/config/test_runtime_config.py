"""The runtime-config core (phaze-mvq8z.4): layering, validate-and-swap, appliers, rejection.

Every store here is built from a REAL ``ControlSettings`` / ``AgentSettings`` constructed from
real env vars, and reads a REAL ``runtime.toml`` from ``tmp_path`` -- the env and file layers
are the production resolution paths, not stand-ins. Only the DB override layer is a fake,
because its provider is an injected seam by design (the table lands in phaze-mvq8z.6), and the
physical-core count is injected so the sizing verdicts do not depend on the machine running
the suite.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

from pydantic import AliasChoices
import pytest
from structlog.testing import capture_logs

from phaze.config import AgentSettings, ControlSettings
from phaze.runtime_config import (
    RELOADABLE_KEYS,
    RESTART_ONLY_KEYS,
    RUNTIME_TOML_NAME,
    ReloadRejectedError,
    RuntimeConfig,
    RuntimeConfigStore,
    current,
    get_runtime_config_store,
)
from phaze.services.analysis_sizing import INTRA_OP_ENV, OMP_ENV


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from phaze.config import BaseSettings


_THREAD_ENV = {"analysis_intra_op_threads": INTRA_OP_ENV, "analysis_omp_threads": OMP_ENV}
_AGENT_MIN_ENV = {
    "PHAZE_AGENT_API_URL": "http://app.runtime-config.invalid:8000",
    "PHAZE_AGENT_TOKEN": "phaze_agent_runtime-config-token-000000",
    "PHAZE_AGENT_SCAN_ROOTS": "/data/music",
    "PHAZE_QUEUE_URL": "postgresql://phaze:phaze@app.runtime-config.invalid:5432/phaze",
}


def _env_names(key: str) -> list[str]:
    alias = ControlSettings.model_fields[key].validation_alias
    names = [choice for choice in alias.choices if isinstance(choice, str)] if isinstance(alias, AliasChoices) else []
    return [*names, key]


def _toml_value(value: Any) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value)


def _write_toml(directory: Path, values: Mapping[str, Any]) -> None:
    body = "".join(f"{key} = {_toml_value(value)}\n" for key, value in values.items())
    (directory / RUNTIME_TOML_NAME).write_text(body, encoding="utf-8")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient value may reach a reloadable key's env layer except the ones a test sets."""
    for key in RELOADABLE_KEYS - _THREAD_ENV.keys():
        for name in _env_names(key):
            monkeypatch.delenv(name.upper(), raising=False)
    for name in (INTRA_OP_ENV, OMP_ENV, "PHAZE_ROLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", "/nonexistent/phaze-runtime-config/backends.toml")
    get_runtime_config_store.cache_clear()
    yield
    get_runtime_config_store.cache_clear()


class _Overrides:
    """The injected DB-override layer: a mutable mapping behind the async provider shape."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.fail: Exception | None = None

    async def __call__(self) -> Mapping[str, Any]:
        if self.fail is not None:
            raise self.fail
        return dict(self.values)


def _store(
    tmp_path: Path,
    *,
    settings: BaseSettings | None = None,
    overrides: _Overrides | None = None,
    env: Mapping[str, str] | None = None,
    cores: int = 64,
) -> RuntimeConfigStore:
    return RuntimeConfigStore(
        settings if settings is not None else ControlSettings(),
        runtime_toml=tmp_path / RUNTIME_TOML_NAME,
        override_provider=overrides,
        env=env or {},
        physical_cores=lambda: cores,
    )


# Per key: (env value, file value, override value). All distinct, all valid, none oversubscribing
# the 64 injected cores.
_LAYER_VALUES: dict[str, tuple[Any, Any, Any]] = {
    "log_level": ("WARNING", "ERROR", "DEBUG"),
    "worker_max_jobs": (5, 6, 7),
    "lane_analyze_concurrency": (13, 14, 15),
    "lane_meta_concurrency": (3, 5, 6),
    "lane_io_concurrency": (5, 6, 7),
    "worker_process_pool_size": (2, 3, 5),
    "analysis_intra_op_threads": (2, 3, 1),
    "analysis_omp_threads": (2, 3, 1),
    "analysis_stall_timeout_sec": (600, 700, 800),
    "cloud_route_threshold_sec": (3600, 3700, 3800),
}


def test_the_layer_table_covers_every_reloadable_key() -> None:
    assert set(_LAYER_VALUES) == RELOADABLE_KEYS


@pytest.mark.parametrize("key", sorted(_LAYER_VALUES))
def test_each_layer_beats_the_one_below_it_per_key(key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """default < env < file < override, for every reloadable key on its own."""
    env_value, file_value, override_value = _LAYER_VALUES[key]

    async def scenario() -> None:
        default_store = _store(tmp_path)
        default_value = getattr(default_store.current(), key)
        assert default_store.snapshot().sources[key] == "default"

        env: dict[str, str] = {}
        if key in _THREAD_ENV:
            env[_THREAD_ENV[key]] = str(env_value)
        else:
            monkeypatch.setenv(_env_names(key)[0], str(env_value))
        overrides = _Overrides()
        store = _store(tmp_path, overrides=overrides, env=env)
        assert (getattr(store.current(), key), store.snapshot().sources[key]) == (env_value, "env")
        assert env_value != default_value

        _write_toml(tmp_path, {key: file_value})
        assert (await store.reload("file")).outcome == "applied"
        assert (getattr(store.current(), key), store.snapshot().sources[key]) == (file_value, "file")

        overrides.values = {key: override_value}
        assert (await store.reload("api")).outcome == "applied"
        assert (getattr(store.current(), key), store.snapshot().sources[key]) == (override_value, "override")

        # Clearing a layer falls back to the next one down.
        overrides.values = {}
        await store.reload("api")
        assert (getattr(store.current(), key), store.snapshot().sources[key]) == (file_value, "file")
        (tmp_path / RUNTIME_TOML_NAME).unlink()
        await store.reload("file")
        assert (getattr(store.current(), key), store.snapshot().sources[key]) == (env_value, "env")

    asyncio.run(scenario())


def test_defaults_come_from_settings_and_derive_sizing(tmp_path: Path) -> None:
    store = _store(tmp_path, cores=8)
    config = store.current()
    settings = ControlSettings()
    assert config.worker_max_jobs == settings.worker_max_jobs
    assert config.lane_analyze_concurrency == settings.lane_analyze_concurrency
    # The thread caps are derive_sizing's, for the core count this store was given.
    assert (config.analysis_intra_op_threads, config.analysis_omp_threads) == (4, 4)
    assert set(store.snapshot().sources.values()) == {"default"}


def test_an_agent_process_reports_the_control_only_key_at_its_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _AGENT_MIN_ENV.items():
        monkeypatch.setenv(name, value)
    store = _store(tmp_path, settings=AgentSettings())
    assert store.current().cloud_route_threshold_sec == ControlSettings.model_fields["cloud_route_threshold_sec"].default
    assert store.snapshot().sources["cloud_route_threshold_sec"] == "default"


def test_an_unknown_start_time_log_level_reports_the_info_configure_logging_falls_back_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_LOG_LEVEL", "TRACE")
    assert _store(tmp_path).current().log_level == "INFO"


def test_a_non_integer_thread_env_var_falls_back_to_the_derived_default(tmp_path: Path) -> None:
    store = _store(tmp_path, env={OMP_ENV: "lots"}, cores=2)
    assert (store.current().analysis_omp_threads, store.snapshot().sources["analysis_omp_threads"]) == (2, "default")


def test_invalid_start_time_settings_fail_fast_naming_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_MAX_JOBS", "0")
    with pytest.raises(ValueError, match="worker_max_jobs"):
        _store(tmp_path)


@pytest.mark.parametrize(
    ("toml_body", "error_fragment"),
    [
        ("worker_max_jobs = 0\n", "worker_max_jobs"),
        ('worker_max_jobs = "4"\n', "worker_max_jobs"),
        ('log_level = "LOUD"\n', "unknown log level"),
        ("analysis_stall_timeout_sec = 90000\n", "analysis_stall_timeout_sec"),
        ("worker_max_jobs = \n", "runtime.toml"),
    ],
    ids=["below-minimum", "wrong-type", "unknown-level", "above-maximum", "broken-toml"],
)
def test_an_invalid_file_is_rejected_and_the_last_good_snapshot_kept(tmp_path: Path, toml_body: str, error_fragment: str) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        _write_toml(tmp_path, {"worker_max_jobs": 3})
        await store.reload("file")
        last_good = store.snapshot()

        (tmp_path / RUNTIME_TOML_NAME).write_text(toml_body, encoding="utf-8")
        result = await store.reload("file")

        assert result.outcome == "rejected"
        assert result.error is not None
        assert error_fragment in result.error
        assert result.changes == {}
        assert store.snapshot() is last_good
        assert store.current().worker_max_jobs == 3
        assert store.last_result is result

    asyncio.run(scenario())


def test_an_invalid_override_is_rejected_and_the_last_good_snapshot_kept(tmp_path: Path) -> None:
    async def scenario() -> None:
        overrides = _Overrides()
        store = _store(tmp_path, overrides=overrides)
        last_good = store.snapshot()
        overrides.values = {"lane_io_concurrency": -1}
        result = await store.reload("api")
        assert result.outcome == "rejected"
        assert "lane_io_concurrency" in (result.error or "")
        assert store.snapshot() is last_good

    asyncio.run(scenario())


def test_an_override_provider_that_raises_rejects_and_keeps_the_last_good_snapshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        overrides = _Overrides()
        store = _store(tmp_path, overrides=overrides)
        last_good = store.snapshot()
        overrides.fail = ConnectionError("database is down")
        result = await store.reload("poll")
        assert (result.outcome, result.error) == ("rejected", "ConnectionError: database is down")
        assert store.snapshot() is last_good

    asyncio.run(scenario())


def test_a_restart_only_key_is_reported_and_nothing_is_applied(tmp_path: Path) -> None:
    """The whole reload fails closed -- the reloadable change beside it does not land either --
    and a credential-bearing key is reported by NAME, its value never reaching the error or the log."""

    async def scenario() -> None:
        store = _store(tmp_path)
        before = store.snapshot()
        _write_toml(tmp_path, {"worker_max_jobs": 3, "database_url": "postgresql+asyncpg://phaze:hunter2@db:5432/phaze"})
        with capture_logs() as logs:
            result = await store.reload("file")

        assert result.outcome == "rejected"
        assert result.restart_required == ("database_url",)
        assert "requires restart: database_url" in (result.error or "")
        assert store.snapshot() is before
        assert store.current().worker_max_jobs != 3
        assert "hunter2" not in repr(result)
        assert "hunter2" not in repr(logs)

    asyncio.run(scenario())


def test_a_restart_only_key_via_the_override_layer_is_rejected_too(tmp_path: Path) -> None:
    async def scenario() -> None:
        overrides = _Overrides()
        store = _store(tmp_path, overrides=overrides)
        overrides.values = {"scan_path": "/elsewhere", "agent_token_prefix": "x_"}
        result = await store.reload("api")
        assert result.restart_required == ("agent_token_prefix", "scan_path")

    asyncio.run(scenario())


def test_registry_and_unknown_keys_are_rejected_with_their_own_reasons(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_job = 3\n[[backends]]\nid = 'local'\n", encoding="utf-8")
        result = await store.reload("file")
        assert result.outcome == "rejected"
        assert result.restart_required == ()
        assert "configured in backends.toml" in (result.error or "")
        assert "unknown key(s): worker_max_job" in (result.error or "")

    asyncio.run(scenario())


def test_the_key_sets_partition_the_settings_surface() -> None:
    settings_fields = set(ControlSettings.model_fields) | set(AgentSettings.model_fields)
    assert not RELOADABLE_KEYS & RESTART_ONLY_KEYS
    assert settings_fields - {"backends", "buckets"} <= RELOADABLE_KEYS | RESTART_ONLY_KEYS
    assert {"database_url", "queue_url", "redis_url", "agent_token", "scan_roots", "agent_ca_file", "runtime_config_dir"} <= RESTART_ONLY_KEYS


def test_an_oversubscribing_sizing_override_is_rejected(tmp_path: Path) -> None:
    """4 cores, derived intra-op 4: a lane of 4 analyses x 4 threads is 16 threads on 4 cores."""

    async def scenario() -> None:
        store = _store(tmp_path, cores=4)
        assert store.current().analysis_intra_op_threads == 4
        before = store.snapshot()
        _write_toml(tmp_path, {"lane_analyze_concurrency": 4})
        result = await store.reload("file")
        assert result.outcome == "rejected"
        assert "oversubscribes 4 physical cores: 16 analysis threads" in (result.error or "")
        assert store.snapshot() is before

        # The same concurrency with one thread per child is coherent, and lands.
        _write_toml(tmp_path, {"lane_analyze_concurrency": 4, "analysis_intra_op_threads": 1, "analysis_omp_threads": 1})
        assert (await store.reload("file")).outcome == "applied"

    asyncio.run(scenario())


def test_a_start_that_already_oversubscribes_may_move_toward_coherence_but_not_away(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The phaze-demo shape: an env that runs 4 x 4 = 16 threads on 4 cores keeps running."""
    monkeypatch.setenv("PHAZE_LANE_ANALYZE_CONCURRENCY", "16")

    async def scenario() -> None:
        store = _store(tmp_path, cores=4)
        _write_toml(tmp_path, {"worker_process_pool_size": 2})  # 16 -> 8 threads: still over, but better
        assert (await store.reload("file")).outcome == "applied"
        _write_toml(tmp_path, {"worker_process_pool_size": 8})  # 16 -> 32: worse than the start
        assert (await store.reload("file")).outcome == "rejected"

    asyncio.run(scenario())


def test_appliers_run_once_with_old_and_new_and_only_on_a_real_change(tmp_path: Path) -> None:
    calls: list[tuple[str, int, int]] = []

    async def async_applier(old: RuntimeConfig, new: RuntimeConfig) -> None:
        await asyncio.sleep(0)
        calls.append(("async", old.worker_max_jobs, new.worker_max_jobs))

    async def scenario() -> None:
        store = _store(tmp_path)
        start = store.current().worker_max_jobs
        store.register_applier("sync", lambda old, new: calls.append(("sync", old.worker_max_jobs, new.worker_max_jobs)))
        store.register_applier("async", async_applier)
        assert calls == []  # registering adopts the current snapshot; nothing is called

        _write_toml(tmp_path, {"worker_max_jobs": 3})
        result = await store.reload("file")
        assert (result.outcome, result.changes) == ("applied", {"worker_max_jobs": (start, 3)})
        assert calls == [("sync", start, 3), ("async", start, 3)]

        # Same content: a hash-equal reload is a no-op.
        result = await store.reload("sighup")
        assert (result.outcome, result.changes) == ("unchanged", {})
        # Different bytes, same values: still a no-op, because the digest is over values.
        (tmp_path / RUNTIME_TOML_NAME).write_text("# retuned\nworker_max_jobs = 3\n", encoding="utf-8")
        assert (await store.reload("file")).outcome == "unchanged"
        assert len(calls) == 2

    asyncio.run(scenario())


def test_a_source_only_change_swaps_the_snapshot_but_calls_no_applier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_MAX_JOBS", "3")
    calls: list[object] = []

    async def scenario() -> None:
        store = _store(tmp_path)
        store.register_applier("probe", lambda _old, new: calls.append(new))
        _write_toml(tmp_path, {"worker_max_jobs": 3})
        assert (await store.reload("file")).outcome == "unchanged"
        assert store.snapshot().sources["worker_max_jobs"] == "file"
        assert calls == []

    asyncio.run(scenario())


def test_a_failing_applier_is_isolated_reported_and_retried(tmp_path: Path) -> None:
    """Defined semantics: the swap stands, later appliers still run, the failure is reported as
    ``partial``, and the failed applier alone is retried -- with ITS old snapshot -- next time."""
    seen: list[tuple[str, int, int]] = []
    broken = {"flag": True}

    def flaky(old: RuntimeConfig, new: RuntimeConfig) -> None:
        seen.append(("flaky", old.worker_max_jobs, new.worker_max_jobs))
        if broken["flag"]:
            raise RuntimeError("pool refused to resize")

    async def scenario() -> None:
        store = _store(tmp_path)
        start = store.current().worker_max_jobs
        store.register_applier("flaky", flaky)
        store.register_applier("steady", lambda old, new: seen.append(("steady", old.worker_max_jobs, new.worker_max_jobs)))

        _write_toml(tmp_path, {"worker_max_jobs": 3})
        result = await store.reload("file")
        assert result.outcome == "partial"
        assert result.applier_errors == {"flaky": "RuntimeError: pool refused to resize"}
        assert store.current().worker_max_jobs == 3  # not un-swapped
        assert seen == [("flaky", start, 3), ("steady", start, 3)]

        broken["flag"] = False
        seen.clear()
        result = await store.reload("sighup")  # nothing changed, but flaky is behind
        assert (result.outcome, result.changes, result.applier_errors) == ("applied", {}, {})
        assert seen == [("flaky", start, 3)]

        seen.clear()
        assert (await store.reload("sighup")).outcome == "unchanged"
        assert seen == []

    asyncio.run(scenario())


def test_duplicate_hook_names_are_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_applier("a", lambda _old, _new: None)
    store.register_validator("v", lambda _config: None)
    store.register_registry_reload_hook("r", lambda _source: asyncio.sleep(0))
    with pytest.raises(ValueError, match="already registered"):
        store.register_applier("a", lambda _old, _new: None)
    with pytest.raises(ValueError, match="already registered"):
        store.register_validator("v", lambda _config: None)
    with pytest.raises(ValueError, match="already registered"):
        store.register_registry_reload_hook("r", lambda _source: asyncio.sleep(0))


def test_a_registry_reload_hook_runs_on_every_reload_even_when_nothing_in_runtimeconfig_changed(tmp_path: Path) -> None:
    """The gap phaze-mvq8z.8 closes: backends.toml lives OUTSIDE RuntimeConfig, so a hook meant
    to notice it must run unconditionally -- never gated behind RuntimeConfig's own digest, the
    way ``register_applier``/``register_validator`` are (see ``test_a_source_only_change_swaps_
    the_snapshot_but_calls_no_applier`` for the applier side of that same gate)."""
    seen: list[str] = []

    async def probe(source: str) -> None:
        seen.append(source)

    async def scenario() -> None:
        store = _store(tmp_path)
        store.register_registry_reload_hook("probe", probe)

        assert (await store.reload("sighup")).outcome == "unchanged"
        assert (await store.reload("file")).outcome == "unchanged"
        assert (await store.reload("api")).outcome == "unchanged"
        assert seen == ["sighup", "file", "api"]

    asyncio.run(scenario())


def test_a_raising_registry_reload_hook_is_isolated_and_does_not_affect_the_result_or_other_hooks(tmp_path: Path) -> None:
    seen: list[str] = []

    async def broken(_source: str) -> None:
        raise RuntimeError("backends.toml is unreadable")

    async def steady(source: str) -> None:
        seen.append(source)

    async def scenario() -> None:
        store = _store(tmp_path)
        store.register_registry_reload_hook("broken", broken)
        store.register_registry_reload_hook("steady", steady)

        result = await store.reload("sighup")
        # The hook failure is invisible to the RuntimeConfig-level result -- it manages its own
        # error handling and reporting (phaze.runtime_config_backends logs its own line).
        assert result.outcome == "unchanged"
        assert seen == ["sighup"]

    asyncio.run(scenario())


def test_registry_reload_hooks_run_serialized_with_the_rest_of_reload(tmp_path: Path) -> None:
    """Hooks run inside the SAME lock as the rest of reload() -- two concurrent triggers still
    invoke a hook once each, never interleaved, mirroring test_concurrent_reloads_are_serialized."""
    calls: list[str] = []

    async def hook(source: str) -> None:
        calls.append(f"start:{source}")
        await asyncio.sleep(0.01)
        calls.append(f"end:{source}")

    async def scenario() -> None:
        store = _store(tmp_path)
        store.register_registry_reload_hook("probe", hook)
        await asyncio.gather(store.reload("sighup"), store.reload("file"))
        # Each hook invocation completes (start, end) before the next one starts.
        assert calls in (
            ["start:sighup", "end:sighup", "start:file", "end:file"],
            ["start:file", "end:file", "start:sighup", "end:sighup"],
        )

    asyncio.run(scenario())


def test_a_validator_can_veto_a_change_and_is_not_consulted_when_nothing_changed(tmp_path: Path) -> None:
    consulted: list[int] = []

    async def no_small_pools(config: RuntimeConfig) -> None:
        consulted.append(config.worker_process_pool_size)
        if config.worker_process_pool_size < 2:
            raise ValueError("in-flight work needs two slots")

    async def scenario() -> None:
        store = _store(tmp_path)
        store.register_validator("no_small_pools", no_small_pools)
        assert (await store.reload("sighup")).outcome == "unchanged"
        assert consulted == []

        before = store.snapshot()
        _write_toml(tmp_path, {"worker_process_pool_size": 1})
        result = await store.reload("file")
        assert result.outcome == "rejected"
        assert "validator 'no_small_pools' rejected the reload: in-flight work needs two slots" in (result.error or "")
        assert store.snapshot() is before

        store.register_validator("sync_ok", lambda _config: None)
        _write_toml(tmp_path, {"worker_process_pool_size": 3})
        assert (await store.reload("file")).outcome == "applied"

    asyncio.run(scenario())


def test_a_late_override_provider_is_consulted_from_then_on(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        overrides = _Overrides()
        overrides.values = {"lane_meta_concurrency": 9}
        store.set_override_provider(overrides)
        await store.reload("api")
        assert store.current().lane_meta_concurrency == 9

    asyncio.run(scenario())


def test_concurrent_readers_never_observe_a_torn_snapshot(tmp_path: Path) -> None:
    """Two generations whose every value AND every source differ, swapped back and forth while
    reader threads read: each read must be wholly one generation -- values and sources together."""
    generation_a = {key: _LAYER_VALUES[key][1] for key in RELOADABLE_KEYS}
    generation_b = {key: _LAYER_VALUES[key][2] for key in RELOADABLE_KEYS}
    stop = threading.Event()
    torn: list[str] = []
    reads = [0]

    def reader(store: RuntimeConfigStore) -> None:
        while not stop.is_set():
            snap = store.snapshot()
            values = snap.config.model_dump()
            layers = set(snap.sources.values())
            is_a = values == generation_a and layers == {"file"}
            is_b = values == generation_b and layers == {"override"}
            if not (is_a or is_b or layers == {"default"}):
                torn.append(f"{values} {dict(snap.sources)}")
            reads[0] += 1
            time.sleep(0)  # yield the GIL so the reload thread is not starved

    async def scenario() -> None:
        overrides = _Overrides()
        _write_toml(tmp_path, generation_a)
        store = _store(tmp_path, overrides=overrides)
        threads = [threading.Thread(target=reader, args=(store,)) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            for index in range(60):
                overrides.values = generation_b if index % 2 else {}
                result = await store.reload("api")
                assert result.outcome == "applied", result.error
        finally:
            stop.set()
            for thread in threads:
                thread.join()

    asyncio.run(scenario())
    assert reads[0] > 0
    assert torn == []


def test_concurrent_reloads_are_serialized(tmp_path: Path) -> None:
    """Two triggers at once (a SIGHUP racing a file event) run one after the other, never interleaved."""
    active = [0]
    peak = [0]

    async def slow_provider() -> Mapping[str, Any]:
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.01)
        active[0] -= 1
        return {}

    async def scenario() -> None:
        store = _store(tmp_path)
        store.set_override_provider(slow_provider)
        results = await asyncio.gather(store.reload("sighup"), store.reload("file"), store.reload("api"))
        assert [result.outcome for result in results] == ["unchanged"] * 3

    asyncio.run(scenario())
    assert peak[0] == 1


def test_the_process_store_reads_runtime_toml_from_the_configured_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from phaze.config import get_settings  # the cache is cleared around this test

    monkeypatch.setenv("PHAZE_RUNTIME_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        _write_toml(tmp_path, {"lane_io_concurrency": 11})
        store = get_runtime_config_store()
        assert get_runtime_config_store() is store
        asyncio.run(store.reload("startup"))
        assert current().lane_io_concurrency == 11
    finally:
        get_settings.cache_clear()


# preview() (phaze-mvq8z.6): validates a candidate override set off the loop WITHOUT swapping --
# the admin API calls this BEFORE writing to the DB override table, so an invalid override is
# rejected with nothing stored.


def test_preview_accepts_a_valid_candidate_without_swapping(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        before = store.current()

        candidate = await store.preview({"worker_max_jobs": 9})

        assert candidate.worker_max_jobs == 9
        # No swap: the live snapshot is untouched, and its digest is unchanged.
        assert store.current() is before
        assert store.current().worker_max_jobs != 9

    asyncio.run(scenario())


def test_preview_rejects_an_unknown_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(ReloadRejectedError, match="unknown key"):
            await store.preview({"not_a_real_key": 1})

    asyncio.run(scenario())


def test_preview_rejects_a_restart_only_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        [restart_only_key] = list(RESTART_ONLY_KEYS)[:1]
        with pytest.raises(ReloadRejectedError, match="requires restart"):
            await store.preview({restart_only_key: "anything"})

    asyncio.run(scenario())


def test_preview_rejects_wrong_type_under_strict_validation(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(ReloadRejectedError):
            await store.preview({"worker_max_jobs": "4"})  # a string, not an int -- strict=True

    asyncio.run(scenario())


def test_preview_rejects_sizing_that_oversubscribes_cores(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path, cores=2)
        with pytest.raises(ReloadRejectedError, match="oversubscribes"):
            # 100 concurrent children x 10 threads each on a 2-core box.
            await store.preview({"lane_analyze_concurrency": 100, "worker_max_jobs": 100, "worker_process_pool_size": 100})

    asyncio.run(scenario())


def test_preview_agrees_with_a_real_reload_for_the_same_candidate(tmp_path: Path) -> None:
    """preview()'s verdict matches what reload("api") actually does for the identical override set."""

    async def scenario() -> None:
        overrides = _Overrides()
        store = _store(tmp_path, overrides=overrides)

        previewed = await store.preview({"worker_max_jobs": 11})

        overrides.values = {"worker_max_jobs": 11}
        result = await store.reload("api")

        assert result.outcome == "applied"
        assert store.current().worker_max_jobs == previewed.worker_max_jobs == 11

    asyncio.run(scenario())
