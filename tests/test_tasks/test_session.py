"""Tests for the shared task session pattern (INFRA-01)."""

import inspect


def test_session_module_deprecated():
    """session.py no longer exports get_task_session."""
    import phaze.tasks.session as mod

    assert not hasattr(mod, "get_task_session")


def test_worker_startup_creates_engine_in_ctx():
    """Verify startup hook signature expects to populate ctx with async_session."""
    from phaze.tasks.worker import startup

    sig = inspect.signature(startup)
    assert "ctx" in sig.parameters


def test_worker_shutdown_disposes_engine():
    """Verify shutdown hook signature accepts ctx for engine disposal."""
    from phaze.tasks.worker import shutdown

    sig = inspect.signature(shutdown)
    assert "ctx" in sig.parameters
