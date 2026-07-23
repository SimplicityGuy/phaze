"""FastAPI wrapper for audfprint audio fingerprinting engine."""

import asyncio
import logging
import os
from pathlib import Path
import re
import subprocess

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


logger = logging.getLogger("audfprint-service")

app = FastAPI(title="audfprint Service", version="0.1.0")

# Serialize ALL access to the pickle DB, reads included. Ingest rewrites fprint.pklz in
# place (upstream audfprint's save_pkl is a plain pickle.dump -- no temp+rename), so a
# match that opens the file mid-rewrite reads a torn gzip-pickle and dies. The former
# _ingest_lock only excluded writer-vs-writer (Research Pitfall 3); reader-vs-writer must
# be excluded too (phaze-orq3).
_db_lock = asyncio.Lock()

AUDFPRINT_SCRIPT = "/app/audfprint/audfprint.py"
FPRINT_DB = "/data/fprint/fprint.pklz"
SUBPROCESS_TIMEOUT = 120


class IngestRequest(BaseModel):
    """Request body for the ingest endpoint."""

    file_path: str


class IngestResponse(BaseModel):
    """Response body for the ingest endpoint."""

    status: str
    file_path: str


class QueryMatch(BaseModel):
    """A single fingerprint match result."""

    track_id: str
    confidence: float


class QueryResponse(BaseModel):
    """Response body for the query endpoint."""

    matches: list[QueryMatch]


class HealthResponse(BaseModel):
    """Response body for the health endpoint."""

    status: str
    engine: str
    detail: str = ""


def _database_bootstrap_status() -> tuple[bool, str]:
    """Report whether the fingerprint DB is present or creatable, without mutating anything.

    Deliberately filesystem-only (no audfprint subprocess invocation): running audfprint's
    ``new`` here with no input file would reintroduce the exact ZeroDivisionError bootstrap
    bug this function exists to detect (phaze-6kw0). A missing DB is healthy as long as its
    directory exists and is writable -- the first real ``POST /ingest`` bootstraps it via
    ``_run_ingest``.
    """
    db_path = Path(FPRINT_DB)
    if db_path.exists():
        return True, "database present"
    parent = db_path.parent
    if not parent.is_dir():
        return False, f"database directory missing: {parent}"
    if not os.access(parent, os.W_OK):
        return False, f"database directory not writable: {parent}"
    return True, "database absent, will bootstrap on first ingest"


def _run_ingest(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run audfprint synchronously (called via to_thread).

    When the database doesn't exist yet, bootstrap it together with THIS (real) file via
    ``new`` -- audfprint's ``new`` subcommand creates the database AND ingests the given
    file in one step, so the ingested duration is nonzero and upstream's unconditional
    summary division (``tothashes / soundfiletotaldur``) never divides by zero. Once the
    database exists, subsequent calls use ``add`` to append. This replaces the old
    empty-file ``new`` bootstrap (formerly ``_ensure_database``), which could never
    succeed -- upstream ``audfprint`` unconditionally divides by total ingested duration
    when printing its summary, and an empty ingest run means dividing by zero (phaze-6kw0).
    """
    command = "add" if Path(FPRINT_DB).exists() else "new"
    return subprocess.run(
        ["python", AUDFPRINT_SCRIPT, command, "--dbase", FPRINT_DB, file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


def _run_query(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run audfprint match command synchronously (called via to_thread)."""
    return subprocess.run(
        ["python", AUDFPRINT_SCRIPT, "match", "--dbase", FPRINT_DB, file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


# Upstream audfprint (dpwe/audfprint) emits a verbose match line in ONE of two shapes,
# selected by the -R/--find-time-range flag. Both end with the identical tail
# " with {N} of {M} common hashes at rank {r}". Source: audfprint_match.py
# Matcher.file_match_to_msgs (https://github.com/dpwe/audfprint) -- the exact format strings:
#
#   default (no -R): "Matched {qrymsg} as {ref} at {t:6.1f} s"
#                    where qrymsg = "{qry} {dur:.1f} sec {nhash} raw hashes"
#     e.g. "Matched /q.wav 8.4 sec 1234 raw hashes as /ref/track.mp3 at   12.3 s
#           with   456 of   789 common hashes at rank  0"
#
#   -R (find-time-range): "Matched {range:6.1f} s starting at {start:6.1f} s in {qry}
#                          to time {t:6.1f} s in {ref}"
#     e.g. "Matched   45.2 s starting at    3.1 s in /q.wav to time   12.3 s in /ref/track.mp3
#           with   456 of   789 common hashes at rank  0"
#
# `audfprint match` runs with --verbose default 1, so these verbose lines are EXACTLY what
# the deployed sidecar receives. The previous parser only understood the -R shape (it keyed off
# " in " / " to time "), but _run_query invokes match WITHOUT -R -- so every real line raised
# and was swallowed, yielding [] for every query and silently degrading the system to
# panako-only (all confidences capped at 70). See phaze-uciu.4.

# Trailing hash-count tail, shared by both shapes. Anchored to end-of-line.
_TAIL_RE = re.compile(r"\s+with\s+(\d+)\s+of\s+(\d+)\s+common hashes\s+at rank\s+-?\d+\s*$")
# default shape: ref sits between the fixed " raw hashes as " terminator of the query message
# and the trailing float-typed " at {t} s". Non-greedy ref + the anchored " at {float} s$" tail
# means a ref path that itself contains " at <n> s" still resolves to the LAST (real) time field.
_DEFAULT_REF_RE = re.compile(r"raw hashes as (?P<ref>.+?)\s+at\s+-?\d+(?:\.\d+)?\s+s$")
# -R shape: ref is everything after the fixed landmark " to time {t} s in ". Anchoring on
# " to time {float} s in " (instead of counting " in " occurrences) is robust to a query path
# that itself contains " in " -- the second ' in ' the old code chased is not positionally stable.
_TIMERANGE_REF_RE = re.compile(r"to time\s+-?\d+(?:\.\d+)?\s+s in (?P<ref>.+)$")


def _parse_matches(stdout: str) -> tuple[list[QueryMatch], int]:
    """Parse audfprint match output into structured results.

    Handles BOTH upstream report shapes (default and -R/--find-time-range); see the module-level
    format documentation above. Returns ``(matches, parse_failures)`` where ``parse_failures`` is
    the count of lines that LOOK like a match report (they carry the "Matched" + "common hashes"
    markers) but could not be parsed. The caller uses that count to escalate a total parse failure
    to a non-2xx response instead of silently returning ``[]`` (phaze-uciu.4).
    """
    matches: list[QueryMatch] = []
    parse_failures = 0
    for line in stdout.strip().splitlines():
        # Only "Matched ... common hashes" lines are match reports. A genuine no-match run emits
        # no such line, so a query with zero candidates is a real empty result, not a failure.
        if "Matched" not in line or "common hashes" not in line:
            continue

        tail = _TAIL_RE.search(line)
        if tail is None:
            parse_failures += 1
            logger.warning("Failed to parse audfprint match line: %s", line)
            continue
        # Strip the shared tail, then match the ref against the remaining head so each ref
        # regex's end-anchor ($) lands on the true end of the "... {ref}" / "... {t} s" segment.
        head = line[: tail.start()]
        ref_match = _DEFAULT_REF_RE.search(head) or _TIMERANGE_REF_RE.search(head)
        if ref_match is None:
            parse_failures += 1
            logger.warning("Failed to parse audfprint match line: %s", line)
            continue

        track_id = ref_match.group("ref").strip()
        matched_hashes = int(tail.group(1))
        total_hashes = int(tail.group(2))
        confidence = (matched_hashes / total_hashes * 100.0) if total_hashes > 0 else 0.0
        confidence = min(100.0, max(0.0, confidence))
        matches.append(QueryMatch(track_id=track_id, confidence=round(confidence, 2)))

    return matches, parse_failures


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint.

    Reflects real database availability instead of a hardcoded "healthy" -- a missing-but-
    creatable DB (fresh volume, nothing ingested yet) is healthy; a DB whose directory is
    missing or unwritable is not. Callers (``AudfprintAdapter.health()``) only look at the
    HTTP status code, so an unhealthy DB is surfaced as a non-2xx response.
    """
    available, detail = _database_bootstrap_status()
    if not available:
        logger.error("audfprint health check failed: %s", detail)
        raise HTTPException(status_code=503, detail=detail)
    return HealthResponse(status="healthy", engine="audfprint", detail=detail)


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest) -> IngestResponse:
    """Ingest a file into the audfprint fingerprint database."""
    async with _db_lock:
        result = await asyncio.to_thread(_run_ingest, request.file_path)
    if result.returncode != 0:
        logger.error("audfprint ingest failed for %s: %s", request.file_path, result.stderr)
        raise HTTPException(status_code=500, detail=result.stderr)
    return IngestResponse(status="ingested", file_path=request.file_path)


@app.post("/query", response_model=QueryResponse)
async def query(request: IngestRequest) -> QueryResponse:
    """Query the audfprint database for matches."""
    if not Path(FPRINT_DB).exists():
        return QueryResponse(matches=[])
    async with _db_lock:
        result = await asyncio.to_thread(_run_query, request.file_path)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    matches, parse_failures = _parse_matches(result.stdout)
    if parse_failures and not matches:
        # audfprint reported matches (candidate lines were present) but the parser understood
        # NONE of them. Returning [] here would silently degrade to a one-engine result capped at
        # 70 and hide the breakage -- exactly the phaze-uciu.4 failure mode. Surface a non-2xx so
        # the orchestrator (and operators) see the parse failure instead of a false "no match".
        logger.error("audfprint match parse failure: %d candidate line(s), 0 parsed", parse_failures)
        detail = f"audfprint match output unparseable: {parse_failures} candidate line(s) matched the report shape but none parsed"
        raise HTTPException(status_code=502, detail=detail)
    if parse_failures:
        # Some lines parsed, some did not: return what we have but do NOT let the partial failure
        # pass unseen -- an aggregate error log makes the degradation observable.
        logger.error("audfprint match partial parse failure: %d of %d candidate line(s) unparsed", parse_failures, parse_failures + len(matches))
    return QueryResponse(matches=matches)
