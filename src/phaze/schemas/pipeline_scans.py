"""Form-body schema for ``POST /pipeline/scans``.

The operator submits `{agent_id, scan_root, subpath}` from the Pipeline page's
"Trigger Scan" card. The router joins root + subpath, NFC-normalizes
the result, validates it starts with one of the agent's `scan_roots`, and
contains no `..` path-traversal component (see `routers/pipeline_scans.py::trigger_scan`).

The schema itself accepts any string and defers semantic validation to the
router — T-27-03 disposition: schema-level regex would be over-restrictive
(legitimate subpaths like `live-sets/2026-04-15` need slashes and hyphens).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TriggerScanForm(BaseModel):
    """Operator-submitted trigger-scan form validated by the router."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    scan_root: str
    subpath: str = ""
