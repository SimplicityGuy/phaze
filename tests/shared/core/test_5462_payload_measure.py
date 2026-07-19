"""phaze-5462 payload measurement: the three enrich tabs under a LARGE backlog.

Not a permanent assertion of byte counts (brittle); this pins the ORDER OF MAGNITUDE the bead's
acceptance criteria demand -- analyze in the same ballpark as its siblings, not 180x.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.analysis import AnalysisResult

from ..services.test_pipeline import _make_pipeline_file


@pytest.mark.asyncio
async def test_enrich_tab_payloads_are_same_order_of_magnitude(session: AsyncSession, client: AsyncClient) -> None:
    """With a 500-file analyze backlog, /s/analyze must not dwarf /s/metadata and /s/fingerprint."""
    files = [_make_pipeline_file() for _ in range(500)]
    names = [f.original_filename for f in files]  # captured BEFORE commit (ORM rows expire on commit)
    session.add_all(files)
    await session.flush()
    for f in files:
        session.add(AnalysisResult(id=uuid.uuid4(), file_id=f.id, fine_windows_analyzed=1, fine_windows_total=10))
    await session.commit()

    sizes = {}
    for stage in ("metadata", "fingerprint", "analyze"):
        resp = await client.get(f"/s/{stage}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        sizes[stage] = len(resp.content)
        hits = sum(1 for name in names if name in resp.text)
        assert hits == 0, f"/s/{stage} server-rendered {hits} file rows inline"
        print(f"  /s/{stage}: {len(resp.content):,} bytes, {hits} filenames inline")

    # The fragment carries the rows. One page is bounded; the old inline render carried ALL of them.
    frag = await client.get("/pipeline/analyze-files")
    assert frag.status_code == 200
    rows_on_page = frag.text.count('hx-get="/record/')
    per_row = len(frag.content) / max(rows_on_page, 1)
    print(f"\nphaze-5462 payloads with a 500-file backlog: {sizes}")
    print(f"  fragment: {len(frag.content):,} bytes for {rows_on_page} rows (~{per_row:.0f} B/row)")
    print(f"  inline-render equivalent for all 500: ~{per_row * 500 / 1024:.0f} KiB (the retired behaviour)")
    assert rows_on_page <= 50, f"the fragment page is unbounded ({rows_on_page} rows)"
    baseline = max(sizes["metadata"], sizes["fingerprint"])
    assert sizes["analyze"] < baseline * 3, f"analyze payload {sizes['analyze']} dwarfs siblings {baseline} -- the inline render is back"
