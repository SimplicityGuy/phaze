"""The dedicated, paired unit-test file for ``src/phaze/config.py`` (phaze-ptlk8).

``config.py`` is the single most-leveraged module in the repo (126 dependents per the
2026-08-11 bug-hunt retrospective) and, until this file, had no test file that paired with
it by name -- only scattered coverage spread across a dozen behavior-focused files under
``tests/shared/config/``, ``tests/shared/core/`` and ``tests/agents/config/`` (e.g.
``test_backend_registry.py``, ``test_redis_password_encoding.py``,
``test_config_role_split.py``, ``test_push_config.py``). That scattered coverage is real and
is NOT duplicated here -- this file exists to (a) be the canonical paired file a hotspot
checker (Repowise) can find by name, and (b) close the ACTUAL gaps that survey left:

* the module-private helpers (``_direct_env_names``, ``_resolution_env``) had no direct
  unit tests of their own branches;
* three ``mode="before"`` validators' defensive non-dict-input branches
  (``_resolve_secret_files``, ``_load_backend_registry``) were never exercised, nor was the
  "referenced field not on this model" skip in ``_resolve_secret_files``;
* ``_apply_redis_password``'s "redis_url doesn't even parse" no-op branch was untested;
* ``_strip_sqlalchemy_driver``'s ``postgresql+psycopg://`` arm (only the ``+asyncpg`` arm was
  exercised elsewhere) and its "non-matching value passes through for pydantic to reject" arm;
* ``_build_default_settings``'s ``PHAZE_ROLE=agent`` arm (distinct from -- and never exercised
  by -- ``get_settings()``'s own role dispatch, which IS covered in ``test_config_role_split.py``);
* most notably, ``_validate_registry``'s TOP-LEVEL duplicate-``[[buckets]]``-id guard (WR-03):
  every existing registry test for duplicate ids covers either a duplicate WITHIN one backend's
  own ``buckets`` list (phaze-ru9oe) or a duplicate compute ``agent_ref`` (D-04) or a duplicate
  BACKEND id (phaze-1sgee) -- none constructs two independent ``[[buckets]]`` entries sharing an
  id, which is the exact "copy-paste id typo" scenario WR-03's docstring describes.

Before this file, ``pytest --cov=phaze.config`` across the scattered suite already measured
97.44% (312 stmts, 8 missed: lines 49, 136, 144, 234, 269, 693, 736, 1619) -- config.py was
under-tested by naming convention, not by raw line coverage. Every test below targets one of
those 8 missed lines, or one of the acceptance criteria's explicit call-outs (registry
duplicate/malformed entries, hostile env values, settings-object invariants).

No DB, no Redis required -- pure pydantic-settings / module-level construction tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from pydantic import AliasChoices, SecretStr, ValidationError
import pytest

from phaze.config import (
    BaseSettings,
    ControlSettings,
    Role,
    Settings,
    _build_default_settings,
    _direct_env_names,
    _resolution_env,
    get_settings,
    settings,
)


# --------------------------------------------------------------------------- #
# `_direct_env_names`: the env-var-name resolver behind the `<VAR>_FILE` secret
# convention. Duck-typed against anything carrying a `.validation_alias` --
# real pydantic FieldInfo objects are exercised elsewhere via full model
# construction; these tests isolate the three alias shapes directly.
# --------------------------------------------------------------------------- #
def test_direct_env_names_with_alias_choices_appends_bare_field_name() -> None:
    """An `AliasChoices` alias yields its choices, then the bare field name if absent."""
    field_info = SimpleNamespace(validation_alias=AliasChoices("FOO", "BAR"))
    assert _direct_env_names("my_field", field_info) == ["FOO", "BAR", "my_field"]


def test_direct_env_names_with_alias_choices_already_containing_field_name() -> None:
    """When the bare field name is already one of the choices, it is not duplicated."""
    field_info = SimpleNamespace(validation_alias=AliasChoices("FOO", "my_field"))
    assert _direct_env_names("my_field", field_info) == ["FOO", "my_field"]


def test_direct_env_names_with_str_alias() -> None:
    """A plain string `validation_alias` yields `[alias, field_name]`."""
    field_info = SimpleNamespace(validation_alias="ALIAS_NAME")
    assert _direct_env_names("field", field_info) == ["ALIAS_NAME", "field"]


def test_direct_env_names_with_no_alias_returns_bare_field_name_only() -> None:
    """No alias at all (`validation_alias=None`) falls through to `names = []`, then appends
    the bare field name -- the one branch of the three-way alias dispatch nothing else here
    exercises (every real config.py field declares an `AliasChoices` or bare name).
    """
    field_info = SimpleNamespace(validation_alias=None)
    assert _direct_env_names("some_field", field_info) == ["some_field"]


# --------------------------------------------------------------------------- #
# `_resolution_env`: case-insensitive .env + process-env merge, process wins.
# --------------------------------------------------------------------------- #
def test_resolution_env_process_env_wins_over_dotenv_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A key present in both the configured .env file and the process env resolves to the
    process-env value; a dotenv-only key still resolves. Keys are upper-cased.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("shared_var=from_dotenv\ndotenv_only=dotenv_value\n", encoding="utf-8")
    monkeypatch.setenv("SHARED_VAR", "from_process")

    result = _resolution_env({"env_file": str(env_file), "env_file_encoding": "utf-8"})

    assert result["SHARED_VAR"] == "from_process"
    assert result["DOTENV_ONLY"] == "dotenv_value"


def test_resolution_env_with_no_env_file_configured_uses_process_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent/falsy `env_file` in model_config skips the dotenv read entirely."""
    monkeypatch.setenv("ONLY_PROCESS_VAR", "value")
    result = _resolution_env({})
    assert result["ONLY_PROCESS_VAR"] == "value"


def test_resolution_env_with_nonexistent_env_file_path_does_not_raise(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A configured but missing .env path is skipped (`Path(path).is_file()` guard), not an error."""
    monkeypatch.setenv("STILL_PRESENT", "yes")
    missing = tmp_path / "does-not-exist.env"
    result = _resolution_env({"env_file": str(missing)})
    assert result["STILL_PRESENT"] == "yes"


# --------------------------------------------------------------------------- #
# `_resolve_secret_files`: the shared `<VAR>_FILE` secret-file before-validator.
# --------------------------------------------------------------------------- #
def test_resolve_secret_files_passes_through_non_dict_data() -> None:
    """Defensive guard: pydantic-settings should always hand this a dict, but if it doesn't
    (e.g. an earlier source returns something else), the validator no-ops rather than crashing.
    """
    assert BaseSettings._resolve_secret_files("not-a-dict") == "not-a-dict"


def test_resolve_secret_files_skips_a_declared_field_absent_from_the_model() -> None:
    """A `SECRET_FILE_FIELDS` entry naming a field the model doesn't actually declare is
    skipped (`field_info is None: continue`) rather than raising -- defensive against a
    future subclass editing `SECRET_FILE_FIELDS` out of step with its own field set.
    """

    class _BogusSecretFieldSettings(BaseSettings):
        SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = frozenset({"totally_not_a_real_field"})

    data = {"database_url": "postgresql+asyncpg://phaze:phaze@postgres:5432/phaze"}
    assert _BogusSecretFieldSettings._resolve_secret_files(dict(data)) == data


# --------------------------------------------------------------------------- #
# `_apply_redis_password`: percent-encodes redis_password into redis_url's userinfo.
# The hostile-character round-trip (`/`, `#`, `?`, `%XX`) is covered exhaustively in
# tests/shared/core/test_redis_password_encoding.py; this file adds only the one
# branch that survey left: an unparseable redis_url the validator declines to touch.
# --------------------------------------------------------------------------- #
def test_apply_redis_password_leaves_an_unparseable_redis_url_untouched() -> None:
    """`urlparse` on a non-URL string yields `hostname is None`; the validator returns early
    rather than trying to repair a `redis_url` it cannot make sense of (it repairs the DSN
    going forward, it does not attempt to recover an already-mangled one, per its docstring).
    """
    cfg = BaseSettings(
        redis_password=SecretStr("hunter2"),
        redis_url="not-a-url-at-all",
        database_url="postgresql+asyncpg://phaze:phaze@postgres:5432/phaze",
        queue_url="postgresql://phaze:phaze@postgres:5432/phaze",
    )
    assert cfg.redis_url == "not-a-url-at-all"


# --------------------------------------------------------------------------- #
# `_strip_sqlalchemy_driver`: normalizes SQLAlchemy-dialect DSNs to raw libpq for
# psycopg3. Existing tests exercise the field end-to-end for the default (already
# bare `postgresql://`) DSN; this file adds the two dialect-prefix arms directly.
# --------------------------------------------------------------------------- #
def test_strip_sqlalchemy_driver_normalizes_asyncpg_prefix() -> None:
    result = ControlSettings._strip_sqlalchemy_driver("postgresql+asyncpg://phaze:phaze@postgres:5432/phaze")
    assert result == "postgresql://phaze:phaze@postgres:5432/phaze"


def test_strip_sqlalchemy_driver_normalizes_psycopg_prefix() -> None:
    """The `+psycopg` arm -- distinct from `+asyncpg` above -- was previously untested."""
    result = ControlSettings._strip_sqlalchemy_driver("postgresql+psycopg://phaze:phaze@postgres:5432/phaze")
    assert result == "postgresql://phaze:phaze@postgres:5432/phaze"


def test_strip_sqlalchemy_driver_leaves_bare_libpq_dsn_untouched() -> None:
    dsn = "postgresql://phaze:phaze@postgres:5432/phaze"
    assert ControlSettings._strip_sqlalchemy_driver(dsn) == dsn


def test_strip_sqlalchemy_driver_lets_pydantic_reject_a_non_string_value() -> None:
    """A non-string `queue_url` passes through the before-validator untouched (per its own
    docstring: "let pydantic raise") and pydantic's own str-coercion then rejects it.
    """
    with pytest.raises(ValidationError):
        ControlSettings(queue_url=["not", "a", "string"])


# --------------------------------------------------------------------------- #
# `_load_backend_registry`: the Idiom-B TOML loader for `backends`/`buckets`.
# --------------------------------------------------------------------------- #
def test_load_backend_registry_passes_through_non_dict_data() -> None:
    """Mirrors `_resolve_secret_files`'s defensive non-dict guard."""
    assert ControlSettings._load_backend_registry("not-a-dict") == "not-a-dict"


# --------------------------------------------------------------------------- #
# `_validate_registry`: the whole-registry cross-entry invariants (REG-04/05,
# D-04/D-08/D-09). The gap this file closes: a TOP-LEVEL duplicate `[[buckets]]`
# id -- two independent bucket entries sharing an id, as opposed to one backend
# listing the same id twice in its own `buckets` list (phaze-ru9oe, already
# covered in tests/shared/config/test_bucket_registry.py). WR-03's own docstring
# names this exact "copy-paste id typo" scenario; nothing before this file
# constructed it.
# --------------------------------------------------------------------------- #
def test_duplicate_top_level_bucket_id_fails_fast_with_id(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """Two distinct `[[buckets]]` entries sharing an id fail fast, naming the id (WR-03)."""
    backends_toml_env(
        """
        [[backends]]
        kind = "kueue"
        id = "kueue-a"
        rank = 10
        cap = 4
        buckets = ["dup-id"]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"

        [[buckets]]
        id = "dup-id"
        scope = "shared"
        endpoint_url = "https://s3-a.example.com"
        bucket = "phaze-a"

        [[buckets]]
        id = "dup-id"
        scope = "shared"
        endpoint_url = "https://s3-b.example.com"
        bucket = "phaze-b"
        """
    )
    with pytest.raises(ValueError, match=r"duplicate bucket ids in registry.*dup-id") as exc_info:
        ControlSettings()
    assert "REG-05" in str(exc_info.value)


def test_malformed_registry_with_unknown_backend_kind_fails_fast(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A `[[backends]]` entry with a `kind` outside the discriminated union rejects at
    construction (the per-variant fail-fast the acceptance criteria calls out), before the
    container-level `_validate_registry` ever runs.
    """
    backends_toml_env(
        """
        [[backends]]
        kind = "not-a-real-kind"
        id = "mystery"
        rank = 10
        cap = 2
        """
    )
    with pytest.raises(ValidationError):
        ControlSettings()


# --------------------------------------------------------------------------- #
# `_build_default_settings`: constructs the module-level `settings` singleton.
# Distinct from -- and NOT exercised by -- `get_settings()`'s own role dispatch
# (covered in test_config_role_split.py): `_build_default_settings` is called
# once at import time to build the ControlSettings-typed singleton regardless
# of role, and its `PHAZE_ROLE=agent` arm returns the identical `ControlSettings()`
# as its `control` arm (both branches are intentionally the same value; the
# comment above it explains why -- see config.py).
# --------------------------------------------------------------------------- #
def test_build_default_settings_returns_control_settings_when_role_is_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    result = _build_default_settings()
    assert isinstance(result, ControlSettings)


def test_build_default_settings_returns_control_settings_when_role_is_control(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHAZE_ROLE", "control")
    result = _build_default_settings()
    assert isinstance(result, ControlSettings)


# --------------------------------------------------------------------------- #
# Settings-object invariants: the module-level back-compat surface every one
# of config.py's 126 dependents ultimately reads through.
# --------------------------------------------------------------------------- #
def test_settings_alias_resolves_to_control_settings() -> None:
    """The pre-Phase-26 `Settings` name is a back-compat alias for `ControlSettings`."""
    assert Settings is ControlSettings


def test_module_level_settings_singleton_is_control_settings_typed() -> None:
    """`from phaze.config import settings` (37+ call sites) is always ControlSettings-typed,
    even under `PHAZE_ROLE=agent` -- the agent worker must use `get_settings()` explicitly.
    """
    assert isinstance(settings, ControlSettings)


def test_role_enum_values_are_control_and_agent() -> None:
    assert Role.CONTROL.value == "control"
    assert Role.AGENT.value == "agent"


def test_get_settings_is_memoized_across_calls_with_the_same_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_settings()` is `lru_cache`d -- two calls in the same role return the identical
    object, not merely an equal one.
    """
    monkeypatch.setenv("PHAZE_ROLE", "control")
    get_settings.cache_clear()
    try:
        first = get_settings()
        second = get_settings()
        assert first is second
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Hostile numeric input: the acceptance criteria calls out "empty/zero/negative
# numerics" explicitly. Bounded fields (gt=0 etc.) already reject these -- see
# tests/shared/config/test_cloud_route_threshold.py, test_push_config.py, and
# the analysis_stall_timeout_sec bounds added below. But a large share of
# config.py's duration/count knobs (`db_pool_size`, `aborting_reap_slack_seconds`,
# `active_reap_slack_seconds`, `scan_stall_seconds`, `worker_max_jobs`, and others)
# declare NO `gt`/`ge` constraint at all, so a negative or zero value is currently
# SILENTLY ACCEPTED rather than failing loud. This is exactly the "config bugs
# only bite at deploy time" pattern the bead describes -- documented here as
# CURRENT BEHAVIOR (a candidate defect for a future bead), not fixed in this one.
# --------------------------------------------------------------------------- #
def test_analysis_stall_timeout_sec_rejects_zero() -> None:
    """gt=0: a zero stall threshold would kill the analysis child instantly (config.py's own
    comment on this field). Bounds added by phaze-w55w1; not directly tested until now.
    """
    with pytest.raises(ValidationError, match="analysis_stall_timeout_sec"):
        BaseSettings(analysis_stall_timeout_sec=0)


def test_analysis_stall_timeout_sec_rejects_out_of_range_high_value() -> None:
    """lt=86400: caps it at one day so a misconfigured huge value cannot silently mean
    "no bound" (config.py's own comment on this field).
    """
    with pytest.raises(ValidationError, match="analysis_stall_timeout_sec"):
        BaseSettings(analysis_stall_timeout_sec=86400)


def test_analysis_stall_timeout_sec_accepts_the_documented_default() -> None:
    assert BaseSettings().analysis_stall_timeout_sec == 1800


def test_analysis_job_heartbeat_sec_is_derived_at_double_the_stall_threshold() -> None:
    """The derived property (2x multiplier, D-08) for a non-default stall threshold."""
    cfg = BaseSettings(analysis_stall_timeout_sec=900)
    assert cfg.analysis_job_heartbeat_sec == 1800


def test_db_pool_size_silently_accepts_a_negative_value_current_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """CURRENT BEHAVIOR, DOCUMENTED NOT FIXED (phaze-ptlk8 finding): unlike the bounded
    duration knobs above, `db_pool_size` (and its siblings `db_max_overflow`,
    `db_pool_timeout`, `db_pool_recycle`, `aborting_reap_slack_seconds`,
    `active_reap_slack_seconds`, `scan_stall_seconds`, and the plain-`int` worker/lane
    knobs) declare no `ge`/`gt` constraint at all. A negative pool size reaches
    SQLAlchemy's `create_engine` unvalidated. This test pins today's permissive
    behavior so a future bug-hunt bead that tightens these fields gets a red test
    here as a deliberate signal, not a silent behavior change.
    """
    monkeypatch.delenv("PHAZE_DB_POOL_SIZE", raising=False)
    cfg = BaseSettings(db_pool_size=-1)
    assert cfg.db_pool_size == -1


def test_worker_max_jobs_silently_accepts_zero_current_behavior() -> None:
    """CURRENT BEHAVIOR, DOCUMENTED NOT FIXED: `worker_max_jobs` is a plain `int` field with
    no Field()/constraint at all, so 0 (a worker that can run no jobs) is accepted rather
    than rejected. See the `db_pool_size` test above for the broader finding.
    """
    cfg = BaseSettings(worker_max_jobs=0)
    assert cfg.worker_max_jobs == 0
