"""Phase 93 (CONSOLE-01): the record slide-in's Stage-Eligibility pills carry the REAL derived status.

Before this phase the six trace-trigger pills were status-blind — a file whose Files-matrix row
showed Meta=done / Analyze=in-flight rendered six identical plain pills. Now each stage row ALSO
renders the shared five-bucket ``_stage_pill.html`` token (the exact partial the Files matrix
renders), fed by the SAME ``stage_status_case`` derivation via ``get_file_stage_buckets`` — one
status source, no divergent second derivation (D-00a honesty).

Rendered through the REAL record slide-in endpoint (``GET /record/{id}`` -> ``record_body.html``),
mirroring ``test_eligibility_trace.py``'s composition-level idiom. The trace-trigger and force-skip
contracts are locked by that module and must stay green alongside this one.

Must pass in the ``shared`` bucket in isolation (consumes the DB fixtures -> auto-marked integration).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.enums.stage import Stage
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.tasks._shared.stage_control import STAGE_TO_FUNCTION


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


_SRC = Path(__file__).resolve().parents[2] / "src" / "phaze"


async def _seed_file(session: AsyncSession) -> uuid.UUID:
    """Seed a committed FileRecord (FK anchor for the per-stage marker rows)."""
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            agent_id="test-fileserver",
            id=file_id,
            sha256_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
            original_path=f"/test/music/{file_id}.mp3",
            original_filename=f"{file_id}.mp3",
            current_path=f"/test/music/{file_id}.mp3",
            file_type="mp3",
            file_size=1024,
        )
    )
    await session.commit()
    return file_id


async def _seed_mixed_state(session: AsyncSession) -> uuid.UUID:
    """Seed the CONSOLE-01 repro shape: metadata DONE + analyze IN-FLIGHT (everything else untouched)."""
    file_id = await _seed_file(session)
    session.add(FileMetadata(file_id=file_id, failed_at=None))
    func_name = STAGE_TO_FUNCTION[Stage.ANALYZE.value]
    session.add(
        SchedulingLedger(
            key=f"{func_name}:{file_id}",
            function=func_name,
            routing="agent",
            payload={"file_id": str(file_id)},
        )
    )
    await session.commit()
    return file_id


@pytest.mark.asyncio
async def test_record_pills_show_mixed_derived_statuses(client: AsyncClient, session: AsyncSession) -> None:
    """Meta=done / Analyze=in-flight renders VISIBLY DISTINCT status tokens, matching the Files-matrix row."""
    file_id = await _seed_mixed_state(session)

    body = (await client.get(f"/record/{file_id}")).text

    # The shared _stage_pill.html tokens (glyph + word + aria-label), per stage label used in the pane.
    assert 'aria-label="Meta: done"' in body, "metadata pill must carry the derived done token"
    assert 'aria-label="Analyze: in flight"' in body, "analyze pill must carry the derived in-flight token"
    assert 'aria-label="Prop: not started"' in body, "untouched propose pill must carry the not-started token"


@pytest.mark.asyncio
async def test_record_pills_render_all_six_stage_statuses(client: AsyncClient, session: AsyncSession) -> None:
    """Every one of the six matrix stages carries a derived status token (none left status-blind)."""
    file_id = await _seed_file(session)  # untouched file -> all six not_started

    body = (await client.get(f"/record/{file_id}")).text

    for label in ("Meta", "FP", "Analyze", "Prop", "Appr", "Exec"):
        assert f'aria-label="{label}: not started"' in body, f"{label} pill is status-blind (no derived token)"


def test_record_pills_reuse_single_status_source() -> None:
    """One status source (D-00a): the pane renders the SHARED _stage_pill partial fed by stage_status_case.

    Source-level guard against a divergent second derivation: record_body.html must include the
    same ``_stage_pill.html`` partial the Files matrix renders, and the record router must obtain
    its buckets from the shared services derivation (``get_file_stage_buckets``), never re-derive.
    """
    template = (_SRC / "templates" / "record" / "record_body.html").read_text()
    assert 'include "pipeline/partials/_stage_pill.html"' in template, (
        "record_body.html must render the SHARED _stage_pill.html token partial — a bespoke pill markup here is a divergent second status rendering"
    )
    router = (_SRC / "routers" / "record.py").read_text()
    assert "get_file_stage_buckets" in router, (
        "record.py must source its per-stage buckets from the shared services derivation "
        "(get_file_stage_buckets), the same stage_status_case ladder the Files matrix uses"
    )
