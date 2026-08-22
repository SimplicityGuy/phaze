"""The `<VAR>_FILE` secret-file resolution half of the settings surface (phaze-bk9el.18).

Extracted VERBATIM from `phaze.config.BaseSettings`, which measured LCOM4=3: its methods
fell into three groups sharing no fields or calls with each other. This module is one of
those groups -- the Docker/Swarm-secret, Kubernetes-mount, SOPS convention by which a
secret-bearing field reads its value from a file named by a `<ALIAS>_FILE` env var instead
of from the env var itself.

`SecretFileSettingsMixin` is a base of `phaze.config.BaseSettings`, so every name here is
still reachable as `BaseSettings.SECRET_FILE_FIELDS` / `ControlSettings.SECRET_FILE_FIELDS`
/ `AgentSettings.SECRET_FILE_FIELDS` exactly as before -- the two subclasses extend the
ClassVars by referring to `BaseSettings.SECRET_FILE_FIELDS`, which resolves here through
the MRO. Nothing about the convention changed: same field set, same env-name search order,
same precedence (a direct env var always beats its `_FILE` sibling), same strip-vs-verbatim
rule, same error message naming the variable.
"""

import os
from pathlib import Path
from typing import Any, ClassVar

from dotenv import dotenv_values
from pydantic import AliasChoices, model_validator
from pydantic_settings import BaseSettings as PydanticBaseSettings, SettingsConfigDict

from phaze.config_backends import _read_secret_file


def _direct_env_names(field_name: str, field_info: Any) -> list[str]:
    """Return the env-var names a field accepts directly: its ``validation_alias``
    string choices, plus the bare field name when not already covered.

    The ``<VAR>_FILE`` sibling names are derived from this set so the file-secret
    convention stays consistent with whatever aliases a field already honors.
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
    return names


def _resolution_env(model_config: SettingsConfigDict) -> dict[str, str]:
    """Build the case-insensitive name->value map used to resolve `_FILE` secrets.

    Mirrors pydantic-settings' own precedence: values from the process environment
    win over values declared in the configured `.env` file(s). Both layers are
    consulted so a `<VAR>_FILE` (or its direct sibling) declared in `.env` — the
    way every other documented var in `.env.example` is consumed — is honored, not
    just process-env vars injected by Docker/Kubernetes.
    """
    merged: dict[str, str] = {}
    env_file = model_config.get("env_file")
    if env_file:
        encoding = model_config.get("env_file_encoding") or "utf-8"
        paths = [env_file] if isinstance(env_file, (str, os.PathLike)) else list(env_file)
        for path in paths:
            if path and Path(path).is_file():
                merged.update({key: value for key, value in dotenv_values(path, encoding=encoding).items() if value is not None})
    merged.update(os.environ)  # process env wins over .env
    return {key.upper(): value for key, value in merged.items()}


class SecretFileSettingsMixin(PydanticBaseSettings):
    """The `<VAR>_FILE` secret convention: which fields honor it, and how one is resolved.

    Carries no fields of its own -- `SECRET_FILE_FIELDS` names fields declared on the
    concrete settings classes downstream, and `_resolve_secret_file_for_field` skips any
    name that is not on the model it runs against. That is what lets one before-validator
    serve `BaseSettings`, `ControlSettings` and `AgentSettings`, each with a different
    (growing) field set.
    """

    # v4.0.1: secret-bearing fields that honor the `<VAR>_FILE` convention
    # (Docker/Swarm secrets, Kubernetes mounts, SOPS). Subclasses extend this set;
    # the shared `_resolve_secret_files` before-validator reads each field's
    # `<ALIAS>_FILE` siblings when the direct env var is unset. `database_url` and
    # `redis_url` live here because both carry credentials and exist on both roles.
    SECRET_FILE_FIELDS: ClassVar[frozenset[str]] = frozenset({"database_url", "redis_url", "queue_url"})

    # WR-01: secret fields whose file contents must be preserved VERBATIM (NOT ``.strip()``-ed).
    # Every other `<VAR>_FILE` secret is stripped so a heredoc/echo trailing newline hashes/parses
    # identically to an operator-typed env var -- but key material (an OpenSSH private key, a
    # known_hosts file) REQUIRES its trailing newline: OpenSSH's parser rejects a key without a
    # final newline ("invalid format" / "error in libcrypto"), so stripping it broke every push that
    # provisioned its key via PHAZE_PUSH_SSH_KEY_FILE. Subclasses extend this set; the shared
    # `_resolve_secret_files` validator consults it to decide strip-vs-verbatim per field.
    SECRET_FILE_PRESERVE_WHITESPACE: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def _resolve_secret_file_for_field(
        cls,
        data: dict[str, Any],
        field_name: str,
        env_upper: dict[str, str],
        present_upper: set[str],
    ) -> None:
        """Resolve one `SECRET_FILE_FIELDS` entry's `<ALIAS>_FILE` sibling into `data`, in place.

        Split out of `_resolve_secret_files` (one field per call, from the same per-field loop)
        so the field/alias search is legible on its own. Pure extraction: same precedence, same
        env-name search order, same early-outs (`return` here where the loop body used
        `continue`/`break`) -- no behavior change.
        """
        field_info = cls.model_fields.get(field_name)
        if field_info is None:
            return

        env_names = _direct_env_names(field_name, field_info)
        # Precedence: an explicitly-set direct env var (or a value already
        # merged from another source into `data`) always wins over `_FILE`.
        if any(name.upper() in present_upper or name.upper() in env_upper for name in env_names):
            return

        for env_name in env_names:
            file_var = f"{env_name.upper()}_FILE"
            if file_var not in env_upper:
                continue
            path = env_upper[file_var]
            # Inject under the field name; every in-scope field is matched
            # either by name (no alias) or by an AliasChoices that includes
            # the bare field name, so this key always resolves. The shared
            # `_read_secret_file` helper (config_backends) applies the single
            # strip-vs-verbatim rule both this env-`_FILE` path and the inline
            # TOML `*_file` reader adopt (D-06: one rule, two call sites). Key
            # material (SECRET_FILE_PRESERVE_WHITESPACE) is kept verbatim so its
            # required trailing newline survives (WR-01); everything else is stripped.
            try:
                data[field_name] = _read_secret_file(path, preserve_whitespace=field_name in cls.SECRET_FILE_PRESERVE_WHITESPACE)
            except ValueError as exc:
                # Re-raise with the `<VAR>_FILE` name so the operator-facing message
                # still names the variable that pointed at the unreadable path.
                msg = f"{file_var} points to {path!r} which could not be read: {exc}"
                raise ValueError(msg) from exc
            break

    @model_validator(mode="before")
    @classmethod
    def _resolve_secret_files(cls, data: Any) -> Any:
        """Resolve `<VAR>_FILE` secrets before any required-field / production guard.

        For each field in `SECRET_FILE_FIELDS`, if no direct env var (or value from
        another already-merged source) is present but a `<ALIAS>_FILE` sibling is
        set, read the secret from that path. The file's surrounding whitespace is
        stripped (`.strip()`) so a heredoc/echo-created secret with a trailing
        newline hashes identically to an operator-typed env var — critical for
        `PHAZE_AGENT_TOKEN`, whose entire wire string is hashed by `hash_token`.

        Runs as `mode="before"` so the resolved value flows through field
        validation (SecretStr coercion) and into the `mode="after"` guards
        (`_enforce_required_agent_fields`, the production validators). A missing or
        unreadable `<ALIAS>_FILE` path raises `ValueError` (surfaced as a
        `ValidationError`) naming the variable and path — never a silent fallback.

        The `<ALIAS>_FILE` vars are read from the process env and the configured
        `.env` file (they are not model fields, so `extra="ignore"` never sees
        them) and matched case-insensitively to mirror pydantic-settings' default
        env handling; the process env wins over `.env`. The per-field search itself is
        `_resolve_secret_file_for_field`; this method just owns the field iteration order.
        """
        if not isinstance(data, dict):
            return data

        env_upper = _resolution_env(cls.model_config)
        present_upper = {str(key).upper() for key in data}

        for field_name in cls.SECRET_FILE_FIELDS:
            cls._resolve_secret_file_for_field(data, field_name, env_upper, present_upper)

        return data
