"""Characterization of the cloud-reconcile observation/classification boundary."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from phaze.tasks import cloud_reconcile_observation as observation, reconcile_cloud_jobs as reconcile_mod
from tests.kube_fakes import ADMITTED, EVICTED, INADMISSIBLE, PENDING, QUOTA_RESERVED, fake_job, fake_workload


def _legacy_job_disposition(job: Any) -> str:
    """Spell the pre-extraction branch order as an executable characterization."""
    if reconcile_mod._job_counter(job, "succeeded") >= 1 or reconcile_mod._job_has_true_condition(job, "Complete"):
        return "succeeded"
    if reconcile_mod._job_counter(job, "failed") >= 1 or reconcile_mod._job_has_true_condition(job, "Failed"):
        return "failed"
    return "in_flight"


def _legacy_workload_disposition(workload: Any) -> str:
    """Spell the pre-extraction Workload precedence as an executable characterization."""
    if reconcile_mod._condition_true(reconcile_mod._workload_condition(workload, "Evicted")):
        return "evicted"
    quota_reserved = reconcile_mod._workload_condition(workload, "QuotaReserved")
    hold_reason = reconcile_mod._quota_hold_reason(quota_reserved)
    if hold_reason == "Inadmissible":
        return "inadmissible"
    if hold_reason == "Pending":
        return "pending"
    if reconcile_mod._condition_true(reconcile_mod._workload_condition(workload, "Admitted")):
        return "admitted"
    if reconcile_mod._condition_true(quota_reserved):
        return "quota_reserved"
    return "unknown"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        pytest.param(fake_job(succeeded=1), "succeeded", id="success-counter"),
        pytest.param(SimpleNamespace(status={"conditions": [{"type": "Complete", "status": "True"}]}), "succeeded", id="complete-condition"),
        pytest.param(fake_job(failed=1), "failed", id="failure-counter"),
        pytest.param(SimpleNamespace(status={"conditions": [{"type": "Failed", "status": "True"}]}), "failed", id="failed-condition"),
        pytest.param(fake_job(succeeded=1, failed=1), "succeeded", id="success-is-primary"),
        pytest.param(fake_job(), "in_flight", id="nonterminal"),
        pytest.param(SimpleNamespace(status={"succeeded": "bad", "failed": None}), "in_flight", id="malformed-counters-hold"),
    ],
)
def test_job_transition_table(job: Any, expected: str) -> None:
    assert _legacy_job_disposition(job) == expected
    assert observation.classify_job(job).value == expected


@pytest.mark.parametrize(
    ("workload", "expected"),
    [
        pytest.param(EVICTED, "evicted", id="evicted"),
        pytest.param(INADMISSIBLE, "inadmissible", id="inadmissible"),
        pytest.param(PENDING, "pending", id="pending"),
        pytest.param(ADMITTED, "admitted", id="admitted"),
        pytest.param(QUOTA_RESERVED, "quota_reserved", id="quota-reserved"),
        pytest.param(fake_workload(), "unknown", id="unknown"),
        pytest.param(
            fake_workload(("Evicted", "True", "WorkloadInactive"), ("Admitted", "True", ""), ("QuotaReserved", "True", "")),
            "evicted",
            id="eviction-is-primary",
        ),
        pytest.param(
            fake_workload(("QuotaReserved", "False", "Pending"), ("Admitted", "True", "")),
            "pending",
            id="quota-hold-precedes-admitted",
        ),
    ],
)
def test_workload_transition_table(workload: Any, expected: str) -> None:
    assert _legacy_workload_disposition(workload) == expected
    assert observation.classify_workload(workload).value == expected


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        pytest.param(0, "fresh", id="new"),
        pytest.param(3600, "fresh", id="boundary-is-fresh"),
        pytest.param(3600.0001, "expired", id="past-boundary"),
    ],
)
def test_pending_confirmation_transition_table(age_seconds: float, expected: str) -> None:
    assert observation.classify_pending_confirmation(age_seconds).value == expected


def test_facade_exposes_kueue_reconcile_patch_points() -> None:
    """The private helpers are established patch/import seams despite their names."""
    for name in (
        "_analysis_completed",
        "_job_gone",
        "_pod_wedge_reason",
        "_reconcile_one",
        "_terminal_node_loss_reason",
        "reconcile_cloud_jobs",
    ):
        assert callable(getattr(reconcile_mod, name))
    assert reconcile_mod.PENDING_SUBMIT_CONFIRMATION_SECONDS == 3600
    assert reconcile_mod.NO_POD_PROBE_SECONDS == 900


def test_backend_cycle_break_import_stays_function_local() -> None:
    """Importing ``services.backends`` eagerly would recreate the production import cycle."""
    source = Path(reconcile_mod.__file__).read_text()
    tree = ast.parse(source)
    eager_imports = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == "phaze.services.backends"]
    assert eager_imports == []

    reconcile = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "reconcile_cloud_jobs")
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "phaze.services.backends"
        and any(alias.name == "resolve_backends" for alias in node.names)
        for node in ast.walk(reconcile)
    )


def test_observation_module_has_no_reconciliation_side_effect_adapters() -> None:
    """The extracted boundary may observe external state but cannot apply transitions."""
    source = Path(observation.__file__).read_text()
    tree = ast.parse(source)
    imported_modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}
    assert "phaze.services.backends" not in imported_modules
    assert "phaze.services.s3_staging" not in imported_modules
    assert "phaze.tasks.submit_cloud_job" not in imported_modules

    forbidden_calls = {"commit", "rollback", "enqueue", "delete_job", "delete_staged_object"}
    called_attributes = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert called_attributes.isdisjoint(forbidden_calls)
