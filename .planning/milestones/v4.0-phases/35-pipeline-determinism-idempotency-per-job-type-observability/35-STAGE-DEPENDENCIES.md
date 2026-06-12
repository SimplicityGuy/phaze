# Phase 35 — Stage Ordering & Parallelization (data-dependency research)

Derived by tracing each SAQ task's inputs (payload), cross-stage DB **reads**, and DB
**writes** in the implementation. A stage is parallelizable with another iff it does not
read data the other produces. Evidence is `file:line` at the time of writing (2026-06-11).

## Per-stage data dependencies (evidence)

| Stage (task) | Input / reads | Reads from earlier stage? | Writes | Hard deps |
|---|---|---|---|---|
| **Discovery** (`scan_directory` `tasks/scan.py:133`; legacy `ingestion.py`) | walks dir, SHA-256, file_type | — (root) | `files` (DISCOVERED), `scan_batches` | none |
| **extract_file_metadata** (`tasks/metadata_extraction.py:32`) | `payload.original_path` → mutagen `extract_tags()` (`:48`) | **No** — file on disk only | `metadata` (PUT upsert) | Discovery |
| **fingerprint_file** (`tasks/fingerprint.py:31`) | `payload.original_path` → `orchestrator.ingest_all()` (`:41`) | **No** — file on disk only | `fingerprint_results` (per-engine UQ upsert) | Discovery |
| **process_file** / analysis (`tasks/functions.py:114`) | `payload.original_path` + `models_path` → essentia in process pool (`:125`) | **No** — file on disk only | `analysis` + `analysis_window` (PUT upsert) | Discovery |
| **scan_live_set** (`tasks/scan.py:87`) | `payload.original_path` → `orchestrator.combined_query()` (`:94`) — does its **own** fp query, does **not** read `fingerprint_results` | **No** — file on disk only | `tracklists` (source=fingerprint) | Discovery |
| **search_tracklist** (`tasks/tracklist.py:111`) | `FileRecord` + `file_metadata` (`:121`); parses filename first (`:128`), **falls back** to `metadata.artist` (`:135`) | **Soft** — metadata optional (improves matching; filename fallback) | `tracklists` | Discovery (metadata = soft) |
| **scrape_and_store_tracklist** (`tasks/tracklist.py:189`) | `Tracklist` by id (`:197`) | **Yes** — a tracklist must exist | `tracklist_versions` + tracks | search/scan_live_set |
| **match_tracklist_to_discogs** (`tasks/discogs.py:21`) | `Tracklist` + `TracklistTrack` latest version (`:34,:41`) | **Yes** — a tracklist must exist | `discogs_links` (delete+insert = idempotent) | search/scan_live_set |
| **generate_proposals** (`tasks/proposal.py:23`) | `FileRecord` + **`AnalysisResult`** (`:49`) + **`FileMetadata`** (`:52`) + companions (`:55`) | **Yes** — analysis **and** metadata (NOT fingerprint, NOT tracklist) | `proposals` | analysis + metadata |
| **execute_approved_batch** (`tasks/execution.py:352`) | approved `proposals` carried in payload | **Yes** — proposals + human approval | moves files, writes tags + `execution_log` | generate_proposals + approval |

## Ordering (topological tiers)

```
Tier 0  Discovery (scan)                      ── root; everything needs the file on disk
          │
Tier 1  ┌─ extract_file_metadata  ┐
        ├─ fingerprint_file        │  fully parallel — each reads ONLY the file on disk
        ├─ process_file (analysis) ┘
        └─ scan_live_set / search_tracklist   ── tracklist branch (search soft-uses metadata)
          │
Tier 2  ├─ generate_proposals      ── JOIN: needs analysis + metadata (fingerprint NOT required)
        └─ scrape_and_store_tracklist → match_tracklist_to_discogs  ── sequential tracklist sub-chain
          │
Tier 3  [human approval] → execute_approved_batch   ── terminal
```

## Parallelization conclusions

1. **The three core per-file stages — `extract_file_metadata`, `fingerprint_file`, `process_file` — are mutually independent** (each consumes only the discovered file, no cross-stage reads). They can run **fully concurrently** per file.
2. **The tracklist branch runs concurrently** with metadata/fingerprint/analysis. `scan_live_set` is fully independent (own fp query); `search_tracklist` has only a **soft** dependency on metadata (better artist signal, else filename parse) — so for best results schedule metadata before search, but it is not a hard ordering constraint.
3. **`generate_proposals` is the first true join point**: it requires `analysis` **and** `metadata` to be present. Critically it does **NOT** depend on `fingerprint_file` or the tracklist branch — so fingerprinting/tracklisting can lag without blocking proposals.
4. **`execute_approved_batch` is terminal**, gated by `generate_proposals` + human approval.
5. The tracklist sub-chain is strictly sequential within itself: `search_tracklist`/`scan_live_set` → `scrape_and_store_tracklist` → `match_tracklist_to_discogs`.

## Implication for Phase 35

- Deterministic keys make every stage safe to (re-)enqueue in any order without duplicate queue items, so the orchestration can dispatch Tier-1 stages as a concurrent fan-out per file and gate `generate_proposals` on `analysis + metadata` only.
- The per-job-type progress bars (work item 5) should reflect these tiers so the operator can see the independent stages advancing in parallel rather than one aggregate bar.
- Soft dependency note: if metadata becomes manual-only (work item 3), `search_tracklist` simply uses the filename-parse path until the operator runs metadata — no hard breakage.
