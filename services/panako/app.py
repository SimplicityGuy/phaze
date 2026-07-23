"""FastAPI wrapper for Panako audio fingerprinting engine."""

import asyncio
import logging
from pathlib import Path
import subprocess

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("panako-service")

app = FastAPI(title="Panako Service", version="0.1.0")

PANAKO_JAR = "/app/panako.jar"
SUBPROCESS_TIMEOUT = 120

# Panako stores fingerprints in LMDB via lmdbjava, which reaches into java.nio.Buffer
# by reflection. Since JDK 16 the module system denies that by default, so EVERY store
# and query dies with:
#   InaccessibleObjectException: Unable to make field long java.nio.Buffer.address
#   accessible: module java.base does not "opens java.nio" to unnamed module
# The runtime stage ships a JRE 21, so this flag is mandatory -- without it the service
# 500s on every request even with a perfectly good jar. Upstream's own build.gradle
# sets the identical flag for its test JVM ("needed for lmdb to work correctly").
JAVA_BASE_CMD = ["java", "--add-opens=java.base/java.nio=ALL-UNNAMED", "-jar", PANAKO_JAR]
# The health probe shells out to the JVM; keep it far below the ingest timeout so a
# wedged engine surfaces as unhealthy quickly instead of hanging the healthcheck.
HEALTH_TIMEOUT = 30
# Truncate captured stderr in logs -- a stack-trace flood per failed file would bury
# the signal, but the head of the trace is what identifies the failure mode.
STDERR_LOG_LIMIT = 2000


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
    detail: str | None = None


def _log_subprocess_failure(operation: str, file_path: str, result: subprocess.CompletedProcess[str]) -> None:
    """Log a failed Panako subprocess server-side, including its stderr.

    During the 2026.7.7 outage every /ingest returned 500 for 40 minutes and left
    ZERO tracebacks in `docker logs phaze-panako` -- only uvicorn access lines. The
    stderr that would have identified the cause in seconds ("Unable to access jarfile
    /app/panako.jar") was returned to the caller and then dropped on the floor.
    """
    stderr = (result.stderr or "").strip()
    logger.error(
        "Panako %s FAILED for %s (exit %d): %s",
        operation,
        file_path,
        result.returncode,
        stderr[:STDERR_LOG_LIMIT] or "<no stderr>",
    )


def _probe_jar() -> str | None:
    """Verify the Panako jar exists and the CLI actually runs.

    Returns None when healthy, or a human-readable reason string when not.
    """
    if not Path(PANAKO_JAR).exists():
        return f"Panako jar missing at {PANAKO_JAR}"
    if Path(PANAKO_JAR).stat().st_size == 0:
        return f"Panako jar at {PANAKO_JAR} is empty (0 bytes)"
    try:
        result = subprocess.run(
            JAVA_BASE_CMD,
            capture_output=True,
            text=True,
            timeout=HEALTH_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return "java runtime not found on PATH"
    except subprocess.TimeoutExpired:
        return f"Panako CLI did not respond within {HEALTH_TIMEOUT}s"
    # Panako's bare-invocation help text goes to stdout and exits non-zero on some
    # builds, so the exit code alone is not a reliable signal. What IS reliable is
    # that a working jar produces Panako's own output; a missing/corrupt one produces
    # a JVM loader error such as "Unable to access jarfile" or "Invalid or corrupt".
    combined = f"{result.stdout}\n{result.stderr}"
    if "Unable to access jarfile" in combined or "Invalid or corrupt jarfile" in combined:
        return f"Panako jar is unreadable or corrupt: {combined.strip()[:STDERR_LOG_LIMIT]}"
    if "panako" not in combined.lower():
        return f"Panako CLI produced unrecognized output: {combined.strip()[:STDERR_LOG_LIMIT] or '<no output>'}"
    return None


def _run_ingest(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run Panako store command synchronously (called via to_thread)."""
    return subprocess.run(
        [*JAVA_BASE_CMD, "store", file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


def _run_query(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run Panako query command synchronously (called via to_thread)."""
    return subprocess.run(
        [*JAVA_BASE_CMD, "query", file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


# A Panako query record has FIXED arity 13 (see _parse_matches); the trailing 7 fields
# (match id .. seconds-with-match) are engine-generated numerics/percentages that can
# never contain ';', so they can always be taken from the RIGHT end of the split.
_RECORD_FIELDS = 13
_TRAILING_FIELDS = 7


def _is_float(fragment: str) -> bool:
    try:
        float(fragment)
    except ValueError:
        return False
    return True


def _match_path(middle: list[str]) -> str | None:
    """Recover the match-path field from the variable-arity middle of a record.

    ``middle`` holds the raw ';'-split fragments of "query path ; query start ;
    query stop ; match path". Either path may itself contain ';', so locate the first
    adjacent pair of purely-numeric fragments (query start/stop) with at least one
    fragment on each side, and re-join everything after that pair -- restoring any ';'
    the split consumed inside the match path. Returns None when no such pair exists
    (structurally unparseable row).
    """
    for i in range(1, len(middle) - 2):
        if _is_float(middle[i]) and _is_float(middle[i + 1]):
            return ";".join(middle[i + 2 :]).strip()
    return None


def _parse_matches(stdout: str) -> list[QueryMatch]:
    """Parse Panako query output into structured results.

    Panako query output is semicolon-separated with FIXED arity 13:
      0 index; 1 total; 2 query path; 3 query start; 4 query stop; 5 match path;
      6 match id; 7 match start; 8 match stop; 9 score; 10 time factor;
      11 freq factor; 12 seconds-with-match percentage

    We use the match path as track_id and the seconds-with-match percentage as
    confidence normalized to 0-100.

    The two path fields embed raw file paths verbatim with NO quoting/escaping, and a
    messy personal archive legitimately contains ';' in filenames ("Sven; Vath -
    Cocoon.mp3" -- the exact corpus this tool exists to clean up). A blind positional
    ``line.split(';')`` shifts every field after an embedded ';', fabricating a phantom
    match (track_id = a path fragment, confidence read from the wrong column) or
    silently dropping a real one (phaze-9pmn). Parse from the record's fixed-arity ends
    instead: leading index/total from the LEFT, the 7 never-semicolon numeric fields
    from the RIGHT, and recover the match path from the variable middle.
    """
    matches: list[QueryMatch] = []
    for line in stdout.strip().splitlines():
        # Skip header lines or empty lines
        if not line.strip() or ";" not in line:
            continue
        parts = line.split(";")
        if len(parts) < _RECORD_FIELDS:
            continue
        try:
            # Field 0 is index -- skip if it's a header (non-numeric)
            int(parts[0])
        except ValueError:
            continue
        tail = [p.strip() for p in parts[-_TRAILING_FIELDS:]]
        track_id = _match_path(parts[2:-_TRAILING_FIELDS])
        if track_id is None or not _is_float(tail[3]) or not _is_float(tail[6]):
            logger.warning("Failed to parse match line: %s", line)
            continue
        # Panako emits a SENTINEL ROW for "no match found" rather than emitting
        # nothing: match path and match id are the literal string "null" and the
        # score/start/stop are -1. Without this guard that row is parsed as a real
        # hit, and the service returns a phantom match {track_id: "null",
        # confidence: 0.0} -- feeding a bogus duplicate into the dedup pipeline.
        if track_id.lower() == "null" or tail[0].lower() == "null":
            continue
        match_score = float(tail[3])  # match score
        if match_score < 0:
            continue
        match_percentage = float(tail[6])  # seconds-with-match percentage
        confidence = min(100.0, max(0.0, match_percentage))
        matches.append(QueryMatch(track_id=track_id, confidence=round(confidence, 2)))
    return matches


@app.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    """Health check endpoint.

    This MUST actually exercise the jar. The 2026.7.7 panako image shipped with no
    /app/panako.jar at all, and because this endpoint used to return a hardcoded
    {"status": "healthy"}, every healthcheck and dashboard reported a healthy engine
    through a total 100%-failure outage. A health check that cannot observe the
    engine's core dependency is worse than no health check at all.
    """
    detail = await asyncio.to_thread(_probe_jar)
    if detail is not None:
        logger.error("Panako health check FAILED: %s", detail)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="unhealthy", engine="panako", detail=detail)
    return HealthResponse(status="healthy", engine="panako")


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest) -> IngestResponse:
    """Ingest a file into the Panako fingerprint database."""
    result = await asyncio.to_thread(_run_ingest, request.file_path)
    if result.returncode != 0:
        _log_subprocess_failure("ingest", request.file_path, result)
        raise HTTPException(status_code=500, detail=result.stderr)
    return IngestResponse(status="ingested", file_path=request.file_path)


@app.post("/query", response_model=QueryResponse)
async def query(request: IngestRequest) -> QueryResponse:
    """Query the Panako database for matches."""
    result = await asyncio.to_thread(_run_query, request.file_path)
    if result.returncode != 0:
        _log_subprocess_failure("query", request.file_path, result)
        raise HTTPException(status_code=500, detail=result.stderr)
    matches = _parse_matches(result.stdout)
    return QueryResponse(matches=matches)
