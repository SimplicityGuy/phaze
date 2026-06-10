# Phase 31: Windowed Time-Series Audio Analysis - Context

**Gathered:** 2026-06-10
**Status:** Ready for planning
**Source:** Design spec (docs/superpowers/specs/2026-06-10-windowed-analysis-design.md) — locked brainstorming decisions

<domain>
## Phase Boundary

Rewrite `analyze_file()` to stream-decode each audio file once and analyze it per-window, producing a two-tier time-series instead of one whole-file scalar per characteristic. This simultaneously fixes:

1. The `RhythmExtractor2013(multifeature)` `OnsetDetectionGlobal` buffer-overflow crash on long files (zero of 11,428 files currently complete analysis).
2. The latent whole-file OOM (`MonoLoader` decodes the entire file — ~1.3 GB for 2h, far worse for the 11 GB outliers, ×8 concurrent jobs).

Deliver: per-window analysis, a new queryable `analysis_window` child table, representative aggregates retained on the existing `analysis` row (so proposals/search/sort are unaffected), wire-schema + API extension with idempotent window replacement, and a compact-expandable review-UI timeline (SVG/CSS, no charting lib). Ships as v4.0.10.

**In scope:** streaming single-pass decode, two-tier windowing, aggregate derivation, `analysis_window` model + additive Alembic migration, `AnalysisWritePayload.windows`, idempotent `put_analysis`, `process_file` payload build, AgentSettings for window config, job timeout/retries tuning, review-UI sparkline + expandable timeline fragment.

**Out of scope:** per-window energy characteristic (no essentia model), beat-synced/overlapping windows, re-analysis automation (existing "Run analysis" handles re-enqueue).
</domain>

<decisions>
## Implementation Decisions (LOCKED — from brainstorming)

### Scope & Granularity
- Everything becomes a time-series via a **two-tier** scheme.
- **Fine tier** (BPM, key): 30 s windows. **Coarse tier** (mood, style, danceability): 180 s (3 min) windows.
- Window sizes are **fixed-duration** (constant resolution regardless of file length) and **configurable**.
- Trailing partial window analyzed only if it holds ≥ `analysis_fine_min_sec` (default 15 s) of audio, else dropped.
- A normal 4-min track degrades gracefully (~8 fine windows, 1–2 coarse windows).

### Processing — single-pass streaming decode (the crash/OOM fix)
- Replace the two whole-file `MonoLoader()` calls with **streaming, single-pass, per-window** analysis. No essentia algorithm ever receives more than one window of audio.
- **Fine pass** (44.1 kHz mono): stream-decode → accumulate 30 s buffer → run `RhythmExtractor2013(multifeature)` + `KeyExtractor` on the buffer → fine window `(start_sec, end_sec, bpm, musical_key)`; clear; continue.
- **Coarse pass** (16 kHz mono): stream-decode → accumulate 180 s buffer → run the existing 34 TF model sets → coarse window `(start_sec, end_sec, mood, style, danceability, features)`.
- Stays CPU-bound in the existing `ProcessPoolExecutor` (`run_in_process_pool`).
- Per-window failures are **isolated**: a window that raises is logged and skipped, never failing the whole file.

### Spike-first (MANDATORY)
- **The implementation plan MUST begin with a spike** validating, on a real ≥2 h file: (a) streaming `MonoLoader` frame accumulation works; (b) `RhythmExtractor2013` succeeds on a 30 s buffer; (c) memory stays bounded; (d) total TF inference time over coarse windows is acceptable.
- **Fallback** if streaming accumulation proves impractical: segmented `EasyLoader(startTime, endTime)` decoding (simpler, but re-decode cost grows with file length — measure before choosing).

### Aggregate derivation (keeps existing consumers working)
- `bpm` = **median** of fine-window BPMs.
- `musical_key` = **modal** key across fine windows (duration-weighted).
- `mood` / `style` = **dominant** label across coarse windows (time-weighted), serialized to existing `String(50)` summary form.
- `danceability` = **mean** across coarse windows.

### Data model — new `analysis_window` table (`AnalysisWindow`, `TimestampMixin`)
- `id` UUID PK; `file_id` UUID FK→`files.id` (indexed, `ON DELETE CASCADE`); `tier` String (`'fine'`|`'coarse'`); `window_index` int; `start_sec`/`end_sec` Float.
- Fine-only: `bpm` Float|None, `musical_key` String(10)|None.
- Coarse-only: `mood` String(50)|None, `style` String(50)|None, `danceability` Float|None, `features` JSONB|None (full per-window distributions for detail view).
- Indexes: `(file_id, tier, window_index)`; partial index on `bpm WHERE tier='fine'`; partial index on `danceability WHERE tier='coarse'`; indexes on `mood`, `style`.
- `analysis` (existing, 1:1) is structurally unchanged — holds aggregates.
- **No data migration** — table is currently empty (0 rows). Migration is purely additive (new table + indexes) via Alembic.

### Wire schema & API
- `AnalysisWritePayload` gains `windows: list[AnalysisWindowPayload] | None`. `AnalysisWindowPayload` carries `tier, window_index, start_sec, end_sec` + tier-specific fields. Existing aggregate fields stay (partial-PUT semantics preserved).
- `PUT /api/internal/agent/analysis/{file_id}` (`agent_analysis.py`): upserts the `analysis` aggregate row as today AND **replaces** the file's `analysis_window` rows (delete-by-`file_id` then bulk insert) so PUT stays idempotent.
- `process_file` (`tasks/functions.py`) builds the windows list from the new `analyze_file` return shape and sends it.

### Job config
- Per-file `timeout`: generous bound or `0`/unbounded (consistent with prior bulk-scan timeout decision) — finalize in planning.
- `retries`: lower to 1–2 (window-isolated failure shouldn't trigger 4 full re-analyses).
- New `AgentSettings`: `analysis_fine_window_sec=30`, `analysis_coarse_window_sec=180`, `analysis_fine_min_sec=15`.

### UI
- **Review list row:** existing aggregates + a small server-rendered SVG BPM sparkline + an expand control.
- **Expanded (HTMX-loaded fragment):** multi-lane timeline on a shared time axis — BPM `<polyline>`, then key/mood/style ribbons as flexed colored `<div>` bands proportional to window duration. All SVG/CSS; **no JS charting dependency**. New endpoint returns the fragment for a file's windows.

### Claude's Discretion
- Exact streaming-vs-`EasyLoader` choice (decided by spike measurement).
- Final `timeout`/`retries` values within the stated bounds.
- Internal helper structure for aggregate reductions and window accumulation.
- Sparkline/timeline SVG markup details and color mapping.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Design contract
- `docs/superpowers/specs/2026-06-10-windowed-analysis-design.md` — full design spec: problem, locked decisions, architecture, data model, wire schema, testing, rollout. **Authoritative source for this phase.**

### Code to modify (source of truth for current patterns)
- `src/phaze/services/analysis.py` — `analyze_file()` being rewritten; current whole-file MonoLoader + RhythmExtractor2013 + 34 TF model usage.
- `src/phaze/models/analysis.py` — existing `Analysis` 1:1 model; add `AnalysisWindow` here.
- `src/phaze/schemas/agent_analysis.py` — `AnalysisWritePayload` wire types.
- `src/phaze/routers/agent_analysis.py` — `put_analysis` PUT handler.
- `src/phaze/tasks/functions.py` — `process_file` job (payload build + PUT).
- AgentSettings / config module — add window-size settings.
- Alembic migrations dir — additive migration.
- Review-UI templates + router — sparkline row + expandable timeline fragment endpoint.
</canonical_refs>

<specifics>
## Specific Ideas

- Median/modal/dominant/mean reductions are the exact aggregate functions — not interchangeable.
- The `features` JSONB column preserves full per-window distributions for the expanded detail view (coarse only).
- Idempotency proof: re-PUT must replace child rows, not duplicate them (delete-by-file_id then bulk insert).
- Timeline ribbons are width-proportional to window duration (flexed colored divs), BPM is a polyline.
</specifics>

<deferred>
## Deferred Ideas

- Energy as a distinct per-window characteristic (no current essentia model; aggregate `energy` field left as-is).
- Beat-synced or overlapping windows (using fixed non-overlapping windows).
- Re-analysis of already-analyzed files (archive is all `discovered`; re-run via existing "Run analysis" after the fix ships).
</deferred>

---

*Phase: 31-windowed-time-series-audio-analysis*
*Context gathered: 2026-06-10 from design spec*
