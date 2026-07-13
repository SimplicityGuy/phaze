"""UI-02 / D-03: the failure/status filter lens over the single paginated files table (87-05).

The filter is "just another lens" on the ONE canonical ``GET /pipeline/files`` list -- there is no
separate failures page. These tests pin, against a real operator ``client``:

* ``?stage=metadata&bucket=failed`` returns ONLY files whose derived metadata bucket is ``failed`` --
  a done-metadata file and a plain (not-started) file are both excluded (UI-02 failure visibility);
* when a failed filter matches nothing, the failed-filter empty-state copy renders (Copywriting
  Contract: "No failed files in {stage}" / "Nothing is stuck in {stage} right now.");
* the filter bar carries URL state -- the swap target hx-gets ``/pipeline/files`` with ``hx-push-url``.

Uses the plain operator ``client`` + ``session`` fixtures (tests/conftest.py). The whole
``tests/integration/`` package is auto-marked ``integration``. The route derives each row's buckets
via the correlated ``stage_status_case`` columns (Plan 04), so the seed markers below (a metadata
``failed_at`` row vs. a metadata payload row) drive the buckets under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata


if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.integration


def _make_file(marker: str) -> FileRecord:
    """A FileRecord whose current_path carries a distinctive ``marker`` so we can assert row presence."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{marker}-{uid.hex}.mp3",
        original_filename=f"{marker}-{uid.hex}.mp3",
        current_path=f"/music/{marker}-{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )


@pytest.mark.asyncio
async def test_metadata_failed_filter_returns_only_failed_rows(client: AsyncClient, session: AsyncSession) -> None:
    """``?stage=metadata&bucket=failed`` renders ONLY the failed-metadata row (UI-02).

    A metadata ``failed_at`` row derives bucket ``failed``; a metadata payload row derives ``done``;
    a bare file derives ``not_started``. Only the first must appear in the filtered table.
    """
    failed = _make_file("failedmeta")
    done = _make_file("donemeta")
    plain = _make_file("plainfile")
    session.add_all([failed, done, plain])
    await session.commit()
    # failed metadata: failed_at set, payload NULL -> done(metadata) derives FAILED.
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    # done metadata: real payload, failed_at NULL -> derives DONE.
    session.add(FileMetadata(file_id=done.id, artist="Real", title="Track"))
    await session.commit()

    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed")
    assert resp.status_code == 200
    body = resp.text

    # Only the failed-metadata row is present; the done + not-started rows are filtered out.
    assert "failedmeta-" in body
    assert "donemeta-" not in body
    assert "plainfile-" not in body


@pytest.mark.asyncio
async def test_failed_filter_empty_renders_failed_filter_copy(client: AsyncClient, session: AsyncSession) -> None:
    """A failed filter that matches nothing renders the failed-filter empty-state copy (Copywriting Contract).

    Only a metadata failure is seeded, so filtering ``stage=fingerprint&bucket=failed`` matches zero rows --
    the empty branch must show "No failed files in Fingerprint" / "Nothing is stuck in Fingerprint right now.",
    NOT the unfiltered "No files yet" copy.
    """
    failed = _make_file("failedmeta")
    session.add(failed)
    await session.commit()
    session.add(FileMetadata(file_id=failed.id, failed_at=datetime.now(UTC), error_message="boom"))
    await session.commit()

    resp = await client.get("/pipeline/files?stage=fingerprint&bucket=failed")
    assert resp.status_code == 200
    body = resp.text

    assert "No failed files in Fingerprint" in body
    assert "Nothing is stuck in Fingerprint right now." in body
    # The unfiltered empty copy must NOT be what renders under an active failed filter.
    assert "No files yet" not in body


@pytest.mark.asyncio
async def test_filter_state_is_url_carried(client: AsyncClient, session: AsyncSession) -> None:
    """The filter bar carries URL state: it hx-gets /pipeline/files with hx-push-url (survives back/forward)."""
    resp = await client.get("/pipeline/files?stage=metadata&bucket=failed")
    assert resp.status_code == 200
    body = resp.text

    # The status filter bar is present and pushes filter state into the URL (D-03).
    assert 'id="status-filter-bar"' in body
    assert 'hx-get="/pipeline/files"' in body
    assert 'hx-push-url="true"' in body
    # The active filter axes are reflected as selected options (survives the record slide-in re-render).
    assert '<option value="metadata" selected>' in body
    assert '<option value="failed" selected>' in body
