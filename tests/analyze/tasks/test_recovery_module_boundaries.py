"""Architecture guards for the recovery capability split."""

import ast
from pathlib import Path

from phaze.services import cloud_staging
from phaze.tasks import recovery_replay, reenqueue


def test_recovery_policy_has_no_database_or_queue_adapter_imports() -> None:
    """The classification and planning boundary stays pure and independently testable."""
    policy_path = Path("src/phaze/tasks/recovery_policy.py")
    tree = ast.parse(policy_path.read_text())
    imported_modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(module == "sqlalchemy" or module.startswith("sqlalchemy.") for module in imported_modules)
    assert "phaze.services.enqueue_router" not in imported_modules
    assert "phaze.services.scheduling_ledger" not in imported_modules


def test_reenqueue_preserves_supported_patch_points() -> None:
    """The facade retains the monkeypatch seams used by recovery consumers and tests."""
    assert reenqueue.cloud_staging is cloud_staging
    assert reenqueue._REPLAY_REGENERATORS is recovery_replay._REPLAY_REGENERATORS
    assert reenqueue.recover_orphaned_work.__module__ == "phaze.tasks.reenqueue"
