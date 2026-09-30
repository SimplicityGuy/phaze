"""Offline, SHA-addressed evaluation of Phaze analysis children.

This never connects to Phaze's database or calls an analysis callback. Audio is
staged separately under its SHA256 name; all outputs stay in the chosen scratch
directory. Run ``--help`` for the command-line contract.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, cast
import uuid

from phaze.job_runner import _build_payload
from phaze.services.analysis_windows import _iter_windows
from phaze.services.set_projection import MOOD_ORDER, camelot_code, positive_class_vector
from phaze.services.set_projection_writer import compute_window_projection


FINE_SEC = 30
COARSE_SEC = 180
FINE_MIN_SEC = 15
_FORMATS = frozenset({"mp3", "m4a", "flac", "wav", "ogg", "mp4"})


def load_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("items"), list) or not data["items"]:
        raise ValueError("expected a nonempty schema_version=1 manifest")
    seen: set[str] = set()
    for item in data["items"]:
        uuid.UUID(item["file_id"])
        sha = item["sha256"]
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("each item needs a lowercase SHA256")
        if sha in seen:
            raise ValueError("duplicate SHA256 in manifest")
        seen.add(sha)
        if item["format"] not in _FORMATS or not math.isfinite(item["duration_sec"]) or item["duration_sec"] <= 0:
            raise ValueError("invalid format or duration")
    return cast("list[dict[str, Any]]", data["items"])


def _audio_path(audio_dir: Path, item: dict[str, Any]) -> Path:
    """Only SHA-addressed staged copies are accepted; archive paths never enter argv."""
    path = audio_dir / f"{item['sha256']}.{item['format']}"
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"missing staged copy for {item['file_id']}")
    return path


def verify_staged_audio(items: list[dict[str, Any]], audio_dir: Path) -> None:
    """Hash before the timed run; never trust a filename as proof of content."""
    for item in items:
        path = _audio_path(audio_dir, item)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != item["sha256"]:
            raise ValueError(f"staged copy SHA256 mismatch for {item['file_id']}")


def model_tree_digest(models_dir: Path) -> tuple[str, int]:
    """Fingerprint the exact model bytes before timing, including relative names."""
    files = sorted(path for path in models_dir.rglob("*") if path.is_file())
    if not files:
        raise ValueError("models directory is empty")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(models_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            digest.update(bytes.fromhex(hashlib.file_digest(stream, "sha256").hexdigest()))
    return digest.hexdigest(), len(files)


def _coverage(windows: list[dict[str, Any]], duration: float, tier: str, max_sec: int) -> list[str]:
    problems: list[str] = []
    rows = sorted((w for w in windows if w["tier"] == tier), key=lambda w: (w["start_sec"], w["end_sec"]))
    expected = _iter_windows(float(int(duration)), max_sec, FINE_MIN_SEC, drop_short_trailing=tier == "fine")
    if len(rows) < len(expected):
        problems.append(f"{tier}: fewer windows than current geometry")
    end = 0.0
    for row in rows:
        start, stop = row["start_sec"], row["end_sec"]
        if start > end + 1.0:
            problems.append(f"{tier}: gap before {start:g}s")
        if stop <= start or stop - start > max_sec + 1.0:
            problems.append(f"{tier}: invalid span")
        end = max(end, stop)
    required_end = float(int(duration)) - (FINE_MIN_SEC if tier == "fine" else 0)
    if not rows or rows[0]["start_sec"] > 1.0 or end < required_end - 1.0:
        problems.append(f"{tier}: incomplete file coverage")
    return problems


def normalize_current(result: dict[str, Any], duration: float) -> tuple[dict[str, Any], list[str]]:
    """Use the production wire adapter, then inspect its full semantic contract."""
    payload = _build_payload(result)
    data = payload.model_dump(mode="json")
    windows = data.get("windows") or []
    problems: list[str] = []
    for name in ("fine_windows_total", "fine_windows_analyzed", "coarse_windows_total", "coarse_windows_analyzed"):
        if data.get(name) is None:
            problems.append(f"missing {name}")
    for tier in ("fine", "coarse"):
        total = data.get(f"{tier}_windows_total")
        analyzed = data.get(f"{tier}_windows_analyzed")
        if total is not None and analyzed is not None and analyzed != total:
            problems.append(f"{tier}: analyzed {analyzed} of {total}")
        problems.extend(_coverage(windows, duration, tier, FINE_SEC if tier == "fine" else COARSE_SEC))
    projection = compute_window_projection(windows)
    normalized_windows: list[dict[str, Any]] = []
    for row, projected in zip(windows, projection, strict=True):
        entry = {k: row.get(k) for k in ("tier", "window_index", "start_sec", "end_sec", "bpm", "musical_key", "mood", "style", "danceability")}
        if row["tier"] == "fine":
            entry["camelot"] = camelot_code(row.get("musical_key"))
            for name in ("bpm", "musical_key"):
                if name not in row:
                    problems.append(f"fine: missing {name}")
        else:
            entry.update(projected)
            features = row.get("features") or {}
            for name in (*MOOD_ORDER, "genre"):
                if name not in features:
                    problems.append(f"coarse: missing {name}")
            if not entry.get("mood_scores") or any(entry["mood_scores"].get(name) is None for name in MOOD_ORDER):
                problems.append("coarse: incomplete 11-score vector")
            for name in ("mood", "style", "danceability"):
                if entry.get(name) is None:
                    problems.append(f"coarse: missing {name}")
        normalized_windows.append(entry)
    if not data.get("style"):
        problems.append("missing ranked genre scores")
    if not data.get("mood") or len(data["mood"]) < 7:
        problems.append("missing seven file-level mood scores")
    for name in ("bpm", "musical_key", "dominant_style", "danceability"):
        if data.get(name) is None:
            problems.append(f"missing file-level {name}")
    normalized = {
        k: data.get(k)
        for k in (
            "bpm",
            "musical_key",
            "mood",
            "style",
            "dominant_style",
            "danceability",
            "energy",
            "fine_windows_total",
            "fine_windows_analyzed",
            "coarse_windows_total",
            "coarse_windows_analyzed",
        )
    }
    raw_features = result.get("features") or {}
    characteristics = dict(zip(MOOD_ORDER, positive_class_vector(raw_features), strict=True))
    normalized["characteristics"] = characteristics
    if any(value is None for value in characteristics.values()):
        problems.append("incomplete representative 11-score vector")
    normalized["windows"] = normalized_windows
    return normalized, sorted(set(problems))


def normalize_neutral(result: dict[str, Any], duration: float) -> tuple[dict[str, Any], list[str]]:
    """Accept a replacement's semantic output without requiring Essentia variants."""
    windows = result.get("windows")
    if not isinstance(windows, list):
        raise ValueError("neutral result needs windows list")
    problems: list[str] = []
    for tier, max_sec in (("fine", FINE_SEC), ("coarse", COARSE_SEC)):
        problems.extend(_coverage(windows, duration, tier, max_sec))
        rows = [window for window in windows if window.get("tier") == tier]
        total = result.get(f"{tier}_windows_total")
        analyzed = result.get(f"{tier}_windows_analyzed")
        if not isinstance(total, int) or not isinstance(analyzed, int) or total != analyzed or analyzed != len(rows):
            problems.append(f"{tier}: invalid total/analyzed counts")
        indexes = [window.get("window_index") for window in rows]
        if indexes != list(range(len(rows))):
            problems.append(f"{tier}: indexes must be exhaustive and ordered")
    for window in windows:
        if window.get("tier") == "fine":
            for name in ("bpm", "musical_key", "camelot"):
                if name not in window:
                    problems.append(f"fine: missing {name}")
        elif window.get("tier") == "coarse":
            for name in ("mood", "style", "danceability", "energy"):
                if name not in window:
                    problems.append(f"coarse: missing {name}")
            scores = window.get("mood_scores")
            if not isinstance(scores, dict) or any(name not in scores for name in MOOD_ORDER):
                problems.append("coarse: incomplete 11-score vector")
            elif any(
                not isinstance(scores[name], (int, float)) or not math.isfinite(scores[name]) or not 0 <= scores[name] <= 1 for name in MOOD_ORDER
            ):
                problems.append("coarse: invalid 11-score values")
        else:
            problems.append("unknown window tier")
    for name in ("bpm", "musical_key", "mood", "style", "dominant_style", "danceability", "windows"):
        if name not in result:
            problems.append(f"missing file-level {name}")
    if not isinstance(result.get("style"), list) or not result["style"]:
        problems.append("missing ranked genre scores")
    if not isinstance(result.get("mood"), dict) or len(result["mood"]) < 7:
        problems.append("missing seven file-level mood scores")
    if "characteristics" not in result:
        coarse = [window for window in windows if window.get("tier") == "coarse"]
        if coarse:
            representative = max(coarse, key=lambda window: window["end_sec"] - window["start_sec"])
            result = {**result, "characteristics": representative.get("mood_scores")}
    characteristics = result.get("characteristics")
    if not isinstance(characteristics, dict) or any(name not in characteristics for name in MOOD_ORDER):
        problems.append("missing representative 11-score vector")
    return result, sorted(set(problems))


def _stage_durations(events: list[dict[str, Any]], wall_sec: float) -> dict[str, float]:
    """Attribute time between heartbeat stage changes to the preceding stage."""
    starts = [(event["at_sec"], event["stage"]) for event in events if event["type"] == "heartbeat" and "stage" in event]
    totals: dict[str, float] = {}
    for index, (start, stage) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else wall_sec
        totals[stage] = totals.get(stage, 0.0) + max(0.0, end - start)
    return totals


def _run_one(
    item: dict[str, Any], audio_dir: Path, models_dir: Path, output_dir: Path, command: list[str], phase: str, repeat: int, protocol: str
) -> dict[str, Any]:
    sha = item["sha256"]
    audio = _audio_path(audio_dir, item)
    args = [part.replace("{audio}", str(audio)).replace("{models}", str(models_dir)) for part in command]
    run_dir = output_dir / f"{sha}-{phase}-{repeat}"
    run_dir.mkdir(parents=True, exist_ok=False)
    events: list[dict[str, Any]] = []
    terminal: dict[str, Any] | None = None
    protocol_error: str | None = None
    start = time.monotonic()
    with (run_dir / "stderr.log").open("wb") as stderr:
        process = subprocess.Popen(  # noqa: S603 -- argv is a local, explicit command template; no shell
            args,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env={k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_URL", "PHAZE_DATABASE_URL", "PHAZE_QUEUE_URL"))},
        )

        def read_protocol() -> None:
            nonlocal terminal, protocol_error
            assert process.stdout is not None
            for line in process.stdout:
                stamp = time.monotonic() - start
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    protocol_error = "invalid JSONL protocol"
                    continue
                kind = message.get("type")
                if kind in {"progress", "heartbeat"}:
                    events.append(
                        {
                            "at_sec": stamp,
                            **{k: v for k, v in message.items() if k in {"type", "stage", "done", "total", "analyzed", "coarse_analyzed"}},
                        }
                    )
                elif kind in {"result", "error"}:
                    terminal = message
                else:
                    protocol_error = "unknown protocol message"

        reader = threading.Thread(target=read_protocol, daemon=True)
        reader.start()
        _pid, status, usage = os.wait4(process.pid, 0)
        process.returncode = os.waitstatus_to_exitcode(status)
        reader.join()
    wall = time.monotonic() - start
    rss_kib = usage.ru_maxrss if platform.system() == "Linux" else usage.ru_maxrss / 1024
    outcome: dict[str, Any] = {
        "file_id": item["file_id"],
        "sha256": sha,
        "band": item["band"],
        "format": item["format"],
        "duration_sec": item["duration_sec"],
        "phase": phase,
        "repeat": repeat,
        "wall_sec": wall,
        "cpu_sec": usage.ru_utime + usage.ru_stime,
        "peak_rss_kib": rss_kib,
        "exit_code": process.returncode,
        "events": events,
        "protocol_error": protocol_error,
        "stage_wall_sec": _stage_durations(events, wall),
    }
    if terminal and terminal.get("type") == "result" and process.returncode == 0:
        try:
            adapter = normalize_current if protocol == "current" else normalize_neutral
            normalized, problems = adapter(terminal["result"], item["duration_sec"])
            outcome["contract_problems"] = problems
            (run_dir / "prediction.json").write_text(json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except (KeyError, TypeError, ValueError) as exc:
            outcome["contract_problems"] = [f"adapter: {type(exc).__name__}"]
    else:
        outcome["contract_problems"] = ["child failed or omitted terminal result"]
    (run_dir / "metrics.json").write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return outcome


def _summary(rows: list[dict[str, Any]], concurrency: int) -> dict[str, Any]:
    wall = sum(row["wall_sec"] for row in rows)
    audio = sum(row["duration_sec"] for row in rows)
    ordered = sorted(row["wall_sec"] for row in rows)
    return {
        "runs": len(rows),
        "concurrency": concurrency,
        "total_audio_hours": audio / 3600,
        "sum_job_wall_hours": wall / 3600,
        "playback_speed_from_sum": audio / wall if wall else None,
        "p50_wall_sec": ordered[len(ordered) // 2],
        "p95_wall_sec": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "failed_runs": sum(row["exit_code"] != 0 or bool(row["contract_problems"]) or bool(row["protocol_error"]) for row in rows),
        "peak_rss_kib_max": max(row["peak_rss_kib"] for row in rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True, help="read-only staged copies named <sha256>.<format>")
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="new scratch directory; never the archive")
    parser.add_argument("--workers", type=int, choices=(1, 4), required=True)
    parser.add_argument("--phase", choices=("cold", "warm"), required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--protocol", choices=("current", "neutral"), default="current")
    parser.add_argument("--command-json", type=Path, help="optional JSON argv template with {audio} and {models}; defaults to production child")
    parser.add_argument("--run-context-json", type=Path, required=True, help="image digest and resource limits recorded with results")
    args = parser.parse_args(argv)
    items = load_manifest(args.manifest)
    if args.output_dir.exists():
        parser.error("output directory already exists; choose fresh scratch")
    for item in items:
        _audio_path(args.audio_dir, item)
    verify_staged_audio(items, args.audio_dir)
    if not args.models_dir.is_dir():
        parser.error("models directory is missing")
    model_sha, model_count = model_tree_digest(args.models_dir)
    context = json.loads(args.run_context_json.read_text(encoding="utf-8"))
    if any(not context.get(key) or context[key] == "FILL_AT_RUN_TIME" for key in ("node", "image_digest", "cpu_limit", "memory_limit")):
        parser.error("run context needs node, image_digest, cpu_limit, and memory_limit")
    command = (
        json.loads(args.command_json.read_text())
        if args.command_json
        else [sys.executable, "-m", "phaze.analysis_child", "{audio}", "--models-dir", "{models}"]
    )
    if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command) or "{audio}" not in " ".join(command):
        parser.error("command JSON must be an argv string list containing {audio}")
    if args.repeat < 1:
        parser.error("repeat must be positive")
    args.output_dir.mkdir(parents=True)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(_run_one, item, args.audio_dir, args.models_dir, args.output_dir, command, args.phase, repeat, args.protocol)
            for repeat in range(1, args.repeat + 1)
            for item in items
        ]
        rows = [future.result() for future in futures]
    elapsed = time.monotonic() - started
    summary = _summary(rows, args.workers)
    summary["batch_wall_sec"] = elapsed
    summary["batch_audio_hours_per_hour"] = sum(row["duration_sec"] for row in rows) / elapsed if elapsed else None
    summary["manifest_schema_version"] = 1
    summary["protocol"] = args.protocol
    summary["python"] = sys.version.split()[0]
    summary["platform"] = platform.platform()
    git = shutil.which("git")
    summary["code_sha"] = subprocess.run([git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip() if git else None  # noqa: S603 -- fixed git subcommand
    summary["thread_env"] = {name: os.environ.get(name) for name in ("TF_NUM_INTRAOP_THREADS", "TF_NUM_INTEROP_THREADS", "OMP_NUM_THREADS")}
    summary["model_tree_sha256"] = model_sha
    summary["model_file_count"] = model_count
    summary["run_context"] = context
    summary["command"] = command
    summary["runs_detail"] = [{k: v for k, v in row.items() if k not in {"events"}} for row in rows]
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "runs_detail"}, sort_keys=True))  # noqa: T201
    return 1 if summary["failed_runs"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
