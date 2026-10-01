"""Summarize a paired one-worker candidate run against both UpCloud baselines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text()))


def _rows(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = summary["runs_detail"]
    by_sha = {row["sha256"]: row for row in rows}
    if len(rows) != 8 or len(by_sha) != len(rows) or summary["runs"] != len(rows) or summary["concurrency"] != 1:
        raise ValueError("expected one complete eight-file, one-worker run")
    if summary["failed_runs"] or any(row["exit_code"] or row["contract_problems"] or row["protocol_error"] for row in rows):
        raise ValueError("run contains a child or semantic-contract failure")
    return by_sha


def finalize(
    first: dict[str, Any],
    repeat: dict[str, Any],
    candidate: dict[str, Any],
    reference: dict[str, Any],
    first_tags: dict[str, Any],
    candidate_tags: dict[str, Any],
) -> dict[str, Any]:
    a, b, c = _rows(first), _rows(repeat), _rows(candidate)
    if set(a) != set(b) or set(a) != set(c):
        raise ValueError("corpus SHA coverage differs")
    if first["protocol"] != "current" or repeat["protocol"] != "current" or candidate["protocol"] != "neutral":
        raise ValueError("unexpected analyzer protocol")
    for key in ("node", "cpu_limit", "memory_limit"):
        if not (first["run_context"][key] == repeat["run_context"][key] == candidate["run_context"][key]):
            raise ValueError(f"unmatched host or resource limit: {key}")
    if first["model_tree_sha256"] != repeat["model_tree_sha256"]:
        raise ValueError("incumbent model bytes changed between passes")
    files = []
    for sha in sorted(a):
        current_fastest = min(a[sha]["wall_sec"], b[sha]["wall_sec"])
        files.append(
            {
                "sha256": sha,
                "baseline_first_wall_sec": a[sha]["wall_sec"],
                "baseline_repeat_wall_sec": b[sha]["wall_sec"],
                "candidate_wall_sec": c[sha]["wall_sec"],
                "candidate_playback_speed": c[sha]["duration_sec"] / c[sha]["wall_sec"],
                "speedup_vs_fastest_baseline": current_fastest / c[sha]["wall_sec"],
                "candidate_peak_rss_kib": c[sha]["peak_rss_kib"],
            }
        )
    baseline_fastest = min(first["batch_wall_sec"], repeat["batch_wall_sec"])
    speedup = baseline_fastest / candidate["batch_wall_sec"]
    if first_tags["summary"]["scored"] != candidate_tags["summary"]["scored"]:
        raise ValueError("source-tag scoring coverage differs")
    return {
        "status": "candidate measured; independent accuracy remains limited by source-label coverage",
        "reference_sha256": reference["reference_sha256"],
        "host": first["run_context"]["node"],
        "candidate_image": candidate["run_context"]["image_digest"],
        "candidate_model_tree_sha256": candidate["model_tree_sha256"],
        "batch_speedup_vs_fastest_baseline": speedup,
        "candidate_batch_playback_speed": candidate["batch_audio_hours_per_hour"],
        "two_x_batch_gate": speedup >= 2,
        "candidate_peak_rss_kib": candidate["peak_rss_kib_max"],
        "reference_agreement": reference["summary"],
        "source_tag_agreement": {"baseline": first_tags["summary"], "candidate": candidate_tags["summary"]},
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--repeat", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reference-comparison", type=Path, required=True)
    parser.add_argument("--first-tag-score", type=Path, required=True)
    parser.add_argument("--candidate-tag-score", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    result = finalize(
        _load(args.first),
        _load(args.repeat),
        _load(args.candidate),
        _load(args.reference_comparison),
        _load(args.first_tag_score),
        _load(args.candidate_tag_score),
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({k: result[k] for k in ("batch_speedup_vs_fastest_baseline", "two_x_batch_gate", "candidate_batch_playback_speed")}))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
