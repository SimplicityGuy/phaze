"""Unit tests for phaze.schemas.pipeline_scans (Phase 27 Plan 02 — D-06).

`TriggerScanForm` is the form-body schema for `POST /pipeline/scans`. The
operator submits `agent_id` + `scan_root` + optional `subpath`; the router
(Plan 06) joins root + subpath, NFC-normalizes, and prefix-validates against
the agent's `scan_roots`.
"""

from __future__ import annotations

import pydantic
import pytest

from phaze.schemas.pipeline_scans import TriggerScanForm


def test_trigger_scan_form_subpath_defaults_to_empty() -> None:
    f = TriggerScanForm(agent_id="agent-a", scan_root="/data/music")
    assert f.subpath == ""


def test_trigger_scan_form_accepts_subpath() -> None:
    f = TriggerScanForm(agent_id="agent-a", scan_root="/data/music", subpath="2026-coachella/")
    assert f.subpath == "2026-coachella/"


def test_trigger_scan_form_rejects_unknown_field() -> None:
    with pytest.raises(pydantic.ValidationError) as exc_info:
        TriggerScanForm.model_validate(
            {"agent_id": "agent-a", "scan_root": "/r", "unknown": "x"},
        )

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_trigger_scan_form_requires_agent_id_and_scan_root() -> None:
    with pytest.raises(pydantic.ValidationError):
        TriggerScanForm.model_validate({})
