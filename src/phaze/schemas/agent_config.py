"""Pydantic schema for GET /api/internal/agent/config (phaze-mvq8z.9, ADR-0019 (runtime config hot-reload) §14).

The response is the DB-override layer only -- the SAME map
:func:`phaze.services.runtime_config_overrides.get_runtime_config_overrides` returns, restricted to
:data:`phaze.runtime_config.RELOADABLE_KEYS` -- plus a content digest so the poller
(``phaze.tasks.heartbeat``) can decide "nothing changed" without rebuilding its local snapshot.
RESPONSE-only model (loose, no ``extra="forbid"``), matching
:class:`~phaze.schemas.agent_identity.AgentIdentity`'s convention: the server can add fields
non-breakingly and an older agent's Pydantic parsing discards unknown keys.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


class AgentConfigResponse(BaseModel):
    """``{overrides, digest}`` -- the DB-override layer for :data:`~phaze.runtime_config.RELOADABLE_KEYS`.

    ``overrides`` carries only the keys currently overridden (an empty dict means every reloadable
    key falls through to the ``file``/``env``/``default`` layers underneath, exactly as
    :func:`~phaze.services.runtime_config_overrides.get_runtime_config_overrides` reports it).
    """

    overrides: dict[str, int | str]
    digest: str


def compute_overrides_digest(overrides: dict[str, int | str]) -> str:
    """Content hash of ``overrides`` -- equal digests mean the DB-override layer has not changed.

    Mirrors :meth:`phaze.runtime_config.RuntimeConfig.digest` (sha256 of the sorted-key JSON dump).
    Kept as a free function rather than a method: the caller here already holds a plain dict (the
    override layer alone), never a built :class:`~phaze.runtime_config.RuntimeConfig` snapshot (which
    also carries every ``env``/``default``-layer value the override map does not include).
    """
    return hashlib.sha256(json.dumps(overrides, sort_keys=True).encode()).hexdigest()
