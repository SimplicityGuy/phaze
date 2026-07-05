"""D-06 golden byte-identical characterization of the ≤1-compute dispatch/resolution path.

ACCEPTANCE SAFETY NET (Phase 72, Plan 01). This module is authored against and run GREEN on
CURRENT (unchanged) production code in Wave 1, BEFORE the fail-fast retirement (Plan 02) and the
per-entry compute binding rewire (Plan 03) touch any ``src/`` file. Waves 2-3 change production
code and MUST keep every cell here green -- that green run IS the byte-identical proof that the
observable ≤1-compute and zero-compute (implicit all-local) behavior is preserved on the real
deploys (mirrors the Phase-68 D-01 golden-characterization precedent).

Every cell pins CURRENT behavior, not a future contract:

* The ≤1-compute lane (cite D-06): a single-compute registry whose compute ``agent_ref`` EQUALS the
  online compute agent's ``Agent.id`` -- the byte-identical single-compute deploy D-01 binds to --
  resolves to ``resolved_non_local_kind == "compute"``, ``cloud_enabled is True``,
  ``active_compute_scratch_dir == "/srv/scratch"``, the exact ``/pushed`` scratch-path format
  ``f"{scratch_dir}/{file_id}.{file_type}"`` (the D-07 boundary agent_push.py must keep
  byte-identical), and ``ComputeAgentBackend.is_available`` True when that agent is online / False
  (never raising) when absent.
* The zero-compute lane (Task 2): the implicit ``_default_local_registry`` all-local baseline has no
  cloud activity at all.

The "compute agent online but id != agent_ref" case is DELIBERATELY NOT characterized here -- that is
the intended behavior CHANGE Plan 03 introduces (per-entry binding), not a byte-identical invariant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest

from tests._queue_fakes import seed_active_agent


# The production target already exists (shipped in a prior phase's Wave 2); ``importorskip`` mirrors
# the ``test_backends.py`` idiom so the module collects cleanly if the target is ever absent.
backends = pytest.importorskip("phaze.services.backends")


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# The single-compute deploy D-01 binds to: one local (rank 99) + one compute whose agent_ref EQUALS
# the online agent's Agent.id ("oci-a1"). This is the byte-identical matching-ref registry.
_LOCAL_1COMPUTE_MATCHING_REF = """
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1

    [[backends]]
    kind = "compute"
    id = "oci-a1"
    rank = 30
    cap = 2
    agent_ref = "oci-a1"
    scratch_dir = "/srv/scratch"
"""


def test_single_compute_registry_resolution_is_byte_identical(backends_toml_env: Any) -> None:
    """D-06: the ≤1-compute registry resolves cloud_enabled/kind/scratch_dir + the /pushed path format.

    Pins the pure (no-DB) resolution surface Waves 2-3 must keep byte-identical: a single-compute
    registry is a live cloud lane (``cloud_enabled``), reduces to the ``"compute"`` non-local kind,
    exposes the sole compute backend's ``scratch_dir``, and composes the exact ``/pushed`` scratch path
    ``f"{active_compute_scratch_dir}/{file_id}.{file_type}"`` -- the D-07 boundary agent_push.py holds.
    """
    from phaze.config import ControlSettings

    backends_toml_env(_LOCAL_1COMPUTE_MATCHING_REF)
    settings = ControlSettings()

    assert settings.cloud_enabled is True
    assert backends.resolved_non_local_kind(settings) == "compute"
    assert settings.active_compute_scratch_dir == "/srv/scratch"

    # D-07 boundary: the /pushed scratch-path format string (agent_push.py ~L133) must stay byte-identical.
    file_id = uuid.UUID("00000000-0000-0000-0000-0000000000ab")
    file_type = "mp3"
    scratch_path = f"{settings.active_compute_scratch_dir}/{file_id}.{file_type}"
    assert scratch_path == "/srv/scratch/00000000-0000-0000-0000-0000000000ab.mp3"


@pytest.mark.asyncio
async def test_compute_backend_is_available_true_when_matching_ref_agent_online(session: AsyncSession, backends_toml_env: Any) -> None:
    """D-06: the resolved ComputeAgentBackend reports available when the matching-ref agent is online.

    Seeds an ONLINE compute agent whose ``Agent.id == "oci-a1"`` equals the registry's ``agent_ref``
    (the byte-identical single-compute deploy), resolves the ComputeAgentBackend from the same registry,
    and asserts ``is_available`` is True -- the current dispatch-gate behavior Plan 02/03 must preserve.
    """
    from phaze.config import ControlSettings

    backends_toml_env(_LOCAL_1COMPUTE_MATCHING_REF)
    settings = ControlSettings()
    [backend] = [b for b in backends.resolve_backends(settings) if b.id == "oci-a1"]

    await seed_active_agent(session, agent_id="oci-a1", kind="compute")
    assert await backend.is_available(session) is True


@pytest.mark.asyncio
async def test_compute_backend_is_available_false_when_agent_absent_never_raises(session: AsyncSession, backends_toml_env: Any) -> None:
    """D-06: with NO compute agent seeded, is_available returns False (degrade-safe hold, never raises).

    The companion to the online cell: an absent compute agent is a clean hold (False), not an error --
    the cron no-op discipline the retirement must keep byte-identical.
    """
    from phaze.config import ControlSettings

    backends_toml_env(_LOCAL_1COMPUTE_MATCHING_REF)
    settings = ControlSettings()
    [backend] = [b for b in backends.resolve_backends(settings) if b.id == "oci-a1"]

    # Deliberately NO compute agent seeded.
    assert await backend.is_available(session) is False


# === Task 2: explicit zero-compute (implicit all-local) regression =======================


def test_implicit_all_local_registry_has_no_cloud_activity(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """D-06: the implicit ``_default_local_registry`` baseline produces no cloud lane and no compute backend.

    With NO ``backends.toml`` present the ``backends`` default_factory synthesizes the single
    ``id=local, rank=99, cap=1`` entry (config_backends._default_local_registry, D-03). This cell pins
    the "no cloud activity" surface Waves 2-3 must keep byte-identical: ``cloud_enabled is False``,
    ``resolved_non_local_kind == "local"``, ``active_compute_scratch_dir is None``, and
    ``resolve_backends`` yields exactly one ``LocalBackend`` with ZERO ``ComputeAgentBackend``.

    Env-clearing discipline: point ``PHAZE_BACKENDS_CONFIG_FILE`` at a NONEXISTENT path so no stray
    process/.env pointer leaks a real registry in (mirrors the default-registry test's isolation), then
    clear the ``get_settings`` lru_cache so no cached singleton bleeds across.
    """
    from phaze.config import ControlSettings, get_settings

    # Nonexistent pointer -> the before-validator injects nothing -> the default_factory fires (D-03).
    monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", str(tmp_path / "nonexistent-backends.toml"))
    get_settings.cache_clear()

    settings = ControlSettings()

    assert settings.cloud_enabled is False
    assert backends.resolved_non_local_kind(settings) == "local"
    assert settings.active_compute_scratch_dir is None

    resolved = backends.resolve_backends(settings)
    assert len(resolved) == 1
    assert isinstance(resolved[0], backends.LocalBackend)
    assert not any(isinstance(b, backends.ComputeAgentBackend) for b in resolved)
