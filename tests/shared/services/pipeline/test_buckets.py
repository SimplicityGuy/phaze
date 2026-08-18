"""Tests for `services/pipeline/buckets.py` (split from test_pipeline.py, phaze-7l8jh).

_safe_bucket_counts / _safe_bucket_snapshot degrade behavior -- `services/pipeline/buckets.py`.
"""

from __future__ import annotations

from tests.shared.services.pipeline._shared import *


@pytest.mark.asyncio
async def test_safe_bucket_counts_degrades_when_savepoint_fails() -> None:
    """``_safe_bucket_counts`` degrades to all-zero when its SAVEPOINT cannot be opened."""
    from phaze.enums.stage import Stage

    class _SavepointExplodingSession:
        def begin_nested(self) -> object:
            msg = "connection already closed"
            raise RuntimeError(msg)

    out = await pipeline_mod._safe_bucket_counts(_SavepointExplodingSession(), Stage.ANALYZE)  # type: ignore[arg-type]

    assert out, "the zero-filled bucket dict must not be empty"
    assert all(v == 0 for v in out.values())
