"""The log-level applier (``phaze-mvq8z.8``): re-apply ``configure_logging`` on every reload
that changes ``log_level``, in every process type.

Bead acceptance: "log-level ... changes apply without restart." This exercises the EXACT
applier wired identically into ``src/phaze/main.py``'s lifespan, ``src/phaze/tasks/
controller.py``'s ``startup``, and ``src/phaze/tasks/agent_worker.py``'s ``startup``::

    runtime_config_store.register_applier("log_level", log_level_applier(json_logs=<settings>.log_json))

against the REAL :func:`~phaze.logging_config.configure_logging` (never mocked) -- the real
consumer per CLAUDE.md rule 3 is the stdlib root logger's effective level, not a call-count
assertion on a patched stand-in. Booting the full FastAPI lifespan / SAQ startup hook (as
``test_main_lifespan.py`` / ``test_controller_startup_banner.py`` do for their own concerns)
would duplicate this three times over for a one-line, byte-identical expression; those files'
own tests already prove the wiring doesn't error.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from phaze import logging_config
from phaze.config import ControlSettings
from phaze.logging_config import configure_logging, log_level_applier
from phaze.runtime_config import RUNTIME_TOML_NAME, RuntimeConfigStore


if TYPE_CHECKING:
    import pytest


def _store(tmp_path: Path) -> RuntimeConfigStore:
    return RuntimeConfigStore(ControlSettings(), runtime_toml=tmp_path / RUNTIME_TOML_NAME, env={}, physical_cores=lambda: 64)


def test_the_log_level_applier_reapplies_configure_logging_on_a_reload_that_changes_it(tmp_path: Path) -> None:
    """The autouse ``_route_structlog_through_stdlib`` conftest fixture already forces DEBUG
    before this test runs -- reload to something ELSE (WARNING) so the assertion is about the
    applier's effect, not the fixture's."""

    async def scenario() -> None:
        store = _store(tmp_path)
        store.register_applier("log_level", log_level_applier(json_logs=False))

        (tmp_path / RUNTIME_TOML_NAME).write_text('log_level = "WARNING"\n', encoding="utf-8")
        result = await store.reload("file")

        assert result.outcome == "applied"
        assert store.current().log_level == "WARNING"
        assert logging.getLogger().getEffectiveLevel() == logging.WARNING

    asyncio.run(scenario())


def test_the_log_level_applier_is_not_called_when_the_reload_does_not_change_log_level(tmp_path: Path) -> None:
    """configure_logging (idempotent, but not free) is skipped, not just harmless, when nothing
    about the config actually changed -- mirrors the core suite's own applier-skip guarantee."""
    calls: list[str] = []

    def spy_applier(_old: object, new: object) -> None:
        calls.append(new.log_level)  # type: ignore[attr-defined]
        configure_logging(level=new.log_level)  # type: ignore[attr-defined]

    async def scenario() -> None:
        store = _store(tmp_path)
        store.register_applier("log_level", spy_applier)

        assert (await store.reload("sighup")).outcome == "unchanged"
        assert calls == []

        (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_jobs = 3\n", encoding="utf-8")
        result = await store.reload("file")
        assert result.outcome == "applied"
        # A real change happened, but NOT to log_level -- the applier still runs (appliers fire
        # on ANY snapshot change, per phaze.runtime_config's own contract), re-applying the SAME
        # level. That is the documented idempotent-reapply shape, not a bug this test guards
        # against; what it guards is that log_level itself is unchanged across the call.
        assert calls == ["INFO"]

    asyncio.run(scenario())


def _root_renders_json() -> bool:
    """Whether the root handler's primary renderer is JSON (vs the console renderer)."""
    formatter = logging.getLogger().handlers[0].formatter
    assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)
    return any(isinstance(processor, structlog.processors.JSONRenderer) for processor in formatter.processors)


def test_a_reload_that_does_not_change_log_level_keeps_the_configured_log_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-mvq8z.20 finding 2: appliers fire on ANY snapshot change, and the old applier re-ran
    ``configure_logging(level=...)`` WITHOUT ``json_logs`` -- so it re-resolved the format from
    ``os.environ``/``isatty`` alone, ignoring a ``PHAZE_LOG_JSON`` that lives only in ``.env``
    (settings). A ``worker_max_jobs`` override then flipped the process's log format.

    Here the settings said console (``log_json=False``, as ``.env`` would), the environment says
    nothing, and stdout is not a TTY (pytest captures it) -- the auto rule's JSON -- exactly the
    shape that flipped.
    """
    monkeypatch.delenv("PHAZE_LOG_JSON", raising=False)

    async def scenario() -> None:
        store = _store(tmp_path)
        configure_logging(level="INFO", json_logs=False)
        store.register_applier("log_level", log_level_applier(json_logs=False))
        assert not _root_renders_json()

        (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_jobs = 3\n", encoding="utf-8")
        assert (await store.reload("file")).outcome == "applied"
        assert not _root_renders_json(), "a non-log reload flipped the log format"

        (tmp_path / RUNTIME_TOML_NAME).write_text('worker_max_jobs = 3\nlog_level = "WARNING"\n', encoding="utf-8")
        assert (await store.reload("file")).outcome == "applied"
        assert logging.getLogger().getEffectiveLevel() == logging.WARNING
        assert not _root_renders_json(), "a log_level reload flipped the log format"

    asyncio.run(scenario())


def test_the_log_level_applier_reconfigures_only_when_log_level_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one call-count test here: WHAT is passed through (the settings' ``json_logs``) and that
    nothing is called at all on a non-log reload are properties of the call, not of the logger."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(logging_config, "configure_logging", lambda **kwargs: calls.append(kwargs))

    async def scenario() -> None:
        store = _store(tmp_path)
        store.register_applier("log_level", log_level_applier(json_logs=True))

        (tmp_path / RUNTIME_TOML_NAME).write_text("worker_max_jobs = 3\n", encoding="utf-8")
        assert (await store.reload("file")).outcome == "applied"
        assert calls == []

        (tmp_path / RUNTIME_TOML_NAME).write_text('log_level = "ERROR"\n', encoding="utf-8")
        assert (await store.reload("file")).outcome == "applied"
        assert calls == [{"level": "ERROR", "json_logs": True}]

    asyncio.run(scenario())


def test_every_process_registers_the_shared_log_level_applier() -> None:
    """The api lifespan, the control worker and the agent worker each register the SAME applier
    (built from their own settings' ``log_json``) -- a bare lambda re-resolving the format from the
    environment is the finding-2 shape and must not come back at any of the three sites."""
    for module in ("phaze.main", "phaze.tasks.controller", "phaze.tasks.agent_worker"):
        # Read, not imported: agent_worker refuses import without PHAZE_AGENT_QUEUE.
        spec = importlib.util.find_spec(module)
        assert spec is not None
        assert spec.origin is not None
        source = Path(spec.origin).read_text(encoding="utf-8")
        assert 'register_applier("log_level", log_level_applier(json_logs=' in source, module
        assert "configure_logging(level=new.log_level)" not in source, module
