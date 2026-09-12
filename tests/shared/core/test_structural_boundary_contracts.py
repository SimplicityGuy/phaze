"""Terminal guards for the structural-maintenance facade and ownership map."""

from __future__ import annotations

import ast
from pathlib import Path

from phaze import config
from phaze.services import (
    proposal,
    proposal_context,
    proposal_parsing,
    review,
    review_changes,
)
from phaze.tasks import (
    cloud_reconcile_observation,
    execution,
    execution_filesystem,
    reconcile_cloud_jobs,
    recovery_policy,
    reenqueue,
)


_BOUNDARIES: dict[str, tuple[str, ...]] = {
    "phaze.config": ("phaze.config_base", "phaze.config_control", "phaze.config_agent"),
    "phaze.services.review": (
        "phaze.services.review_changes",
        "phaze.services.review_tagwrite",
        "phaze.services.review_dedupe",
        "phaze.services.review_cue",
    ),
    "phaze.services.proposal": (
        "phaze.services.proposal_context",
        "phaze.services.proposal_parsing",
        "phaze.services.proposal_persistence",
        "phaze.services.proposal_provider",
    ),
    "phaze.tasks.execution": ("phaze.tasks.execution_filesystem",),
    "phaze.tasks.reenqueue": (
        "phaze.tasks.recovery_backfill",
        "phaze.tasks.recovery_policy",
        "phaze.tasks.recovery_queries",
        "phaze.tasks.recovery_replay",
    ),
    "phaze.tasks.reconcile_cloud_jobs": ("phaze.tasks.cloud_reconcile_observation",),
}

_LIVE_CONSUMERS: dict[Path, set[tuple[str, str]]] = {
    Path("src/phaze/tasks/controller.py"): {
        ("phaze.services.proposal", "ProposalService"),
        ("phaze.tasks.reenqueue", "recover_orphaned_work"),
        ("phaze.tasks.reconcile_cloud_jobs", "reconcile_cloud_jobs"),
    },
    Path("src/phaze/tasks/agent_worker.py"): {("phaze.tasks.execution", "execute_approved_batch")},
    Path("src/phaze/services/backends/kueue.py"): {("phaze.tasks.reconcile_cloud_jobs", "_reconcile_one")},
    Path("src/phaze/routers/duplicates.py"): {("phaze.services.review", "get_dedupe_groups")},
    Path("src/phaze/main.py"): {("phaze.config", "settings")},
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for alias in node.names
    )
    return imports


def _from_imports(path: Path) -> set[tuple[str, str]]:
    tree = ast.parse(path.read_text())
    return {
        (node.module, alias.name) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None for alias in node.names
    }


def test_capability_imports_point_outward_from_each_facade() -> None:
    """Every facade imports its owners, and no owner imports back through the facade."""
    for facade, owners in _BOUNDARIES.items():
        facade_path = Path("src") / Path(*facade.split(".")).with_suffix(".py")
        facade_imports = _imports(facade_path)
        for owner in owners:
            owner_path = Path("src") / Path(*owner.split(".")).with_suffix(".py")
            assert owner in facade_imports
            assert facade not in _imports(owner_path)


def test_facades_remain_anchored_to_live_production_consumers() -> None:
    """The retained facades include framework, router, task, and compatibility consumers."""
    for path, expected in _LIVE_CONSUMERS.items():
        assert expected <= _from_imports(path)


def test_compatibility_exports_keep_their_object_identities() -> None:
    """Moving ownership must not manufacture replacement types or change task identities."""
    assert config.Settings is config.ControlSettings
    assert proposal.BatchProposalResponse is proposal_parsing.BatchProposalResponse
    assert proposal.build_file_context is proposal_context.build_file_context
    assert review.ChangesReviewPage is review_changes.ChangesReviewPage
    assert execution._MoveStep is execution_filesystem.MoveStep
    assert reenqueue._DoneSets is recovery_policy._DoneSets
    assert reconcile_cloud_jobs.PENDING_SUBMIT_CONFIRMATION_SECONDS == cloud_reconcile_observation.PENDING_SUBMIT_CONFIRMATION_SECONDS
    assert reconcile_cloud_jobs.NO_POD_PROBE_SECONDS == cloud_reconcile_observation.NO_POD_PROBE_SECONDS


def test_maintained_ownership_map_names_every_facade_and_owner() -> None:
    """The maintained project map cannot silently omit a living structural boundary."""
    text = Path("docs/project-structure.md").read_text()
    for facade, owners in _BOUNDARIES.items():
        assert facade in text
        for owner in owners:
            assert owner.rsplit(".", maxsplit=1)[-1] + ".py" in text
