"""The drain's SAQ entry points (phaze-fq9h.7). No network, no browser, no live requests.

The service layer's behaviour is covered in ``tests/identify/services/test_tracklist_drain.py``.
What is asserted HERE is the task shell, which has its own failure modes and is the layer an
operator actually pushes buttons on:

* the browser and the http client are ALWAYS torn down, including when the pass raises -- a leaked
  headful Chrome plus its Xvfb display is one orphaned process per job, inside a worker image
  (phaze-fq9h.5) where nothing else will reap it;
* the propagation gate fails toward ``EXACT``, so a typo in an operator's argument can never
  silently WIDEN it into writing wrong tracklists;
* the status read spends no requests at all, because "how much is left" is asked far more often
  than a run is started;
* the two tasks are registered AND routable, since a task registered in only one of the two places
  is either unroutable or unconsumable.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze.enums.tracklist_candidate import DuplicateConfidence
from phaze.services.enqueue_router import CONTROLLER_TASKS
from phaze.tasks import tracklist_drain as task_module
from phaze.tasks.controller import settings as controller_settings
from phaze.tasks.tracklist_drain import _parse_confidence, _parse_file_ids, drain_tracklists, tracklist_drain_status


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


class RecordingScraper:
    """Stands in for ``TracklistScraper``: never opens a client, records that it was closed."""

    def __init__(self) -> None:
        self.closed = False

    async def search(self, query: str) -> list[Any]:  # noqa: ARG002 -- protocol signature
        return []

    async def close(self) -> None:
        self.closed = True


class RecordingRenderer:
    """Stands in for ``TracklistRenderer``: never launches a browser, records that it was closed."""

    def __init__(self) -> None:
        self.closed = False

    async def render(self, url: str) -> Any:  # noqa: ARG002 -- protocol signature
        raise AssertionError("no render should be reached in these tests")

    async def aclose(self) -> None:
        self.closed = True


def ctx_for(session: AsyncSession) -> dict[str, Any]:
    """A SAQ ctx whose ``async_session`` hands back the test's savepoint-nested session."""

    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return {"async_session": _factory}


@pytest.fixture
def fake_clients(monkeypatch: pytest.MonkeyPatch) -> tuple[RecordingScraper, RecordingRenderer]:
    scraper, renderer = RecordingScraper(), RecordingRenderer()
    monkeypatch.setattr(task_module, "TracklistScraper", lambda: scraper)
    monkeypatch.setattr(task_module, "TracklistRenderer", lambda: renderer)
    return scraper, renderer


class TestArgumentParsing:
    def test_file_ids_are_parsed_and_bad_ones_dropped_rather_than_failing_the_slice(self) -> None:
        """A typo in a flag list must not stop a months-long drain for a cosmetic reason."""
        good = uuid.uuid4()
        assert _parse_file_ids([str(good), "not-a-uuid", ""]) == [good]
        assert _parse_file_ids(None) == []

    @pytest.mark.parametrize("value", ["low", "medium", "high", "exact"])
    def test_every_real_tier_is_accepted(self, value: str) -> None:
        assert _parse_confidence(value) is DuplicateConfidence(value)

    @pytest.mark.parametrize("value", [None, "exakt", "EXACT", "very-sure", ""])
    def test_anything_unrecognized_falls_back_to_the_strictest_tier(self, value: str | None) -> None:
        """Fails CLOSED. A typo that widened the gate would write wrong tracklists silently."""
        assert _parse_confidence(value) is DuplicateConfidence.EXACT


class TestTaskShell:
    async def test_an_empty_corpus_returns_a_zero_report_and_closes_both_clients(
        self, session: AsyncSession, fake_clients: tuple[RecordingScraper, RecordingRenderer]
    ) -> None:
        scraper, renderer = fake_clients
        result = await drain_tracklists(ctx_for(session), limit=5)

        assert result["attempted"] == 0
        assert result["host_requests"] == 0
        assert (scraper.closed, renderer.closed) == (True, True)

    async def test_the_browser_is_torn_down_even_when_the_pass_raises(
        self, session: AsyncSession, fake_clients: tuple[RecordingScraper, RecordingRenderer], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leaked headful Chrome is one orphaned process per job, with nothing to reap it."""
        scraper, renderer = fake_clients

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("pass exploded")

        monkeypatch.setattr(task_module, "drain_once", boom)
        with pytest.raises(RuntimeError, match="pass exploded"):
            await drain_tracklists(ctx_for(session), limit=5)

        assert (scraper.closed, renderer.closed) == (True, True)

    @pytest.mark.usefixtures("fake_clients")
    async def test_the_gate_argument_reaches_the_pass(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        async def capture(_factory: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            from phaze.services.tracklist_drain import DrainReport

            return DrainReport()

        monkeypatch.setattr(task_module, "drain_once", capture)
        flagged = uuid.uuid4()
        target = uuid.uuid4()
        await drain_tracklists(
            ctx_for(session),
            limit=7,
            flagged_file_ids=[str(flagged)],
            target_file_ids=[str(target)],
            propagation_min_confidence="high",
            agent_id="a1",
        )

        assert seen["propagation_min"] is DuplicateConfidence.HIGH
        assert seen["flagged_file_ids"] == [flagged]
        assert seen["target_file_ids"] == [target]
        assert (seen["limit"], seen["agent_id"]) == (7, "a1")


class TestStatusRead:
    async def test_status_reports_the_funnel_without_spending_a_request(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """ "How long will this take" is asked constantly and must never cost budget."""
        from phaze.models.metadata import FileMetadata

        file = await make_file(original_filename="Artist - Live @ Event 2024-04-12.mp3")
        session.add(FileMetadata(file_id=file.id, duration=3600.0))
        await session.flush()

        status = await tracklist_drain_status(ctx_for(session))

        assert status["queued"] == 1
        assert status["unique_sets"] == 1
        assert status["collapse_ratio"] == pytest.approx(1.0)
        assert status["projected_days"] >= 0.0
        assert len(status["next_set_keys"]) == 1


class TestRegistration:
    """A task registered in only one of the two places is unroutable or unconsumable."""

    def test_both_tasks_are_registered_on_the_controller_worker(self) -> None:
        registered = {fn.__name__ for fn in controller_settings["functions"]}
        assert {"drain_tracklists", "tracklist_drain_status"} <= registered

    def test_both_tasks_are_routable(self) -> None:
        assert {"drain_tracklists", "tracklist_drain_status"} <= CONTROLLER_TASKS

    def test_the_drain_has_no_cron_job(self) -> None:
        """Operator-initiated by design (the epic's ethics bound), never a self-starting crawl."""
        cron_functions = {job.function.__name__ for job in controller_settings["cron_jobs"]}
        assert "drain_tracklists" not in cron_functions
