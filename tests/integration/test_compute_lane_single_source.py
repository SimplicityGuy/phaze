"""Epic phaze-zlv verification: the single-source compute-lane invariant lock.

The epic reshaped the three operator-facing compute-lane surfaces so they all resolve from ONE
derivation path -- there is exactly one lane-identity source,
:func:`phaze.services.agent_liveness.derive_compute_lane_identities`, and ONE registry projection,
:func:`phaze.services.agent_liveness.non_local_backend_kinds`, that both the lanes and the file
badges consume. This module proves no SECOND derivation path exists by stubbing
``derive_compute_lane_identities`` ONCE (a fixed ``vox`` ACTIVE + ``xenolab`` IDLE lane list) and
asserting all three surfaces reflect that single source:

* (a) the header agent count -- ``dag['computeLanesActive']`` seeded onto ``GET /pipeline/stats``;
* (b) the Agents-page Section-2 tiles -- ``GET /admin/agents`` (+ the 5s ``/_table`` poll partial);
* (c) the Analyze-stage file badges -- ``get_analyze_working_set`` derives ``lane_kind`` through the
      SAME ``non_local_backend_kinds`` registry projection the lane identities are built from.

An import-identity test additionally pins that every consumer references the SAME function objects
(no shadow copy / re-derivation), so the stub-once/observe-everywhere proof is structurally sound.

Harness idiom mirrors ``tests/integration/`` (auto-marked ``integration`` by ``tests/conftest.py``)
and reuses the shared hermetic ``client`` / ``session`` / ``make_file`` / ``backends_toml_env``
fixtures. Run with real PG via ``just integration-test`` (ephemeral Postgres :5433).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.services.agent_liveness import ComputeLane


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


# Two non-local compute clusters -- vox (doing work) + xenolab (configured but quiet). Both carry
# ``kind = "compute"`` so the registry projection maps each backend_id -> "compute", which is what
# the file-badge surface (c) independently reads.
_TWO_CLUSTER_REGISTRY = """
    [[backends]]
    kind = "compute"
    id = "vox"
    rank = 10
    cap = 2
    agent_ref = "vox-node"
    scratch_dir = "/scratch/vox"
    push_host = "vox.push"

    [[backends]]
    kind = "compute"
    id = "xenolab"
    rank = 20
    cap = 2
    agent_ref = "xenolab-node"
    scratch_dir = "/scratch/xenolab"
    push_host = "xenolab.push"
    """

# THE fixed lane list every surface must reflect: vox ACTIVE (running work) + xenolab IDLE (listed,
# never DEAD). computeLanesActive must count ONLY the ACTIVE lane (== 1).
_STUB_LANES = [
    ComputeLane(backend_id="vox", kind="compute", state="ACTIVE", running=2, waiting=0),
    ComputeLane(backend_id="xenolab", kind="compute", state="IDLE", running=0, waiting=0),
]


async def _stub_derive_compute_lane_identities(_session: AsyncSession) -> list[ComputeLane]:
    """The SINGLE stub every surface consumes -- returns the fixed lane list, ignores the session."""
    return list(_STUB_LANES)


def test_single_derivation_symbol_is_shared_by_all_consumers() -> None:
    """Every consumer references the SAME derivation + registry-projection function objects (no shadow copy).

    The stub-once/observe-everywhere proof only holds if there is literally one derivation path. Both
    routers import ``derive_compute_lane_identities`` and the file-badge service + the Agents router
    import ``non_local_backend_kinds`` from ``phaze.services.agent_liveness`` -- assert those bound names
    are the very same objects the service module exposes, so no surface can carry a second derivation.
    """
    import phaze.routers.admin_agents as admin_agents_router
    import phaze.routers.pipeline as pipeline_router
    import phaze.services.agent_liveness as agent_liveness
    import phaze.services.pipeline as pipeline_service

    # ONE lane-identity source, referenced identically by both HTTP surfaces.
    assert pipeline_router.derive_compute_lane_identities is agent_liveness.derive_compute_lane_identities
    assert admin_agents_router.derive_compute_lane_identities is agent_liveness.derive_compute_lane_identities
    # ONE registry projection, referenced identically by the file-badge service and the Agents router.
    assert pipeline_service.non_local_backend_kinds is agent_liveness.non_local_backend_kinds
    assert admin_agents_router.non_local_backend_kinds is agent_liveness.non_local_backend_kinds


@pytest.mark.asyncio
async def test_all_three_surfaces_reflect_one_stubbed_lane_list(
    client: AsyncClient,
    session: AsyncSession,
    make_file,  # type: ignore[no-untyped-def]
    backends_toml_env,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the ONE derivation once; assert the header seed, Agents tiles, and file badge all reflect it.

    Patches ``derive_compute_lane_identities`` in BOTH router namespaces to the same fixed-list stub
    (vox ACTIVE + xenolab IDLE), then reads all three surfaces. Because there is a single derivation
    path and a single registry projection, the header count, the Section-2 tiles, and the file-badge
    kind mapping all agree with the one stubbed source -- proving no second derivation path exists.
    """
    backends_toml_env(_TWO_CLUSTER_REGISTRY)
    monkeypatch.setattr("phaze.routers.pipeline.derive_compute_lane_identities", _stub_derive_compute_lane_identities)
    monkeypatch.setattr("phaze.routers.admin_agents.derive_compute_lane_identities", _stub_derive_compute_lane_identities)

    # ---- Surface (a): the header agent count seed on the 5s stats poll -------------------------------
    # computeLanesActive counts ONLY the ACTIVE lane (vox) -- the IDLE xenolab lane is not an online
    # worker. The OOB seed writes the store key server-side (stats_bar.html dag.items() loop).
    stats = await client.get("/pipeline/stats")
    assert stats.status_code == 200, stats.text
    assert "$store.pipeline.computeLanesActive = 1" in stats.text, "header seed must reflect the ONE ACTIVE stubbed lane"

    # ---- Surface (b): the Agents-page Section-2 tiles (full page + the /_table poll partial) ---------
    for path in ("/admin/agents", "/admin/agents/_table"):
        resp = await client.get(path)
        assert resp.status_code == 200, resp.text
        section2 = resp.text.split('id="compute-lanes"', 1)[1]
        assert "vox" in section2, f"vox tile missing from Section 2 of {path}"
        assert "xenolab" in section2, f"xenolab tile missing from Section 2 of {path}"
        assert "ACTIVE" in section2, f"vox ACTIVE pill missing from {path}"
        assert "IDLE" in section2, f"xenolab IDLE pill missing from {path}"
        # A compute lane is an ephemeral Job-based identity -- never a perpetually-DEAD agent.
        assert "DEAD" not in section2, f"DEAD leaked into Section 2 of {path}"

    # ---- Surface (c): the Analyze-stage file badge derives its kind from the SAME registry projection.
    # The badge does NOT re-derive lanes; it reads non_local_backend_kinds(settings) -- the exact helper
    # the lane identities are composed from -- so a vox-stamped cloud job's badge kind == the vox lane's
    # kind. Seed a RUNNING vox cloud job and assert the derived badge, then pin the registry-projection
    # agreement for every stubbed lane.
    from phaze.config import get_settings
    from phaze.services.agent_liveness import non_local_backend_kinds
    from phaze.services.pipeline import get_analyze_working_set

    file = await make_file(original_filename="vox-analyze.mp3")
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            s3_key=f"staging/{file.id}",
            status=CloudJobStatus.RUNNING.value,
            backend_id="vox",
        )
    )
    await session.commit()

    analyze_files = (await get_analyze_working_set(session)).rows
    vox_badges = [f for f in analyze_files if f["lane"] == "vox"]
    assert len(vox_badges) == 1, "the vox-stamped analyze file must carry a per-cluster lane badge"
    assert vox_badges[0]["lane_kind"] == "compute", "the file-badge kind must come from the registry projection"

    # The single-source guarantee: the registry projection the badge reads maps EACH stubbed lane's
    # backend_id back to that lane's own kind -- one registry answer, shared by lanes and badges.
    projection = non_local_backend_kinds(get_settings())  # type: ignore[arg-type]
    for lane in _STUB_LANES:
        assert projection[lane.backend_id] == lane.kind, f"registry projection disagrees with the {lane.backend_id} lane kind"
