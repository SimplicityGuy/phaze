"""DB-free unit tests for the per-row ``resolve_status`` precedence ladder (Phase 78, D-04).

Bucket: ``shared`` (path segment immediately under ``tests/``). NO ``pytest.mark.integration``,
NO Postgres, NO SQLAlchemy object construction — the resolver operates on plain scalars so a
Postgres-free compute / file-server agent can derive per-stage status with no DB round-trip.

Covers DERIV-02 (precedence ``in_flight ≻ done ≻ failed ≻ not_started``), DERIV-03 (a partial
analyze row with ``completed_at IS NULL`` is NOT done), DERIV-05 (a fingerprint file with one
``success`` and one ``failed`` engine resolves to done), and D-03 (a metadata failure-only row
resolves to FAILED, not DONE). A subprocess banned-import guard proves ``phaze.enums.stage``
never drags ``phaze.models`` / ``phaze.database`` / ``sqlalchemy`` into its import graph
(mirrors ``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from phaze.enums.stage import Stage, Status, resolve_status


_TS = "2026-07-08T00:00:00+00:00"  # any non-None sentinel timestamp scalar


# --------------------------------------------------------------------------------------
# analyze — completion discriminator + precedence (DERIV-02 / DERIV-03)
# --------------------------------------------------------------------------------------
def test_analyze_not_started() -> None:
    assert resolve_status(Stage.ANALYZE, {}) is Status.NOT_STARTED


def test_analyze_done_on_completed_at() -> None:
    assert resolve_status(Stage.ANALYZE, {"completed_at": _TS}) is Status.DONE


def test_analyze_failed_on_failed_at_without_completed() -> None:
    assert resolve_status(Stage.ANALYZE, {"completed_at": None, "failed_at": _TS}) is Status.FAILED


def test_analyze_partial_row_completed_at_null_is_not_started() -> None:
    # DERIV-03: a partial in-flight row upserted at analysis START has completed_at NULL.
    # completed_at NULL != done — it must read NOT_STARTED, never DONE.
    assert resolve_status(Stage.ANALYZE, {"completed_at": None}) is Status.NOT_STARTED


def test_analyze_inflight_wins_over_failed() -> None:
    # DERIV-02 precedence proof: the ledger (in_flight) wins even with failed_at set.
    got = resolve_status(Stage.ANALYZE, {"completed_at": None, "failed_at": _TS, "inflight": True})
    assert got is Status.IN_FLIGHT


def test_analyze_inflight_wins_over_done() -> None:
    got = resolve_status(Stage.ANALYZE, {"completed_at": _TS, "failed_at": _TS, "inflight": True})
    assert got is Status.IN_FLIGHT


# --------------------------------------------------------------------------------------
# metadata — D-03: a failure-only row is FAILED, not DONE
# --------------------------------------------------------------------------------------
def test_metadata_not_started() -> None:
    assert resolve_status(Stage.METADATA, {"row_present": False}) is Status.NOT_STARTED


def test_metadata_done_requires_row_and_no_failure() -> None:
    assert resolve_status(Stage.METADATA, {"row_present": True, "failed_at": None}) is Status.DONE


def test_metadata_failure_only_row_is_failed_not_done() -> None:
    # D-03: done(metadata) = row present AND failed_at IS NULL. A failure-only row derives FAILED.
    assert resolve_status(Stage.METADATA, {"row_present": True, "failed_at": _TS}) is Status.FAILED


def test_metadata_inflight_precedence() -> None:
    got = resolve_status(Stage.METADATA, {"row_present": True, "failed_at": _TS, "inflight": True})
    assert got is Status.IN_FLIGHT


# --------------------------------------------------------------------------------------
# fingerprint — 1:N aggregation, DERIV-05 (one success beats a failed engine)
# --------------------------------------------------------------------------------------
def test_fingerprint_not_started_on_empty() -> None:
    assert resolve_status(Stage.FINGERPRINT, {"engine_statuses": []}) is Status.NOT_STARTED


def test_fingerprint_deriv05_success_wins_over_failed() -> None:
    # DERIV-05: a file with one 'success' and one 'failed' engine resolves to done.
    got = resolve_status(Stage.FINGERPRINT, {"engine_statuses": ["success", "failed"]})
    assert got is Status.DONE


def test_fingerprint_completed_alias_counts_as_done() -> None:
    assert resolve_status(Stage.FINGERPRINT, {"engine_statuses": ["completed"]}) is Status.DONE


def test_fingerprint_failed_only_is_failed() -> None:
    assert resolve_status(Stage.FINGERPRINT, {"engine_statuses": ["failed"]}) is Status.FAILED


def test_fingerprint_inflight_precedence() -> None:
    got = resolve_status(Stage.FINGERPRINT, {"engine_statuses": ["failed"], "inflight": True})
    assert got is Status.IN_FLIGHT


# --------------------------------------------------------------------------------------
# downstream presence stages -- every stage x 4 statuses coverage
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("stage", [Stage.TRACKLIST, Stage.PROPOSE, Stage.REVIEW, Stage.APPLY])
def test_downstream_stage_four_way(stage: Stage) -> None:
    assert resolve_status(stage, {}) is Status.NOT_STARTED
    assert resolve_status(stage, {"row_present": True}) is Status.DONE
    assert resolve_status(stage, {"row_present": False, "failed": True}) is Status.FAILED
    assert resolve_status(stage, {"row_present": True, "failed": True, "inflight": True}) is Status.IN_FLIGHT


@pytest.mark.parametrize("stage", list(Stage))
def test_every_stage_reaches_in_flight(stage: Stage) -> None:
    # Every stage's twin applies the ladder with inflight first.
    assert resolve_status(stage, {"inflight": True}) is Status.IN_FLIGHT


# --------------------------------------------------------------------------------------
# D-04 / T-78-01 agent import boundary — the resolver module is DB-free
# --------------------------------------------------------------------------------------
def test_stage_module_stays_db_free() -> None:
    """``phaze.enums.stage`` must not transitively import phaze.models / phaze.database / sqlalchemy.

    Run in a SUBPROCESS (mirroring ``tests/shared/core/test_task_split.py``) so a contaminated
    import cannot poison downstream tests via sys.modules caching. This is the T-78-01 boundary
    guard: the module is imported inside the Postgres-free agent worker process.
    """
    script = textwrap.dedent("""
        import sys
        import phaze.enums.stage  # noqa: F401

        forbidden = ("phaze.models", "phaze.database", "sqlalchemy")
        present = [m for m in forbidden if m in sys.modules]
        if present:
            for m in present:
                mod = sys.modules[m]
                sys.stderr.write(f"BANNED MODULE IMPORTED: {m} (file={getattr(mod, '__file__', '?')})\\n")
            sys.exit(1)
        sys.exit(0)
    """)
    result = subprocess.run(  # noqa: S603  # trusted input: literal sys.executable + literal -c script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, f"phaze.enums.stage import contaminated sys.modules:\nstdout={result.stdout}\nstderr={result.stderr}"
