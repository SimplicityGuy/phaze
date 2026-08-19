"""Tests for `services/pipeline/files.py` (split from test_pipeline.py, phaze-7l8jh).

get_file_stage_buckets and friends -- `services/pipeline/files.py`.
"""

from __future__ import annotations

from tests.shared.services.pipeline._shared import (
    _NullSavepoint,
    pytest,
    uuid,
)


@pytest.mark.asyncio
async def test_get_file_stage_buckets_degrades_to_all_not_started_on_db_error() -> None:
    """A DB error on the single-file stage-buckets read degrades to an all-``not_started`` mapping.

    Mirrors ``get_files_page``'s SAVEPOINT degrade posture -- the record slide-in pane renders
    instead of 500ing.
    """
    from phaze.services.pipeline import get_file_stage_buckets

    class _ExplodingSession:
        def begin_nested(self) -> _NullSavepoint:
            return _NullSavepoint()

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            msg = 'relation "files" does not exist'
            raise RuntimeError(msg)

    buckets = await get_file_stage_buckets(_ExplodingSession(), uuid.uuid4())  # type: ignore[arg-type]

    assert buckets, "the not-started fallback mapping must not be empty"
    assert all(v == "not_started" for v in buckets.values())
