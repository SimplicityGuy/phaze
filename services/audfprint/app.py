"""FastAPI wrapper for audfprint audio fingerprinting engine."""

import asyncio
import logging
from pathlib import Path
import subprocess

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


logger = logging.getLogger("audfprint-service")

app = FastAPI(title="audfprint Service", version="0.1.0")

# Serialize write operations to prevent concurrent pickle corruption (Research Pitfall 3)
_ingest_lock = asyncio.Lock()

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


def _ensure_database() -> None:
    """Create the audfprint database if it does not exist."""
    if not Path(FPRINT_DB).exists():
        result = subprocess.run(
            ["python", AUDFPRINT_SCRIPT, "new", "--dbase", FPRINT_DB],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            msg = f"Failed to create database: {result.stderr}"
            raise RuntimeError(msg)


def _run_ingest(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run audfprint add command synchronously (called via to_thread)."""
    _ensure_database()
    return subprocess.run(
        ["python", AUDFPRINT_SCRIPT, "add", "--dbase", FPRINT_DB, file_path],
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


def _parse_matches(stdout: str) -> list[QueryMatch]:
    """Parse audfprint match output into structured results.

    audfprint match output lines look like:
      Matched ... s starting at ... s in <track_id> to time ... s in <query> with ... of ... common hashes at rank ...
    We extract track_id and compute a normalized 0-100 confidence from the hash ratio.
    """
    matches: list[QueryMatch] = []
    for line in stdout.strip().splitlines():
        if "Matched" not in line or "common hashes" not in line:
            continue
        try:
            # Extract track path from "in <track_id> to time"
            in_idx = line.index(" in ") + 4
            to_idx = line.index(" to time ", in_idx)
            track_id = line[in_idx:to_idx].strip()

            # Extract hash ratio "with N of M common hashes"
            with_idx = line.index(" with ") + 6
            of_idx = line.index(" of ", with_idx)
            common_idx = line.index(" common hashes", of_idx)
            matched_hashes = int(line[with_idx:of_idx])
            total_hashes = int(line[of_idx + 4 : common_idx])

            confidence = (matched_hashes / total_hashes * 100.0) if total_hashes > 0 else 0.0
            confidence = min(100.0, max(0.0, confidence))
            matches.append(QueryMatch(track_id=track_id, confidence=round(confidence, 2)))
        except (ValueError, IndexError):
            logger.warning("Failed to parse match line: %s", line)
            continue
    return matches


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(status="healthy", engine="audfprint")


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest) -> IngestResponse:
    """Ingest a file into the audfprint fingerprint database."""
    async with _ingest_lock:
        result = await asyncio.to_thread(_run_ingest, request.file_path)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return IngestResponse(status="ingested", file_path=request.file_path)


@app.post("/query", response_model=QueryResponse)
async def query(request: IngestRequest) -> QueryResponse:
    """Query the audfprint database for matches."""
    if not Path(FPRINT_DB).exists():
        return QueryResponse(matches=[])
    result = await asyncio.to_thread(_run_query, request.file_path)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    matches = _parse_matches(result.stdout)
    return QueryResponse(matches=matches)
