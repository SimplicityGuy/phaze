"""FastAPI wrapper for Panako audio fingerprinting engine."""

import asyncio
import logging
import subprocess

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


logger = logging.getLogger("panako-service")

app = FastAPI(title="Panako Service", version="0.1.0")

PANAKO_JAR = "/app/panako.jar"
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


def _run_ingest(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run Panako store command synchronously (called via to_thread)."""
    return subprocess.run(
        ["java", "-jar", PANAKO_JAR, "store", file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


def _run_query(file_path: str) -> subprocess.CompletedProcess[str]:
    """Run Panako query command synchronously (called via to_thread)."""
    return subprocess.run(
        ["java", "-jar", PANAKO_JAR, "query", file_path],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )


def _parse_matches(stdout: str) -> list[QueryMatch]:
    """Parse Panako query output into structured results.

    Panako query output is semicolon-separated with fields:
      index; total; query path; query start; query end; match path; match ID;
      match start; match end; score; time factor; freq factor; match percentage

    We use the match path as track_id and match percentage (field 12, 0-based)
    as confidence normalized to 0-100.
    """
    matches: list[QueryMatch] = []
    for line in stdout.strip().splitlines():
        # Skip header lines or empty lines
        if not line.strip() or ";" not in line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 13:
            continue
        try:
            # Field 0 is index -- skip if it's a header (non-numeric)
            int(parts[0])
        except ValueError:
            continue
        try:
            track_id = parts[5]  # match path
            match_percentage = float(parts[12])  # match percentage
            confidence = min(100.0, max(0.0, match_percentage))
            matches.append(QueryMatch(track_id=track_id, confidence=round(confidence, 2)))
        except (ValueError, IndexError):
            logger.warning("Failed to parse match line: %s", line)
            continue
    return matches


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(status="healthy", engine="panako")


@app.post("/ingest", response_model=IngestResponse)
async def ingest(request: IngestRequest) -> IngestResponse:
    """Ingest a file into the Panako fingerprint database."""
    result = await asyncio.to_thread(_run_ingest, request.file_path)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return IngestResponse(status="ingested", file_path=request.file_path)


@app.post("/query", response_model=QueryResponse)
async def query(request: IngestRequest) -> QueryResponse:
    """Query the Panako database for matches."""
    result = await asyncio.to_thread(_run_query, request.file_path)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    matches = _parse_matches(result.stdout)
    return QueryResponse(matches=matches)
