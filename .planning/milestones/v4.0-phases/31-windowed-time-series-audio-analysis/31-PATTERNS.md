# Phase 31: Windowed Time-Series Audio Analysis - Pattern Map

**Mapped:** 2026-06-10
**Files analyzed:** 8 (3 new, 5 modified/rewritten)
**Analogs found:** 7 / 8 (the timeline SVG fragment is genuinely new — no charting analog exists)

> RESEARCH.md already names every integration anchor. This file adds the **concrete code excerpts** the planner copies patterns from. Where RESEARCH and this file overlap, RESEARCH is the rationale and this is the literal copy-source.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/models/analysis.py` (add `AnalysisWindow`) | model | CRUD | `AnalysisResult` (same file, l.12-25) + `fingerprint_results` child table | exact (same file, same base) |
| `alembic/versions/018_*.py` (NEW) | migration | batch/DDL | `007_add_fingerprint_results_table.py` (child table) + `012` l.104-110 (partial index) | exact (composed of two existing patterns) |
| `src/phaze/schemas/agent_analysis.py` (add `AnalysisWindowPayload`) | schema | transform | `AnalysisWritePayload` (same file, l.21-31) | exact (same file) |
| `src/phaze/routers/agent_analysis.py::put_analysis` | router | CRUD (upsert + replace-children) | `put_analysis` itself (same file) + `012` delete-then-insert idiom | exact (extend in place) |
| `src/phaze/services/analysis.py::analyze_file` (REWRITE) | service | streaming/transform | `analyze_file` itself + `_classifier_cache`/`_predict_single` (same file) | exact (rewrite in place, keep helpers) |
| `src/phaze/tasks/functions.py::process_file` | task | request-response | `process_file` itself + `_features_to_*_dict` (same file) | exact (extend in place) |
| `src/phaze/config.py::AgentSettings` (3 new fields) | config | — | `AgentSettings` `scan_chunk_size`/`watcher_*` fields (l.355-384) | exact (same class) |
| Review-UI sparkline row + HTMX timeline fragment + endpoint (NEW) | router + template | request-response (SSR fragment) | `proposals.py::row_detail` (l.147-161) + `proposal_row.html` l.74-84 + `row_detail.html` | role-match (HTMX expand); SVG render = no analog |

---

## Pattern Assignments

### `src/phaze/models/analysis.py` — add `AnalysisWindow` (model, CRUD)

**Analog:** `AnalysisResult` in the SAME file + child-table FK from `fingerprint_results`.

**Existing model + imports to extend** (`models/analysis.py:1-25`):
```python
import uuid
from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from phaze.models.base import Base, TimestampMixin

class AnalysisResult(TimestampMixin, Base):
    __tablename__ = "analysis"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id"), unique=True, nullable=False)
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    musical_key: Mapped[str | None] = mapped_column(String(10), nullable=True)
    mood: Mapped[str | None] = mapped_column(String(50), nullable=True)
    style: Mapped[str | None] = mapped_column(String(50), nullable=True)
    features: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
```

**Copy-this for the new model** — same `TimestampMixin, Base` order; `default=uuid.uuid4` PK; reuse `Float`/`String`/`JSONB`. The FK differs in two ways from `AnalysisResult`: it is **NOT** `unique` (1:many) and it carries **`ondelete="CASCADE"`** (locked decision). The existing `AnalysisResult.file_id` shows the `ForeignKey("files.id")` target; add `ondelete="CASCADE"` to it:
```python
file_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), index=True, nullable=False,
)
```
> `TimestampMixin` (`models/base.py:24-28`) auto-adds `created_at`/`updated_at` — do NOT redeclare them. `Base.metadata` carries the naming convention (`models/base.py:9-21`) so index/FK names are auto-generated.

---

### `alembic/versions/018_*.py` — additive create-table + indexes (migration, DDL)

**Analog A — child table create** (`007_add_fingerprint_results_table.py:23-43`):
```python
def upgrade() -> None:
    op.create_table(
        "fingerprint_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engine", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fingerprint_results")),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_fingerprint_results_file_id_files")),
    )
    op.create_index("ix_fprint_file_engine", "fingerprint_results", ["file_id", "engine"], unique=True)
```
> Note `op.f(...)` wraps constraint names so they match `Base`'s naming convention. For the CASCADE FK use `sa.ForeignKeyConstraint([...], ["files.id"], ..., ondelete="CASCADE")`.

**Analog B — partial index** (`012_add_agents_table_and_backfill.py:104-110`) — this is the exact `postgresql_where` idiom for the locked `bpm WHERE tier='fine'` / `danceability WHERE tier='coarse'` partial indexes:
```python
op.create_index(
    "uq_scan_batches_agent_id_live",
    "scan_batches",
    ["agent_id"],
    unique=True,
    postgresql_where=sa.text("status = 'live'"),
)
```
Apply as (non-unique):
```python
op.create_index("ix_analysis_window_bpm_fine", "analysis_window", ["bpm"], postgresql_where=sa.text("tier = 'fine'"))
op.create_index("ix_analysis_window_dance_coarse", "analysis_window", ["danceability"], postgresql_where=sa.text("tier = 'coarse'"))
```

**Revision header** (`017_add_scan_batches_last_progress_at.py:26-37`) — copy this exact shape; set `revision = "018"`, `down_revision = "017"`:
```python
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: str | Sequence[str] | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```
> RESEARCH.md cites `down_revision="017_add_scan_batches_last_progress_at"` but the actual `revision`/`down_revision` strings in this repo are **bare numbers** (`"017"`, `"016"`). Use `"017"`. `downgrade()` drops indexes then the table (mirror `007:40-43`).

---

### `src/phaze/schemas/agent_analysis.py` — add `AnalysisWindowPayload`, extend `AnalysisWritePayload` (schema, transform)

**Analog:** `AnalysisWritePayload` in the SAME file (`schemas/agent_analysis.py:21-31`):
```python
from pydantic import BaseModel, ConfigDict, Field

class AnalysisWritePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")  # D-26 -- strict body parsing
    bpm: float | None = Field(default=None, ge=0.0)
    musical_key: str | None = None
    mood: dict[str, float] | None = None
    style: dict[str, float] | None = None
    danceability: float | None = Field(default=None, ge=0.0, le=1.0)
    energy: float | None = Field(default=None, ge=0.0, le=1.0)
```
**Copy patterns:** keep `model_config = ConfigDict(extra="forbid")` on the new `AnalysisWindowPayload`; keep `Field(default=None, ge=...)` numeric guards (apply `ge=0.0` to `start_sec`/`end_sec`, `ge=0` to `window_index`). Constrain `tier` to `Literal["fine", "coarse"]` (V5 input-validation control in RESEARCH). Add `windows: list[AnalysisWindowPayload] | None = None` to `AnalysisWritePayload` — its `| None` default preserves the partial-PUT contract (router guards on `is not None`). Per RESEARCH security: bound the list length (e.g. `Field(default=None, max_length=50000)`).

---

### `src/phaze/routers/agent_analysis.py::put_analysis` — upsert aggregate + replace children (router, CRUD)

**Analog:** `put_analysis` itself. The existing upsert is the aggregate path; **append** the child-replace after it, in the same session, before `commit()`.

**Existing upsert (keep as-is)** (`routers/agent_analysis.py:117-132`):
```python
payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
stmt = pg_insert(AnalysisResult).values([payload])
if dumped:
    stmt = stmt.on_conflict_do_update(index_elements=["file_id"], set_={k: stmt.excluded[k] for k in dumped})
else:
    stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])
await session.execute(stmt)
await session.commit()
```
> `pg_insert` bypasses the Python-only `default=uuid.uuid4` PK, so the existing code **stamps `id` explicitly** (`l.117`). Do the same for each child row (`"id": uuid.uuid4()` per dict). The `_summarize_dict_to_string` + `_ANALYSIS_COLUMN_FIELDS` overflow funnel (`l.51-113`) for aggregates stays untouched.

**Child-replace to insert before `commit()`** — delete-then-bulk-insert, guarded on `is not None` (RESEARCH "Idempotent child-row replace", l.306-323). The delete idiom mirrors `012`'s `op.execute(... DELETE ...)` pattern but in app-session form:
```python
from sqlalchemy import delete
from phaze.models.analysis import AnalysisWindow

if body.windows is not None:                       # partial-PUT guard: omit windows => don't touch them
    await session.execute(delete(AnalysisWindow).where(AnalysisWindow.file_id == file_id))
    if body.windows:
        await session.execute(
            pg_insert(AnalysisWindow).values(
                [{"id": uuid.uuid4(), "file_id": file_id, **w.model_dump()} for w in body.windows]
            )
        )
await session.commit()                             # single transaction: aggregate upsert + child replace
```
> Auth/guard pattern is unchanged and already correct (`l.67-73`): `agent: Annotated[Agent, Depends(get_authenticated_agent)]`, `session: Annotated[AsyncSession, Depends(get_session)]`. `agent_id` never comes from the body (AUTH-01).

---

### `src/phaze/services/analysis.py::analyze_file` — segmented per-window decode (service, streaming)

**Analog:** `analyze_file` itself (REWRITE in place) — **keep** the entire model registry (`MODEL_SETS`, `GENRE_MODEL`), the `_classifier_cache`/`_labels_cache` module caches, `_get_classifier`/`_get_labels`/`_predict_single`, `_suppress_essentia_logging`, `derive_mood`, `derive_style`. Only the body of `analyze_file` changes from whole-file to per-window.

**Whole-file code being REPLACED** (`services/analysis.py:230-276`) — the two `MonoLoader` calls are the crash/OOM source:
```python
audio_44k = es.MonoLoader(filename=file_path, sampleRate=44100)()          # <-- REMOVE (whole-file OOM)
rhythm = es.RhythmExtractor2013(method="multifeature")
bpm, _beats, _beats_confidence, _, _beats_intervals = rhythm(audio_44k)     # <-- crash on long files
key, scale, _strength = es.KeyExtractor(profileType="edma")(audio_44k)
audio_16k = es.MonoLoader(filename=file_path, sampleRate=16000)()           # <-- REMOVE (whole-file OOM)
for model_set in MODEL_SETS:
    for model in model_set.models:
        predictions = _predict_single(audio_16k, model, models_dir)         # <-- KEEP per-window: feed buf180
        ...
```

**Per-window replacement** — the loop primitive (RESEARCH Pattern 1, validated) replaces each `MonoLoader` with segmented `EasyLoader`:
```python
buf = es.EasyLoader(filename=file_path, sampleRate=44100, startTime=start, endTime=end)()
# fine:  es.RhythmExtractor2013(method="multifeature")(buf) ; es.KeyExtractor(profileType="edma")(buf)
# coarse: same MODEL_SETS loop, but classifier(buf180) instead of classifier(audio_16k)
```

**Preserve the classifier-cache call shape** (`services/analysis.py:149-153`) — per-window cost stays inference-only (RESEARCH Pitfall 5):
```python
def _predict_single(audio_16k: Any, model: ModelConfig, models_dir: str) -> Any:
    classifier = _get_classifier(model, models_dir)   # cached by model.filename across calls in the worker
    activations = classifier(audio_16k)
    return np.mean(activations, axis=0)               # per-window mean still valid on a 180s buffer
```

**Per-window failure isolation** (RESEARCH Pattern 2) — wrap each window's essentia calls in `try/except Exception` + log + `continue`; never fail the file. `# noqa: BLE001` is the project's accepted marker for deliberate broad-except (isolation is the requirement).

**Aggregate reductions** — pure-Python helpers (RESEARCH "Code Examples", l.275-304): `median` of fine BPMs, duration-weighted `Counter` mode for key, time-weighted dominant for mood/style, `mean` for danceability. These need **no essentia** and are the cheapest high-value unit tests.

**Return shape** stays a `dict[str, Any]` (current return `l.270-276`) but adds `"windows": [...]`:
```python
return {"bpm": ..., "musical_key": ..., "mood": ..., "style": ..., "features": ..., "windows": [...]}
```
> Determine `total_sec` via `es.MetadataReader`/`es.AudioLoader` or `ffprobe` — **never** `MonoLoader()` (materializes full PCM). The function stays **synchronous** (runs in `run_in_process_pool`). Module-level `os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"` before essentia import (`l.15-18`) stays.

---

### `src/phaze/tasks/functions.py::process_file` — build windows payload, PUT (task, request-response)

**Analog:** `process_file` itself (`tasks/functions.py:114-148`). Extend the existing PUT body build.

**Existing payload build to extend** (`tasks/functions.py:132-147`):
```python
features = analysis.get("features", {}) if isinstance(analysis, dict) else {}
mood_dict = _features_to_mood_dict(features) if isinstance(features, dict) else None
style_dict = _features_to_style_dict(features) if isinstance(features, dict) else None
await api.put_analysis(
    payload.file_id,
    AnalysisWritePayload(
        bpm=analysis.get("bpm"),
        musical_key=analysis.get("musical_key"),
        mood=mood_dict,
        style=style_dict,
        danceability=analysis.get("danceability"),
        energy=analysis.get("energy"),
    ),
)
```
**Copy patterns:** keep the `_features_to_mood_dict`/`_features_to_style_dict` aggregate→wire-dict conversion (`l.62-111`) for the aggregate fields. Add a `windows=[AnalysisWindowPayload(**w) for w in analysis.get("windows", [])]` mapping from the new `analyze_file` return shape. The deferred-import guard (`_load_analyze_file`, `l.38-44`) and the **"MUST NOT import phaze.database / phaze.models / sqlalchemy"** constraint (module docstring, enforced by `tests/test_task_split.py`) **must be preserved** — build windows from the plain dict, not ORM objects.

---

### `src/phaze/config.py::AgentSettings` — 3 new window-config fields (config)

**Analog:** the integer `AgentSettings` fields with `AliasChoices` (`config.py:355-384`):
```python
watcher_settle_seconds: int = Field(
    default=10,
    validation_alias=AliasChoices("PHAZE_WATCHER_SETTLE_SECONDS", "watcher_settle_seconds"),
    description="Seconds a file's mtime must be stable before the watcher posts it (D-01).",
)
scan_chunk_size: int = Field(
    default=500,
    validation_alias=AliasChoices("PHAZE_SCAN_CHUNK_SIZE", "scan_chunk_size"),
    description="Number of FileUpsertRecord rows per chunk in scan_directory (D-11).",
)
```
**Copy verbatim shape** for the three new fields (defaults from CONTEXT): `analysis_fine_window_sec: int = Field(default=30, validation_alias=AliasChoices("PHAZE_ANALYSIS_FINE_WINDOW_SEC", "analysis_fine_window_sec"), ...)`, likewise `analysis_coarse_window_sec=180`, `analysis_fine_min_sec=15`. The agent worker reads these (window sizing is an agent-role concern).

> **Job timeout/retries (separate from the 3 new fields):** the knobs already exist on `BaseSettings` — `worker_job_timeout: int = 600`, `worker_max_retries: int = 4` (`config.py:194-195`). They are applied per-Job by the `apply_project_job_defaults` `before_enqueue` hook (`tasks/_shared/queue_defaults.py:62-86`), NOT in `Worker.__init__`. To lower retries, change `worker_max_retries` (or set `timeout=0` per RESEARCH Open Q1); the hook's `if job.retries == _SAQ_DEFAULT_RETRIES` guard (`queue_defaults.py:81-82`) means it only fills defaults — explicit enqueue overrides are untouched.

---

### Review-UI sparkline row + HTMX timeline fragment + endpoint (router + template)

**Analog (HTMX expand mechanism):** `proposals.py::row_detail` + `proposal_row.html` + `row_detail.html`. This is an **exact** structural analog for "compact row + expand-on-demand fragment"; only the rendered content (SVG timeline) is new.

**Endpoint analog** (`routers/proposals.py:147-161`):
```python
@router.get("/{proposal_id}/detail", response_class=HTMLResponse)
async def row_detail(
    request: Request,
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    proposal = await get_proposal_with_file(session, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return templates.TemplateResponse(
        request=request,
        name="proposals/partials/row_detail.html",
        context={"request": request, "proposal": proposal},
    )
```
> Router setup pattern (`proposals.py:23-25`): `TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"`; `templates = Jinja2Templates(directory=str(TEMPLATES_DIR))`. The HTMX-vs-full-page split (`l.69-72`, `if request.headers.get("HX-Request") == "true"`) is the convention if the fragment is also reachable as a page. New endpoint must sit behind the same admin auth as the rest of the review UI and scope strictly by `file_id` (RESEARCH V4 control).

**HTMX expand trigger analog** (`proposal_row.html:74-84`) — the new sparkline row's expand control copies this exactly (lazy `hx-get` into a hidden sibling row):
```html
<button hx-get="/proposals/{{ proposal.id }}/detail"
        hx-target="#detail-{{ proposal.id }}"
        hx-swap="innerHTML"
        class="..." aria-label="Expand details for {{ proposal.proposed_filename }}">
    Details
</button>
...
<tr id="detail-{{ proposal.id }}" class="hidden"></tr>
```
**Fragment-reveal JS analog** (`row_detail.html:30-40`) — the fragment un-hides its parent `<tr>` after load; copy this self-contained script if using the hidden-sibling-row pattern.

**SVG/CSS timeline — NO direct analog.** No charting/`<polyline>` timeline exists in the codebase (only icon SVGs in `base.html`, `pipeline/partials/recent_scans_table.html`). Build from scratch per the locked design: BPM `<polyline>` with numeric-only geometry attributes; key/mood/style as flexed colored `<div>` ribbons width-proportional to `(end_sec - start_sec)`. **Security (RESEARCH V5):** rely on Jinja2 autoescaping for all essentia-derived label text (mood/style/key) and keep SVG coordinate attributes numeric-only to prevent SVG/HTML injection.

---

## Shared Patterns

### Idempotent upsert via `pg_insert` + explicit PK stamp
**Source:** `routers/agent_analysis.py:117-130` and `routers/agent_metadata.py` (the canonical mirror).
**Apply to:** the aggregate upsert (unchanged) and the new child bulk-insert.
```python
from sqlalchemy.dialects.postgresql import insert as pg_insert
payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}   # stamp PK: pg_insert bypasses Python default
stmt = pg_insert(Model).values([payload])
stmt = stmt.on_conflict_do_update(index_elements=["file_id"], set_={k: stmt.excluded[k] for k in dumped})
```

### Partial index declaration
**Source:** `alembic/versions/012_add_agents_table_and_backfill.py:104-110`.
**Apply to:** migration 018's `bpm WHERE tier='fine'` and `danceability WHERE tier='coarse'` indexes.
```python
op.create_index("ix_name", "table", ["col"], postgresql_where=sa.text("tier = 'fine'"))
```

### TimestampMixin + naming-convention base
**Source:** `models/base.py:9-28`.
**Apply to:** `AnalysisWindow` — inherit `(TimestampMixin, Base)`, never redeclare `created_at`/`updated_at`; constraint/index names auto-generate from `convention`.

### Per-Job SAQ defaults via before_enqueue hook
**Source:** `tasks/_shared/queue_defaults.py:62-86` + `config.py:194-195`.
**Apply to:** lowering `process_file` retries / timeout policy. Change `worker_max_retries`/`worker_job_timeout`; the hook fills only jobs still at the SAQ default, leaving explicit enqueue overrides intact.

### essentia classifier cache (process-pool worker memoization)
**Source:** `services/analysis.py:95-96, 114-132`.
**Apply to:** the rewritten coarse pass — keep `_classifier_cache`/`_get_classifier` so per-window cost is inference-only, never graph-reload.

### HTMX lazy-expand fragment
**Source:** `routers/proposals.py:147-161` + `templates/proposals/partials/proposal_row.html:74-84` + `row_detail.html:30-40`.
**Apply to:** the review-UI timeline expand endpoint + sparkline row.

---

## No Analog Found

| File / Unit | Role | Data Flow | Reason |
|-------------|------|-----------|--------|
| Timeline SVG `<polyline>` + ribbon rendering (inside the new fragment template) | template | SSR render | No existing server-rendered chart/sparkline. Only icon SVGs exist (`base.html`, `pipeline/partials/recent_scans_table.html`). Build from the locked design spec (RESEARCH UI section) — autoescaped labels, numeric-only geometry. The *expand mechanism* has an analog (above); the *chart markup* does not. |

> All other units extend or rewrite an existing file with a strong same-file or same-pattern analog. The streaming `EasyLoader` window-loop primitive itself is new code but is fully specified and verified in RESEARCH Pattern 1 (l.166-199) — treat that as the copy-source, not a codebase analog.

## Metadata

**Analog search scope:** `src/phaze/models/`, `src/phaze/schemas/`, `src/phaze/routers/`, `src/phaze/services/`, `src/phaze/tasks/` (+ `_shared/`), `src/phaze/config.py`, `alembic/versions/`, `src/phaze/templates/`.
**Files scanned:** ~18 (8 read in full: models/analysis, schemas/agent_analysis, routers/agent_analysis, services/analysis, tasks/functions, models/base, config AgentSettings, queue_defaults; 4 migrations; proposals router + 3 templates).
**Pattern extraction date:** 2026-06-10
</content>
</invoke>
