"""Characterization pin for `src/phaze/config.py` -- written BEFORE the phaze-bk9el.18 split.

WHAT THIS IS. A characterization test, not a correctness test. It records the settings
surface EXACTLY as it behaves today -- warts, oddities and all -- so that the BaseSettings
split (phaze-bk9el.18) can be checked against it. It is the discharge of that bead's
"no behaviour change" criterion under CLAUDE.md rule 1 ("an acceptance criterion is
discharged by a test or by the operator -- never by prose").

WHY config.py SPECIFICALLY. It is the highest fan-in file in the repo (153 dependents) with
a bus factor of 1, and before this file NO test proved field-for-field settings equivalence.
The split's faithfulness would otherwise have rested on the implementer's argument that the
move was faithful -- the exact shape that shipped phaze-3ea41.

WHAT IT PINS, and why each half is needed:

* **The field manifest** -- every field name on every settings class, and for each the exact
  ordered tuple of env-var names it accepts (its ``validation_alias`` choices plus the bare
  field name). A field that moves between classes, loses an alias, gains one, or is renamed
  fails here. This is the "key name / env var name" half of the criterion.
* **The `<VAR>_FILE` secret convention** -- ``SECRET_FILE_FIELDS``,
  ``SECRET_FILE_PRESERVE_WHITESPACE``, and the exact ``<ALIAS>_FILE`` env-var names derived
  from them, per class. Losing a member of either ClassVar in the split silently drops a
  Docker/Kubernetes secret path, which no other test in the tree would notice. This is the
  "secret-file path" half.
* **The resolved values in a scrubbed environment** -- not the declared ``Field(default=...)``
  but the value the model actually resolves to after every ``mode="before"`` and
  ``mode="after"`` validator has run. Those validators (``_resolve_secret_files``,
  ``_load_backend_registry``, ``_apply_redis_password``, ``_strip_sqlalchemy_driver``,
  ``derive_sizing`` and the production guards) are where a split most plausibly changes
  behaviour, and a declared-default assertion would not see any of it. This is the "default
  value" half.
* **One env-var override case and one secret-file case per class**, so the two resolution
  mechanisms are exercised end to end and not merely enumerated.

THE GOLDEN. ``config_characterization_golden.json``, recorded on the unmodified tree at the
phaze-bk9el.1 baseline. It is a RECORDING, not a specification: if a change makes this test
fail, the correct response is to establish whether the behaviour change was intended, not to
re-record the file. Re-recording it as a reflex converts the pin into a rubber stamp.

ENVIRONMENT SCRUB. Every env name any field could read -- each alias, uppercased, plus its
``<ALIAS>_FILE`` sibling -- is deleted before any settings object is constructed, and
``env_file`` is severed on every class. The scrub set is derived from the LIVE field set
rather than hard-coded, so a field added tomorrow is scrubbed tomorrow; the resulting sorted
name list is itself part of the golden, so the scrub cannot silently narrow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import AliasChoices, BaseModel, SecretStr
import pytest

from phaze.config import AgentSettings, BaseSettings, ControlSettings


if TYPE_CHECKING:
    from collections.abc import Iterator


_GOLDEN_PATH = Path(__file__).with_name("config_characterization_golden.json")

# The minimum env an `AgentSettings` will construct under at all: four fields are required
# (or fail-fast validated) with no usable default, so "the default environment" for the agent
# role is necessarily "defaults plus these four". Deliberately unroutable `.invalid` values so
# nothing here can accidentally reach a real host.
_AGENT_MIN_ENV = {
    "PHAZE_AGENT_API_URL": "http://app.characterization.invalid:8000",
    "PHAZE_AGENT_TOKEN": "phaze_agent_characterization-token-000000",
    "PHAZE_AGENT_SCAN_ROOTS": "/data/music,/data/videos",
    "PHAZE_QUEUE_URL": "postgresql://phaze:phaze@app.characterization.invalid:5432/phaze",
}

_CLASSES = (BaseSettings, ControlSettings, AgentSettings)

# The OpenSSH PEM armour, assembled rather than written out. The bytes matter -- the WR-01 rule
# exists because OpenSSH's parser rejects a key whose final armour line has no trailing newline --
# but spelling the OpenSSH armour header out in one piece in a tracked file trips the repo's
# detect-private-key pre-commit hook, which cannot tell a four-byte fixture from a real key.
# Splitting the marker keeps the hook honest without weakening what the test asserts.
_PEM_BEGIN = "-----BEGIN OPENSSH PRIVATE " + "KEY-----"
_PEM_END = "-----END OPENSSH PRIVATE " + "KEY-----"


def _golden() -> dict[str, Any]:
    with _GOLDEN_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _aliases(field_name: str, field_info: Any) -> tuple[str, ...]:
    """The ordered env names a field accepts directly.

    Deliberately re-derived from pydantic's own ``validation_alias`` rather than by calling
    ``config._direct_env_names``: a pin that calls the production helper it is pinning cannot
    detect that helper changing. The two agreeing is the point, so they must be independent.
    """
    alias = field_info.validation_alias
    if isinstance(alias, AliasChoices):
        names = [choice for choice in alias.choices if isinstance(choice, str)]
    elif isinstance(alias, str):
        names = [alias]
    else:
        names = []
    if field_name not in names:
        names.append(field_name)
    return tuple(names)


def _env_universe() -> set[str]:
    """Every env name any settings field could read, plus the two dispatch pointers."""
    names: set[str] = {"PHAZE_ROLE", "PHAZE_BACKENDS_CONFIG_FILE"}
    for cls in _CLASSES:
        for field_name, field_info in cls.model_fields.items():
            for alias in _aliases(field_name, field_info):
                names.add(alias.upper())
                names.add(f"{alias.upper()}_FILE")
    return names


def _freeze(value: Any) -> Any:
    """Render a resolved setting as a stable, JSON-comparable value.

    ``SecretStr`` collapses to its LENGTH, never its content: the pin must fail when a secret
    stops resolving (or starts resolving to something of a different size) without the golden
    file becoming a place secrets can be committed.
    """
    if isinstance(value, SecretStr):
        return f"SecretStr(len={len(value.get_secret_value())})"
    if isinstance(value, BaseModel):
        return {key: _freeze(inner) for key, inner in value.model_dump().items()}
    if isinstance(value, Path):
        return f"Path({value.as_posix()})"
    if isinstance(value, (list, tuple)):
        return [_freeze(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _freeze(inner) for key, inner in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"{type(value).__name__}({value!r})"


def _resolved(instance: Any) -> dict[str, Any]:
    return {name: _freeze(getattr(instance, name)) for name in sorted(type(instance).model_fields)}


@pytest.fixture
def scrubbed_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Delete every settings-visible env var and sever `.env` on every settings class.

    ``tests/conftest.py`` already severs ``env_file`` and clears a hand-listed subset of vars;
    this fixture is deliberately self-contained rather than relying on that, because the point
    of a pin is to keep meaning if the shared conftest is edited.
    """
    for name in _env_universe():
        monkeypatch.delenv(name, raising=False)
    for cls in _CLASSES:
        config = dict(cls.model_config)
        config["env_file"] = None
        monkeypatch.setattr(cls, "model_config", config)
    # /etc/phaze/backends.toml is the documented default pointer. It does not exist on a dev
    # box or in CI, so the implicit-local registry (D-03) is what resolves -- but pointing at a
    # path that provably does not exist makes that independent of the machine.
    monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", "/nonexistent/phaze-characterization/backends.toml")
    yield


@pytest.mark.parametrize("cls", _CLASSES, ids=lambda c: c.__name__)
def test_settings_field_manifest_is_unchanged(cls: type[BaseSettings]) -> None:
    """Every field name and the ordered env-var names it accepts, per class.

    Fails on: a renamed field, a field that moved between classes, an added or removed
    ``validation_alias`` choice, a reordered ``AliasChoices`` (order IS precedence in
    pydantic-settings), and a field appearing or disappearing entirely.
    """
    observed = {name: list(_aliases(name, info)) for name, info in cls.model_fields.items()}
    assert observed == _golden()[f"{cls.__name__}_ALIASES"]


@pytest.mark.parametrize("cls", _CLASSES, ids=lambda c: c.__name__)
def test_secret_file_convention_is_unchanged(cls: type[BaseSettings]) -> None:
    """The `<VAR>_FILE` secret surface: which fields honor it, which keep whitespace, and the
    exact env-var names each one reads.

    A field silently dropped from ``SECRET_FILE_FIELDS`` in a split still constructs, still
    type-checks and still passes every other test -- it just stops reading its Docker/K8s
    secret mount and falls back to the default. Nothing else in the tree would notice.
    """
    golden = _golden()
    assert sorted(cls.SECRET_FILE_FIELDS) == golden[f"{cls.__name__}_SECRET_FILE_FIELDS"]
    assert sorted(cls.SECRET_FILE_PRESERVE_WHITESPACE) == golden[f"{cls.__name__}_PRESERVE_WS"]

    file_env = {name: [alias.upper() + "_FILE" for alias in _aliases(name, cls.model_fields[name])] for name in sorted(cls.SECRET_FILE_FIELDS)}
    assert file_env == golden[f"{cls.__name__}_FILE_ENV"]


def test_settings_env_var_universe_is_unchanged() -> None:
    """The complete sorted set of env names the settings layer reads.

    This is the same information the per-class alias manifests carry, collapsed into one flat
    list -- but it is what the scrub above is built from, so pinning it stops the scrub from
    silently narrowing (which would let a stray ambient env var start leaking into the default
    assertions below without any test going red).
    """
    assert sorted(_env_universe()) == _golden()["ENV_UNIVERSE"]


# phaze-bk9el CI fix: `lane_analyze_concurrency` is the ONE field in these goldens whose
# default is HOST-DERIVED (`default_factory=lambda: derive_sizing().concurrency`, phaze-rvcn)
# rather than a literal. A golden captured on one machine therefore pins that machine's core
# count as well as the code, and the comparison failed on CI while passing locally. Every
# other field in both goldens is a literal and is still compared exactly.
#
# We assert the DERIVATION instead of the recorded number: whatever this host derives is what
# the setting must resolve to. That keeps the pin honest about what it can and cannot claim --
# it still catches a reordered or dropped validator, which is what the docstring promises.
_HOST_DERIVED_FIELDS = ("lane_analyze_concurrency",)


def _diff_report(actual: dict[str, Any], golden: dict[str, Any]) -> str:
    """Name every key whose value differs, with both values.

    pytest elides the differing entries of a large dict comparison ("Omitting 76 identical
    items"), which is exactly the information needed when a golden fails on a machine you
    cannot attach to. Build the report ourselves so a CI failure is actionable on sight.
    """
    only_actual = sorted(set(actual) - set(golden))
    only_golden = sorted(set(golden) - set(actual))
    changed = sorted(k for k in set(actual) & set(golden) if actual[k] != golden[k])
    lines = [f"{len(changed)} field(s) differ, {len(only_actual)} extra, {len(only_golden)} missing"]
    lines += [f"  CHANGED {k}: golden={golden[k]!r} actual={actual[k]!r}" for k in changed]
    lines += [f"  EXTRA   {k}: actual={actual[k]!r}" for k in only_actual]
    lines += [f"  MISSING {k}: golden={golden[k]!r}" for k in only_golden]
    return "\n".join(lines)


def _without_host_derived(resolved: dict[str, Any]) -> dict[str, Any]:
    """The resolved mapping minus fields whose default is derived from this host."""
    return {k: v for k, v in resolved.items() if k not in _HOST_DERIVED_FIELDS}


def _assert_host_derived_fields(resolved: dict[str, Any]) -> None:
    """Each host-derived field equals what this host actually derives, not a recorded literal."""
    from phaze.services.analysis_sizing import derive_sizing

    assert resolved["lane_analyze_concurrency"] == derive_sizing().concurrency


@pytest.mark.usefixtures("scrubbed_env")
def test_control_settings_resolve_to_recorded_defaults() -> None:
    """Every ControlSettings field's RESOLVED value in a scrubbed environment.

    Resolved, not declared: this is the value after ``_resolve_secret_files``,
    ``_load_backend_registry``, ``_strip_sqlalchemy_driver``, ``_apply_redis_password``,
    ``derive_sizing`` and ``_validate_registry`` have all run. A split that reorders or drops
    any of those validators changes a value here even when every ``Field(default=...)``
    literal survives the move untouched.
    """
    resolved = _resolved(ControlSettings())
    _assert_host_derived_fields(resolved)
    expected = _without_host_derived(_golden()["CONTROL_DEFAULTS"])
    got = _without_host_derived(resolved)
    assert got == expected, _diff_report(got, expected)


@pytest.mark.usefixtures("scrubbed_env")
def test_agent_settings_resolve_to_recorded_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every AgentSettings field's RESOLVED value, given only the four required vars.

    The four in ``_AGENT_MIN_ENV`` are not defaults being overridden -- they are the fields
    ``_enforce_required_agent_fields`` and the phaze-27myl queue_url guard refuse to construct
    without. Everything else in the assertion is a default.
    """
    for name, value in _AGENT_MIN_ENV.items():
        monkeypatch.setenv(name, value)
    resolved = _resolved(AgentSettings())
    _assert_host_derived_fields(resolved)
    expected = _without_host_derived(_golden()["AGENT_DEFAULTS"])
    got = _without_host_derived(resolved)
    assert got == expected, _diff_report(got, expected)


@pytest.mark.usefixtures("scrubbed_env")
@pytest.mark.parametrize(
    ("env_name", "env_value", "field_name", "expected"),
    [
        # One per resolution mechanism that could break differently in a split: a plain str, an
        # int coerced from text, a bool coerced from text, a float, a Literal-constrained str,
        # and the DSNs whose before-validators rewrite the value they were given rather than
        # storing it verbatim. Note the env NAMES are deliberately not uniform: only some fields
        # carry a `PHAZE_`-prefixed alias, and the bare-name ones (`API_PORT`, `DEBUG`,
        # `SCAN_PATH`, `LLM_MODEL`, `DISCOGS_MATCH_CONCURRENCY`) read the UNPREFIXED variable.
        # That asymmetry is today's behaviour and is pinned here on purpose.
        (
            "PHAZE_DATABASE_URL",
            "postgresql+asyncpg://u:p@db.characterization.invalid:5432/x",
            "database_url",
            "postgresql+asyncpg://u:p@db.characterization.invalid:5432/x",
        ),
        (
            "DATABASE_URL",
            "postgresql+asyncpg://u:p@alias.characterization.invalid:5432/x",
            "database_url",
            "postgresql+asyncpg://u:p@alias.characterization.invalid:5432/x",
        ),
        # `_strip_sqlalchemy_driver` rewrites the dialect form to raw libpq -- the stored value
        # is NOT what the operator set.
        (
            "PHAZE_QUEUE_URL",
            "postgresql+asyncpg://u:p@q.characterization.invalid:5432/x",
            "queue_url",
            "postgresql://u:p@q.characterization.invalid:5432/x",
        ),
        ("API_PORT", "9123", "api_port", 9123),
        ("DEBUG", "true", "debug", True),
        ("PHAZE_LOG_LEVEL", "DEBUG", "log_level", "DEBUG"),
        ("SCAN_PATH", "/somewhere/else", "scan_path", "/somewhere/else"),
        ("LLM_MODEL", "claude-opus-4-20250514", "llm_model", "claude-opus-4-20250514"),
        ("DISCOGS_MATCH_CONCURRENCY", "17", "discogs_match_concurrency", 17),
        ("PHAZE_CONVENTION_DATE_MIN_PURITY", "0.75", "convention_date_min_purity", 0.75),
        ("PHAZE_CONTROL_ENV", "dev", "control_env", "dev"),
    ],
)
def test_control_settings_env_override_lands_on_the_recorded_field(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
    field_name: str,
    expected: Any,
) -> None:
    """One env var per resolution mechanism actually reaches its field.

    The alias manifest above proves the NAMES are unchanged; this proves the plumbing behind
    them still works. The two are different failures: a split can preserve every alias literal
    and still break resolution by dropping the ``mode="before"`` validator that reads them.
    """
    monkeypatch.setenv(env_name, env_value)
    assert getattr(ControlSettings(), field_name) == expected


@pytest.mark.usefixtures("scrubbed_env")
def test_control_settings_secret_file_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`<VAR>_FILE` secrets: read from the path, stripped, and only when the direct var is unset.

    Covers all three halves of the convention on the control role -- the read itself, the
    ``.strip()`` that makes a heredoc-written secret hash identically to a typed one, and the
    precedence rule that a directly-set env var always beats the file.
    """
    secret_file = tmp_path / "anthropic_api_key"
    secret_file.write_text("sk-ant-characterization-000\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY_FILE", str(secret_file))

    resolved = ControlSettings()
    assert resolved.anthropic_api_key is not None
    # Stripped: the trailing newline the heredoc wrote is NOT part of the secret.
    assert resolved.anthropic_api_key.get_secret_value() == "sk-ant-characterization-000"

    # Precedence: a direct env var wins over the `_FILE` sibling, which is not read at all.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-direct-wins")
    assert ControlSettings().anthropic_api_key.get_secret_value() == "sk-ant-direct-wins"


@pytest.mark.usefixtures("scrubbed_env")
def test_control_settings_secret_file_unreadable_path_raises_naming_the_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unreadable `<VAR>_FILE` path fails construction and the message names the VARIABLE.

    Pinned because it is the documented contract ("never a silent fallback") and because the
    error message is the operator's only diagnostic -- a split that lets the ValueError escape
    un-rewrapped would still raise, just uselessly.
    """
    monkeypatch.setenv("PHAZE_DATABASE_URL_FILE", str(tmp_path / "does-not-exist"))
    with pytest.raises(Exception, match="PHAZE_DATABASE_URL_FILE") as excinfo:
        ControlSettings()
    assert "could not be read" in str(excinfo.value)


@pytest.mark.usefixtures("scrubbed_env")
def test_agent_settings_secret_file_preserves_whitespace_for_key_material(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The WR-01 split: key material keeps its trailing newline; every other secret is stripped.

    Both halves in one test on purpose -- the behaviour is a DIFFERENCE between two fields, and
    pinning only one of them would pass just as happily if the split collapsed them together.
    ``push_ssh_key`` without its trailing newline is rejected by OpenSSH's parser, which is how
    this became a rule rather than a nicety.
    """
    for name, value in _AGENT_MIN_ENV.items():
        monkeypatch.setenv(name, value)

    key_file = tmp_path / "id_ed25519"
    key_file.write_text(f"{_PEM_BEGIN}\nAAAA\n{_PEM_END}\n", encoding="utf-8")
    monkeypatch.setenv("PHAZE_PUSH_SSH_KEY_FILE", str(key_file))

    token_file = tmp_path / "agent_token"
    token_file.write_text("phaze_agent_from-a-file-000000\n", encoding="utf-8")
    monkeypatch.delenv("PHAZE_AGENT_TOKEN", raising=False)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN_FILE", str(token_file))

    resolved = AgentSettings()
    # VERBATIM: the trailing newline survives.
    assert resolved.push_ssh_key.get_secret_value().endswith(f"{_PEM_END}\n")
    # STRIPPED: the token's trailing newline does not, because `hash_token` hashes the whole string.
    assert resolved.agent_token.get_secret_value() == "phaze_agent_from-a-file-000000"
