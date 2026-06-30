"""Pydantic schemas for PUT /api/internal/agent/analysis/{file_id} (Phase 26 D-26).

Per D-26: idempotent upsert on AnalysisResult.file_id (unique constraint).
All fields optional so partial-PUT semantics (field-level last-write-wins,
mirroring Phase 25 CR-01 fix in agent_metadata.py) preserve unset columns.

NOTE on column types: the AnalysisResult model (src/phaze/models/analysis.py)
currently stores `mood: String(50)` and `style: String(50)`. D-26 specifies
the *wire* type as `dict[str, float]` -- the router will serialize to a
JSON string for storage (or the executor may opt to migrate the column to
JSONB during Plan 06; that's a discretion area documented in Plan 06).
The wire type here matches CONTEXT.md D-26 exactly; storage representation
is the router's concern.
"""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field


class AnalysisWindowPayload(BaseModel):
    """One per-window time-series row (Phase 31, ANL-01).

    Two tiers share this shape: fine-tier windows populate ``bpm``/``musical_key``;
    coarse-tier windows populate ``mood``/``style``/``danceability``/``features``.
    All analysis columns are optional so either tier omits the other tier's fields.
    ``tier`` is a ``Literal`` (V5 input-validation control) and the numeric ``ge``
    guards bound malformed payloads at the wire boundary.
    """

    model_config = ConfigDict(extra="forbid")  # strict body parsing

    tier: Literal["fine", "coarse"]
    window_index: int = Field(ge=0)
    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(ge=0.0)
    # Fine-tier fields
    bpm: float | None = Field(default=None, ge=0.0)
    musical_key: str | None = None
    # Coarse-tier fields
    mood: str | None = None
    style: str | None = None
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    features: dict | None = None


class AnalysisWritePayload(BaseModel):
    """Audio analysis upsert body. All optional -- partial-PUT preserves unset fields."""

    model_config = ConfigDict(extra="forbid")  # D-26 -- strict body parsing

    bpm: float | None = Field(default=None, ge=0.0)
    musical_key: str | None = None
    mood: dict[str, float] | None = None
    style: dict[str, float] | None = None
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
    # Phase 43 windowed-analysis coverage (the five-field contract analyze_file
    # returns). These land in dedicated `analysis` columns -- the router adds them
    # to `_ANALYSIS_COLUMN_FIELDS` so they do NOT funnel into `features` JSONB. All
    # optional so partial-PUT preserves unset coverage; counts are `ge=0`.
    fine_windows_analyzed: int | None = Field(default=None, ge=0)
    fine_windows_total: int | None = Field(default=None, ge=0)
    coarse_windows_analyzed: int | None = Field(default=None, ge=0)
    coarse_windows_total: int | None = Field(default=None, ge=0)
    sampled: bool | None = None
    # Per-window time-series (Phase 31). `| None` default preserves the partial-PUT
    # contract (router only replaces windows when this is not None); `max_length`
    # bounds the DoS-via-huge-bulk-insert threat (a 24h file at 30s windows is
    # ~2,880 fine windows, so 50000 is generous).
    windows: list[AnalysisWindowPayload] | None = Field(default=None, max_length=50000)


class AnalysisWriteResponse(BaseModel):
    """Minimal echo response confirming the upsert (D-26 success body)."""

    agent_id: str
    file_id: uuid.UUID


class AnalysisProgressPayload(BaseModel):
    """Counter-only mid-flight progress body (Phase 57.1 D-01/D-02).

    Carries ONLY the two fine-window counts that advance during an in-flight
    analysis run. Unlike ``AnalysisWritePayload`` the counts are REQUIRED (no
    ``| None``, no default) -- a progress POST always carries both (the START
    call sends ``analyzed=0, total=N``; bumps send ``analyzed=k, total=N``).
    ``extra='forbid'`` rejects any attempt to ride an ``agent_id``/``file_id``
    along in the body (AUTH-01, T-57.1-02 -> 422 at the route); the ``ge=0``
    guards bound malformed counts at the wire boundary.

    Fine-only per Claude's Discretion (CONTEXT D-01): fine-only satisfies
    WORK-04; coarse counts are intentionally omitted (do NOT add coarse fields).
    """

    model_config = ConfigDict(extra="forbid")  # strict body parsing -- forged agent_id/file_id -> 422

    fine_windows_analyzed: int = Field(ge=0)
    fine_windows_total: int = Field(ge=0)


class AnalysisProgressResponse(BaseModel):
    """Minimal echo confirming the counter-only progress upsert (Phase 57.1).

    Mirrors ``AnalysisWriteResponse`` verbatim: ``agent_id`` comes from the auth
    dep (NEVER the body) and ``file_id`` is the PATH value.
    """

    agent_id: str
    file_id: uuid.UUID


class AnalysisFailurePayload(BaseModel):
    """Terminal analysis-failure report body (Phase 43).

    The Postgres-free worker POSTs this to ``/analysis/{file_id}/failed`` when
    windowed analysis terminally fails (timeout / crash / error). ``reason`` is a
    ``Literal`` so the wire can only carry the three classifications; ``error`` is
    a bounded free-text detail string (``max_length`` caps the DoS-via-huge-string
    threat, T-43-06). ``extra='forbid'`` rejects any attempt to smuggle an
    ``agent_id``/``file_id`` in the body (AUTH-01)."""

    model_config = ConfigDict(extra="forbid")

    reason: Literal["timeout", "crashed", "error"]
    error: str | None = Field(default=None, max_length=2000)


class AnalysisFailureResponse(BaseModel):
    """Minimal echo confirming the terminal-failure report (Phase 43)."""

    agent_id: str
    file_id: uuid.UUID


class PresignDownloadResponse(BaseModel):
    """Presign-download response consumed by the DB-less one-shot pod (Phase 52, KJOB-02).

    The control plane mints a short-TTL presigned GET URL for a file's bytes and
    returns it alongside ``expected_sha256`` -- the ONLY hash a Postgres-free pod
    can integrity-verify the download against (Pitfall 3). ``expected_sha256`` is
    sourced server-side from ``FileRecord.sha256_hash``, exactly as
    ``ProcessFilePayload.expected_sha256`` is pinned in v5.0. The field is required
    (``extra='forbid'``) so a response missing the integrity hash fails validation
    rather than silently disabling the verify step.

    NOTE: the SERVER side (POST /api/internal/agent/files/{file_id}/presign-download)
    ships in Phase 53 (KSTAGE-03). Phase 52 defines and unit-tests the CLIENT-consumed
    shape only.
    """

    model_config = ConfigDict(extra="forbid")  # strict body parsing

    download_url: str
    # Constrain to a 64-char lowercase-hex sha256 digest. ``compute_sha256``
    # returns lowercase hex and ``FileRecord.sha256_hash`` is stored lowercase,
    # so a server-side format skew (uppercase/prefixed/short) fails validation
    # at the wire boundary rather than silently tripping every download as an
    # integrity mismatch (exit 11) with no diagnostic (IN-02).
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
