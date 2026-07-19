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

from phaze.schemas.wire_bounds import INT32_MAX


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
    # window_index has no domain narrower than "an index into a windowed analysis run" -- it maps
    # to analysis_windows.window_index Integer (int4), so the fallback column bound applies
    # (wire_bounds rule 3, phaze-01gh).
    window_index: int = Field(ge=0, le=INT32_MAX)
    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(ge=0.0)
    # Fine-tier fields
    bpm: float | None = Field(default=None, ge=0.0)
    # -> analysis_windows.musical_key String(10), rule 1. Written as f"{key} {scale}" by
    # _analyze_fine_windows (services/analysis.py) from essentia's KeyExtractor(profileType="edma").
    # NOT a guess: the installed essentia-tensorflow's own `Key` algorithm docstring pins
    # `scale` to exactly {"major", "minor"} (never a multi-word MIREX-style name like "harmonic
    # minor"), and `es.KeyExtractor(profileType="edma")` run against synthetic C#-major and
    # A-minor triads on this checkout empirically returned key="C#"/scale="major" and
    # key="A"/scale="minor" -- confirming the longest real combined value ("C# minor"/"G# major")
    # is 8 chars. The column is tight but not too narrow for the data it actually receives
    # (phaze-ty0o investigation), so the wire is capped to match rather than the column widened.
    musical_key: str | None = Field(default=None, max_length=10)
    # Coarse-tier fields
    # -> analysis_windows.mood String(50), rule 1. derive_mood (services/analysis.py) returns one
    # of the fixed `_MOOD_SET_NAMES` values with the "mood_" prefix stripped -- all seven are
    # essentia's standard BINARY mood classifiers (mood_happy/mood_sad/etc., each a 3-variant
    # musicnn/vggish head per `_make_standard_set`), not the multi-word MIREX mood-cluster head
    # (which this codebase does not use anywhere in MODEL_SETS). Longest label is
    # "electronic"/"aggressive" at 10 chars, well inside the column.
    mood: str | None = Field(default=None, max_length=50)
    # -> analysis_windows.style String(50), rule 1. derive_style (services/analysis.py) returns the
    # top genre_discogs400 label from `GENRE_MODEL` (filename="discogs-effnet-bs64-1", the
    # discogs-effnet head -- NOT the newer discogs519 taxonomy, which would need re-checking if
    # ever swapped in), with "---" replaced by "/". The discogs400 class list's longest label is
    # "Folk, World, & Country---Canzone Napoletana" (44 raw chars); after the "---"->"/"
    # substitution (-2 chars) the stored value is 42 chars -- inside the column with an ~8-char
    # margin, not a wide one. A non-ASCII label ("Electronic---Musique Concrète") is also fine:
    # Postgres varchar(N) and Pydantic `max_length` both count characters, not encoded bytes, and
    # nothing in this write path measures `len()` on an encoded form.
    style: str | None = Field(default=None, max_length=50)
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    features: dict | None = None


class AnalysisWritePayload(BaseModel):
    """Audio analysis upsert body. All optional -- partial-PUT preserves unset fields."""

    model_config = ConfigDict(extra="forbid")  # D-26 -- strict body parsing

    bpm: float | None = Field(default=None, ge=0.0)
    # -> analysis_results.musical_key String(10), rule 1. Same essentia KeyExtractor provenance,
    # same empirically-confirmed 8-char max ("C# minor"/"G# major"), as AnalysisWindowPayload.musical_key
    # above -- the column is tight but sufficient for the data it actually receives (phaze-ty0o
    # investigation), so the wire is capped to match rather than the column widened.
    musical_key: str | None = Field(default=None, max_length=10)
    mood: dict[str, float] | None = None
    style: dict[str, float] | None = None
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
    # Phase 43 windowed-analysis coverage (the five-field contract analyze_file
    # returns). These land in dedicated `analysis` columns -- the router adds them
    # to `_ANALYSIS_COLUMN_FIELDS` so they do NOT funnel into `features` JSONB. All
    # optional so partial-PUT preserves unset coverage. Bounded by the SAME
    # realistic-windows-per-file domain as `windows` below (a 24h file at 30s
    # windows is ~2,880 fine windows; 50000 is generous) rather than the wider
    # int32 column fallback -- a count can never legitimately exceed the number of
    # windows a run could produce (wire_bounds rule 3, phaze-01gh).
    fine_windows_analyzed: int | None = Field(default=None, ge=0, le=50000)
    fine_windows_total: int | None = Field(default=None, ge=0, le=50000)
    coarse_windows_analyzed: int | None = Field(default=None, ge=0, le=50000)
    coarse_windows_total: int | None = Field(default=None, ge=0, le=50000)
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
    along in the body (AUTH-01, T-57.1-02 -> 422 at the route); the ``ge=0``/``le=50000``
    bound malformed counts at the wire boundary -- same realistic-windows-per-file domain
    as ``AnalysisWritePayload.fine_windows_analyzed``/``.fine_windows_total`` (wire_bounds
    rule 3, phaze-01gh): these land in the same ``analysis_results`` int4 counter columns,
    so an out-of-range value would otherwise raise Postgres ``NumericValueOutOfRange``
    unhandled, and here the abort happens BEFORE the ledger clear, re-queuing the file.

    Fine-only per Claude's Discretion (CONTEXT D-01): fine-only satisfies
    WORK-04; coarse counts are intentionally omitted (do NOT add coarse fields).
    """

    model_config = ConfigDict(extra="forbid")  # strict body parsing -- forged agent_id/file_id -> 422

    fine_windows_analyzed: int = Field(ge=0, le=50000)
    fine_windows_total: int = Field(ge=0, le=50000)


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


class PresignDownloadMetadata(BaseModel):
    """Optional display-metadata block threaded through ``PresignDownloadResponse`` (Phase 100, phaze-sfbx.1).

    The Postgres-less one-shot pod (``job_runner.py``) knows only ``PHAZE_JOB_FILE_ID`` and the
    presign response -- it has no human-readable identity to print in its startup banner
    (phaze-sfbx.3 consumes this block for that banner, OBS-02). Populated SERVER-side by the
    presign-download handler from the ``FileRecord`` + ``CloudJob`` rows it ALREADY loads for the
    existing readiness gating (design decision 1, epic phaze-sfbx), plus one narrow extra select
    against ``FileMetadata.duration`` (a separate 1:1-with-files table the handler otherwise
    never touches) -- neither of which changes the auth gating or the 404/409 readiness paths,
    which are both fully resolved before either query runs.

    Every field is individually optional so a partially-populated control-plane row degrades
    field-by-field instead of omitting the whole block: a ``CloudJob`` with no ``backend_id``
    stamped yet (Phase 68 D-06 shipped it with no backfill) yields ``backend_id=None``, and a
    file with no ``FileMetadata`` row yet (extraction is operator-triggered, MANUAL-META --
    Phase 35 D-06) yields ``duration_sec=None``. The pod tolerates unknown additive keys
    (``extra='ignore'``) for the same rolling-deploy forward-compat reason as the parent response.
    """

    model_config = ConfigDict(extra="ignore")

    # Human-readable filename (FileRecord.original_filename).
    original_filename: str | None = None
    # Source path/origin on the owning agent's filesystem (FileRecord.current_path).
    current_path: str | None = None
    # Source agent/fileserver identity that owns the file (FileRecord.agent_id).
    source_agent_id: str | None = None
    # Track/clip duration in seconds (FileMetadata.duration). None until metadata extraction has
    # run for this file (MANUAL-META, operator-triggered) or if no metadata row exists at all.
    duration_sec: float | None = None
    # File size in bytes (FileRecord.file_size).
    file_size: int | None = None
    # Registry bucket id the object was staged into (CloudJob.staging_bucket) -- the same value
    # the presign handler resolves via `s3_staging.resolve_bucket_config`.
    staging_bucket: str | None = None
    # Config-derived backend/cluster registry id stamped at dispatch (CloudJob.backend_id, Phase 68
    # D-06). NULLABLE with no backfill -- rows written before Phase 68 never got one.
    backend_id: str | None = None


class PresignDownloadResponse(BaseModel):
    """Presign-download response consumed by the DB-less one-shot pod (Phase 52, KJOB-02).

    The control plane mints a short-TTL presigned GET URL for a file's bytes and
    returns it alongside ``expected_sha256`` -- the ONLY hash a Postgres-free pod
    can integrity-verify the download against (Pitfall 3). ``expected_sha256`` is
    sourced server-side from ``FileRecord.sha256_hash``, exactly as
    ``ProcessFilePayload.expected_sha256`` is pinned in v5.0. ``expected_sha256`` is
    required so a response missing the integrity hash fails validation rather than
    silently disabling the verify step.

    NOTE: the SERVER side (POST /api/internal/agent/files/{file_id}/presign-download)
    ships in Phase 53 (KSTAGE-03). Phase 52 defines and unit-tests the CLIENT-consumed
    shape only.

    Forward-compat (cloud-analyze-empty-no-ext specialist review): this is a
    RESPONSE-only model the pod TRUSTS from the control plane -- NOT an attacker-facing
    request body -- so it must use ``extra='ignore'`` (Pydantic's default), NOT
    ``extra='forbid'``. During a rolling deploy the control plane may ship a NEWER
    schema first (e.g. an additive ``audio_ext`` key) while an OLDER Kueue Job pod
    still runs the previous image. With ``extra='forbid'`` that additive-but-backward-
    compatible field would raise ``ValidationError`` on ``model_validate`` -> the pod's
    presign call fails -> EXIT_DOWNLOAD (10, fail-fast, no retry), permanently failing
    every file for the rollout window. Tolerating unknown additive fields keeps old
    pods forward-compatible. (The request-body models KEEP ``extra='forbid'`` -- there
    it guards against smuggled ``agent_id``/``file_id``.)

    Phase 100 (phaze-sfbx.1): ``metadata`` is a further OPTIONAL, ADDITIVE block (see
    ``PresignDownloadMetadata``) carrying human-readable display identity for the pod's console
    banner (phaze-sfbx.3). It defaults to ``None`` so an older pod build that doesn't know the
    field simply never reads it, and a newer pod against an older control plane that never sends
    it degrades to a UUID-only banner rather than failing.
    """

    model_config = ConfigDict(extra="ignore")  # forward-compat: tolerate additive fields from a newer control plane (rollout skew)

    download_url: str
    # Constrain to a 64-char lowercase-hex sha256 digest. ``compute_sha256``
    # returns lowercase hex and ``FileRecord.sha256_hash`` is stored lowercase,
    # so a server-side format skew (uppercase/prefixed/short) fails validation
    # at the wire boundary rather than silently tripping every download as an
    # integrity mismatch (exit 11) with no diagnostic (IN-02).
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The file's real audio extension WITHOUT a leading dot (e.g. ``"mp3"``,
    # ``"m4a"``, ``"ogg"``), sourced server-side from ``FileRecord.file_type``.
    # The Postgres-less pod names its downloaded temp file ``<file_id>.<audio_ext>``
    # so essentia's EXTENSION-BASED format detection (``es.MetadataReader``) can
    # decode it. The staged S3 key carries no extension, so without this the pod
    # would fall back to ``.audio`` — undecodable by essentia → duration 0 → 0
    # windows → a silent EMPTY-but-"successful" analysis (cloud-analyze-empty-no-ext).
    # Optional (default None) so an older control plane that omits it degrades to
    # the URL-suffix fallback rather than failing response validation on rollout.
    audio_ext: str | None = Field(default=None, max_length=10)
    # Phase 100 (phaze-sfbx.1): optional display-identity block for the pod's console banner.
    # Absent (None) against an older control plane -- the pod (AgentClient.request_download_url)
    # must degrade to a UUID-only banner without error, never fail response validation.
    metadata: PresignDownloadMetadata | None = Field(default=None)
