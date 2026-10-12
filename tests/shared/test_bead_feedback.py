"""Acceptance is joined from actual review events, never a green or merge proxy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from scripts import bead_feedback
from scripts.bead_feedback import attempt, elapsed, read_rows, summarize_lifecycle


if TYPE_CHECKING:
    from pathlib import Path


SHA = "12345678" + "a" * 32


def history() -> dict:
    return {
        "issue": {"started_at": "2026-10-11T09:00:00Z"},
        "events": [
            {
                "id": "bead.1",
                "issue_type": "event",
                "title": "State change: review → pending",
                "description": "Reason: submitted 12345678",
                "resolved_sha": SHA,
                "created_at": "2026-10-11T10:00:00Z",
            },
            {"id": "bead.2", "issue_type": "event", "title": "State change: review → changes-requested", "created_at": "2026-10-11T10:10:00Z"},
            {
                "id": "bead.3",
                "issue_type": "event",
                "title": "State change: review → pending",
                "description": "Reason: submitted 12345678",
                "resolved_sha": SHA,
                "created_at": "2026-10-11T11:00:00Z",
            },
        ],
        "gates": [
            {
                "id": "gate",
                "status": "closed",
                "description": "Reason: bh:review 12345678",
                "resolved_sha": SHA,
                "close_reason": "approved by reviewer",
                "closed_at": "2026-10-11T11:05:00Z",
            }
        ],
    }


def test_join_accepted_revision_and_latest_round_keeps_first_submit_and_bounce() -> None:
    result = summarize_lifecycle(history(), {SHA})
    assert result["population"] == "accepted-review"
    assert result["acceptance"]["sha"] == SHA
    assert result["review_wait_seconds"] == 300
    assert result["first_observed_submit_at"] == "2026-10-11T10:00:00Z"
    assert result["observed_bounces"] == [{"event": "bead.2", "at": "2026-10-11T10:10:00Z"}]
    assert result["accepted_bead_velocity_seconds"] is None
    assert result["original_work_start_at"] is None


def test_later_same_revision_resubmission_does_not_hide_earlier_approval() -> None:
    data = history()
    data["events"].append(
        {
            "id": "bead.4",
            "issue_type": "event",
            "title": "State change: review → pending",
            "description": "Reason: submitted 12345678",
            "resolved_sha": SHA,
            "created_at": "2026-10-11T11:10:00Z",
        }
    )
    result = summarize_lifecycle(data, {SHA})
    assert result["population"] == "accepted-review"
    assert result["acceptance"] == {
        "gate": "gate",
        "sha": SHA,
        "submitted_at": "2026-10-11T11:00:00Z",
        "accepted_at": "2026-10-11T11:05:00Z",
    }
    assert result["review_wait_seconds"] == 300
    assert result["first_observed_submit_at"] == "2026-10-11T10:00:00Z"


@pytest.mark.parametrize(
    "change", ["bounce", "open", "wrong-sha", "missing-submit", "ambiguous-sha", "negative-time", "missing-close", "missing-object", "wrong-submit"]
)
def test_untrusted_or_unjoined_gate_never_becomes_acceptance(change: str) -> None:
    data = history()
    shas = {SHA}
    gate = data["gates"][0]
    match change:
        case "bounce":
            gate["close_reason"] = "changes requested by reviewer"
        case "open":
            gate["status"] = "open"
        case "wrong-sha":
            gate["description"] = "Reason: bh:review abcdef00"
        case "missing-submit":
            data["events"] = []
        case "ambiguous-sha":
            shas.add("12345678" + "b" * 32)
        case "negative-time":
            gate["closed_at"] = "2026-10-11T08:00:00Z"
        case "missing-close":
            gate.pop("closed_at")
        case "missing-object":
            gate["resolved_sha"] = None
        case "wrong-submit":
            for row in data["events"]:
                row["resolved_sha"] = "f" * 40
    result = summarize_lifecycle(data, shas)
    assert result["acceptance"] is None
    assert result["population"] == "unknown-or-censored"


def test_failure_prose_is_not_a_lifecycle_bounce() -> None:
    data = history()
    data["events"] = [{"id": "error", "issue_type": "event", "title": "Command failed", "description": "review changes-requested text"}]
    assert summarize_lifecycle(data, {SHA})["observed_bounces"] == []


@pytest.mark.parametrize(
    "start,end",
    [
        (None, None),
        ("bad", "2026-10-11T10:00:00Z"),
        ("2026-10-11T11:00:00Z", "2026-10-11T10:00:00Z"),
        ("2026-10-11T10:00:00", "2026-10-11T11:00:00Z"),
    ],
)
def test_missing_malformed_negative_naive_boundaries_are_unknown(start: str | None, end: str | None) -> None:
    assert elapsed(start, end) is None


def test_attempt_reads_selector_reason_without_inventing_permit_or_generation(tmp_path: Path) -> None:
    log = tmp_path / "gate.log"
    log.write_text("🎯 selector: escalate no accepted coverage\n", encoding="utf-8")
    errors: list[str] = []
    result = attempt(
        {
            "run_id": "run",
            "bead": "bead",
            "started_at": "2026-10-11T10:00:00Z",
            "finished_at": "2026-10-11T10:00:10Z",
            "verdict": "red",
            "lifecycle": "cancelled",
            "admission": {"queue_seconds": 2.5},
            "artifacts": {"gate_log": str(log)},
        },
        tmp_path,
        errors,
    )
    assert result["execution_seconds"] == 10
    assert result["queue_seconds"] == 2.5
    assert result["selector"] == [{"verdict": "escalate", "reason": "no accepted coverage"}]
    assert result["verdict"] == "red"
    assert result["lifecycle"] == "cancelled"
    assert all(result[key] is None for key in ("arrival_at", "admitted_at", "released_at", "evidence_generation"))
    assert errors == []


def test_retained_corrupt_schema_is_visible_and_unreadable_log_is_unknown(tmp_path: Path) -> None:
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps({"schema": 1, "run_id": "run"}), encoding="utf-8")
    bad.write_text(json.dumps({"schema": 2}), encoding="utf-8")
    errors: list[str] = []
    assert read_rows([good, bad], errors) == [{"schema": 1, "run_id": "run"}]
    assert len(errors) == 1
    result = attempt({"run_id": "run", "admission": {"queue_seconds": -2}}, tmp_path, errors)
    assert result["selector"] == []
    assert result["queue_seconds"] is None
    assert len(errors) == 2


def test_cross_bead_reuse_reads_source_without_counting_it_as_new_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "bh/validation/runs/source"
    uses = tmp_path / "bh/validation/uses"
    runs.mkdir(parents=True)
    uses.mkdir(parents=True)
    log = tmp_path / "gate.log"
    log.write_text("🎯 selector: run 5\n", encoding="utf-8")
    (runs / "manifest.json").write_text(
        json.dumps({"schema": 1, "run_id": "source", "bead": "other", "tree": "tree", "command_hash": "cmd", "artifacts": {"gate_log": str(log)}}),
        encoding="utf-8",
    )
    for name, tree in (("reuse", "tree"), ("mismatch", "wrong")):
        (uses / f"{name}.json").write_text(
            json.dumps({"schema": 1, "use_id": name, "run_id": "source", "bead": "bead", "tree": tree, "command_hash": "cmd", "reused": True}),
            encoding="utf-8",
        )
    monkeypatch.setattr(bead_feedback, "lifecycle", lambda _repo, _bead: {"events": [], "gates": []})
    result = bead_feedback.report(tmp_path, tmp_path, ["bead"])
    assert result["attempts"] == []
    assert len(result["supporting_attempts"]) == 1
    assert result["supporting_attempts"][0]["bead"] == "other"
    joined = {row["use_id"]: row["source_attempt_join"] for row in result["uses"]}
    assert joined == {"reuse": "exact-tree-command", "mismatch": "unknown-or-mismatched"}
    assert result["inventory"]["selected_attempts"] == 0
