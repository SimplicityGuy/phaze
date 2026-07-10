"""The three former ``get_pipeline_stats`` callers re-express the seven keys off ``get_stage_progress`` (READ-02, D-05).

Phase 82 removes ``get_pipeline_stats`` (the linear ``GROUP BY FileRecord.state`` count) entirely and
re-expresses its seven consumed keys from the derived ``get_stage_progress`` output-table counts. This
file locks the router-side contract:

* ``build_dashboard_context`` seeds a derived ``stats`` dict whose seven keys equal the mapped
  ``get_stage_progress`` values (discovered->discovery.done, metadata_extracted->metadata.done,
  fingerprinted->fingerprint.done, analyzed->analyze.done, proposal_generated->proposals.done,
  approved->execute.total, executed->execute.done).
* ``_build_dag_context`` sets ``notYetEnriched = max(metadata.total - metadata.done, 0)`` (D-05) --
  NOT the old ``discovered - metadata_extracted`` (which read ``FileRecord.state``).
* the ``/pipeline/stats`` poll partial still emits the three OOB store ids writing into
  ``$store.pipeline.discovered / .metadataExtracted / .analyzed`` (Pitfall 4 -- the Alpine store keys
  stay stable; only the server-side source changes).
* ``get_pipeline_stats`` is GONE and no ``GROUP BY FileRecord.state`` survives in the stats path.

RED against the pre-cutover code: ``get_pipeline_stats`` still exists (the removed-function assertion
fails), and the seeded corpus makes the state-derived counts differ from the output-derived ones (the
derivation + notYetEnriched assertions fail). GREEN after Tasks 2/3.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord, FileState
from phaze.models.metadata import FileMetadata
from phaze.services.pipeline import get_stage_progress


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _music_file(*, state: str = FileState.DISCOVERED) -> FileRecord:
    uid = uuid.uuid4()
    return FileRecord(
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
        state=state,
    )


async def _seed_state_derived_divergent_corpus(session: AsyncSession) -> None:
    """Seed a corpus where the OUTPUT-table counts diverge from the linear-state counts.

    Two files carry a metadata row but sit in ``METADATA_EXTRACTED`` state, and one is a bare
    ``DISCOVERED`` file. Post-cutover the derived ``discovery.done`` counts ALL files (3) while the
    old ``FileRecord.state`` ``discovered`` count is 1 -- so an assertion tying ``stats`` to
    ``get_stage_progress`` is RED against the state-derived code and GREEN once derived.
    """
    for _ in range(2):
        f = _music_file(state=FileState.METADATA_EXTRACTED)
        session.add(f)
        await session.flush()  # persist the FK parent before adding the child metadata row
        session.add(FileMetadata(file_id=f.id, failed_at=None))  # metadata done (row + failed_at NULL)
    session.add(_music_file(state=FileState.DISCOVERED))  # bare -> metadata not_started
    await session.commit()


@pytest.mark.asyncio
async def test_get_pipeline_stats_is_removed_no_filestate_group_by(client: AsyncClient) -> None:
    """``get_pipeline_stats`` no longer exists and no ``GROUP BY FileRecord.state`` survives in the stats path (D-05, SC#2)."""
    import phaze.routers.pipeline as router_mod
    import phaze.services.pipeline as service_mod

    assert not hasattr(service_mod, "get_pipeline_stats"), "get_pipeline_stats must be deleted (D-05)"
    assert "group_by(FileRecord.state)" not in inspect.getsource(service_mod), "no FileRecord.state GROUP BY may survive in the stats path"
    assert "get_pipeline_stats" not in inspect.getsource(router_mod), "the router must not reference the removed get_pipeline_stats"


@pytest.mark.asyncio
async def test_dashboard_context_stats_derived_from_stage_progress(client: AsyncClient, session: AsyncSession) -> None:
    """``build_dashboard_context`` seeds the seven ``stats`` keys off ``get_stage_progress`` (the derived re-expression table)."""
    from phaze.routers.pipeline import build_dashboard_context

    await _seed_state_derived_divergent_corpus(session)
    app_state = client._transport.app.state  # type: ignore[union-attr]

    progress = await get_stage_progress(session)
    ctx = await build_dashboard_context(app_state, session)
    stats = ctx["stats"]

    assert stats["discovered"] == int(progress["discovery"]["done"] or 0)
    assert stats["metadata_extracted"] == int(progress["metadata"]["done"] or 0)
    assert stats["fingerprinted"] == int(progress["fingerprint"]["done"] or 0)
    assert stats["analyzed"] == int(progress["analyze"]["done"] or 0)
    assert stats["proposal_generated"] == int(progress["proposals"]["done"] or 0)
    assert stats["approved"] == int(progress["execute"]["total"] or 0)
    assert stats["executed"] == int(progress["execute"]["done"] or 0)


@pytest.mark.asyncio
async def test_not_yet_enriched_is_metadata_total_minus_done(client: AsyncClient, session: AsyncSession) -> None:
    """``notYetEnriched`` == ``max(metadata.total - metadata.done, 0)`` -- derived, NOT ``discovered - metadata_extracted`` (D-05)."""
    from phaze.routers.pipeline import build_dashboard_context

    await _seed_state_derived_divergent_corpus(session)
    app_state = client._transport.app.state  # type: ignore[union-attr]

    progress = await get_stage_progress(session)
    expected = max(int(progress["metadata"]["total"] or 0) - int(progress["metadata"]["done"] or 0), 0)

    ctx = await build_dashboard_context(app_state, session)
    assert ctx["dag"]["notYetEnriched"] == expected


@pytest.mark.asyncio
async def test_pipeline_stats_partial_emits_stable_oob_store_ids(client: AsyncClient, session: AsyncSession) -> None:
    """The ``/pipeline/stats`` poll partial still emits the three OOB store writes into the STABLE Alpine keys (Pitfall 4)."""
    session.add(_music_file(state=FileState.DISCOVERED))
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    body = response.text

    # The three OOB store-write anchors + their stable $store.pipeline.* target keys.
    assert 'id="analyze-files-ready"' in body
    assert "$store.pipeline.discovered =" in body
    assert 'id="fingerprint-files-ready"' in body
    assert "$store.pipeline.metadataExtracted =" in body
    assert 'id="proposals-files-ready"' in body
    assert "$store.pipeline.analyzed =" in body


@pytest.mark.asyncio
async def test_pipeline_stats_partial_renders_derived_card_labels(client: AsyncClient, session: AsyncSession) -> None:
    """The stats bar still renders its six visible cards (the derived dict feeds the same template keys)."""
    f = _music_file(state=FileState.METADATA_EXTRACTED)
    session.add(f)
    await session.flush()  # persist the FK parent before adding child rows
    session.add(FileMetadata(file_id=f.id, failed_at=None))
    session.add(AnalysisResult(file_id=f.id, analysis_completed_at=None))
    await session.commit()

    response = await client.get("/pipeline/stats")
    assert response.status_code == 200
    body = response.text
    for label in ("Discovered", "Fingerprinted", "Analyzed", "Proposed", "Approved", "Executed"):
        assert label in body
