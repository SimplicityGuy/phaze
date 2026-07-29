# ADR-0002 — Remove audio fingerprinting entirely

| | |
| --- | --- |
| **Status** | Accepted — removed |
| **Date** | 2026-07-28 |
| **Decider** | Repository owner (operator decision) |
| **Investigation** | 2026-07-27 bug hunt; epic `phaze-p3hj`; bug `phaze-iq65`; capacity measurement recorded in spike epic `phaze-oof3` |
| **Supersedes** | — |
| **Reinstatement** | `phaze-oof3` (SPIKE epic, filed deliberately un-kicked-off in the backlog) |

______________________________________________________________________

## Context

phaze shipped audio fingerprinting as two independent engines behind HTTP sidecars:
**audfprint** (a landmark/constellation matcher, Dan Ellis's implementation of the Shazam/Wang
algorithm) and **Panako** (deployed in its `OLAF` strategy — a landmark matcher tolerant of
±10% time-scale/pitch modification, not the stretch-invariant "Panako" strategy the same tool
also ships). Both wrote to a `fingerprint_results` table (one row per `file_id` + `engine`) and
fed a dedicated pipeline stage, an admin "Track ID" match-confidence workspace, and a
per-agent `scan_live_set` task that used fingerprint matching to identify concert sets.

A 2026-07-25 bug report (epic `phaze-p3hj`) that the audfprint column read "failed" on every
file led to a 2026-07-27 bug hunt and a follow-on capacity investigation (spike epic
`phaze-oof3`) that together surfaced three independent findings, each sufficient on its own to
make the feature not worth carrying. All figures below are exact, measured against the live
archive on 2026-07-28: **11,412 files, 11,492.2 h total content, median file 58.9 min, longest
file 12.07 h.**

## Decision

**Remove audio fingerprinting in full**: both engines, their sidecars, the pipeline stage, the
task-queue work (`fingerprint_file`, `scan_live_set`), the API surface
(`/api/v1/fingerprint*`, `/api/internal/agent/fingerprints/*`), the admin "Track ID" workspace,
the persisted `fingerprint_results` table, and every configuration knob
(`AUDFPRINT_URL`/`PANAKO_URL`/`PHAZE_LANE_FINGERPRINT_CONCURRENCY`). This is a **deletion, not
a soft-disable** — no feature flag, no dormant adapter, no skipped test survives.

This does **not** remove tracklist lookup (epic `phaze-fq9h`), which is an independent
capability sourced from 1001tracklists rather than from audio matching, and is unaffected.

Reinstatement is **not abandoned** — it is deferred to `phaze-oof3`, a spike-gated epic filed
alongside this removal that carries the full quantitative record below (and more) so a future
attempt starts from measurement, not from repeating it.

## Rationale

### 1. Capacity — the archive does not fit, at any configuration

audfprint packs each stored landmark into a single `uint32` as
`(track_id + 1) << maxtimebits | (frame_time & mask)`. Time bits are bought directly out of
track-id capacity: `max_track_ids = 2**(32 - maxtimebits) - 1`. One frame is `256/11025 =
0.023220 s`. The product of time horizon and corpus capacity is therefore **invariant at every
bit split**: `2^32` frame-slots = **27,702 h of audio**, full stop — no re-tuning escapes this
ceiling, only trades horizon for capacity along the same fixed budget:

| Time bits | Landmark horizon | Max track ids |
| --- | --- | --- |
| 13 | 190.2 s (3.2 min) | 524,287 |
| 14 | 380.4 s (6m20s) | 262,143 ← deployed default |
| 15 | 760.9 s (12m41s) | 131,071 |
| 16 | 1521.7 s (25m22s) | 65,535 |
| 17 | 3043.5 s (50m43s) | 32,767 |
| 18 | 6087.0 s (1h41m) | 16,383 |
| 19 | 12173.9 s (3h23m) | 8,191 |
| 20 | 24347.9 s (6h46m) | 4,095 |
| 21 | 48695.8 s (13h32m) | 2,047 |

The archive already holds **11,492.2 h across 11,412 files — 41% of the 27,702 h ceiling, at
5.7% of the 200,000-file design target.** At that target, holding the same mean duration, the
corpus needs **201,406 h — 7.3× the entire addressable space of one database.** Sharding
multiplies the storage budget; changing the bit split cannot, because horizon × capacity is
fixed. This is the finding that drove removal.

The consequences at the *deployed* 14-bit split, confirmed against the real duration
distribution: **89.3% of files (10,189 of 11,412) exceed the 380.4 s landmark horizon**; the
median file is **58.9 min and wraps the horizon 9.3×**; the longest file (12.07 h) would need
21 bits to cover — which buys only 2,047 track ids, fewer than the file count, so no bit split
can cover it at all. Match timestamps past the horizon alias modulo it (wrong, not imprecise —
the wrapped block number is discarded), and at the deployed split a 3-hour set matching
*itself* scores 2.98 instead of 83.01 — a ~28× collapse — because the true alignment is split
across one delta bin per 380.4 s block and only the winning bin counts.

Panako's volume is worse in absolute terms, though never confirmed to scale linearly: the
deployed LMDB store carried **23,892,353 hashes for 466,674.8 s (129.6 h)** of audio — about
**51 fingerprints/second** — on a **42,755,796,992-byte** on-disk store for that 129.6 h.
Extrapolated (unverified) to the full 11,492.2 h archive, that is **~2.1 billion hashes**.

### 2. Both engines were silently dead — success was inferred, never observed

**audfprint failed on every file for ~10 days across 11,180 files.** `/data/fprint/fprint.pklz`
was a zero-byte file, so every add and match died in `pickle.load` with `EOFError: Ran out of
input` before touching audio. Root cause: upstream `HashTable.save` does an in-place
`gzip.open(live_path, "wb")` that truncates the file before flushing, so any abnormal
termination during a save is fatal — and it could not self-heal because both the bootstrap path
and the `/health` check tested only `Path.exists()`, which a zero-byte file satisfies. Fixed in
`phaze-p3hj.2` (atomic write + loadability probe + a real `HEALTHCHECK`) — **merged to `main`
but never deployed** before this removal.

**Panako was storing almost nothing.** Its store held fingerprints for **19 audio files**
against **11,411 rows in `fingerprint_results` recorded as successful Panako ingests**
(`phaze-iq65`). The cause: `/ingest` returned `{"status": "ingested"}` whenever the ingest
subprocess exited `0`, and never verified that anything was actually written to storage.

**The common defect, and the single most important requirement on any future reinstatement:**
both engines reported success that was *inferred* — from an exit code, or from a file existing
— and never *observed*. Neither outage was caught by the health checks or the pipeline's own
success bookkeeping; both were found by manual investigation. The feature had therefore never
actually delivered value in production.

### 3. Fit — the content is the workload both engines are least suited to

The archive is multi-hour DJ sets, not discrete tracks. As measured in §1, 89.3% of files
exceed audfprint's deployed landmark horizon and the median file wraps it 9.3×; audfprint
assumes the query is at the same speed and pitch as the reference (a few percent of
time-stretch destroys the delta consistency the match depends on), and its CLI has no built-in
time-range or start-offset option, so any windowing has to be built externally. Panako's
deployed `OLAF` strategy tolerates only ±10% time-scale/pitch modification — enough for typical
DJ beatmatching (~±8%) but not more, and it is *not* the stretch-invariant "Panako" strategy the
same tool also ships. Both engines can be made to work on this content only with substantial
re-architecture (windowed precompute, a different matching strategy, or both) — re-architecture
that was never attempted because findings #1 and #2 made it not worth attempting first.

## Chromaprint — kept, and why

`chromaprint`/`fpcalc`/`libchromaprint` remain in the main application and agent Docker images.
**Verified before removal, not assumed:** this is a runtime dependency of
`essentia-tensorflow`'s native `_essentia` extension itself, wholly independent of the two
fingerprinting engines removed here — confirmed by the Dockerfiles' own comments ("without
these, `import essentia` fails at runtime and every analysis job dead-letters") and by
`docs/essentia-analysis.md`'s own prior note that audio fingerprinting was never essentia's
concern. Only `services/audfprint/` and `services/panako/` (their own separate Dockerfiles and
dependencies) were removed. `pyacoustid` was never a dependency and remains unused — it was
never wired to either engine.

## Consequences

### Accepted

- phaze has no audio-fingerprinting or fingerprint-based deduplication/identification
  capability. Tracklist identification for concert sets now relies solely on 1001tracklists
  lookup (`search_tracklist`, epic `phaze-fq9h`); the sibling `scan_live_set` audio-matching
  producer is gone, so a set with no 1001tracklists match has no fallback identification path.
- File-level deduplication is unaffected: it was always driven by the discovery-time SHA-256
  hash (`services/dedup.py`), not by either fingerprinting engine.
- The `fingerprint` agent-worker lane (2 of 6 CPU-bound concurrency slots) is retired; the
  file-server agent runs three lanes (`analyze`/`meta`/`io`) instead of four.
- Removing the Panako sidecar also moots the pre-existing AGPL-3.0-from-an-MIT-repository
  licence-exposure gap tracked at `phaze-dnso` (Panako conveyed in phaze's published container
  images with no NOTICE and no source offer) — that bead's fix is no longer applicable once no
  image bundles Panako, though closing it is `phaze-0jpe.6`'s call, not this ADR's.
- The live `audfprint`/`panako` Docker volumes on any already-deployed file server are **not**
  touched by this removal — they are the only surviving evidence for the outages and capacity
  measurements above, and their disposal is a separate, explicitly-approved operator step (see
  `docs/runbook.md`).

### Left open, for `phaze-oof3` to answer if reopened

- Whether Panako's ~51 fingerprints/second observed rate extrapolates linearly to the full
  archive was never established.
- No accuracy bar was ever measured for either engine at scale on this archive's real duration
  distribution (only the capacity and outage findings above were established).
- A capacity-safe reinstatement needs a fundamentally different storage design (not a
  bit-split retune, per §1) plus a design that *observes* rather than *infers* write success,
  per §2.

## Notes on this record

Consistent with repository practice for investigation records, no host names, filesystem paths,
or archive identifiers appear in this ADR beyond the generic container path
`/data/fprint/fprint.pklz` (a fixed path inside the audfprint image, not archive-specific).
Every measured quantity above is reproduced exactly as recorded in the source investigation.
