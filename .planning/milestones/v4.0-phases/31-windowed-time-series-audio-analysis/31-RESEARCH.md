# Phase 31: Windowed Time-Series Audio Analysis - Research

**Researched:** 2026-06-10
**Domain:** essentia (MIR) single-pass / bounded-memory audio decoding + per-window standard-mode analysis on Python 3.14
**Confidence:** HIGH (core claims validated by live experiment against the installed `essentia-tensorflow 2.1b6.dev1438` cp314 wheel in this repo's `.venv`)

## Summary

This phase rewrites `analyze_file()` to stop decoding whole multi-hour files into one buffer (the latent OOM) and to stop feeding long audio to `RhythmExtractor2013(multifeature)` (the `OnsetDetectionGlobal` buffer-overflow crash). Both are fixed by the same move: never let any essentia algorithm see more than one short window.

I validated the essentia API surface directly in this repo's venv. The single most important finding **inverts the design spec's primary/fallback assumption**: essentia's Python *streaming* framework cannot bound memory for this use case. `MonoLoader -> FrameCutter -> Pool` runs to completion and accumulates **every** frame into the Pool (measured: `(21, 1323000)` = the entire 10-min file back in RAM), reintroducing the exact OOM we are fixing. essentia's streaming framework has no per-frame Python callback sink that processes-and-discards. The genuinely bounded, Python-accessible primitive is **segmented decode via `EasyLoader(filename, startTime, endTime, sampleRate)`** — which the spec lists as the *fallback*. Measured RSS stayed flat (240 MB -> 259 MB) across 20 sequential 30 s window decodes of a 10-min file.

`RhythmExtractor2013(method="multifeature")` succeeds cleanly on 30 s and 15 s buffers (correct 120 BPM, confidence ~3.8); at 5 s it returns confidence `0.0` (no exception), which directly validates the locked `analysis_fine_min_sec=15` floor. `KeyExtractor(profileType="edma")` works on a 30 s buffer. `EasyLoader` seek cost on compressed mp3/ogg is **non-quadratic** — the 570 s window decoded *faster* than mid-file windows, proving ffmpeg timestamp seeking (not rescan-from-start).

**Primary recommendation:** Implement per-window analysis with **`EasyLoader(startTime, endTime, sampleRate=...)` segmented decode** as the primary (not fallback) decode strategy. Run two decode passes per file — one at 44.1 kHz for the fine tier (`RhythmExtractor2013` + `KeyExtractor` per 30 s window) and one at 16 kHz for the coarse tier (34 TF model sets per 180 s window). Keep everything in the existing synchronous `analyze_file` running inside `run_in_process_pool`. The mandatory spike must confirm these characteristics on a **real** ≥2 h compressed file before the full build, but every API question the spike was meant to de-risk is already answered below.

## User Constraints (from CONTEXT.md)

### Locked Decisions

**Scope & Granularity**
- Everything becomes a time-series via a **two-tier** scheme.
- **Fine tier** (BPM, key): 30 s windows. **Coarse tier** (mood, style, danceability): 180 s (3 min) windows.
- Window sizes are **fixed-duration** (constant resolution regardless of file length) and **configurable**.
- Trailing partial window analyzed only if it holds ≥ `analysis_fine_min_sec` (default 15 s) of audio, else dropped.
- A normal 4-min track degrades gracefully (~8 fine windows, 1–2 coarse windows).

**Processing — single-pass streaming decode (the crash/OOM fix)**
- Replace the two whole-file `MonoLoader()` calls with single-pass, per-window analysis. No essentia algorithm ever receives more than one window of audio.
- **Fine pass** (44.1 kHz mono): decode → accumulate 30 s buffer → run `RhythmExtractor2013(multifeature)` + `KeyExtractor` → fine window `(start_sec, end_sec, bpm, musical_key)`; clear; continue.
- **Coarse pass** (16 kHz mono): decode → accumulate 180 s buffer → run the existing 34 TF model sets → coarse window `(start_sec, end_sec, mood, style, danceability, features)`.
- Stays CPU-bound in the existing `ProcessPoolExecutor` (`run_in_process_pool`).
- Per-window failures are **isolated**: a window that raises is logged and skipped, never failing the whole file.

**Spike-first (MANDATORY)** — the plan MUST begin with a spike validating, on a real ≥2 h file: (a) frame accumulation works; (b) `RhythmExtractor2013` succeeds on a 30 s buffer; (c) memory stays bounded; (d) total TF inference time over coarse windows is acceptable. Fallback if accumulation proves impractical: segmented `EasyLoader(startTime, endTime)` decoding — measure before choosing.

**Aggregate derivation** — `bpm` = **median** of fine-window BPMs; `musical_key` = **modal** key (duration-weighted); `mood`/`style` = **dominant** label across coarse windows (time-weighted), serialized to existing `String(50)` summary form; `danceability` = **mean** across coarse windows.

**Data model — new `analysis_window` table** (`AnalysisWindow`, `TimestampMixin`): `id` UUID PK; `file_id` UUID FK→`files.id` (indexed, `ON DELETE CASCADE`); `tier` String (`'fine'`|`'coarse'`); `window_index` int; `start_sec`/`end_sec` Float. Fine-only: `bpm` Float|None, `musical_key` String(10)|None. Coarse-only: `mood` String(50)|None, `style` String(50)|None, `danceability` Float|None, `features` JSONB|None. Indexes: `(file_id, tier, window_index)`; partial index on `bpm WHERE tier='fine'`; partial index on `danceability WHERE tier='coarse'`; indexes on `mood`, `style`. `analysis` (existing, 1:1) structurally unchanged. **No data migration** — additive only (new table + indexes) via Alembic.

**Wire schema & API** — `AnalysisWritePayload` gains `windows: list[AnalysisWindowPayload] | None`. `AnalysisWindowPayload` carries `tier, window_index, start_sec, end_sec` + tier-specific fields. Existing aggregate fields stay (partial-PUT semantics preserved). `PUT /api/internal/agent/analysis/{file_id}` upserts the `analysis` aggregate AND **replaces** the file's `analysis_window` rows (delete-by-`file_id` then bulk insert) so PUT stays idempotent. `process_file` builds the windows list from the new `analyze_file` return shape.

**Job config** — Per-file `timeout`: generous bound or `0`/unbounded (finalize in planning). `retries`: lower to 1–2. New `AgentSettings`: `analysis_fine_window_sec=30`, `analysis_coarse_window_sec=180`, `analysis_fine_min_sec=15`.

**UI** — Review list row: aggregates + small server-rendered SVG BPM sparkline + expand control. Expanded (HTMX fragment): multi-lane timeline on a shared time axis — BPM `<polyline>`, then key/mood/style ribbons as flexed colored `<div>` bands proportional to window duration. All SVG/CSS; **no JS charting dependency**. New endpoint returns the fragment.

### Claude's Discretion
- Exact streaming-vs-`EasyLoader` choice (decided by spike measurement). **Research recommends `EasyLoader` — see Pitfall 1.**
- Final `timeout`/`retries` values within the stated bounds.
- Internal helper structure for aggregate reductions and window accumulation.
- Sparkline/timeline SVG markup details and color mapping.

### Deferred Ideas (OUT OF SCOPE)
- Energy as a distinct per-window characteristic (no current essentia model; aggregate `energy` field left as-is).
- Beat-synced or overlapping windows (fixed non-overlapping windows only).
- Re-analysis automation (existing "Run analysis" handles re-enqueue).

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| ANL-01 | BPM/key/mood/style detection, extended to time-series | Validated `RhythmExtractor2013` + `KeyExtractor` per 30 s window and the existing 34 TF model sets per 180 s window; aggregate reductions (median/modal/dominant/mean) keep the existing `analysis` row populated so all current ANL-01 consumers (proposals/search/sort) are unaffected. |
| (new) cross-archive queryability of time-varying characteristics | New `analysis_window` child table + partial indexes (`bpm WHERE tier='fine'`, `danceability WHERE tier='coarse'`, `mood`, `style`) enable "files that ever exceed N BPM" style queries. |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Segmented decode + per-window analysis | Agent worker (file-server, `services/analysis.py` in `ProcessPoolExecutor`) | — | CPU-bound, must run where the file bytes live; existing `run_in_process_pool` already isolates the GIL/crash blast radius. |
| Aggregate reduction (median/modal/dominant/mean) | Agent worker (`services/analysis.py`) | — | Pure-Python over the per-window results; produced before the HTTP PUT so the wire payload carries both aggregates and windows. |
| Window persistence (idempotent replace) | API / Backend (`routers/agent_analysis.py`) | Database | Delete-by-`file_id` + bulk insert is a transactional DB concern owned by the application server, not the agent. |
| Window config (`analysis_*_window_sec`) | Config (`AgentSettings`) | Agent worker | Agent reads window sizing; lives in the agent-role settings the worker already loads. |
| Job timeout/retries | Agent worker queue hook (`queue_defaults.apply_project_job_defaults`) | Config (`worker_job_timeout`, `worker_max_retries`) | Per-Job SAQ settings applied via the existing `before_enqueue` hook. |
| Timeline / sparkline rendering | Frontend Server (SSR Jinja2 + HTMX fragment) | — | Server-rendered SVG/CSS, no client JS framework — matches the project's locked HTMX+Jinja2 stack. |

## Standard Stack

### Core (all already present — this phase adds **zero new runtime dependencies**)
| Library | Version (verified in `.venv`) | Purpose | Why Standard |
|---------|------|---------|--------------|
| essentia-tensorflow | 2.1b6.dev1438 (`essentia.__version__` -> `2.1-beta6-dev`) | Decode + MIR + TF inference | Project constraint; cp314 wheel confirmed importable on Python 3.14. [VERIFIED: `uv pip show essentia-tensorflow` + `import essentia`] |
| numpy | (essentia dep) | Buffer slicing, aggregate math | Already a transitive dep; `analyze_file` already imports it. [VERIFIED: codebase] |
| SQLAlchemy / asyncpg / Alembic | 2.0.x / 0.30.x / 1.18.x | `AnalysisWindow` model + additive migration 018 | Existing stack; latest migration is `017_*`. [VERIFIED: `alembic/versions/` listing] |
| pydantic | 2.x | `AnalysisWindowPayload` wire type | Existing `AnalysisWritePayload` lives in `schemas/agent_analysis.py`. [VERIFIED: codebase] |
| Jinja2 + HTMX + Tailwind (CDN) | per CLAUDE.md | Sparkline row + expandable timeline fragment | Locked UI stack; SVG/CSS only, no charting lib. [CITED: CLAUDE.md] |

### Key essentia algorithms (standard mode — `essentia.standard as es`)
| Algorithm | Call signature (verified) | Notes |
|-----------|--------------------------|-------|
| `es.EasyLoader` | `EasyLoader(filename=p, sampleRate=44100, startTime=s, endTime=e)()` -> `np.float32[]` | **Primary decode primitive.** Params verified: `audioStream, downmix, endTime, filename, replayGain, sampleRate, startTime`. Output `audio`. |
| `es.MonoLoader` | `MonoLoader(filename=p, sampleRate=sr)()` | Whole-file decode — **only safe for the final trailing window or short files**; do NOT use on whole multi-hour files. |
| `es.RhythmExtractor2013` | `RhythmExtractor2013(method="multifeature")(buf)` -> `(bpm, beats, confidence, _, beats_intervals)` | Validated OK on 30 s and 15 s buffers; conf `0.0` (no raise) at 5 s. |
| `es.KeyExtractor` | `KeyExtractor(profileType="edma")(buf)` -> `(key, scale, strength)` | Validated OK on 30 s buffer. |
| `es.Resample` | `Resample(inputSampleRate=44100, outputSampleRate=16000)(buf44)` -> `buf16` | Validated: 7,938,000 -> 2,880,000 samples (exact). Optional optimization to avoid a 2nd file decode — see Pitfall 4. |
| `es.TensorflowPredictMusiCNN` / `...VGGish` / `...EffnetDiscogs` | unchanged from current `analysis.py` | Run per 180 s coarse window instead of whole file. Per-window activations still `np.mean(..., axis=0)`. |

**Installation:** No `uv add` required — all dependencies already in `pyproject.toml`. (Verify with `uv sync`.)

## Package Legitimacy Audit

> This phase installs **no new external packages**. All algorithms come from the already-pinned `essentia-tensorflow 2.1b6.dev1438` and existing stack. No audit table required; no slopcheck gate triggered.

If the planner adds any helper package (none anticipated), run the Package Legitimacy Gate first.

## Architecture Patterns

### System Architecture Diagram (data flow through the rewritten `analyze_file`)

```
                          original_path, models_dir
                                    │
                                    ▼
                 ┌──────────────────────────────────────────┐
                 │  analyze_file()  (sync, in ProcessPool)   │
                 └──────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┴───────────────────────────┐
        ▼ FINE PASS (44.1 kHz)                                    ▼ COARSE PASS (16 kHz)
   probe duration (es.AudioLoader/                          probe duration
   MetadataReader or ffprobe)                                    │
        │                                                        │
   for i, [s,e) in 30s windows:                            for j, [s,e) in 180s windows:
        │  EasyLoader(sr=44100, startTime=s, endTime=e)         │  EasyLoader(sr=16000, startTime=s, endTime=e)
        │      -> buf30  (≤1.32M samples, ~5 MB)                 │      -> buf180 (≤2.88M samples @16k, ~11 MB)
        │  drop if last & len < fine_min_sec(15s)               │  (analyze whatever audio is present)
        │  try:                                                 │  try:
        │    RhythmExtractor2013(multifeature)(buf30) -> bpm    │    for model_set in 34 TF models:
        │    KeyExtractor(edma)(buf30) -> key,scale             │       classifier(buf180) -> activations
        │  except: log + skip window                            │    derive mood/style/danceability + features
        │  -> fine_windows[]                                    │  except: log + skip window
        ▼                                                       ▼  -> coarse_windows[]
        └───────────────────────────┬───────────────────────────┘
                                    ▼
                    aggregate reductions:
                      bpm = median(fine.bpm)
                      musical_key = duration-weighted mode(fine.key)
                      mood/style = time-weighted dominant(coarse)
                      danceability = mean(coarse.danceability)
                                    │
                                    ▼
              return { **aggregates, "windows": [fine... , coarse...] }
                                    │
                                    ▼  (tasks/functions.py::process_file)
       AnalysisWritePayload(aggregates) + windows[]  ──PUT──▶  routers/agent_analysis.py
                                                                  │ upsert analysis row
                                                                  │ DELETE analysis_window WHERE file_id=…
                                                                  └ bulk INSERT analysis_window rows
```

> Memory bound: at any instant the worker holds **one** window buffer per pass (~5–11 MB) plus the cached TF graphs — never the whole file. Measured stable RSS across 20 sequential decodes.

### Component Responsibilities

| File | Change | Notes |
|------|--------|-------|
| `src/phaze/services/analysis.py` | Rewrite `analyze_file` → segmented per-window; add `_iter_windows`, aggregate helpers (`_median_bpm`, `_modal_key`, `_dominant_label`, `_mean`) | Keep `derive_mood`/`derive_style` (now applied per coarse window). Stays sync for `run_in_process_pool`. |
| `src/phaze/models/analysis.py` | Add `AnalysisWindow(TimestampMixin, Base)` | Same file as `AnalysisResult`. |
| `alembic/versions/018_*.py` | Additive: create `analysis_window` + indexes | Next number after `017`. `down_revision="017_add_scan_batches_last_progress_at"`. |
| `src/phaze/schemas/agent_analysis.py` | Add `AnalysisWindowPayload`; add `windows: list[...] | None` to `AnalysisWritePayload` | Keep `extra="forbid"` and optional-fields/partial-PUT contract. |
| `src/phaze/routers/agent_analysis.py` | After aggregate upsert, replace child rows: `DELETE … WHERE file_id` then bulk `pg_insert` | Same transaction/session; preserves idempotency. The existing `_summarize_dict_to_string` + features-overflow funnel for aggregates stays. |
| `src/phaze/tasks/functions.py` | Build `windows` from new return shape; pass to `AnalysisWritePayload` | Aggregate `mood`/`style` still converted to wire dicts via existing `_features_to_mood_dict`/`_features_to_style_dict`. |
| `src/phaze/config.py` | Add `analysis_fine_window_sec=30`, `analysis_coarse_window_sec=180`, `analysis_fine_min_sec=15` to `AgentSettings` | Use `AliasChoices("PHAZE_ANALYSIS_FINE_WINDOW_SEC", ...)` per the file's existing convention. |
| `src/phaze/tasks/_shared/queue_defaults.py` (+ `config.worker_max_retries`) | Lower process_file retries to 1–2; confirm timeout policy | Per-Job defaults applied via the `apply_project_job_defaults` `before_enqueue` hook (see `agent_worker.py:185`). |
| Review-UI templates + router | Sparkline in list row; new HTMX fragment endpoint for expanded timeline | SVG `<polyline>` for BPM, flexed colored `<div>` ribbons for key/mood/style. |

### Pattern 1: Segmented bounded decode loop (the core primitive)
**What:** Decode only the current window's bytes, analyze, discard, repeat.
**When to use:** Every fine and coarse window.
**Example (verified pattern):**
```python
# Source: validated against essentia-tensorflow 2.1b6.dev1438 in this repo's .venv
import essentia.standard as es

def _iter_fine_windows(path: str, total_sec: float, win_sec: int, min_sec: int):
    start = 0.0
    idx = 0
    while start < total_sec:
        end = min(start + win_sec, total_sec)
        if (end - start) < min_sec and idx > 0:   # trailing-partial drop (keep window 0)
            break
        buf = es.EasyLoader(filename=path, sampleRate=44100,
                            startTime=start, endTime=end)()
        yield idx, start, end, buf      # caller analyzes buf, then it is GC'd
        start = end
        idx += 1
```
> Determine `total_sec` cheaply without a full decode via `es.AudioLoader`/`es.MetadataReader` (no PCM materialized) or `subprocess` ffprobe. Do **not** call `MonoLoader()` just to measure length.

### Pattern 2: Per-window failure isolation
```python
for idx, s, e, buf in _iter_fine_windows(...):
    try:
        bpm, *_ , conf = es.RhythmExtractor2013(method="multifeature")(buf)[:1] + (...)
        key, scale, _ = es.KeyExtractor(profileType="edma")(buf)
        fine.append(FineWindow(idx, s, e, round(float(bpm), 1), f"{key} {scale}"))
    except Exception:                  # noqa: BLE001 — isolation is the requirement
        log.warning("fine window %d [%s,%s) failed; skipping", idx, s, e, exc_info=True)
        continue
```

### Anti-Patterns to Avoid
- **Streaming `MonoLoader -> FrameCutter -> Pool` for windowing.** Pool accumulates ALL frames = whole file in RAM = the OOM you're fixing (measured `(21, 1323000)`). See Pitfall 1.
- **Calling `MonoLoader(sampleRate=44100)()` once and slicing.** Fixes the *crash* (short slices into `RhythmExtractor`) but NOT the OOM — the whole decoded array is resident.
- **Re-instantiating TF classifiers per window.** Keep the existing `_classifier_cache`; instantiating 34 graphs per coarse window would dominate runtime.
- **Using `MonoLoader` to measure file duration.** Materializes the full PCM array.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Decode a time range from a compressed file | Custom ffmpeg subprocess + PCM parsing | `es.EasyLoader(startTime, endTime, sampleRate)` | Handles seek, downmix, resample, replayGain; returns ready float32. Verified seek is non-quadratic. |
| 44.1k→16k conversion of an in-memory buffer | Manual decimation/FFT | `es.Resample(inputSampleRate, outputSampleRate)` | Exact sample-count, anti-aliased. Verified. |
| File duration | Parse headers yourself | `es.AudioLoader`/`es.MetadataReader` or `ffprobe` | No PCM materialized. |
| Beat tracking / key / mood | Anything | existing essentia algos per window | Already the project's analysis engine. |

**Key insight:** The only thing genuinely worth hand-building here is the **windowing loop and the four aggregate reductions** — everything audio-DSP is essentia's job.

## Runtime State Inventory

> This is a code/data-model change, not a rename/migration. Most categories are N/A, but the decode-state and DB categories matter.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `analysis_window` table is **new and empty (0 rows)** per CONTEXT; `analysis` table currently has 0 completed rows (0 of 11,428 files analyzed). | Additive Alembic migration only. No backfill/data migration. |
| Live service config | `process_file` per-Job `timeout`/`retries` come from the `apply_project_job_defaults` `before_enqueue` hook on each SAQ queue (`worker_job_timeout=600`, `worker_max_retries=4` in `config.py:194-195`). Lowering retries affects the **live** enqueue policy, not git-only state. | Adjust hook/config; redeploy required (v4.0.10). |
| OS-registered state | None — analysis runs inside the containerized agent worker. | None. |
| Secrets/env vars | New `AgentSettings` env vars (`PHAZE_ANALYSIS_FINE_WINDOW_SEC`, etc.) have safe defaults; nothing secret. | Document defaults; no secret rotation. |
| Build artifacts | None — no package rename. | None. |

**The canonical question:** After the code ships, the only runtime state that matters is the empty `analysis_window` table (created by migration) and the SAQ job policy (retries lowered) — both handled by the migration + redeploy, then "Run analysis" re-enqueues the intact 11,428 `discovered` files.

## Common Pitfalls

### Pitfall 1: essentia streaming cannot bound memory here — `EasyLoader` is the real fix
**What goes wrong:** Following the spec literally ("streaming `MonoLoader` accumulation") and wiring `MonoLoader -> FrameCutter -> Pool` (or `RealAccumulator`) reintroduces the whole-file buffer.
**Why it happens:** essentia's streaming network runs to completion in one `run()` call; `Pool`/`RealAccumulator` are accumulators by design and hold every emitted frame. There is no exposed per-frame Python callback sink that processes-and-discards.
**Evidence:** Live test produced `pool['frames'].shape == (21, 1323000)` — the entire 10-min file resident. By contrast, 20 sequential `EasyLoader` 30 s decodes held RSS at 240→259 MB.
**How to avoid:** Use `EasyLoader(startTime, endTime, sampleRate)` segmented decode as the **primary** strategy. Treat "streaming accumulation" as the rejected option, not the fallback.
**Warning signs:** Any plan task that imports `essentia.streaming`, or RSS that scales with file length.

### Pitfall 2: trailing/short windows and `RhythmExtractor2013` confidence
**What goes wrong:** A 5–10 s trailing window yields `confidence == 0.0` and an unreliable BPM that pollutes the median.
**Why it happens:** `RhythmExtractor2013(multifeature)` needs enough audio for a stable onset profile.
**Evidence:** 30 s → conf 3.83; 15 s → conf 3.79; 5 s → conf **0.00** (no exception).
**How to avoid:** Honor `analysis_fine_min_sec=15` (drop sub-15 s trailing windows, except keep window 0 for very short tracks). Consider treating `confidence == 0.0` windows as drop-from-aggregate while still recording the raw window.
**Warning signs:** Median BPM skewed by a single end-of-file outlier.

### Pitfall 3: `EasyLoader` seek cost on *real* compressed files (the spike's job)
**What goes wrong:** On pathological VBR mp3 / m4a without a seek index, per-window decode could degrade.
**Why it happens:** ffmpeg seeks to the nearest keyframe/timestamp; sparse indexes mean more decode-and-discard before `startTime`.
**Evidence (synthetic mp3/ogg, 10-min):** per-30 s-window decode 0.25–0.76 s, **not** monotonic with position (570 s window was the *fastest* at 0.26 s) — proving timestamp seeking, not rescan-from-start. Full-file decode ~0.28 s.
**How to avoid:** The mandatory spike MUST measure this on a real ≥2 h concert file (mp3/m4a). Pass/fail threshold below. If a real file shows roughly-constant per-window cost (expected), `EasyLoader` is confirmed. If it shows O(position) growth, fall back to the single-decode-at-44.1k + slice + `Resample` hybrid (accepts a larger but still single resident buffer, or chunked streaming-to-temp).
**Warning signs:** Per-window decode time climbing linearly with `window_index`.

### Pitfall 4: dual sample-rate = two decode passes (or one decode + `Resample`)
**What goes wrong:** Decoding the file twice (once at 44.1k, once at 16k) doubles decode cost.
**Why it happens:** Fine tier needs 44.1k; the 34 TF models need 16k; `EasyLoader` decodes at one `sampleRate`.
**Options:**
- **(A) Two passes — recommended for v1.** Fine pass `sampleRate=44100`, coarse pass `sampleRate=16000`. Simplest; decode cost is small vs. TF inference (the dominant cost). The spike measures TF inference time, which dwarfs decode.
- **(B) One decode + `Resample`.** Decode coarse windows at 44.1k and `es.Resample(44100, 16000)` in-memory (verified exact). Saves a decode pass but complicates window alignment (coarse 180 s = 6× fine 30 s) — an optimization, not v1.
**How to avoid the trap:** Start with (A); only adopt (B) if the spike shows decode (not inference) is the bottleneck.

### Pitfall 5: TF classifier cache lifetime under per-window loops
**What goes wrong:** Naively constructing `TensorflowPredict*` per window explodes runtime.
**How to avoid:** The existing module-level `_classifier_cache` already memoizes by filename across calls within a worker process — keep it. Per-window cost becomes inference-only.
**Warning signs:** Coarse pass time ≈ (num_windows × 34 × graph-load) instead of (num_windows × 34 × inference).

### Pitfall 6: `analysis` row has no `danceability` column (existing quirk to preserve)
**What goes wrong:** Assuming `danceability`/`energy` are columns on `AnalysisResult`.
**Reality:** `models/analysis.py` has only `bpm, musical_key, mood, style, fingerprint, features`. The router **funnels** `danceability`/`energy` into the `features` JSONB (`routers/agent_analysis.py:107`). The new aggregate `danceability` mean continues into that funnel unless the planner chooses to add a real column (optional, additive in the same migration).
**How to avoid:** Don't add code assuming a `danceability` column unless you also migrate it. The window-level `danceability` lives on `analysis_window.danceability` (new column, per the data model).

## Code Examples

### Aggregate reductions (pure-Python, unit-testable without essentia)
```python
# Source: derived from CONTEXT locked decisions; no external API
from collections import Counter
from statistics import median, mean

def aggregate_bpm(fine: list["FineWindow"]) -> float | None:
    vals = [w.bpm for w in fine if w.bpm is not None]
    return round(median(vals), 1) if vals else None

def aggregate_key(fine: list["FineWindow"]) -> str | None:
    # duration-weighted mode: weight each key by window duration
    weights: Counter[str] = Counter()
    for w in fine:
        if w.musical_key:
            weights[w.musical_key] += (w.end_sec - w.start_sec)
    return weights.most_common(1)[0][0] if weights else None

def aggregate_dominant(coarse, attr: str) -> str | None:  # mood / style
    weights: Counter[str] = Counter()
    for w in coarse:
        label = getattr(w, attr)
        if label:
            weights[label] += (w.end_sec - w.start_sec)
    return weights.most_common(1)[0][0] if weights else None

def aggregate_danceability(coarse) -> float | None:
    vals = [w.danceability for w in coarse if w.danceability is not None]
    return mean(vals) if vals else None
```

### Idempotent child-row replace (router)
```python
# Source: SQLAlchemy 2.0 async + existing agent_analysis.py upsert pattern
from sqlalchemy import delete
from phaze.models.analysis import AnalysisWindow

# ... after the existing aggregate pg_insert upsert, same session/transaction:
if body.windows is not None:                      # only replace when client sent windows
    await session.execute(delete(AnalysisWindow).where(AnalysisWindow.file_id == file_id))
    if body.windows:
        await session.execute(
            pg_insert(AnalysisWindow).values(
                [{"id": uuid.uuid4(), "file_id": file_id, **w.model_dump()} for w in body.windows]
            )
        )
await session.commit()
```
> Guard on `body.windows is not None` so an aggregate-only partial PUT does not wipe windows (preserves the existing partial-PUT contract). `ON DELETE CASCADE` on the FK also covers file deletion.

## State of the Art

| Old Approach | Current Approach | Why |
|--------------|------------------|-----|
| Whole-file `MonoLoader` + whole-file `RhythmExtractor2013` | Segmented `EasyLoader` per 30 s/180 s window | Fixes both the `OnsetDetectionGlobal` overflow crash and the whole-file OOM. |
| One scalar per characteristic | Two-tier time-series + representative aggregates | Multi-hour sets evolve; a single BPM/mood is semantically meaningless. |
| (spec) "streaming accumulation primary, EasyLoader fallback" | **EasyLoader primary** | Empirically, streaming Pool/RealAccumulator hold the whole file — no bounded streaming sink exists in essentia's Python API. |

**Deprecated/outdated:** none introduced; no dependency changes.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Real ≥2 h compressed concert files exhibit the same non-quadratic `EasyLoader` seek behavior as the synthetic mp3/ogg tested | Pitfall 3 | If real VBR files seek poorly, per-window decode degrades; mitigated by the **mandatory spike** measuring exactly this before the full build. |
| A2 | Two decode passes (44.1k + 16k) keep total per-file time acceptable because TF inference dominates | Pitfall 4 / spike | If decode dominates, switch to one-decode+`Resample` (option B). Spike measures (d) TF inference time. |
| A3 | `subprocess`/`AudioLoader`/`MetadataReader` gives correct duration cheaply for all archive formats (mp3/m4a/ogg/flac/wav/aac/opus/wma/aiff) | Pattern 1 | A wrong duration over/under-counts windows; low risk — verify in spike for one file of each container. |
| A4 | `security_enforcement` defaults to enabled (key absent from `config.json`) | Security Domain | If the team treats absent-as-disabled, the Security Domain section is informational only. |

**Note:** A1/A2 are precisely the unknowns the locked **spike-first** decision exists to resolve. Everything else (API shape, crash fix, memory bounding, key/BPM on 30 s, Resample exactness, cp314 import) is **VERIFIED** in this session.

## Open Questions

1. **Per-file `timeout` final value (0/unbounded vs. generous bound).**
   - What we know: prior bulk-scan incident (260609-glv) set `timeout=0` for `scan_directory`; the stall reaper (`scan_stall_seconds=86400`) is the liveness guard.
   - What's unclear: whether `process_file` should mirror `timeout=0` or take a generous finite bound (e.g., 2–4 h).
   - Recommendation: `timeout=0` (unbounded) + `retries=1`, consistent with the prior decision; window-isolation already prevents one bad window from failing the file. Finalize in planning.

2. **Promote `danceability` to a real `analysis` column?**
   - What we know: currently funneled into `features` JSONB; the migration is already additive.
   - Recommendation: optional — add the column in the same migration if sort-by-danceability on the aggregate is desired; otherwise keep the funnel. Out-of-scope-safe either way.

3. **Coarse-window minimum length.** CONTEXT defines `analysis_fine_min_sec` but no coarse floor.
   - Recommendation: analyze any coarse window with audio present (TF models tolerate short input); optionally reuse a small floor. Low risk.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| essentia-tensorflow (cp314 wheel) | all decode/analysis | ✓ | 2.1b6.dev1438 | — (platform-gated off linux-arm64; agent images are x86_64) |
| ffmpeg / libavcodec (bundled or system) | `EasyLoader` compressed decode | ✓ (wheel-bundled; system ffmpeg also in Dockerfile per 260610-fp9) | 8.x | — |
| PostgreSQL 16+ | `analysis_window` table | ✓ (existing) | 16+ | — |
| A real ≥2 h concert fixture (mp3/m4a) | the mandatory spike | ✗ (not in repo) | — | Synthetic concatenated/sine ≥2 h file (spec-sanctioned) for memory/crash; but seek-cost (A1) needs a real file — source one from the archive on the file-server for the spike. |

**Missing dependencies with no fallback:** none blocking code; the spike's real-file seek measurement (A1) should use an actual archive file on the homelab agent.
**Missing dependencies with fallback:** ≥2 h test fixture — synthetic is fine for memory/crash; real file recommended for seek-cost confidence.

## Validation Architecture

> Nyquist validation is enabled (`workflow.nyquist_validation: true`).

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`asyncio_mode = "auto"`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]`, `testpaths = ["tests"]` |
| Quick run command | `uv run pytest tests/test_services/test_analysis.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |
| Coverage gate | 85% (CLAUDE.md) |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Automated Command | File Exists? |
|-----|----------|-----------|-------------------|-------------|
| ANL-01 | windowing boundaries incl. trailing-partial drop (<15 s) | unit | `uv run pytest tests/test_services/test_analysis.py -k window_boundaries` | ❌ Wave 0 |
| ANL-01 | aggregate reductions (median/modal/dominant/mean) | unit | `uv run pytest tests/test_services/test_analysis.py -k aggregate` | ❌ Wave 0 |
| ANL-01 | per-window failure isolation (one raises → others survive) | unit | `uv run pytest tests/test_services/test_analysis.py -k failure_isolation` | ❌ Wave 0 |
| (new) | `AnalysisWindowPayload` (de)serialization round-trip | unit | `uv run pytest tests/test_schemas/test_agent_analysis.py -k window` | ⚠️ extend existing |
| (new) | `put_analysis` idempotency: re-PUT replaces, not duplicates, child rows | integration | `uv run pytest tests/test_routers -k analysis_window_idempotent -m integration` | ❌ Wave 0 |
| ANL-01 | long synthetic ≥2 h file completes without crash/unbounded memory | integration | `uv run pytest tests -k long_file_bounded -m integration` | ❌ Wave 0 |
| ANL-01 | short real fixture → expected window counts + aggregates | integration | `uv run pytest tests -k real_fixture_windows -m integration` | ❌ Wave 0 |

> Existing `tests/test_services/test_analysis.py` mocks `essentia` (`@patch ... mock_es`). New unit tests should mock `EasyLoader`/`RhythmExtractor2013`/`KeyExtractor` the same way; aggregate-reduction tests need **no** essentia mock (pure-Python) and are the cheapest high-value coverage.

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_analysis.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** full suite green + `pre-commit run --all-files` (ruff/mypy/bandit) before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] Window-boundary + trailing-partial-drop unit tests
- [ ] Aggregate-reduction unit tests (no essentia mock needed)
- [ ] Per-window failure-isolation unit test
- [ ] `analysis_window` idempotency integration test (delete-then-insert)
- [ ] ≥2 h synthetic-file bounded-memory integration test (mark `integration`, may be slow)
- [ ] `AnalysisWindowPayload` schema round-trip (extend `tests/test_schemas/test_agent_analysis.py`)
- [ ] Spike script (throwaway, not committed to `tests/`) — see Spike Design below

### Spike Design (mandatory first plan task)
Minimal throwaway script (e.g. `scripts/spike_windowed_analysis.py`, run via `uv run`, **not committed**):

| Validates | Method | Pass/Fail Threshold |
|-----------|--------|---------------------|
| (a) per-window decode works | `EasyLoader` loop over a real ≥2 h file at 44.1k | All windows decode; no exception |
| (b) `RhythmExtractor2013` on 30 s buffer | run on each fine window | No `OnsetDetectionGlobal` overflow; BPM returned (conf may be 0 on silence) |
| (c) bounded memory | sample RSS (`resource.getrusage`) every N windows over the full file | Peak RSS stays roughly flat (e.g. < 1.5 GB, ≪ whole-file 1.3 GB×rate); does NOT grow with `window_index` |
| (d) coarse TF inference time | time the 34-model pass per 180 s window; extrapolate to full file | Total per-file analysis acceptable for 8× concurrency (record seconds/hour-of-audio; flag if a 2 h file > ~tens of minutes wall) |
| (A1) seek cost | log per-window decode time vs `window_index` | Roughly constant (non-quadratic). If linear growth → choose decode+Resample hybrid |

> The spike answers Assumptions A1/A2 with real numbers; its output should be pasted into the plan's decision log to lock the EasyLoader-vs-hybrid choice.

## Security Domain

> `security_enforcement` key absent from `.planning/config.json` → treated as enabled. This phase adds no auth surface; the PUT endpoint already enforces agent bearer auth (`get_authenticated_agent`) and `agent_id` never comes from the body (AUTH-01).

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no (unchanged) | Existing agent bearer token on `PUT /api/internal/agent/analysis/{file_id}`. |
| V3 Session Management | no | N/A (stateless internal API). |
| V4 Access Control | yes (light) | New review-UI timeline fragment endpoint must sit behind the same admin auth as the existing review UI; `file_id` scoping only. |
| V5 Input Validation | yes | `AnalysisWindowPayload` via pydantic with `extra="forbid"`; constrain `tier ∈ {fine,coarse}`, `window_index ≥ 0`, `start_sec/end_sec ≥ 0`, bounded list length. Window data is essentia-derived (low-trust path-traversal risk: `original_path` already containment-checked against `scan_roots`). |
| V6 Cryptography | no | None. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Oversized `windows[]` payload (DoS via huge bulk insert) | DoS | Bound `windows` list length in pydantic (a 24 h file at 30 s windows ≈ 2,880 fine windows — cap generously, e.g. ≤ 50k). |
| SVG/HTML injection via essentia-derived label strings (mood/style/key) in the timeline | Tampering/XSS | Jinja2 autoescaping on all label text; numeric-only attributes for SVG geometry. |
| Child-row orphaning on file delete | Integrity | `ON DELETE CASCADE` on `analysis_window.file_id` FK (locked). |

## Sources

### Primary (HIGH confidence — verified this session in repo `.venv`)
- Live experiments against `essentia-tensorflow 2.1b6.dev1438` (`essentia.__version__ == "2.1-beta6-dev"`): `EasyLoader`/`MonoLoader`/`RhythmExtractor2013`/`KeyExtractor`/`Resample` signatures + behavior; streaming `MonoLoader→FrameCutter→Pool` whole-file accumulation; RSS memory bounding; mp3/ogg seek-cost profile. [VERIFIED: session bash output]
- `src/phaze/services/analysis.py`, `routers/agent_analysis.py`, `schemas/agent_analysis.py`, `models/analysis.py`, `tasks/functions.py`, `tasks/agent_worker.py`, `config.py` — current implementation patterns. [VERIFIED: Read]
- `alembic/versions/` — latest migration `017`; next is `018`. [VERIFIED: ls]
- `CLAUDE.md`, `.planning/config.json`, `31-CONTEXT.md`, design spec — constraints + locked decisions. [CITED]

### Secondary (MEDIUM)
- essentia algorithm reference (training knowledge cross-checked against verified signatures): `RhythmExtractor2013` method=`multifeature`, `KeyExtractor` profile=`edma`. [ASSUMED→corroborated by VERIFIED behavior]

### Tertiary (LOW)
- Real-archive ≥2 h compressed seek behavior (A1) — extrapolated from synthetic mp3/ogg; **flagged for the spike**.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new deps; all versions verified in `.venv`.
- Architecture (segmented decode, bounded memory, crash fix): HIGH — directly measured.
- Decode strategy choice (EasyLoader > streaming): HIGH — streaming Pool accumulation measured to defeat the OOM fix.
- Real-file seek cost + total inference time: MEDIUM — synthetic-validated, real-file confirmation is the spike's explicit job (A1/A2).
- Pitfalls: HIGH for 1–2, 4–6; MEDIUM for 3 (pending spike).

**Research date:** 2026-06-10
**Valid until:** ~2026-09-10 (90 days; essentia is effectively frozen at this pinned dev build, integration points are this repo's own code).
