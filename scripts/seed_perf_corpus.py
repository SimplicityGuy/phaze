"""Seed a synthetic ~200K-file corpus for the PERF-02 /pipeline/stats latency measurement (Phase 82, D-06).

Standalone ``uv run`` script (``scripts/coverage_floor.py`` shape) that bulk-inserts ~N music/video
:class:`~phaze.models.file.FileRecord` rows plus output-table rows (metadata / fingerprint / analysis /
cloud_job / dedup_resolution / scheduling_ledger) distributed to the D-06 mid-pipeline selectivity profile,
so the derived read queries (the three enrich pending sets + the four-bucket ``GROUP BY stage_status_case``
counts + the full ``/pipeline/stats`` endpoint) can be EXPLAIN-ANALYZEd at realistic scale against a DB that
carries the migration-032 partial indexes the anti-joins ride.

Why local-synthetic and NOT a live prod probe (RESEARCH Pitfall 5 / D-06): prod is at Alembic ~031 and
LACKS the 032 partial indexes, so a live EXPLAIN would show pessimistic Seq Scans and misjudge the plan.
The measurement is only valid at migration HEAD (>=036) where the indexes exist -- so we seed our OWN
throwaway DB at HEAD and measure there.

Safety (T-82-07 / T-82-08): every insert is a PARAMETERIZED ``unnest``-array bulk INSERT -- NO f-string /
string-interpolated SQL touches a value. ``--reseed`` TRUNCATEs, and is HARD-GATED to a database whose name
contains ``perf`` (mirrors the ``*_test`` destructive-DB guard) so it can never nuke prod or the shared
``phaze_test`` DB.

Distribution (D-06, fractions of N, deterministic by ``i % 100`` so a re-run reproduces the same corpus):

* ~70% carry a ``metadata`` row; ~2% of those are failure-only (``failed_at`` set -> metadata FAILED bucket).
* ~55% carry a ``fingerprint_results`` success row; ~2% are failure-only (``status='failed'``, no success).
* ~40% carry an ``analysis`` row with ``analysis_completed_at`` set (DONE); ~5% are ``failed_at`` (terminal).
  The two are DISJOINT so the ``analysis_completed_xor_failed`` CHECK (migration 033) always holds.
* ~1% carry a ``cloud_job`` row cycling the active statuses; ~2% carry a ``dedup_resolution`` marker.
* A few-thousand ``scheduling_ledger`` in-flight rows keyed ``"<function>:<file_id>"`` (process_file /
  extract_file_metadata / fingerprint_file) -> those files read IN_FLIGHT for that stage.
* ~3% get a non-media ``file_type`` (``txt``) so the ``file_type IN MUSIC_VIDEO_TYPES`` scope is exercised.

Idempotency: row ids are deterministic ``uuid5`` values and every INSERT is ``ON CONFLICT DO NOTHING``, so a
re-run without ``--reseed`` is a no-op (never duplicates / corrupts). ``FileRecord.state`` is stamped to the
furthest reached stage (dual-write realism) so a downstream shadow-compare stays representative.

Usage::

    uv run python scripts/seed_perf_corpus.py --n 200000 \\
        --dsn postgresql://phaze:phaze@localhost:5433/phaze_perf82 --reseed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import asyncpg  # type: ignore[import-untyped]


# Deterministic id namespace so a re-run reproduces the SAME corpus (idempotent ON CONFLICT DO NOTHING).
_NS = uuid.UUID("00000000-0000-0000-0000-0000000082e2")

# Music/video extensions -- MUST match phaze.services.pipeline.MUSIC_VIDEO_TYPES (the scope the derived
# pending sets + four-bucket counts filter on). Hard-coded here to keep this a dependency-light standalone
# script; a drift is caught the moment a query returns 0 rows at seed scale.
_MUSIC_VIDEO = ("mp3", "m4a", "ogg", "flac", "wav", "aiff", "wma", "aac", "opus", "mp4", "mkv", "avi", "webm", "mov", "wmv", "flv")
_NON_MEDIA = "txt"

_LEGACY_AGENT = "legacy-application-server"
_DEFAULT_DSN = "postgresql://phaze:phaze@localhost:5433/phaze_perf82"

# Stage label -> SAQ function name for the scheduling_ledger in-flight key prefix. MUST match
# phaze.tasks._shared.stage_control.STAGE_TO_FUNCTION (the real ledger PK builder).
_STAGE_FUNCS = ("extract_file_metadata", "process_file", "fingerprint_file")

# Tables the --reseed TRUNCATE clears (children first is unnecessary under CASCADE, listed for clarity).
_SEED_TABLES = ("scheduling_ledger", "dedup_resolution", "cloud_job", "analysis", "metadata", "fingerprint_results", "files")

_BATCH = 10_000


def _fid(i: int) -> uuid.UUID:
    """Deterministic per-file UUID (idempotent re-runs)."""
    return uuid.uuid5(_NS, f"file:{i}")


def _rid(kind: str, i: int) -> uuid.UUID:
    """Deterministic per-output-row UUID."""
    return uuid.uuid5(_NS, f"{kind}:{i}")


class _Plan:
    """Deterministic per-file disposition derived purely from ``i`` (no RNG -> reproducible corpus)."""

    __slots__ = (
        "analysis_completed",
        "analysis_failed",
        "bucket",
        "cloud",
        "dedup",
        "fp_failed",
        "fp_success",
        "is_media",
        "ledger_stage",
        "metadata_failed",
        "metadata_present",
        "state",
    )

    def __init__(self, i: int, n_ledger: int) -> None:
        b = i % 100
        self.bucket = b
        self.is_media = b not in (95, 96, 97)  # ~3% non-media
        self.metadata_present = b < 70  # ~70%
        self.metadata_failed = b in (68, 69)  # ~2%, within the metadata_present band
        self.fp_success = b < 55  # ~55%
        self.fp_failed = b in (55, 56)  # ~2%, failure-only (no success row)
        self.analysis_completed = b < 40  # ~40%
        self.analysis_failed = b in (40, 41, 42, 43, 44)  # ~5%, DISJOINT from completed (XOR safe)
        self.cloud = b == 50  # ~1%
        self.dedup = b in (98, 99)  # ~2%
        # A few-thousand in-flight ledger rows over the FIRST n_ledger files, cycling the three stages.
        self.ledger_stage = _STAGE_FUNCS[i % 3] if i < n_ledger else None
        self.state = self._state()

    def _state(self) -> str:
        # Dual-write realism: stamp state to the furthest stage the file reached (derived readers IGNORE it).
        if self.dedup:
            return "duplicate_resolved"
        if self.cloud:
            return "awaiting_cloud"
        if self.analysis_completed:
            return "analyzed"
        if self.analysis_failed:
            return "analysis_failed"
        if self.fp_success:
            return "fingerprinted"
        if self.metadata_present and not self.metadata_failed:
            return "metadata_extracted"
        return "discovered"

    def cloud_status(self, i: int) -> str:
        # Cycle the ACTIVE cloud_job statuses (+ terminal failed) so the awaiting partial index is exercised.
        return ("awaiting", "uploading", "uploaded", "submitted", "running", "succeeded", "failed")[i % 7]


async def _copy_unnest(conn: asyncpg.Connection, table: str, columns: list[str], types: list[str], rows: list[tuple]) -> int:
    """Idempotent parameterized bulk INSERT via ``unnest`` arrays (NO f-string on any VALUE).

    The column/type identifiers come ONLY from the hard-coded call sites below (never user input); the
    row VALUES are bound as PostgreSQL arrays, so this carries no injection surface (T-82-07).
    """
    if not rows:
        return 0
    cols_sql = ", ".join(columns)
    selects = ", ".join(f"c{j}" for j in range(len(columns)))
    unnest_args = ", ".join(f"${j + 1}::{types[j]}[]" for j in range(len(columns)))
    col_aliases = ", ".join(f"c{j}" for j in range(len(columns)))
    stmt = f"INSERT INTO {table} ({cols_sql}) SELECT {selects} FROM unnest({unnest_args}) AS t({col_aliases}) ON CONFLICT DO NOTHING"  # noqa: S608 -- identifiers are hard-coded literals; all VALUES are bound arrays
    inserted = 0
    for start in range(0, len(rows), _BATCH):
        chunk = rows[start : start + _BATCH]
        arrays = [[r[j] for r in chunk] for j in range(len(columns))]
        # `stmt` interpolates only hard-coded identifiers (see the S608 note where it is built); every VALUE is
        # a bound `*arrays` param, not string-interpolated. Dev/CI-only seeder, no user input (T-82-07).
        # nosemgrep: python.lang.security.audit.sqli.asyncpg-sqli.asyncpg-sqli,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        await conn.execute(stmt, *arrays)
        inserted += len(chunk)
    return inserted


async def seed(dsn: str, n: int, *, reseed: bool) -> dict[str, int]:
    """Seed ``n`` files + their output rows into ``dsn``; return per-table inserted counts."""
    n_ledger = min(n // 40, 6000)
    conn = await asyncpg.connect(dsn)
    try:
        db_name = await conn.fetchval("SELECT current_database()")
        if reseed:
            if "perf" not in str(db_name):
                raise SystemExit(f"--reseed refused: database {db_name!r} is not a perf DB (name must contain 'perf'). Refusing to TRUNCATE.")
            # `_SEED_TABLES` is a module-level constant of hard-coded table names (no user input); additionally
            # gated by the perf-DB-name guard above (T-82-07 / T-82-08).
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.lang.security.audit.sqli.asyncpg-sqli.asyncpg-sqli,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            await conn.execute(f"TRUNCATE {', '.join(_SEED_TABLES)} RESTART IDENTITY CASCADE")

        # FK parent: the legacy agent every FileRecord references by default.
        await conn.execute(
            "INSERT INTO agents (id, name, kind, scan_roots) VALUES ($1, $2, 'fileserver', '[]'::jsonb) ON CONFLICT (id) DO NOTHING",
            _LEGACY_AGENT,
            _LEGACY_AGENT,
        )

        files: list[tuple] = []
        metadata: list[tuple] = []
        fingerprints: list[tuple] = []
        analyses: list[tuple] = []
        cloud_jobs: list[tuple] = []
        dedups: list[tuple] = []
        ledger: list[tuple] = []

        for i in range(n):
            p = _Plan(i, n_ledger)
            fid = _fid(i)
            ext = _MUSIC_VIDEO[i % len(_MUSIC_VIDEO)] if p.is_media else _NON_MEDIA
            path = f"/music/perf/{i:07d}.{ext}"
            files.append((fid, f"{i:064x}"[:64], path, f"{i:07d}.{ext}", path, ext, 4_000_000 + i, p.state, _LEGACY_AGENT))

            if p.metadata_present:
                metadata.append((_rid("md", i), fid, 210.5, "failed extraction" if p.metadata_failed else None))
            if p.fp_success:
                fingerprints.append((_rid("fp", i), fid, "chromaprint", "success", None))
            elif p.fp_failed:
                fingerprints.append((_rid("fp", i), fid, "chromaprint", "failed", "engine error"))
            if p.analysis_completed:
                analyses.append((_rid("an", i), fid, 128.0, None, "done"))
            elif p.analysis_failed:
                analyses.append((_rid("an", i), fid, None, "analysis crashed", "failed"))
            if p.cloud:
                cloud_jobs.append((_rid("cj", i), fid, p.cloud_status(i)))
            if p.dedup:
                dedups.append((_rid("dd", i), fid))
            if p.ledger_stage is not None:
                ledger.append((f"{p.ledger_stage}:{fid}", p.ledger_stage, "controller", json.dumps({"file_id": str(fid)})))

        counts: dict[str, int] = {}
        counts["files"] = await _copy_unnest(
            conn,
            "files",
            ["id", "sha256_hash", "original_path", "original_filename", "current_path", "file_type", "file_size", "state", "agent_id"],
            ["uuid", "text", "text", "text", "text", "text", "int8", "text", "text"],
            files,
        )
        # metadata: (id, file_id, duration, failed_at?) -- failed_at is now() when the 4th arg is non-NULL.
        counts["metadata"] = await _copy_unnest(
            conn,
            "metadata",
            ["id", "file_id", "duration", "error_message"],
            ["uuid", "uuid", "float8", "text"],
            metadata,
        )
        # Stamp metadata.failed_at for the failure-only rows (error_message carries the marker text).
        await conn.execute("UPDATE metadata SET failed_at = now() WHERE error_message IS NOT NULL AND failed_at IS NULL")
        counts["fingerprint_results"] = await _copy_unnest(
            conn,
            "fingerprint_results",
            ["id", "file_id", "engine", "status", "error_message"],
            ["uuid", "uuid", "text", "text", "text"],
            fingerprints,
        )
        # analysis: 5th arg is a tag ('done'|'failed'); translate to completed_at / failed_at post-insert.
        counts["analysis"] = await _copy_unnest(
            conn,
            "analysis",
            ["id", "file_id", "bpm", "error_message", "musical_key"],
            ["uuid", "uuid", "float8", "text", "text"],
            analyses,
        )
        await conn.execute("UPDATE analysis SET analysis_completed_at = now() WHERE musical_key = 'done' AND analysis_completed_at IS NULL")
        await conn.execute("UPDATE analysis SET failed_at = now() WHERE musical_key = 'failed' AND failed_at IS NULL")
        await conn.execute("UPDATE analysis SET musical_key = NULL WHERE musical_key IN ('done', 'failed')")
        counts["cloud_job"] = await _copy_unnest(
            conn,
            "cloud_job",
            ["id", "file_id", "status"],
            ["uuid", "uuid", "text"],
            cloud_jobs,
        )
        counts["dedup_resolution"] = await _copy_unnest(
            conn,
            "dedup_resolution",
            ["id", "file_id"],
            ["uuid", "uuid"],
            dedups,
        )
        counts["scheduling_ledger"] = await _copy_unnest(
            conn,
            "scheduling_ledger",
            ["key", "function", "routing", "payload"],
            ["text", "text", "text", "jsonb"],
            ledger,
        )
        # Keep the planner's statistics fresh so the EXPLAIN plans reflect the seeded distribution.
        await conn.execute("ANALYZE")
        return counts
    finally:
        await conn.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed a synthetic music/video corpus for the PERF-02 /pipeline/stats bench (Phase 82).")
    parser.add_argument("--n", type=int, default=200_000, help="number of FileRecord rows to seed (default 200000)")
    parser.add_argument("--dsn", default=_DEFAULT_DSN, help=f"asyncpg DSN of the perf DB (default {_DEFAULT_DSN})")
    parser.add_argument("--reseed", action="store_true", help="TRUNCATE the seed tables first (perf-DB only; name must contain 'perf')")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    counts = asyncio.run(seed(args.dsn, args.n, reseed=args.reseed))
    total = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"seeded n={args.n} into {args.dsn}: {total}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
