"""Static guards for the Phase 56 Kubernetes/Kueue operator runbook (KDEPLOY-01 / D-01).

These tests prove ``docs/k8s-burst.md`` ships copy-paste-ready, valid Kueue manifests AND a namespaced
RBAC Role whose verb set covers phaze's ACTUAL kr8s call graph -- WITHOUT a live cluster. They parse the
fenced ```yaml blocks with ``yaml.safe_load_all`` (multi-doc, ``---``-separated) and assert on the parsed
structure (robust against reformatting). Three guards:

a. ``test_runbook_manifests_are_valid_yaml`` -- every yaml fence parses without a YAML error.
b. ``test_runbook_has_required_kinds`` -- the operator-owned object set is complete (ResourceFlavor,
   ClusterQueue, LocalQueue, ServiceAccount, Role, RoleBinding, Secret).
c. ``test_rbac_covers_call_graph`` -- the Role's rules are a SUPERSET of ``REQUIRED_RBAC``, the floor
   derived from the kr8s call graph: ``batch/jobs`` {create,get,delete} (submit/get/delete_job),
   ``kueue.x-k8s.io/workloads`` ⊇ {list} (get_workload_for; get/watch are the conservative D-01 spec),
   and the NEW ``kueue.x-k8s.io/localqueues`` {get} (the Phase 56 startup reachability probe). Guarding
   the floor against drift is the T-56-RBAC mitigation: no cluster-wide grant / extra verb slips in, and
   the probe's ``get localqueues`` can never be forgotten (which would 403 the probe into a false
   "unreachable" forever -- RESEARCH Pitfall 2).

RED until 56-04 writes ``docs/k8s-burst.md`` (the file-not-found assert trips inside the loader).
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK_PATH = REPO_ROOT / "docs" / "k8s-burst.md"

_YAML_FENCE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)

# The verb floor derived from phaze's actual kr8s call graph (RESEARCH §Runbook RBAC / Validation
# Architecture). Keyed by (apiGroup, resource); each value is the MINIMUM verb set the Role must grant.
# The runbook MAY grant more (e.g. workloads get/watch as the conservative spec) -- this is a subset gate.
REQUIRED_RBAC: dict[tuple[str, str], set[str]] = {
    ("batch", "jobs"): {"create", "get", "delete"},  # submit_job / get_job / delete_job
    ("kueue.x-k8s.io", "workloads"): {"list"},  # get_workload_for(.list); get/watch = conservative spec
    ("kueue.x-k8s.io", "localqueues"): {"get"},  # NEW: the Phase 56 startup reachability probe
}

_REQUIRED_KINDS = {
    "ResourceFlavor",
    "ClusterQueue",
    "LocalQueue",
    "ServiceAccount",
    "Role",
    "RoleBinding",
    "Secret",
}


def _yaml_blocks() -> list[str]:
    """Return every fenced ```yaml block in the runbook (asserts the runbook exists)."""
    assert RUNBOOK_PATH.exists(), (
        f"docs/k8s-burst.md missing at {RUNBOOK_PATH}. Phase 56 (KDEPLOY-01 / D-01) requires the "
        "cluster-admin runbook with copy-paste-ready Kueue + RBAC manifests."
    )
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    return _YAML_FENCE.findall(text)


def _manifest_docs() -> list[dict[str, Any]]:
    """Parse every yaml fence and return the dict manifests (skip ``None``/non-dict snippets gracefully)."""
    docs: list[dict[str, Any]] = []
    for block in _yaml_blocks():
        for doc in yaml.safe_load_all(block):
            if isinstance(doc, dict):
                docs.append(doc)
    return docs


def test_runbook_manifests_are_valid_yaml() -> None:
    """Guard (a): every fenced yaml block parses without a YAML error."""
    blocks = _yaml_blocks()
    assert blocks, "docs/k8s-burst.md contains no ```yaml manifest blocks (D-01 requires copy-paste manifests)."
    for i, block in enumerate(blocks):
        try:
            list(yaml.safe_load_all(block))
        except yaml.YAMLError as exc:  # pragma: no cover - the assert message carries the failure
            raise AssertionError(f"yaml block #{i} in docs/k8s-burst.md is invalid YAML: {exc}") from exc


def test_runbook_has_required_kinds() -> None:
    """Guard (b): the runbook documents every operator-owned object kind (D-01)."""
    kinds = {doc.get("kind") for doc in _manifest_docs()}
    missing = _REQUIRED_KINDS - kinds
    assert not missing, f"docs/k8s-burst.md is missing manifest kind(s): {sorted(missing)} (have: {sorted(k for k in kinds if k)})."


def test_rbac_covers_call_graph() -> None:
    """Guard (c): the Role's rules are a superset of REQUIRED_RBAC (the kr8s call-graph floor, T-56-RBAC)."""
    roles = [doc for doc in _manifest_docs() if doc.get("kind") == "Role"]
    assert roles, "docs/k8s-burst.md has no RBAC Role (D-01 requires a namespaced least-privilege Role)."

    # Union the granted verbs across every rule, keyed by (apiGroup, resource).
    granted: dict[tuple[str, str], set[str]] = {}
    for role in roles:
        for rule in role.get("rules", []) or []:
            verbs = set(rule.get("verbs", []) or [])
            for group in rule.get("apiGroups", []) or []:
                for resource in rule.get("resources", []) or []:
                    granted.setdefault((group, resource), set()).update(verbs)

    for key, required_verbs in REQUIRED_RBAC.items():
        assert key in granted, f"RBAC Role does not grant any verbs on {key[0]}/{key[1]} (need ⊇ {sorted(required_verbs)})."
        missing_verbs = required_verbs - granted[key]
        assert not missing_verbs, (
            f"RBAC Role on {key[0]}/{key[1]} is missing verb(s) {sorted(missing_verbs)} "
            f"(granted: {sorted(granted[key])}; required floor: {sorted(required_verbs)})."
        )
