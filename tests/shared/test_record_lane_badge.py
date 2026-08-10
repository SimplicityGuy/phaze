"""phaze-lljfx: the record page's facts-grid Lane tile reflects the file's REAL lane.

Regression for the hardcoded ``🖥️ local`` literal in ``record_body.html`` -- ``GET /record/{id}``
used to assert local for every file regardless of how it was actually analyzed. Mirrors
``test_record_tracklist_review.py``'s idiom: seed a file into each lane shape a real ``CloudJob``
row can produce, then assert the REAL record endpoint renders the honest badge for each, matching
the same ``CloudJob.backend_id`` -> registry-``kind`` derivation the Analyze matrix
(``_analyze_files.html`` / ``services.pipeline.derive_file_lane``) uses.

Must pass in the ``shared`` bucket in isolation (consumes the DB fixtures -> auto-marked integration).
"""

from __future__ import annotations

import uuid

import pytest

from phaze.config import get_settings
from phaze.config_backends import ComputeBackend, KubeConfig, KueueBackend
from phaze.models.cloud_job import CloudJob, CloudJobStatus


@pytest.fixture(autouse=True)
def _local_only_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default this module to an all-local registry -- most cases here assert non-local lanes
    off the file's OWN ``CloudJob.backend_id``, not off registry-wide routing, and a stray
    compute/kueue default (as other router-test modules pin) would make the "no cloud_job at all"
    case ambiguous about which signal actually produced "local".

    ``routers/record.py`` reads the registry through ``get_settings()`` (the ``@lru_cache``d
    singleton), NOT the separate module-level ``phaze.config.settings`` instance other router
    test modules patch for OTHER endpoints -- the two are genuinely different objects, so this
    module patches the one this route actually reads.
    """
    monkeypatch.setattr(get_settings(), "backends", [])


async def test_no_cloud_job_renders_local_lane(client, session, make_file) -> None:  # type: ignore[no-untyped-def]
    """No ``CloudJob`` row at all -- analyzed purely locally -- still reads 'local', honestly."""
    file_rec = await make_file()

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "🖥️ local" in body
    assert "☁️" not in body
    assert "⎈" not in body


async def test_compute_backend_job_renders_compute_lane(client, session, make_file, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A CloudJob attributed to a registered compute backend reads '☁️ <backend id>', not 'local'."""
    compute_backend = ComputeBackend(kind="compute", id="a1", rank=10, cap=2, agent_ref="cloud-1", scratch_dir="/scratch", push_host="a1.push")
    monkeypatch.setattr(get_settings(), "backends", [compute_backend])

    file_rec = await make_file()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_rec.id, status=CloudJobStatus.RUNNING.value, backend_id="a1"))
    await session.commit()

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "☁️ a1" in body
    assert "🖥️ local" not in body


async def test_kueue_backend_job_renders_kueue_lane(client, session, make_file, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A CloudJob attributed to a registered kueue backend reads '⎈ <backend id>', matching the
    Analyze pane's per-file row for a burst-to-cluster file (the bead's own failure scenario)."""
    kueue_backend = KueueBackend(kind="kueue", id="k8s", rank=10, cap=2, kube=KubeConfig())
    monkeypatch.setattr(get_settings(), "backends", [kueue_backend])

    file_rec = await make_file()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_rec.id, status=CloudJobStatus.RUNNING.value, backend_id="k8s"))
    await session.commit()

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "⎈ k8s" in body
    assert "🖥️ local" not in body


async def test_unattributed_cloud_job_falls_back_to_the_cloud_glyph(client, session, make_file) -> None:  # type: ignore[no-untyped-def]
    """A stamped CloudJob with no backend_id yet (not attributed to a registry cluster) reads the
    truthful unattributed 'cloud' fallback -- never a silent 'local'."""
    file_rec = await make_file()
    session.add(CloudJob(id=uuid.uuid4(), file_id=file_rec.id, status=CloudJobStatus.AWAITING.value, backend_id=None))
    await session.commit()

    body = (await client.get(f"/record/{file_rec.id}")).text

    assert "▪ cloud" in body
    assert "🖥️ local" not in body
