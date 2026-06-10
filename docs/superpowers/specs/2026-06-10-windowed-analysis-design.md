# Windowed Time-Series Audio Analysis — Design Spec

**Date:** 2026-06-10
**Status:** Draft for review
**Author:** brainstormed with Claude

## Problem

`analyze_file()` (`src/phaze/services/analysis.py`) loads an entire audio file into memory and runs whole-file BPM/key/mood/style detection, producing one scalar value per characteristic. On the project's primary content — multi-hour live/DJ sets (median file ≈ 82 MB, max 11 GB; 79% of the 11,428-file archive is >50 MB) — this fails two ways:

1. **Crash.** `RhythmExtractor2013(method="multifeature")` overflows essentia's fixed-size `OnsetDetectionGlobal` output buffer on long audio:
   `RuntimeError: ... OnsetDetectionGlobal::onsetDetections: Could not push 1 value, output buffer is full`. With `retries: 4`, every long file churns the CPU four times before dead-lettering. Zero of 11,428 files completed analysis.
2. **Latent OOM.** `es.MonoLoader(sampleRate=44100)` decodes the whole file into one float32 array (~1.3 GB for a 2-hour file; far worse for the 11 GB outliers), ×8 concurrent jobs.

A single whole-file BPM/mood for a 2-hour set is also semantically near-meaningless — the set evolves.

## Goal

Analyze the **whole file** as a **time-series**: characteristics sampled across the file's duration, stored queryably, displayed as a compact-expandable timeline in the review UI. This simultaneously fixes the crash and the memory blowup (no algorithm ever sees more than one short window).

## Decisions (locked during brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Scope | Everything becomes a time-series, via a **two-tier** scheme (richness of "B", cost profile of "C"). |
| 2 | Granularity | **Fine tier** (BPM, key): 30 s windows. **Coarse tier** (mood, style, danceability): 180 s (3 min) windows. Fixed-duration, **configurable**. |
| 3 | Storage | **Queryable child table** (`analysis_window`) + representative aggregates kept on the existing `analysis` row. |
| 4 | UI | **Compact + expand-on-demand**: list rows show aggregates + a tiny BPM sparkline; click expands the full multi-lane timeline inline via HTMX. Plain SVG/CSS, no charting library. |

Window sizes are fixed-duration so resolution is constant regardless of file length; a normal 4-minute track degrades gracefully (~8 fine windows, 1–2 coarse windows ≈ a whole-file value).

## Architecture

### Processing — single-pass streaming decode (the crash/OOM fix)

Replace the two whole-file `MonoLoader()` calls with **streaming, single-pass, per-window** analysis. No essentia algorithm ever receives more than one window of audio, so the `OnsetDetectionGlobal` buffer never overflows and memory stays bounded (one ~30 s or ~180 s buffer at a time).

- **Fine pass (44.1 kHz mono):** stream-decode the file; accumulate samples into a 30 s buffer; when full, run `RhythmExtractor2013(multifeature)` and `KeyExtractor` on that buffer → one fine window `(start_sec, end_sec, bpm, musical_key)`; clear and continue. The trailing partial window is analyzed if it holds enough audio for a stable estimate (min length, configurable; default 15 s) else dropped.
- **Coarse pass (16 kHz mono):** stream-decode; accumulate into a 180 s buffer; run the existing 34 TF model sets on that buffer → one coarse window `(start_sec, end_sec, mood, style, danceability, features)`.

> **Highest implementation risk.** essentia's streaming framework and per-window standard-algorithm invocation must be validated against a real multi-hour file before building the full feature. **The implementation plan must begin with a spike** confirming: (a) streaming `MonoLoader` frame accumulation works; (b) `RhythmExtractor2013` succeeds on a 30 s buffer; (c) memory stays bounded on a real ≥2 h file; (d) total TF inference time over coarse windows is acceptable. If streaming accumulation proves impractical, fall back to segmented `EasyLoader(startTime, endTime)` decoding (simpler, but re-decode cost grows with file length — measure before choosing).

Execution stays CPU-bound in the existing `ProcessPoolExecutor` (`run_in_process_pool`). Per-window failures are **isolated**: a window that raises is logged and skipped, never failing the whole file.

### Aggregate derivation (keeps existing consumers working)

The `analysis` row keeps one representative value per characteristic so filename proposals, search, and sorting are unchanged:

- `bpm` = **median** of fine-window BPMs (robust to transitions/outliers).
- `musical_key` = **modal** key across fine windows (duration-weighted).
- `mood` / `style` = **dominant** label across coarse windows (time-weighted), serialized to the existing `String(50)` summary form.
- `danceability` = **mean** across coarse windows.

### Data model

New table `analysis_window` (model `AnalysisWindow`, `TimestampMixin`):

| column | type | notes |
|--------|------|-------|
| `id` | UUID PK | |
| `file_id` | UUID FK→`files.id` | indexed; `ON DELETE CASCADE` |
| `tier` | `String` | `'fine'` \| `'coarse'` |
| `window_index` | `int` | ordinal within tier |
| `start_sec` / `end_sec` | `Float` | window bounds |
| `bpm` | `Float \| None` | fine only |
| `musical_key` | `String(10) \| None` | fine only |
| `mood` | `String(50) \| None` | coarse only — dominant label |
| `style` | `String(50) \| None` | coarse only |
| `danceability` | `Float \| None` | coarse only |
| `features` | `JSONB \| None` | coarse only — full per-window distributions for the detail view |

Indexes (for the cross-archive queries that motivated option B):
- `(file_id, tier, window_index)` — fetch a file's series in order.
- Partial index on `bpm WHERE tier='fine'` — "files that ever exceed N BPM".
- Partial index on `danceability WHERE tier='coarse'`; index on `mood`, `style` — filter/sort by character.

`analysis` (existing, 1:1) is unchanged structurally; it holds the aggregates. **No data migration** — the table is currently empty (0 rows). Migration is purely additive (new table + indexes) via Alembic.

### Wire schema & API

- `AnalysisWritePayload` gains `windows: list[AnalysisWindowPayload] | None`, where `AnalysisWindowPayload` carries `tier, window_index, start_sec, end_sec` and the tier-specific fields. Existing aggregate fields stay (partial-PUT semantics preserved).
- `PUT /api/internal/agent/analysis/{file_id}` (`agent_analysis.py`): upserts the `analysis` aggregate row as today, and **replaces** the file's `analysis_window` rows (delete-by-`file_id` then bulk insert) so the PUT stays idempotent.
- `process_file` (`tasks/functions.py`) builds the windows list from the new `analyze_file` return shape and sends it.

### Job config

- Per-file `timeout`: long sets legitimately take many minutes; set a generous bound (or `0`/unbounded, consistent with the prior bulk-scan timeout decision) — to be finalized in planning.
- `retries`: reduce churn — a window-isolated failure shouldn't trigger 4 full re-analyses. Lower to 1–2.
- New `AgentSettings`: `analysis_fine_window_sec=30`, `analysis_coarse_window_sec=180`, `analysis_fine_min_sec=15`.

### UI

- **Review list row:** existing aggregates + a small server-rendered SVG BPM sparkline + an expand control.
- **Expanded (HTMX-loaded fragment):** multi-lane timeline on a shared time axis — BPM `<polyline>`, then key / mood / style ribbons as flexed colored `<div>` bands proportional to window duration. All SVG/CSS; no JS charting dependency. New endpoint returns the fragment for a file's windows.

## Components & boundaries

| Unit | Responsibility | Depends on |
|------|----------------|-----------|
| `analysis.py::analyze_file` (rewritten) | streaming decode → per-window analysis → `{aggregates, windows[]}` | essentia |
| `analysis.py` aggregate helpers | median/modal/dominant/mean reduction | — |
| `models/analysis.py::AnalysisWindow` (new) | ORM for child rows | base |
| Alembic migration | create table + indexes | model |
| `schemas/agent_analysis.py` | wire types incl. windows | pydantic |
| `routers/agent_analysis.py::put_analysis` | upsert aggregate + replace windows | model, session |
| `tasks/functions.py::process_file` | build windows payload, PUT | analysis, client |
| UI fragment + endpoint | sparkline + expandable timeline | templates, query |

## Testing

- **Unit:** windowing boundaries (incl. trailing partial window, sub-min-length drop); aggregate reductions (median/modal/dominant/mean); wire (de)serialization of windows; per-window failure isolation.
- **Integration:** short real fixture → expected window counts + aggregates; long synthetic file (e.g. concatenated/sine, ≥2 h) → completes without crash or unbounded memory; `put_analysis` idempotency (re-PUT replaces, doesn't duplicate, child rows).
- Coverage ≥ 85% (project gate).

## Out of scope / future

- Energy as a distinct per-window characteristic (no current essentia model; aggregate `energy` field left as-is).
- Beat-synced or overlapping windows (using fixed non-overlapping windows).
- Re-analysis of already-analyzed files (archive is currently all `discovered`; re-run via existing "Run analysis" after the fix ships).

## Rollout

Code change → PR → **v4.0.10** release (annotated tag push → GHCR publish) → homelab redeploy → click "Run analysis" (re-enqueues from the intact 11,428 discovered files; no rescan). Redis was already purged of the doomed/stale jobs.
