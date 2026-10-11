"""Read existing BH lifecycle and validation evidence without recording new events.

This is a readback report, not a velocity pilot. Explicit bead membership is required;
missing boundaries remain null. Gate creation is not a successful submit event, a green
validation is not approval, and execution completion is not admission-permit release.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any


Row = dict[str, Any]
REVIEW = re.compile(r"\b(?:bh:)?review\s+([0-9a-f]{7,40})\b", re.IGNORECASE)
SUBMIT = re.compile(r"\bsubmitted\s+([0-9a-f]{7,40})\b", re.IGNORECASE)


def timestamp(value: object) -> datetime | None:
    """Reject missing, malformed and timezone-free boundaries rather than invent UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def elapsed(start: object, end: object) -> float | None:
    begin, finish = timestamp(start), timestamp(end)
    if begin is None or finish is None or finish < begin:
        return None
    return (finish - begin).total_seconds()


def read_rows(paths: list[Path], errors: list[str]) -> list[Row]:
    """Quarantine malformed records visibly; do not silently narrow the denominator."""
    rows = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != 1:
                raise ValueError("unsupported schema")
            rows.append(data)
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    return rows


def bh_read(repo: Path, *args: str) -> Any:
    """Only first-class read verbs are exposed, never an external runtime import."""
    if args[0] not in {"issue", "show", "list"}:
        raise ValueError("read-only BH verb required")
    executable = shutil.which("bh")
    if executable is None:
        raise OSError("bh is unavailable")
    result = subprocess.run([executable, "work", *args, "--json"], cwd=repo, capture_output=True, text=True, check=True, timeout=60)  # noqa: S603
    return json.loads(result.stdout)


def lifecycle(repo: Path, bead: str) -> Row:
    """Read all historical dotted-child events and convention review gates."""
    issue = bh_read(repo, "issue", bead)
    if not isinstance(issue, list) or len(issue) != 1 or not isinstance(issue[0], dict) or issue[0].get("id") != bead:
        raise ValueError("issue identity mismatch")
    packet = bh_read(repo, "show", bead)
    events = bh_read(repo, "list", "--parent", bead, "--all", "--include-infra", "--limit", "0")
    if not isinstance(packet, dict) or not isinstance(events, list):
        raise ValueError("invalid lifecycle readback")
    gates = []
    for gate in packet.get("gates", []):
        if not isinstance(gate, dict):
            raise ValueError("invalid gate packet")
        if gate.get("kind") != "review":
            continue
        rows = bh_read(repo, "issue", gate["id"])
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("id") != gate["id"]:
            raise ValueError("gate identity mismatch")
        row = rows[0]
        row["resolved_sha"] = resolve_revision(repo, revision(REVIEW, row))
        gates.append(row)
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("invalid lifecycle event")
        if review_destination(event) == "pending":
            event["resolved_sha"] = resolve_revision(repo, revision(SUBMIT, event))
    return {"issue": issue[0], "events": events, "gates": gates}


def resolve_revision(repo: Path, marker: str | None) -> str | None:
    """Mirror BH's unambiguous commit-peel rule; ledger membership alone is insufficient."""
    git = shutil.which("git")
    if git is None or marker is None or re.fullmatch(r"[0-9a-f]{7,40}", marker) is None:
        return None
    result = subprocess.run([git, "rev-parse", "--verify", "-q", f"{marker}^{{commit}}"], cwd=repo, text=True, capture_output=True, timeout=30)  # noqa: S603
    resolved = result.stdout.strip()
    return resolved if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", resolved) else None


def revision(pattern: re.Pattern[str], row: Row) -> str | None:
    match = pattern.search(" ".join(str(row.get(key) or "") for key in ("title", "description", "reason")))
    return match[1].lower() if match else None


def review_destination(row: Row) -> str | None:
    """Match BH's structured state-event conventions, never arbitrary failure prose."""
    if row.get("issue_type") != "event":
        return None
    title = str(row.get("title") or "")
    match = re.fullmatch(r"State change: review → (pending|changes-requested)", title)
    return match[1] if match else None


def summarize_lifecycle(data: Row, shas: set[str]) -> Row:
    events = data.get("events", [])
    pending = [row for row in events if review_destination(row) == "pending"]
    submitted = sorted(
        (row for row in pending if timestamp(row.get("created_at"))),
        key=lambda row: timestamp(row["created_at"]) or datetime.min.replace(tzinfo=UTC),
    )
    accepted = []
    for gate in data.get("gates", []):
        marker = revision(REVIEW, gate)
        matches = {sha for sha in shas if marker and sha.startswith(marker)}
        if gate.get("status") != "closed" or not str(gate.get("close_reason") or "").lower().startswith("approved") or len(matches) != 1:
            continue
        sha = next(iter(matches))
        if gate.get("resolved_sha") != sha:
            continue
        same_round = [
            row for row in submitted if row.get("resolved_sha") == sha and elapsed(row.get("created_at"), gate.get("closed_at")) is not None
        ]
        # Missing same-round handoff or timestamps cannot become a joined acceptance.
        if same_round and elapsed(same_round[-1].get("created_at"), gate.get("closed_at")) is not None:
            accepted.append({"gate": gate["id"], "sha": sha, "submitted_at": same_round[-1]["created_at"], "accepted_at": gate["closed_at"]})
    accepted.sort(key=lambda row: timestamp(row["accepted_at"]) or datetime.min.replace(tzinfo=UTC))
    acceptance = accepted[-1] if accepted else None
    return {
        "current_claim_at": data.get("issue", {}).get("started_at"),
        "original_work_start_at": None,
        "first_observed_submit_at": submitted[0]["created_at"] if submitted else None,
        "acceptance": acceptance,
        "review_wait_seconds": elapsed(acceptance["submitted_at"], acceptance["accepted_at"]) if acceptance else None,
        "accepted_bead_velocity_seconds": None,
        "observed_bounces": [{"event": row["id"], "at": row.get("created_at")} for row in events if review_destination(row) == "changes-requested"],
        "population": "accepted-review" if acceptance else "unknown-or-censored",
    }


def attempt(row: Row, repo: Path, errors: list[str]) -> Row:
    raw_artifacts = row.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, dict) else {}
    log = artifacts.get("gate_log") or str(repo / ".bh/validation/runs" / str(row.get("run_id")) / "gate.log")
    selector = []
    try:
        content = Path(log).read_text(encoding="utf-8")
        selector = re.findall(r"selector: (run|docs|escalate|fail)\b([^\n]*)", content)
    except OSError:
        errors.append(f"{row.get('run_id')}: gate log unavailable")
    raw_admission = row.get("admission")
    admission = raw_admission if isinstance(raw_admission, dict) else {}
    queue = admission.get("queue_seconds")
    return {
        **{
            key: row.get(key)
            for key in (
                "run_id",
                "bead",
                "tree",
                "sha",
                "command",
                "command_hash",
                "phase",
                "started_at",
                "finished_at",
                "verdict",
                "lifecycle",
                "reason",
                "exit_code",
            )
        },
        "queue_seconds": queue if isinstance(queue, (int, float)) and not isinstance(queue, bool) and queue >= 0 else None,
        "execution_seconds": elapsed(row.get("started_at"), row.get("finished_at")),
        "arrival_at": None,
        "admitted_at": None,
        "released_at": None,
        "evidence_generation": row.get("evidence_generation"),
        "selector": [{"verdict": verdict, "reason": reason.strip()} for verdict, reason in selector],
    }


def report(repo: Path, common: Path, beads: list[str]) -> Row:
    started = time.perf_counter()
    errors: list[str] = []
    run_paths = sorted((common / "bh/validation/runs").glob("*/manifest.json"))
    use_paths = sorted((common / "bh/validation/uses").glob("*.json"))
    runs = read_rows(run_paths, errors)
    uses = read_rows(use_paths, errors)
    selected = [row for row in runs if row.get("bead") in beads]
    selected_uses = [row for row in uses if row.get("bead") in beads]
    source_ids = {row.get("run_id") for row in selected_uses if row.get("run_id")}
    supporting = [row for row in runs if row.get("bead") not in beads and row.get("run_id") in source_ids]
    use_rows = []
    for use in selected_uses:
        candidates = [run for run in runs if run.get("run_id") and run.get("run_id") == use.get("run_id")]
        matched = len(candidates) == 1 and all(use.get(key) and use.get(key) == candidates[0].get(key) for key in ("tree", "command_hash"))
        use_rows.append(
            {
                **{
                    key: use.get(key)
                    for key in ("use_id", "run_id", "bead", "sha", "tree", "command_hash", "phase", "created_at", "reused", "coalesced")
                },
                "source_attempt_join": "exact-tree-command" if matched else "unknown-or-mismatched",
            }
        )
    joined = []
    for bead in beads:
        shas = {
            str(row["sha"]) for row in [*selected, *selected_uses] if row.get("bead") == bead and re.fullmatch(r"[0-9a-f]{40}", str(row.get("sha")))
        }
        try:
            data = summarize_lifecycle(lifecycle(repo, bead), shas)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"{bead}: lifecycle unavailable: {exc}")
            data = {"population": "unknown-or-censored", "acceptance": None, "accepted_bead_velocity_seconds": None}
        joined.append({"bead": bead, **data})
    attempts = [{**attempt(row, repo, errors), "membership_role": "direct"} for row in selected]
    supporting_attempts = [{**attempt(row, repo, errors), "membership_role": "referenced-source"} for row in supporting]
    result: Row = {
        "schema": 1,
        "kind": "readback-not-pilot",
        "membership": beads,
        "beads": joined,
        "attempts": attempts,
        "supporting_attempts": supporting_attempts,
        "uses": use_rows,
        "inventory": {"manifests": len(run_paths), "uses": len(use_paths), "selected_attempts": len(selected), "selected_uses": len(selected_uses)},
        "errors": errors,
        "limitations": [
            "No authoritative original work-start event: current claim is not original claim; accepted-bead velocity remains unknown.",
            "Admission queue duration exists, but exact arrival/admission/release timestamps are unavailable; execution end is not permit release.",
            "No generation producer is added; absent evidence_generation remains unknown.",
            "Only retained managed attempts and use decisions are visible; manual/unrecorded validation denominator is unknown.",
            "Lifecycle reads are not an atomic snapshot; historical event retention and read races can leave joins unknown.",
        ],
        "readback_seconds": time.perf_counter() - started,
        "recording_mode": "none-read-only",
        "recording_overhead_seconds": None,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--bead", action="append", required=True, help="Explicit membership; repeated for multiple beads. No automatic cohort selection."
    )
    args = parser.parse_args(argv)
    git = shutil.which("git")
    if git is None:
        parser.error("git is unavailable")
    raw = subprocess.run([git, "rev-parse", "--git-common-dir"], cwd=args.repo, check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603
    common = (args.repo / raw).resolve()
    result = report(args.repo.resolve(), common, list(dict.fromkeys(args.bead)))
    print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
