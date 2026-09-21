#!/usr/bin/env python3
"""Run or resume the frozen 500-track Jev benchmark with atomic checkpoints.

This evaluation utility reads TYPESAFE_API_KEY only from its process environment.
It never serializes the key, request headers, raw HTTP bodies, or source paths.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

import run_jev_pilot as pilot


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


BENCHMARK_SCHEMA = "phaze-jev-benchmark-corpus-v1"
RESULT_SCHEMA = "phaze-jev-mood-benchmark-v1"
ARMS = ("jev_only", "hybrid")
REPEATS = 3
EXPECTED_REQUESTS = 500 * len(ARMS) * REPEATS


def load_json(path: Path) -> dict[str, Any]:
    if path.suffix == ".gz":
        value: dict[str, Any] = json.loads(gzip.decompress(path.read_bytes()))
    else:
        value = json.loads(path.read_text())
    return value


def validate_benchmark(corpus: Mapping[str, Any]) -> list[dict[str, Any]]:
    if corpus.get("schema_version") != BENCHMARK_SCHEMA:
        raise ValueError("benchmark corpus schema is not frozen v1")
    if tuple(corpus.get("mood_order", ())) != pilot.MOODS:
        raise ValueError("benchmark mood order differs from the pilot")
    tracks = corpus.get("tracks")
    if not isinstance(tracks, list) or len(tracks) != 500:
        raise ValueError("benchmark must contain exactly 500 tracks")
    seen_ids: set[str] = set()
    group_counts: dict[str, int] = {}
    validated = []
    for index, track in enumerate(tracks, start=1):
        if not isinstance(track, dict):
            raise ValueError("benchmark track must be an object")
        file_id = track.get("file_id")
        if not isinstance(file_id, str) or not file_id or file_id in seen_ids:
            raise ValueError("benchmark file_id must be present and unique")
        seen_ids.add(file_id)
        if track.get("subject_code") != f"B{index:03d}" or track.get("benchmark_index") != index:
            raise ValueError("benchmark subjects must follow frozen B001-B500 order")
        group = track.get("leakage_group")
        if not isinstance(group, str) or not group:
            raise ValueError("benchmark leakage_group must be present")
        group_counts[group] = group_counts.get(group, 0) + 1
        if group_counts[group] > 2:
            raise ValueError("benchmark leakage group cap exceeded")
        moods = track.get("moods")
        if not isinstance(moods, dict) or set(moods) != set(pilot.MOODS):
            raise ValueError("benchmark mood vector must contain seven labels")
        for mood in pilot.MOODS:
            pilot._finite_number(moods[mood], field=f"{file_id}.moods.{mood}")
        validated.append(dict(track))
    return validated


def _manifest_digest(corpus: Mapping[str, Any]) -> str:
    canonical = json.dumps(corpus, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def _key(record: Mapping[str, Any]) -> tuple[str, str, int]:
    return str(record["file_id"]), str(record["arm"]), int(record["repeat"])


def _summary(records: Sequence[Mapping[str, Any]], attempt_history: Sequence[Mapping[str, Any]] = ()) -> dict[str, int | float]:
    successful = [record for record in records if record["status"] == "ok"]
    input_tokens = sum(int(record["usage"]["input_tokens"]) for record in successful)
    return {
        "subjects": 500,
        "requests": len(records),
        "successful_requests": len(successful),
        "failed_requests": len(records) - len(successful),
        "attempted_request_events": len(records) + len(attempt_history),
        "terminal_failure_events": sum(record["status"] == "failed" for record in [*records, *attempt_history]),
        "total_transport_attempts": sum(int(record["attempts"]) for record in [*records, *attempt_history]),
        "total_internal_retries": sum(int(record["retries"]) for record in [*records, *attempt_history]),
        "reissued_terminal_dns_failures": len(attempt_history),
        "input_tokens": input_tokens,
        "output_tokens": sum(int(record["usage"]["output_tokens"]) for record in successful),
        "cost_usd_public_rate": input_tokens / 1_000_000 * pilot.PRICE_USD_PER_MILLION_INPUT_TOKENS,
    }


def _new_result(corpus: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "manifest_sha256": _manifest_digest(corpus),
        "request_contract": {
            "endpoint": pilot.ENDPOINT,
            "requested_model": pilot.MODEL,
            "questions": pilot.QUESTION_DEFINITIONS,
            "repeats": REPEATS,
            "arms": {
                "jev_only": "metadata plus BPM, key, and duration; no classifier evidence",
                "hybrid": "the same state plus seven stored Essentia mood probabilities",
            },
        },
        "price_basis": {
            "usd_per_million_input_tokens": pilot.PRICE_USD_PER_MILLION_INPUT_TOKENS,
            "output_tokens": "free at the public price retrieved 2026-09-20",
        },
        "summary": _summary([]),
        "records": [],
        "attempt_history": [],
    }


def _load_checkpoint(path: Path, corpus: Mapping[str, Any], allowed_ids: set[str]) -> dict[str, Any]:
    if not path.exists():
        return _new_result(corpus)
    result: dict[str, Any] = load_json(path)
    if result.get("schema_version") != RESULT_SCHEMA or result.get("manifest_sha256") != _manifest_digest(corpus):
        raise ValueError("checkpoint does not match the frozen benchmark")
    expected_contract = _new_result(corpus)["request_contract"]
    if result.get("request_contract") != expected_contract:
        raise ValueError("checkpoint request contract differs from the pilot")
    keys: set[tuple[str, str, int]] = set()
    for record in result["records"]:
        key = _key(record)
        if key[0] not in allowed_ids or key[1] not in ARMS or key[2] not in range(1, REPEATS + 1):
            raise ValueError("checkpoint has an out-of-scope request key")
        if key in keys:
            raise ValueError("checkpoint has duplicate request keys")
        keys.add(key)
    attempt_history = result.setdefault("attempt_history", [])
    for old_record in attempt_history:
        key = _key(old_record)
        if key not in keys or not _terminal_dns_failure(old_record):
            raise ValueError("checkpoint has invalid terminal DNS attempt history")
    expected_summary = _summary(result["records"], attempt_history)
    recorded_summary = result["summary"]
    if not attempt_history and "attempted_request_events" not in recorded_summary:
        # Upgrade the existing three-failure v1 checkpoint without deleting evidence.
        if any(recorded_summary.get(field) != value for field, value in expected_summary.items() if field in recorded_summary):
            raise ValueError("checkpoint summary does not match its records")
        result["summary"] = expected_summary
    elif recorded_summary != expected_summary:
        raise ValueError("checkpoint summary does not match its records")
    return result


def _terminal_dns_failure(record: Mapping[str, Any]) -> bool:
    errors = record.get("errors", [])
    return record.get("status") == "failed" and bool(errors) and all(error == "network gaierror" for error in errors)


def _atomic_checkpoint(path: Path, result: Mapping[str, Any]) -> None:
    if path.suffix != ".gz":
        raise ValueError("benchmark checkpoint must use compressed .json.gz format")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            encoded = (json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
            handle.write(gzip.compress(encoded, mtime=0))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_benchmark(
    corpus: Mapping[str, Any],
    checkpoint: Path,
    *,
    api_key: str,
    caller: Callable[..., pilot.ApiResult] = pilot.call_jev,
    progress: Callable[[str], None] | None = print,
    max_new_requests: int | None = None,
    retry_terminal_dns: bool = False,
) -> dict[str, Any]:
    if corpus.get("source_data_redacted") and caller is pilot.call_jev:
        raise ValueError("redacted study evidence is for offline scoring, not live Jev requests")
    tracks = validate_benchmark(corpus)
    result = _load_checkpoint(checkpoint, corpus, {track["file_id"] for track in tracks})
    existing_by_key = {_key(record): record for record in result["records"]}
    completed = {key for key, record in existing_by_key.items() if not (retry_terminal_dns and _terminal_dns_failure(record))}
    new_requests = 0
    consecutive_dns_failures = 0
    for track in tracks:
        for arm in ARMS:
            payload = pilot.build_payload(track, arm)
            for repeat in range(1, REPEATS + 1):
                key = (track["file_id"], arm, repeat)
                if key in completed:
                    continue
                if max_new_requests is not None and new_requests >= max_new_requests:
                    return result
                api_result = caller(payload, api_key=api_key)
                record: dict[str, Any] = {
                    "file_id": track["file_id"],
                    "arm": arm,
                    "repeat": repeat,
                    "latency_ms": round(api_result.latency_ms, 3),
                    "attempts": api_result.attempts,
                    "retries": api_result.attempts - 1,
                    "errors": list(api_result.errors),
                }
                if api_result.response is None:
                    record["status"] = "failed"
                else:
                    record.update(
                        {
                            "status": "ok",
                            "resolved_model": api_result.response.get("model"),
                            "answers": pilot._answer_values(api_result.response),
                            "usage": pilot._usage(api_result.response),
                        }
                    )
                previous = existing_by_key.get(key)
                if previous is not None:
                    if not retry_terminal_dns or not _terminal_dns_failure(previous):
                        raise ValueError("unexpected repeat of a completed request key")
                    result["attempt_history"].append(previous)
                    result["records"][result["records"].index(previous)] = record
                else:
                    result["records"].append(record)
                existing_by_key[key] = record
                if not _terminal_dns_failure(record):
                    completed.add(key)
                new_requests += 1
                result["updated_at"] = datetime.now(UTC).isoformat()
                result["summary"] = _summary(result["records"], result["attempt_history"])
                _atomic_checkpoint(checkpoint, result)
                if progress:
                    progress(f"{len(completed)}/{EXPECTED_REQUESTS} {track['subject_code']} {arm} repeat={repeat} {record['status']}")
                consecutive_dns_failures = consecutive_dns_failures + 1 if _terminal_dns_failure(record) else 0
                if consecutive_dns_failures >= 3:
                    if progress:
                        progress("stopped after three consecutive terminal DNS failures; checkpoint retained")
                    return result
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument(
        "--retry-terminal-dns",
        action="store_true",
        help="explicitly reissue only prior terminal network gaierror keys and retain failure history",
    )
    args = parser.parse_args(argv)
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        parser.error("TYPESAFE_API_KEY must be set in the process environment")
    corpus = load_json(args.corpus)
    result = run_benchmark(corpus, args.checkpoint, api_key=api_key, retry_terminal_dns=args.retry_terminal_dns)
    summary = result["summary"]
    print(  # noqa: T201 -- evaluation CLI progress
        f"checkpointed {summary['requests']}/{EXPECTED_REQUESTS} requests; "
        f"{summary['successful_requests']} succeeded, {summary['failed_requests']} failed"
    )
    return 0 if summary["requests"] == EXPECTED_REQUESTS and summary["failed_requests"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
