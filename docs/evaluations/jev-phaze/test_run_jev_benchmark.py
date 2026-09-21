from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any

import pytest


if TYPE_CHECKING:
    from types import ModuleType


HERE = Path(__file__).parent
CORPUS = HERE / "phaze-eswzd.1-benchmark.json.gz"
PILOT = HERE / "phaze-eswzd.1-corpus.json"


def _module(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def modules() -> tuple[ModuleType, ModuleType]:
    pilot = _module("run_jev_pilot", "run_jev_pilot.py")
    benchmark = _module("run_jev_benchmark", "run_jev_benchmark.py")
    return pilot, benchmark


def test_frozen_sample_has_500_distinct_pilot_disjoint_subjects_and_groups(
    modules: tuple[ModuleType, ModuleType],
) -> None:
    _, benchmark = modules
    corpus = benchmark.load_json(CORPUS)
    tracks = benchmark.validate_benchmark(corpus)
    pilot_tracks = json.loads(PILOT.read_text())["tracks"]
    pilot_ids = {track["file_id"] for track in pilot_tracks}
    pilot_groups = {
        f"{' '.join(track['artist'].lower().split())}|{' '.join((track['album'] or 'unknown album').lower().split())}" for track in pilot_tracks
    }
    assert len(tracks) == 500
    assert not pilot_ids.intersection(track["file_id"] for track in tracks)
    assert not pilot_groups.intersection(track["leakage_group"] for track in tracks)
    assert len({track["leakage_group"] for track in tracks}) == 437
    assert {track["metadata_completeness"] for track in tracks} == {"complete", "incomplete"}


def test_redacted_evidence_cannot_be_replayed_to_jev(modules: tuple[ModuleType, ModuleType], tmp_path: Path) -> None:
    _, benchmark = modules
    corpus = benchmark.load_json(CORPUS)
    with pytest.raises(ValueError, match="offline scoring"):
        benchmark.run_benchmark(corpus, tmp_path / "checkpoint.json.gz", api_key="test-only-value", progress=None)


def test_checkpoint_is_atomic_and_resume_never_duplicates_request_keys(modules: tuple[ModuleType, ModuleType], tmp_path: Path) -> None:
    pilot, benchmark = modules
    corpus = benchmark.load_json(CORPUS)
    checkpoint = tmp_path / "checkpoint.json.gz"
    calls: list[dict[str, Any]] = []

    def fake_caller(payload: dict[str, Any], *, api_key: str) -> Any:
        assert api_key == "test-only-value"
        calls.append(payload)
        answers = {mood: {"noul": 0.25} for mood in pilot.MOODS}
        return pilot.ApiResult(
            {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 20}},
            12.5,
            1,
            (),
        )

    first = benchmark.run_benchmark(corpus, checkpoint, api_key="test-only-value", caller=fake_caller, progress=None, max_new_requests=2)
    assert first["summary"]["requests"] == 2
    assert checkpoint.exists()
    assert not list(tmp_path.glob(".*.tmp"))
    second = benchmark.run_benchmark(corpus, checkpoint, api_key="test-only-value", caller=fake_caller, progress=None, max_new_requests=3)
    assert second["summary"]["requests"] == 5
    assert len(calls) == 5
    assert len({(record["file_id"], record["arm"], record["repeat"]) for record in second["records"]}) == 5
    assert "essentia_classifier_evidence" not in calls[0]["state"]
    assert "essentia_classifier_evidence" in calls[3]["state"]
    assert second["request_contract"]["questions"] == pilot.QUESTION_DEFINITIONS
    assert second["summary"]["cost_usd_public_rate"] == pytest.approx(0.000021)
    assert "test-only-value" not in json.dumps(benchmark.load_json(checkpoint))


def test_resume_rejects_mismatched_manifest_and_duplicate_keys(modules: tuple[ModuleType, ModuleType], tmp_path: Path) -> None:
    pilot, benchmark = modules
    corpus = benchmark.load_json(CORPUS)
    checkpoint = tmp_path / "checkpoint.json.gz"

    def fake_caller(_payload: dict[str, Any], *, api_key: str) -> Any:
        assert api_key
        return pilot.ApiResult(
            {
                "model": "jev-test",
                "answers": {mood: {"noul": 0.25} for mood in pilot.MOODS},
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
            12.5,
            1,
            (),
        )

    benchmark.run_benchmark(corpus, checkpoint, api_key="test-only-value", caller=fake_caller, progress=None, max_new_requests=1)
    changed = benchmark.load_json(CORPUS)
    changed["tracks"][0]["title"] = "Changed"
    with pytest.raises(ValueError, match="does not match"):
        benchmark.run_benchmark(changed, checkpoint, api_key="test-only-value", caller=fake_caller, progress=None)
    payload = benchmark.load_json(checkpoint)
    payload["records"].append(payload["records"][0])
    benchmark._atomic_checkpoint(checkpoint, payload)
    with pytest.raises(ValueError, match="duplicate request keys"):
        benchmark.run_benchmark(corpus, checkpoint, api_key="test-only-value", caller=fake_caller, progress=None)


def test_explicit_dns_reissue_preserves_prior_failures_and_upgrades_checkpoint(modules: tuple[ModuleType, ModuleType], tmp_path: Path) -> None:
    pilot, benchmark = modules
    corpus = benchmark.load_json(CORPUS)
    checkpoint = tmp_path / "checkpoint.json.gz"

    def dns_failure(_payload: dict[str, Any], *, api_key: str) -> Any:
        assert api_key
        return pilot.ApiResult(None, 3000.0, 3, ("network gaierror",) * 3)

    first = benchmark.run_benchmark(corpus, checkpoint, api_key="test-only-value", caller=dns_failure, progress=None)
    assert first["summary"]["failed_requests"] == 3
    assert first["summary"]["cost_usd_public_rate"] == 0

    # Recreate the original three-request checkpoint schema created before retry support.
    legacy = benchmark.load_json(checkpoint)
    legacy.pop("attempt_history")
    for field in (
        "attempted_request_events",
        "terminal_failure_events",
        "total_transport_attempts",
        "total_internal_retries",
        "reissued_terminal_dns_failures",
    ):
        legacy["summary"].pop(field)
    benchmark._atomic_checkpoint(checkpoint, legacy)

    def success(_payload: dict[str, Any], *, api_key: str) -> Any:
        assert api_key
        return pilot.ApiResult(
            {
                "model": "jev-test",
                "answers": {mood: {"noul": 0.25} for mood in pilot.MOODS},
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
            100.0,
            1,
            (),
        )

    resumed = benchmark.run_benchmark(
        corpus,
        checkpoint,
        api_key="test-only-value",
        caller=success,
        progress=None,
        max_new_requests=3,
        retry_terminal_dns=True,
    )
    assert resumed["summary"]["requests"] == 3
    assert resumed["summary"]["successful_requests"] == 3
    assert resumed["summary"]["failed_requests"] == 0
    assert resumed["summary"]["attempted_request_events"] == 6
    assert resumed["summary"]["terminal_failure_events"] == 3
    assert resumed["summary"]["total_transport_attempts"] == 12
    assert resumed["summary"]["total_internal_retries"] == 6
    assert resumed["summary"]["reissued_terminal_dns_failures"] == 3
    assert len(resumed["attempt_history"]) == 3
    assert all(record["errors"] == ["network gaierror"] * 3 for record in resumed["attempt_history"])
    assert len({(record["file_id"], record["arm"], record["repeat"]) for record in resumed["records"]}) == 3
    assert "test-only-value" not in json.dumps(benchmark.load_json(checkpoint))
