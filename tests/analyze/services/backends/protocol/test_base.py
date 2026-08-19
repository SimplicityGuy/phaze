"""Tests for `services/backends/base.py` (split from test_backends.py, phaze-7l8jh).

The Layer 2 D-02 cross-backend `in_flight_count` equivalence invariant -- `services/backends/base.py`.
"""

from __future__ import annotations

from tests.analyze.services.backends.protocol._shared import *


# === Layer 2: D-02 equivalence invariant =================================================


@pytest.mark.asyncio
async def test_in_flight_equivalence(session: AsyncSession) -> None:
    """D-02: sum(in_flight_count(b)) == the derived in-flight window for the single-backend case.

    Construct a set of in-flight cloud_job rows for one compute backend; the per-backend cloud_job
    count must equal the number of distinct files carrying an in-flight cloud_job row. Post-MIG-04
    the window derives ONLY from the ``cloud_job`` sidecar (there is no scalar ``{PUSHING, PUSHED}``
    state to count). A divergence is the Pitfall-1 double/under-count bug -- every in-flight row maps
    to exactly one windowed file.
    """
    from sqlalchemy import func, select

    backend = _compute(id="compute-a1")
    for status in IN_FLIGHT_STATUSES:
        await _seed_cloud_job(session, backend_id="compute-a1", status=status)

    resolved = [backend]
    per_backend = sum([await b.in_flight_count(session) for b in resolved])
    window = int(
        (
            await session.execute(
                select(func.count(func.distinct(CloudJob.file_id))).where(CloudJob.status.in_([s.value for s in IN_FLIGHT_STATUSES]))
            )
        ).scalar()
        or 0
    )
    assert per_backend == window
