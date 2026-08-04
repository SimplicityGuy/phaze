"""Tests for the ``learn_filename_conventions`` SAQ entry point + its registration (phaze-5fta.3).

The learning itself is covered in ``tests/identify/services/test_filename_convention_learner.py``.
What is asserted here is the SEAM: the task reads its session factory from the SAQ ctx, returns a
JSON-safe report, and is wired into the controller as an operator-enqueueable function with no cron
-- the three facts that decide whether an operator can actually run it.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from saq import Job

from phaze.services.enqueue_router import CONTROLLER_TASKS
from phaze.services.filename_convention_learner import CONVENTION_KIND, CONVENTION_SCOPE
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS, apply_deterministic_key
from phaze.tasks.filename_convention import learn_filename_conventions


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


def _ctx_for(session: AsyncSession) -> dict[str, Any]:
    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return {"async_session": _factory}


class TestTaskEntryPoint:
    async def test_returns_a_json_safe_report(self, session: AsyncSession, make_file: Any) -> None:
        for day in (13, 14, 15):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")

        result = await learn_filename_conventions(_ctx_for(session))

        assert result["groups_written"] == 1
        assert result["self_resolving"] == 3
        assert result["contradictions"] == 0
        assert isinstance(result["computed_at"], str)
        assert isinstance(result["rejection_reasons"], dict)

    async def test_batch_size_is_threaded_through_to_the_sweep(self, session: AsyncSession, make_file: Any) -> None:
        for day in (13, 14, 15, 16):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")

        result = await learn_filename_conventions(_ctx_for(session), batch_size=1)

        assert result["files_scanned"] == 4

    async def test_an_empty_corpus_is_a_clean_no_op(self, session: AsyncSession) -> None:
        assert (await learn_filename_conventions(_ctx_for(session)))["groups_written"] == 0


class TestRegistration:
    def test_registered_as_a_controller_function_with_no_cron(self) -> None:
        """Operator-triggered, deliberately: it sweeps the whole corpus and gates rename proposals."""
        from phaze.tasks import controller

        fn_names = {getattr(fn, "__name__", "") for fn in controller.settings["functions"]}
        cron_names = {getattr(cj.function, "__name__", "") for cj in controller.settings["cron_jobs"]}
        assert "learn_filename_conventions" in fn_names
        assert "learn_filename_conventions" not in cron_names

    def test_is_operator_routable(self) -> None:
        assert "learn_filename_conventions" in CONTROLLER_TASKS

    async def test_a_double_enqueue_collapses_onto_one_key(self) -> None:
        """A second click while a sweep is in flight must not start a second full-corpus sweep."""
        first = Job(function="learn_filename_conventions", kwargs={})
        second = Job(function="learn_filename_conventions", kwargs={"batch_size": 100})
        for job in (first, second):
            await apply_deterministic_key(job)

        assert first.key == second.key == "learn_filename_conventions:release_group:date_order"

    def test_learner_key_matches_the_learner_module(self) -> None:
        """The key text is duplicated (the agent import boundary forbids importing the service).

        This is the guard that keeps the duplication from drifting silently.
        """
        assert _KEY_BUILDERS["learn_filename_conventions"]({}) == f"{CONVENTION_SCOPE}:{CONVENTION_KIND}"
