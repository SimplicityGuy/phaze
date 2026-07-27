# S4 — Vector storage and ANN search at phaze's archive scale

- **Bead:** `phaze-ytgo.6` (epic `phaze-ytgo` — AudioMuse-AI: clean-room vs sidecar, per purpose)
- **Date:** 2026-07-26
- **Tree:** branch `wt/bead/issue/phaze-ytgo.6`, forked at `b051b3b`
- **Depends on:** `phaze-ytgo.1` ([S1 rubric](phaze-ytgo.1-purpose-rubric.md)) for the P1..P4 bar,
  the EFB baseline and blockers B1/B2/B4; `phaze-ytgo.2`
  ([S2 embeddings](phaze-ytgo.2-essentia-embeddings.md)) for the input dimension and for open
  question **S2-O4**, which this bead was asked to close; `phaze-ytgo.5`
  ([S6 licence](phaze-ytgo.5-agpl-mit-compliance.md)) for the clean-room rule this document obeys.
- **Status:** investigation only. No product code, no migration, no `docker-compose` change, no
  `pyproject.toml` change. Every measurement script lives in the session scratchpad.

> ## Clean-room disclosure
>
> **No AudioMuse-AI source code was read while producing this document.** No `.py`, no template,
> no SQL file, no model binary from that project was opened, listed, greped or quoted.
>
> Per the replacement rule in [`phaze-ytgo.5` V1](phaze-ytgo.5-agpl-mit-compliance.md), which
> supersedes the epic's original seal, the constraint is on **output**, not input. What was read,
> and why:
>
> | Artifact | Read? | Why |
> | -------- | ----- | --- |
> | `README.md` | **yes** | Published prose. Hardware requirements and stated library scale. |
> | `docs/ARCHITECTURE.md` | **yes** | Published prose. Where the index lives and who loads it. |
> | `docs/ALGORITHM.md` ch. 4 | **yes** | Published prose. The index design and its stated goal. |
> | `docs/FAQ.md`, `docs/DEPLOYMENT.md` | **yes** | Published prose. Hardware and collection size. |
> | Any `.py` / SQL / template | **no** | Not needed, and out of bounds for this bead. |
>
> **This document reproduces no AudioMuse expression.** In particular it deliberately does **not**
> transcribe their configuration variable *names*, even though those names appear in their public
> prose and in this bead's own filed text. Per `phaze-ytgo.5` hole **H2**, short identifiers are the
> one category of prose that *is* literal expression, phaze gains nothing from adopting the
> spelling, and a systematic adoption of another project's naming scheme is exactly the pattern
> that produces bad structure-sequence-and-organisation facts. Their tuning knobs are therefore
> described **functionally** throughout — "the setting that controls how many cells a query
> probes", not the identifier. Every numeric constant that reaches a phaze recommendation below
> cites a phaze measurement or pgvector's own documentation, never AudioMuse.

______________________________________________________________________

## Question

`phaze-ytgo.2` settled that phaze's own `discogs-effnet-bs64-1` graph emits a **1280-dimension**
embedding at ~1.005 Hz for +1.6 % marginal cost. That makes the clean-room option's *production*
side nearly free. This bead asks whether its *storage and retrieval* side is equally cheap, or
whether it needs a separate ANN component — which would erode most of clean-room's "one stack"
advantage and change the answer to the epic.

Five questions:

1. **Does Postgres + pgvector carry the index at phaze's real scale**, for both row counts:
   track-level (**200,000**) and window-level (**~10.3 M**)? Measure HNSW and IVFFlat on recall@k,
   query latency, **index build time** and **on-disk size** — build time and size matter as much as
   latency, because an index that answers in 5 ms but takes half a day to build and cannot be
   updated incrementally is a different product from the one it appears to be at small N.
2. **Do `halfvec` / quantization change the answer** at window scale?
3. **Does every purpose actually need ANN?** P1 near-duplicate detection may be an all-pairs
   blocking problem rather than a top-k problem; P4 may only ever read a precomputed 2-D
   projection. Do not assume the index is load-bearing for all four.
4. **What does pgvector cost phaze's pinned `postgres:18-alpine`**, its `docker-compose.yml` and
   its CI? S1's blocker **B1** records that `vector` is not merely uninstalled but *not available*
   in that image. Is that a base-image change, or something smaller?
5. **Why did AudioMuse build its own disk-paged IVF index instead of using a database index**, and
   does that reasoning transfer to phaze or is it an artifact of their deployment envelope?

And one inherited question this bead was explicitly scoped to close:

6. **`S2-O4` — what is the quality cost of PCA 1280-d → ~200-d?** `phaze-ytgo.2` left it open and
   named it as the thing that decides whether P3 storage is ~0.6 TB or ~0.1 TB.

______________________________________________________________________

## Method

### Hardware and environment

| | |
| --- | --- |
| Benchmark host | Apple **M1 Pro**, 10 cores, 32 GiB RAM, macOS 26.5.2 arm64 |
| Postgres under test | `pgvector/pgvector:pg18` — **PostgreSQL 18.4**, **pgvector 0.8.5**, in Docker |
| **Docker VM envelope** | **2 vCPU, 8.3 GB RAM, 98 GB disk** (Docker Desktop allocation, unchanged) |
| Postgres settings | `shared_buffers=1GB`, `maintenance_work_mem=2GB`, `max_parallel_maintenance_workers=2`, `work_mem=64MB`, `jit=off`, `/dev/shm=3g` |
| Client | `psycopg` 3.3.4 over TCP to `localhost:5547`; the measured no-op round-trip floor is reported alongside every latency figure so it can be subtracted |
| Embedding extraction | `essentia-tensorflow` 2.1-beta6-dev, Python 3.14, CPU only, via `uv run` |
| Production reference host | **host-prod** — 125 GB RAM, 14 cores, **47 GB free** on a 98 %-full 1.7 TB NVMe |

**The 2 vCPU / 8.3 GB Docker envelope is the single most important caveat in this document.** It is
far smaller than the production host and it bounds what could be built at all. Every build-time
number below is therefore a *pessimistic* figure for a 14-core box and every "did not fit" is
stated as a hardware fact, not a pgvector limit. Sizes on disk, by contrast, are hardware-independent
and transfer directly.

The pgvector benchmark ran in its own dedicated container (`phaze-ytgo6-pgvector`, port 5547),
never in the shared test harness. The seat's isolated test database and Redis logical DB were
provisioned per the repo rule and are unused by the benchmark itself:

```console
$ just test-db-for vector
✅ created phaze_vector_test
✅ created phaze_vector_migrations_test
✅ allocated Redis logical DB 34 to 'vector'
```

### Corpus — REAL audio from the production archive, with provenance

The audio lives on **host-store** at `<archive-mount>` (a 29 TB array, 93 % full), which is the host
bind-mount behind `<archive-mount-in-container>` inside host-store's `phaze-agent-worker-*` containers — the path that
appears in `files.original_path` in the live database.

> **Host note for the record.** `<archive-mount-in-container>` exists on **host-store** only. `host-prod` — which runs
> `phaze-api`, `pgbouncer` and the production Postgres — has **no** `<archive-mount-in-container>` and no
> `<other-mount>/staging`; its `phaze-api` container has no such mount, and `<other-mount>` there holds
> only `postgres-dumps`. There is therefore **no** duplicate mount path across the two hosts.
> host-prod was used for exactly one thing: **read-only aggregate SQL against the production database**
> to draw the sample and to establish the archive's real shape (E2). No benchmark, no write, and
> no audio came from host-prod.

The sample was drawn **from the database, not from the filesystem**, so every file carries a real
`files.id` and a real `metadata.duration`, and the stratification is against the archive's own
duration distribution rather than a guess:

```sql
-- stratified by the archive's own duration buckets, deterministic on md5(id)
row_number() over (partition by <duration bucket> order by md5(f.id::text))
-- kept: 8 from <=600s, 14 from 600-3600s, 12 from 3600-10800s, 2 from >10800s
```

| | |
| --- | --- |
| Files | **36** (8 / 14 / 12 / 2 across the four duration buckets) |
| Audio | **42.34 hours**; durations 39 s → 27,710 s (7 h 42 m), median **3,464 s** (58 min) |
| Copied from | `host-store:<archive-mount>` via `rsync`, read-only, 3.9 GB, 102 s |
| **REAL 1 Hz patch vectors** | **153,413** × 1280-d |
| **REAL coarse-window vectors** | **627** × 1280-d (phaze's own 180 s tier, strided to cap 30) |
| **REAL track vectors** | **36** × 1280-d (two-level mean-pool, per S2 E3) |

Extraction used `phaze-ytgo.2`'s method unmodified — `discogs-effnet-bs64-1.pb`, output node
`PartitionedCall:1`, 16 kHz mono, essentia's own `TensorflowPredictEffnetDiscogs` — and phaze's
**shipped** windowing functions (`_iter_windows`, `_stride_to_cap` imported from
`src/phaze/services/analysis.py`) to derive the coarse tier, so the 627 coarse vectors are exactly
the rows `analyze_file` would emit. Measured patch rate: **1.0034–1.0066 Hz**, matching S2's 1.005 Hz.

A second, smaller REAL set was extracted first from the local spike corpus `phaze-ytgo.2` used
(**33,797** × 1280-d from a 6 h 08 m hardcore countdown, a 1 h 19 m set and 21 short tracks). It is
used only for the contiguous-decode measurement in [E3](#e3--extraction-cost-and-the-answer-to-s2-o5),
because that corpus contains the exact file S2 measured its 259 s deep seek on.

### Synthetic padding — what it is, and why it is conservative

200,000 track vectors and 10.3 M window vectors cannot be produced from 42 hours of audio. The
scale tiers are therefore padded with synthetic vectors, generated by **probabilistic PCA fitted
to the 153,413 REAL patch vectors**:

1. Fit the mean μ and the top **r = 256** eigenvectors *W* / eigenvalues λ of the real sample
   covariance.
2. Residual variance σ² = (tr Σ − Σλ) / (D − r), i.e. the variance the rank-256 basis does not
   explain, spread isotropically.
3. Sample **x = μ + W(√λ ⊙ z_r) + σ z_D**, z ~ N(0, I), seed 20260726.

Measured on the real corpus: tr Σ = 11.568, the rank-256 basis explains **90.13 %** of it,
σ = 0.0334, and the covariance **participation ratio — the effective dimensionality — is 27.8**,
against a nominal 1280. The effnet embedding lives on a low-dimensional manifold, and the generator
reproduces that.

This preserves the eigenvalue spectrum, hence the effective dimensionality, which is the dominant
driver of ANN recall. It does **not** preserve cluster structure: the output is a single unimodal
blob where real audio is strongly clustered. **Synthetic padding is therefore strictly harder for
an ANN index than real data**, which [E5](#e5--real-versus-synthetic-the-size-of-the-padding-penalty)
quantifies directly. Scale recall figures measured on padding are conservative; index size and
build time do not depend on cluster structure at all and transfer without a caveat.

**Every table below labels each row REAL or SYNTHETIC.** No recall number in this document is
computed on random vectors.

### What could NOT be measured, and why

1. **10.3 M rows at the full 1280-d could not be built.** The arithmetic is 61.5 GB of heap+TOAST
   plus 84.4 GB of HNSW index = **146 GB**, against 98 GB of Docker VM disk (55 GB free at the
   time) and 47 GB free on the production host. This tier is reported as a **measured per-row cost
   multiplied out**, not as an extrapolated curve — the per-row bytes were measured at four row
   counts and are flat to within 1 %, so the multiplication is arithmetic, not a forecast.
2. **Two HNSW builds were abandoned incomplete, and both are reported as such.** 500,000 × 1280-d
   at **> 21 min 39 s** and 10,300,000 × 200-d at **> 57 min 48 s**, both after the graph exceeded
   `maintenance_work_mem` on a 2 vCPU VM ([E9](#e9--build-memory-is-the-binding-constraint-not-query-latency)).
   Neither is a pgvector limit; both are this machine's. They are recorded as lower bounds on build
   time, never converted into an estimate of what the build *would* have taken.
3. **There is therefore no HNSW recall / latency row at the window tier.** Its *size* is known —
   the 200-d HNSW per-row cost was measured on a dedicated 1,000,000-row build (1,170 B/row
   `vector`, 745 B/row `halfvec`) — but its build time, recall and latency at 10.3 M rows were not
   obtained, and are left open as **S4-O2**.
4. **No production-hardware run.** Everything is on a 2 vCPU / 8.3 GB Docker VM against a host with
   14 cores and 125 GB RAM. Build times on production would be materially better; query latency,
   being single-threaded per query, less so. Sizes are hardware-independent.
5. **The 1,000,000-row and 2,000,000-row 1280-d ladder points were dropped** for machine time once
   the disk arithmetic had already ruled the 1280-d window tier out. The ladder therefore has three
   points (153 K, 200 K, 500 K-abandoned), not five.
6. **Quality of the *embedding* is not this bead's subject.** S1's P1/P2/P4 accuracy bars remain
   `UNMEASURED` exactly as `phaze-ytgo.2` left them; nothing here closes them. What this document
   measures is the **retrieval-fidelity** question — does the index return what an exact scan would
   return — which is a different and narrower thing.
7. **`analysis_window` counts come from a partially-analysed archive.** Only 1,886 of 11,428 files
   have window rows (2,614 have an `analysis` row). The per-file row counts in E2 are real, but they
   are an average over the analysed subset, not the whole archive.
8. **Binary quantization (`bit` + Hamming + re-rank) was not measured at all** — see **S4-O1**. It
   is the one representation that might put the window tier inside the disk envelope at full
   dimensionality, and its absence is the largest single gap in this document.

______________________________________________________________________

## Evidence

### E1 — pgvector on the pinned image: a 3-line Dockerfile, not a base-image change

**S1's blocker B1 is confirmed on its facts and materially softened on its conclusion.**

Confirmed first, on the **live production database**, not just a test container:

```console
$ # read-only, against host-prod's production phaze DB
  select name, installed_version from pg_available_extensions where name in ('vector','cube')
cube|                       ← available, not installed
                            ← 'vector' returns NO ROW: not available
$ select version()
PostgreSQL 18.4 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit
```

So B1's fact holds in production as well as in the harness. But B1 concludes "adding pgvector is
therefore a **base-image change**, not a `CREATE EXTENSION`", and that conclusion is **wrong** —
Alpine ships the extension, it just installs to the wrong prefix for this particular image.

Alpine 3.24 community (the base of `postgres:18-alpine`) carries `postgresql-pgvector-0.8.1-r0`,
built against `postgresql18`. It installs to `/usr/lib/postgresql18` and
`/usr/share/postgresql18/extension`, whereas the Docker image compiles PostgreSQL from source into
`/usr/local`. That prefix mismatch — and nothing else — is why a bare `apk add` leaves
`CREATE EXTENSION vector` reporting *"extension is not available"*. Copying the two artifacts into
the paths `pg_config` reports fixes it:

```dockerfile
FROM postgres:18-alpine
RUN apk add --no-cache postgresql-pgvector=0.8.1-r0 \
 && cp /usr/lib/postgresql18/vector.so "$(pg_config --pkglibdir)/" \
 && cp /usr/share/postgresql18/extension/vector* "$(pg_config --sharedir)/extension/"
```

Verified end to end on that image — extension created, both column types accepted at phaze's real
dimension, both index types built:

```console
$ docker exec … psql -tAc "create extension vector; select extversion …"
0.8.1
CREATE TABLE      -- vector(1280)
CREATE INDEX      -- hnsw (v vector_cosine_ops)
CREATE TABLE      -- halfvec(1280)
CREATE INDEX      -- hnsw (v halfvec_cosine_ops)
```

**Cost:** the image goes **425 MB → 456 MB (+31 MB, +7.3 %)**. It stays Alpine, stays
`postgres:18`, and keeps the major-version data-directory layout the compose file already
documents. For contrast, the upstream `pgvector/pgvector:pg18` image is Debian-based and **650 MB**
(+225 MB over the pin, and a libc change).

#### What has to change, concretely

| Where | Change | Note |
| ----- | ------ | ---- |
| **New file** — e.g. `docker/postgres/Dockerfile` | The 3 lines above | Pin the `=0.8.1-r0` version explicitly; an unpinned `apk add` silently floats |
| `docker-compose.yml:79` | `image: postgres:18-alpine` → the published phaze image | **8** further `postgres:18-alpine` occurrences in `justfile` (lines 234, 241, 414, 420, 424, 430, 777, 780) must move together or the harness diverges from production. **Only 4 are real `docker run` arguments** (241, 420, 430, 780); the other **4 are `echo` strings** (234, 414, 424, 777) that merely *claim* the version — so a partial bump prints the old tag while running the new one, which is the failure mode that survives casual review |
| `docker-compose.yml` postgres service | **add `shm_size:` ≥ `maintenance_work_mem`** | Not optional — see below |
| `.github/workflows/tests.yml:39` | `services.postgres.image` | GitHub Actions **service containers cannot be built**; they can only reference a published image. This forces phaze to publish its own image (it already publishes to GHCR — `just image-push`) or to adopt `pgvector/pgvector:pg18` in CI and diverge from production |
| Alembic | one migration whose first statement is `CREATE EXTENSION IF NOT EXISTS vector` | Requires superuser or a pre-granted extension; on a managed Postgres this is the step that fails |

**The `shm_size` requirement was found the hard way and is worth stating loudly.** pgvector's
*parallel* HNSW build allocates a dynamic shared-memory segment sized by `maintenance_work_mem`,
which on Docker lands in `/dev/shm` — default **64 MB**. With `maintenance_work_mem=4GB` every
parallel HNSW build failed identically:

```
ERROR:  could not resize shared memory segment "/PostgreSQL.285100124" to 4291858144 bytes:
        No space left on device
```

This is not a disk error and not a RAM error; it is the container's `/dev/shm`. phaze's compose
file sets no `shm_size` today, so **any HNSW index build with a non-trivial `maintenance_work_mem`
will fail on a stock phaze deployment** until it does. Every measurement below was re-run after
setting `--shm-size=3g` with `maintenance_work_mem=2GB`.

**Version note.** Alpine's package is pgvector **0.8.1**; the upstream image is **0.8.5**. All
measurements below are on 0.8.5. A multi-stage source build (`make && make install` from a pinned
tag into the `postgres:18-alpine` prefix) would let phaze pin an exact version rather than track
Alpine's; that path was not measured here.

### E2 — the real archive, which is bigger and far set-heavier than the molecule assumed

Read-only aggregates against the **live production database** on host-prod (2026-07-26). This closes
S1's open questions **O1** and **O2** with measurements instead of estimates.

| | measured |
| --- | --- |
| `files` | **11,428** |
| `metadata` rows with a duration | 11,412 |
| Total audio | **11,492 hours** |
| Mean duration | **3,625 s (60 min)** |
| Duration p05 / p25 / **p50** / p75 / p95 | 224 / 2,647 / **3,532** / 3,916 / 7,277 s |
| Max duration | 43,467 s (12 h 04 m) |
| Files > 600 s | **9,808 (85.9 %)** |
| Files > 3,600 s | **4,497 (39.4 %)** |
| `analysis` rows | 2,614 |
| `analysis_window` rows | **134,768** — 102,073 fine + 32,695 coarse, over **1,886** files |
| Whole `phaze` database today | **212 MB** |

Three consequences, in ascending order of importance:

1. **S1's O1 answered.** `analysis_window` averages **71.5 rows/file** on analysed files
   (54.1 fine + 17.3 coarse), against S1's code-derived worst-case ceiling of 90. At 200,000 files
   that is **14.3 M window rows** total, or **3.47 M** if only the coarse tier carries an embedding
   — which is what `phaze-ytgo.2` recommends (its E3: the fine tier is 44.1 kHz and neural-free, so
   embeddings there are a new cost, not a marginal one). *The bead's mandated **10.3 M** window
   tier sits between these two and is what is benchmarked below.*
2. **S1's O2 answered, and it moves P3 by an order of magnitude.** S1 carried an illustrative
   "5 % of the archive is multi-hour sets". The real figure is **39.4 % over an hour and 85.9 %
   over ten minutes**, with a **60-minute mean**. This archive is not a track collection with some
   sets in it; it is a set collection with some tracks in it.
3. **Striding is far less severe than S1 feared — for the coarse tier.** Of 1,822 completed
   analyses, 1,570 (86 %) are flagged `sampled`, but coarse coverage is
   `32,205 / 33,027 = **97.5 %**` and fine coverage `99,316 / 193,004 = **51.5 %**`. A 60-minute
   set has 20 natural coarse windows against a cap of 30, so the coarse cap barely bites. S1's E3
   objection to using existing window rows for P3 stands on its *other* leg — 180 s coarse windows
   are longer than the tracks they would have to separate — but the coverage leg is much weaker
   than S1's worst-case arithmetic suggested.

#### The row counts every tier below is sized against

| tier | rows at 200,000 files | derivation |
| ---- | --------------------- | ---------- |
| **Track** | **200,000** | one vector per file |
| **Window (coarse only)** | 3.47 M | 17.3 coarse rows/file (E2) |
| **Window (the bead's tier)** | **10.3 M** | the count this bead was told to benchmark as primary |
| Window (all `analysis_window`) | 14.3 M | 71.5 rows/file (E2) |
| **P3 contiguous, 10 s hop** | **72.5 M** | 200,000 × 3,625 s ÷ 10 |
| **P3 contiguous, native 1 Hz** | **725 M** | 200,000 × 3,625 s × 1.005 Hz |

The last two are the numbers S1 and S2 approximated at ~1.1 × 10⁷ from a 5 %-sets assumption. On
the archive's real duration distribution, P3's contiguous ask is **7× larger at a 10 s hop and 70×
larger at the native rate** than the molecule has been carrying.

### E3 — extraction cost, and the answer to S2-O5

`phaze-ytgo.2` left open (**S2-O5**) whether a single contiguous decode of a long set is cheaper
than the ≤ 30 strided deep seeks phaze performs today, having observed 259 s to decode one 600 s
excerpt at offset 3,600 s inside a 6 h mp3.

**Measured, on the same file S2 used, decoded whole and sequentially:**

| file | duration | **contiguous decode** | patches | embed | embed / audio-min |
| ---- | -------- | --------------------- | ------- | ----- | ----------------- |
| 6 h 08 m hardcore countdown (S2's own `S2`) | 22,098 s | **19.93 s** | 22,209 | 129.1 s | 0.350 s |
| 1 h 19 m set (S2's own `S1`) | 4,721 s | 4.75 s | 4,745 | 16.4 s | 0.208 s |
| 36 production files (E-corpus) | 152,400 s | 143.1 s total | 153,413 | 552.5 s | **0.193 s** (median) |

**S2-O5 is answered: yes, decisively.** A single sequential `ffmpeg` decode of the 6 h file to
16 kHz mono took **19.9 s** — against the **259 s** S2 measured for *one* 600 s deep-seek excerpt
from the same file. Contiguous decode runs at a median **3.5 s per audio-hour**. Deep seeking into
a long VBR mp3, not decoding, is what was expensive. A contiguous P3 pass is cheaper on the decode
axis than what phaze already does today, exactly as S2 suspected.

Embedding inference costs a median **0.193 s per audio-minute** (S2 measured 0.17 s; the 13 %
difference is machine load, three sibling spikes having run concurrently for S2 and one for this
one). At 200,000 files × 3,625 s that is **1,340 CPU-hours** for a full contiguous 1 Hz pass over
the archive — a real number for D1, and one that dwarfs the index build times below.

### E4 — S2-O4 answered: PCA 1280-d → 200-d costs 16 % of the true top-10

Measured on **REAL** vectors. Ground truth is exact cosine top-10 in the full 1280-d space; each
row re-runs exact search inside a PCA subspace of the given rank and reports the overlap.

**Corpus A — 153,213 REAL 1 Hz patch vectors, 200 held-out real queries:**

| target dim | explained variance | **recall@10 vs 1280-d exact** | top-1 agreement |
| ---------- | ------------------ | ----------------------------- | --------------- |
| **1280** (no reduction) | 1.000 | **1.000** | 1.000 |
| 512 | 0.955 | 0.878 | 0.855 |
| 256 | 0.901 | 0.851 | 0.815 |
| **200** | **0.879** | **0.836** | **0.775** |
| 128 | 0.833 | 0.802 | 0.740 |
| 64 | 0.755 | 0.744 | 0.690 |
| 32 | 0.670 | 0.637 | 0.535 |

**Corpus B — 471 REAL coarse-window vectors, 156 real queries** (small n; reported because the
coarse tier is the one S2 recommends carrying embeddings):

| target dim | explained variance | recall@10 vs 1280-d exact | top-1 agreement |
| ---------- | ------------------ | ------------------------- | --------------- |
| 512 | 1.000 | 0.849 | 0.795 |
| 256 | 0.992 | 0.851 | 0.776 |
| **200** | **0.985** | **0.853** | 0.782 |
| 128 | 0.965 | 0.853 | 0.724 |
| 64 | 0.919 | 0.838 | 0.686 |

**Three readings.**

1. **PCA to 200-d is not free: it loses ~16 % of the true top-10 and misplaces the nearest
   neighbour on ~22 % of queries.** For a purpose whose output a human reviews (P2 browsing, P4
   map) that is probably tolerable. For P1, where the whole question is whether *this specific*
   pair is a near-duplicate, a 22 % top-1 disagreement is not a rounding error.
2. **Most of the loss is paid immediately and then flattens.** Going 1280 → 512 already costs 12
   points of recall; 512 → 200 costs only another 4. There is no cheap intermediate: if you reduce
   at all, 200-d costs barely more than 512-d and saves 2.5× the space. **If the answer is to
   reduce, reduce properly.**
3. **Explained variance badly overstates retrieval fidelity, and this is the trap.** At 200-d the
   projection retains 88 % of the variance and 84 % of the neighbours; on the coarse corpus it
   retains **98.5 %** of the variance and still only **85 %** of the neighbours. The effective
   dimensionality is 27.8, so variance concentrates in a handful of directions while neighbour
   *ordering* is decided by the long tail. Anyone sizing this from a scree plot will get it wrong.

### E5 — real versus synthetic: the size of the padding penalty

Same row count, same index parameters, same query protocol; only the data distribution differs.
**N = 153,203, dim 1280, cosine, HNSW `m=16 ef_construction=64`, IVFFlat `lists=153`.**

| | HNSW build | ef 40 | ef 100 | ef 200 | IVF build | probes 5 | probes 10 | probes 20 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **REAL** (153,203 real 1 Hz vectors) | **77.2 s** | **0.937** | **0.991** | **0.999** | 14.9 s | 0.912 | 0.983 | 0.996 |
| **SYNTHETIC** (PPCA padding, same N) | 193.9 s | 0.837 | 0.923 | 0.955 | 14.7 s | 0.751 | 0.923 | 0.986 |

*(cells are recall@10)*

**The padding is 2.5× more expensive to index and ~10 recall points harder at every operating
point.** That is the design working as intended: the generator preserves the eigenvalue spectrum
but destroys cluster structure, and clustered data is exactly what HNSW and IVFFlat exploit. It
also means the scale tiers below, which are necessarily synthetic, **understate** the recall phaze
would actually get and **overstate** the build time. They are conservative, and the direction of
the bias is known.

It also gives the headline recall number of this document, on real data:

> **On 153,203 REAL production-archive embeddings, pgvector HNSW returns 99.1 % of the exact
> top-10 at a p50 of 2.4 ms and a p95 of 3.2 ms**, against a client round-trip floor of 0.45 ms.

### E6 — the benchmark matrix

Cosine throughout (`vector_cosine_ops` / `halfvec_cosine_ops`, `<=>`). k = 10. 210 queries per
cell, first 10 discarded as warm-up, held out of the indexed set. HNSW `m=16 ef_construction=64`
(pgvector defaults); IVFFlat `lists = N/1000` up to 1 M and `√N` above, per pgvector's own
guidance. The client round-trip floor was 0.25–0.75 ms in every cell and is **included** in the
latency figures — subtract it for a server-side number.

#### Tier 0 — REAL data control, 153,203 REAL 1 Hz production embeddings, 1280-d

**This is the only recall measurement in the document taken on real vectors, and it is the one to
quote.**

| index | build | data | index size | param | **recall@10** | p50 | p95 |
| ----- | ----- | ---- | ---------- | ----- | ------------- | --- | --- |
| HNSW | **77.2 s** | 0.92 GB | 1.25 GB | ef 40 | **0.937** | 1.67 ms | 2.24 ms |
| | | | | **ef 100** | **0.991** | **2.36 ms** | **3.18 ms** |
| | | | | ef 200 | **0.999** | 3.97 ms | 5.34 ms |
| IVFFlat | 14.9 s | 0.92 GB | 1.26 GB | probes 1 | 0.496 | 2.50 ms | 4.63 ms |
| | | | | probes 5 | 0.912 | 12.49 ms | 15.19 ms |
| | | | | probes 10 | 0.983 | 21.88 ms | 24.92 ms |
| | | | | probes 20 | 0.996 | 40.70 ms | 43.14 ms |
| | | | | probes 50 | 1.000 | 83.16 ms | 87.82 ms |

*(This tier and its synthetic twin in E5 were measured at `shared_buffers=2GB`; every other tier at
1 GB. Recall is unaffected; latency would move by a few percent.)*

#### Tier 1 — TRACK, 200,000 rows, 1280-d, SYNTHETIC padding

| type | index | build | data | index size | **total** | param | recall@10 | p50 | p95 |
| ---- | ----- | ----- | ---- | ---------- | --------- | ----- | --------- | --- | --- |
| `vector` | HNSW | 145.8 s | 1.19 GB | 1.64 GB | **2.83 GB** | ef 40 | 0.824 | 5.09 ms | 7.27 ms |
| | | | | | | ef 100 | 0.914 | 5.67 ms | 7.31 ms |
| | | | | | | ef 200 | 0.950 | 9.38 ms | 12.90 ms |
| `vector` | IVFFlat | 17.4 s | 1.19 GB | 1.64 GB | 2.84 GB | probes 10 | 0.902 | 26.57 ms | 34.28 ms |
| | | | | | | probes 20 | 0.978 | 47.89 ms | 57.00 ms |
| | | | | | | probes 50 | 0.999 | 112.20 ms | 130.55 ms |
| **`halfvec`** | **HNSW** | **94.9 s** | **0.57 GB** | **0.82 GB** | **1.39 GB** | ef 40 | 0.825 | **2.85 ms** | **3.45 ms** |
| | | | | | | **ef 100** | **0.912** | **4.30 ms** | **5.41 ms** |
| | | | | | | ef 200 | 0.942 | 6.62 ms | 7.97 ms |
| `halfvec` | IVFFlat | **7.9 s** | 0.57 GB | 0.55 GB | 1.11 GB | probes 10 | 0.904 | 9.44 ms | 12.16 ms |
| | | | | | | probes 20 | 0.975 | 17.89 ms | 26.14 ms |
| | | | | | | probes 50 | 0.997 | 37.90 ms | 45.21 ms |

**`halfvec` is free.** Recall tracks the float32 arm to within **0.008 at every one of the eight
operating points**, while halving the data, halving the HNSW index, cutting build time by 35 % and
cutting query latency by 1.8×. This is the clearest single result in the document.

#### Tier 2 — WINDOW, **10,300,000 rows**, PCA-200, SYNTHETIC padding

The bead's primary tier. At the native 1280-d this tier is 146 GB and could not be built (see the
verdict table); at PCA-200 it builds and answers.

| type | index | build | data | index size | **total** | param | recall@10 | p50 | p95 |
| ---- | ----- | ----- | ---- | ---------- | --------- | ----- | --------- | --- | --- |
| `vector` | IVFFlat `lists=3209` | **1,102.9 s (18.4 min)** | 9.38 GB | 9.39 GB | **18.77 GB** | probes 10 | 0.728 | 34.95 ms | 92.91 ms |
| | | | | | | probes 20 | 0.856 | 51.69 ms | 73.27 ms |
| | | | | | | probes 50 | 0.957 | 121.03 ms | 233.00 ms |
| | | | | | | probes 100 | 0.990 | 227.66 ms | 284.13 ms |
| **`halfvec`** | IVFFlat `lists=3209` | **813.9 s (13.6 min)** | **4.69 GB** | **4.45 GB** | **9.14 GB** | probes 10 | 0.735 | 27.04 ms | 103.02 ms |
| | | | | | | **probes 20** | **0.862** | **31.46 ms** | **42.65 ms** |
| | | | | | | probes 50 | 0.955 | 74.92 ms | 102.36 ms |
| | | | | | | probes 100 | 0.985 | 140.02 ms | 183.14 ms |
| `vector` | **HNSW** | **> 57 min 48 s — abandoned** | 9.38 GB | 12.05 GB\* | 21.4 GB\* | — | — | — | — |
| `halfvec` | HNSW | not attempted | 4.69 GB | 7.67 GB\* | 12.4 GB\* | — | — | — | — |
| `vector` | IVFFlat at default `maintenance_work_mem=2GB` | **REFUSED**: *"memory required is 2136 MB"* | — | — | — | — | — | — | — |

*\* HNSW sizes are the measured 200-d per-row cost (below) multiplied out — the size is known, the
build time and recall at this row count are not.*

*(Both IVFFlat rows were built at `maintenance_work_mem = 3GB`, above the 2,136 MB pgvector
demands. HNSW was abandoned at 57 min 48 s after the graph — ~13 GB at 200-d — exceeded the 2 GB
budget on a 2 vCPU VM and took the on-disk path; see [E9](#e9--build-memory-is-the-binding-constraint-not-query-latency). On a host that can hold the graph the picture would differ, and S4-O2 records that as open.)*

**The headline:** **10.3 M window rows fit in 9.14 GB with `halfvec` at PCA-200** — **19 % of the
production host's free disk** — build in **13.6 minutes**, and answer at **recall 0.86, p50 31 ms,
p95 43 ms**. On synthetic padding, which E5 shows understates real recall by ~10 points.

#### Per-row cost — measured, and flat across every row count tested

These are what the verdict table multiplies out. They did not vary by more than 1 % between 153 K
and 10.3 M rows.

| dim | type | data B/row | HNSW index B/row | IVFFlat index B/row | raw B/row | data overhead |
| --- | ---- | ---------- | ---------------- | ------------------- | --------- | ------------- |
| 1280 | `vector` | **5,975** | **8,192** | 8,200 | 5,120 | 1.17× (TOASTed — `attstorage` is `e`/EXTERNAL) |
| 1280 | `halfvec` | **2,831** | **4,096** | 2,736 | 2,560 | 1.11× (TOASTed) |
| 200 | `vector` | **911** | **1,170** | 912 | 800 | 1.14× (inline, no TOAST) |
| 200 | `halfvec` | **455** | **745** | 432 | 400 | 1.14× (inline, no TOAST) |

*(The 200-d HNSW figures come from a dedicated 1,000,000-row build — the tier where the graph still
fits the 2 GB budget: `vector` 296.3 s for a 1.17 GB graph, `halfvec` 215.6 s for a 0.74 GB graph.)*

**A 1280-d vector is 5,120 bytes and is therefore TOASTed** — pushed out of line into the TOAST
relation, uncompressed (pgvector sets `EXTERNAL`). `pg_relation_size` reports only 8–11 MB for a
200,000-row table whose vectors occupy 1.2 GB; anyone sizing this tier with `pg_relation_size`
instead of `pg_total_relation_size` will be wrong by two orders of magnitude. At 200-d the vectors
are 800 bytes and stay inline.

### E7 — is an ANN index required at all? The exact alternative, measured

Measured on **REAL** vectors, on the 10-core host (numpy/Accelerate BLAS, everything resident in
RAM — a *lower bound* for any database seq scan).

**Exact all-pairs top-10, blocked, retaining only top-k:**

| n | dim | seconds | effective Gflop/s |
| - | --- | ------- | ----------------- |
| 20,000 | 1280 | 2.57 | 397.8 |
| 50,000 | 1280 | 18.93 | 338.0 |
| 100,000 | 1280 | 86.28 | 296.7 |
| 150,000 | 1280 | 202.15 | 284.9 |

O(n²) exactly, as it must be. Extrapolating the measured 150,000-row time by the square:

| target | exact all-pairs cost |
| ------ | -------------------- |
| **200,000 track vectors (P1's actual ask)** | **≈ 6 minutes** |
| 3.47 M coarse-window vectors | ≈ 30 hours |
| 10.3 M window vectors | ≈ 11 days |
| 725 M contiguous 1 Hz vectors | ≈ 160 years |

**Single exact query** over 153,413 REAL 1280-d vectors, entire matrix resident in RAM:
**p50 32.3 ms, p95 39.4 ms**. Scaled to 200,000 rows that is ~42 ms; at 10.3 M rows, ~2.2 s.

**Neighbourhood structure of the real embedding** (500 real queries, cosine distance to the k-th
neighbour):

| | p05 | p50 | p95 |
| --- | --- | --- | --- |
| nearest neighbour | 0.0080 | **0.0256** | 0.1056 |
| 10th neighbour | 0.0165 | 0.0547 | 0.2035 |
| 100th neighbour | 0.0581 | 0.1298 | 0.3074 |
| random pair | — | **0.5596** | — |

A 22× gap between the median nearest-neighbour distance and the median random-pair distance. This
is a well-separated, strongly clustered space — which is why E5's real-data recall is so much
better than its synthetic control, and why a distance *threshold* is a meaningful object here at
all.

### E8 — AudioMuse's index choice, from their published prose only

Sourced from `docs/ALGORITHM.md` ch. 4 ("Similarity Indexes (disk-paged IVF)"),
`docs/ARCHITECTURE.md`, `README.md` and `docs/FAQ.md`. No source code was read. Their tuning knobs
are described functionally, never by identifier.

**First, a correction to this bead's own premise.** The bead asks why AudioMuse chose "a **Voyager**
disk-paged IVF index". The string *Voyager* does not appear anywhere in their current published
documentation, and neither does any other named third-party ANN library. What ch. 4 describes is a
**hand-rolled IVF** whose cells are stored as rows in PostgreSQL and memory-mapped by the web
process at load time. If a Voyager dependency existed historically it is not what they document
today, and the comparison below is against what they actually publish.

**What they describe, in their own words for the parts that matter:**

- **The stated design goal**, verbatim: *"a very large library stays queryable on ordinary
  hardware: memory use is bounded both while building and while querying."*
- **Build**: embeddings are streamed out of PostgreSQL with a server-side cursor in batches *"so
  the whole library is never in RAM at once"*; k-means over a sample produces coarse centroids;
  each vector is assigned to its nearest centroid; oversized cells are split; cells are written
  back to PostgreSQL incrementally *"with `STORAGE EXTERNAL` so PostgreSQL does not try to compress
  vector data"*.
- **Quantization**: vectors are stored at a configurable precision whose **default is int8**, with
  half- and single-precision available. *"Smaller means less RAM and less IO."*
- **Query**: at load time each index is exported to a local cell file and memory-mapped *"so
  queries are served from the OS page cache instead of a PostgreSQL round trip per cell"*; a query
  probes the N nearest centroids and reads only those cells; because int8 is *"only a coarse
  stage"*, the query over-fetches and **re-ranks against exact float32 vectors read from the source
  embedding table**, so the final ordering matches full precision. Two cache layers sit in front,
  the process-wide one is capped, and resident pages are released after an idle interval.
- **Rebuild model**: indexes are rebuilt in full by the workers periodically during an analysis run
  and again at the end, then a message on a pub/sub channel makes the web process swap them in
  without a restart.
- **Six** such indexes exist over different vector spaces (audio, text, lyrics, lyrics-axes, a
  fused one, artist), plus two 2-D projections.
- **Their target hardware**, from `README.md` and `docs/FAQ.md`: *"RAM: 8 GB"* minimum, reference
  machines an i5-6500 mini PC with 16 GB or a Raspberry Pi 5 with 8 GB; and they expect
  *"big collections (100k+ songs)"* where analysis of *"1 week+ … can be totally normal"*.

**Why they built it, and how much of that reasoning transfers to phaze.** Five drivers, scored:

| # | Their driver (from the prose) | Applies to phaze? |
| - | ----------------------------- | ----------------- |
| **1** | **They cannot assume the database has an ANN extension.** They ship Docker Compose, Kubernetes, *and* native macOS/Windows/Linux desktop builds against whatever PostgreSQL the user brings. A hard dependency on a compiled Postgres extension would break a large fraction of their deployment matrix. | **No — this is the big one.** phaze pins and builds its own Postgres image (E1). It has exactly one supported topology, and it already publishes images to GHCR. The constraint that most forces their design simply is not phaze's. |
| **2** | **8 GB RAM is the design point.** An index that must stay graph-resident is the wrong shape for a Raspberry Pi; a disk-paged, int8-quantized, mmap'd, cache-capped, memory-returning IVF is exactly the right shape. | **No.** phaze's production host has **125 GB RAM and 14 cores** (host-prod). phaze's binding constraint is **disk — 47 GB free on a 98 %-full volume** — which is a *different* constraint that happens to push in a similar direction at window scale. Same conclusion, different reason; do not import the reasoning. |
| **3** | **Six indexes over different spaces amortize one framework.** Building a general IVF once and pointing it at six vector spaces is a good trade at six. | **No.** phaze needs **one**. The amortization argument inverts: one bespoke index is all cost and no reuse. |
| **4** | **int8 storage with float32 re-rank.** A genuinely good technique: coarse search in a quantized space, exact re-rank against the source vectors, so final ordering matches full precision. | **Yes, and phaze gets it natively.** This is the same idea pgvector packages as `halfvec` / binary quantization plus a re-ranking subquery. E6 measures `halfvec` doing exactly this job. Their reasoning about quantization transfers completely; their *implementation* of it is what phaze does not need. |
| **5** | **Full periodic rebuild, hot-swapped via pub/sub.** Necessary because an in-process mmap'd IVF has no incremental insert path. | **No — and this is where phaze is strictly ahead.** pgvector's HNSW accepts inserts into a live index inside the same transaction as the row, with no rebuild, no swap and no pub/sub channel. E6 measures the incremental cost. |

**Verdict on the comparison:** their design is a well-reasoned response to constraints phaze does
not have — an unknown-Postgres deployment matrix, an 8 GB memory target and six vector spaces.
**Four of the five drivers are artifacts of their stack.** The fifth, quantization with exact
re-rank, is a real technique phaze should adopt — and can adopt without adopting anything of theirs,
because pgvector ships it. The one thing worth borrowing wholesale is the *insight* that at large N
the binding constraint is bytes, not FLOPs; E6 confirms that for phaze on a different resource.

### E9 — build memory is the binding constraint, not query latency

Three independent measurements say the same thing, and it is the single most operationally
important finding in this document.

**(a) HNSW build time falls off a cliff when the graph exceeds `maintenance_work_mem`.** The graph
size is not a mystery — it is exactly the index size the finished build reports, which E6 measures
at a flat **8,192 B/row at 1280-d `vector`** and **4,096 B/row at 1280-d `halfvec`**. Measured, all
at `maintenance_work_mem = 2GB` on 2 vCPU:

| N | dim | type | HNSW graph (= index size) | vs `maintenance_work_mem` | build |
| - | --- | ---- | ------------------------- | ------------------------- | ----- |
| 153,203 | 1280 | `vector` | 1.25 GB | **fits** | **77 s** (REAL) / 194 s (SYNTHETIC) |
| 200,000 | 1280 | `vector` | 1.64 GB | **fits** | **146 s** |
| 200,000 | 1280 | `halfvec` | 0.82 GB | **fits** | **95 s** |
| 1,000,000 | 200 | `halfvec` | 0.74 GB | **fits** | **216 s** |
| 1,000,000 | 200 | `vector` | 1.17 GB | **fits** | **296 s** |
| 500,000 | 1280 | `vector` | 4.10 GB (projected) | **2.0× over** | **> 21 min 39 s — abandoned, incomplete** |
| 10,300,000 | 200 | `vector` | 12.05 GB (projected) | **5.9× over** | **> 57 min 48 s — abandoned, incomplete** |

The cliff is unmistakable once the rows are read in graph-size order rather than row-count order.
**1,000,000 rows at 200-d build in 296 seconds** because the 1.17 GB graph fits the budget;
**10,300,000 rows at the same 200-d — 10.3× the rows, so ~6 minutes on any reasonable scaling law —
had not finished at 57 minutes 48 seconds**, because its 12.05 GB graph does not. Likewise the
200,000 → 500,000 step at 1280-d is 2.5× the rows, where a linear build predicts ~6 minutes and an
*n log n* build ~7; it had not finished at 21 minutes 39 seconds. Meanwhile **IVFFlat on the
identical 10.3 M table finished in 13.6 minutes** (E6), because its build streams rather than
holding a graph.

Crossing `maintenance_work_mem` switches pgvector to a two-phase on-disk build, and on 2 vCPU that
is not a constant factor anyone can plan around. Both abandoned builds are **lower bounds on the
true build time**, not estimates of it — neither is a pgvector limit. On a host that can hold the
graph resident (host-prod has 125 GB of RAM) the picture is expected to look like the sub-threshold rows
above, and **S4-O2** records that expectation as untested.

Both were abandoned to free the machine, so both are **lower bounds on the true build time**, not
estimates of it. Neither is a pgvector limit — on a host that can hold the graph resident (host-prod has
125 GB of RAM) the picture is expected to look like the sub-threshold rows above, and **S4-O2**
records that expectation as untested.

**(b) IVFFlat refuses outright, with an exact number.** At the window tier the default build did
not degrade — it failed:

```
ERROR:  memory required is 2136 MB, maintenance_work_mem is 2048 MB
```

10.3 M rows at 200-d with `lists = √N = 3209` needs **2,136 MB**. That is a *hard* precondition,
not a performance note, and it is trivially satisfiable — but only if someone knows to set it.
The measurement was re-run at `maintenance_work_mem = 3GB` to obtain the row in E6.

**(c) `/dev/shm` must be sized to `maintenance_work_mem`** or the parallel HNSW build dies
([E1](#e1--pgvector-on-the-pinned-image-a-3-line-dockerfile-not-a-base-image-change)). phaze's
compose file sets no `shm_size`.

**Why this matters more than latency.** Every query number in E6 is comfortably inside S1's
budgets. None of the failures in this document is a latency failure. **All three are build-side
resource failures**, and all three are invisible at the 11,428-file scale phaze runs today: a
200,000-row track index builds in 146 s inside a 2 GB budget and gives no warning at all that
500,000 rows will not.

**The rule that falls out, and it is a formula rather than a number:**

> `maintenance_work_mem` must exceed **N × (index bytes per row)**, and `/dev/shm` must be at least
> as large again. The multiplier is measured and flat (E6):
>
> | dim | `vector` | `halfvec` |
> | --- | -------- | --------- |
> | 1280 | 8,192 B/row | 4,096 B/row |
> | 200 | 1,170 B/row | 745 B/row |
>
> Worked example — the two tiers that matter: the **track** index needs **1.6 GB** (1280-d
> `vector`) or **0.8 GB** (`halfvec`); the **window** index at PCA-200 needs **12.1 GB** or
> **7.7 GB**. Production (host-prod, **125 GB RAM**) satisfies all four comfortably. The 8.3 GB benchmark
> VM satisfies only the track pair, which is exactly why the two window-tier HNSW builds above did
> not complete.

### E10 — the index-free floor, and incremental insert

**(a) Without an index, Postgres misses S1's interactive budget at every tier.** Exact seq scan,
`enable_indexscan = off`, same query shape, same client:

| N | dim | type | **p50** | p95 | vs S1's 200 ms P2 budget |
| - | --- | ---- | ------- | --- | ------------------------ |
| 200,000 | 1280 | `vector` | **538.4 ms** | 592.8 ms | **2.7× over — fails** |
| 200,000 | 1280 | `halfvec` | **196.1 ms** | 207.0 ms | **at the line; p95 just over — fails** |
| 1,000,000 | 200 | `vector` | 205.9 ms | 213.1 ms | over |
| 10,300,000 | 200 | `vector` | **3,757 ms** | — | **19× over** |

For contrast, the same 200,000-row `halfvec` table **with** an HNSW index answers at
**p50 4.3 ms / p95 5.4 ms** — a 45× improvement, and the difference between a search-as-you-type
palette and a spinner. **P2 is the one purpose that genuinely needs the index**, and E7's numbers
show equally clearly that P1 and P4 do not: their shapes are a 6-minute batch all-pairs pass and a
2-column projection read, neither of which is an ANN query.

**(b) Incremental insert works, and costs ~9 ms per row.** 10,000 rows appended into a live index
over 200,000 existing rows, compared against rebuilding the whole index:

| existing rows | dim | type | index | **inserted** | per row | full rebuild | insert as % of rebuild |
| ------------- | --- | ---- | ----- | ------------ | ------- | ------------ | ---------------------- |
| 200,000 | 1280 | `vector` | HNSW | 10 K in 90.3 s | **9.0 ms** | 137.4 s | 66 % |
| 200,000 | 1280 | `vector` | IVFFlat | 10 K in 1.55 s | 0.16 ms | 17.9 s | 8.7 % |
| **200,000** | **1280** | **`halfvec`** | **HNSW** | **10 K in 46.4 s** | **4.6 ms** | 93.1 s | 50 % |
| 200,000 | 1280 | `halfvec` | IVFFlat | 10 K in 0.65 s | 0.07 ms | 7.3 s | 8.9 % |
| 1,000,000 | 200 | `vector` | HNSW | 20 K in 72.4 s | **3.6 ms** | 297.1 s | 24 % |
| 1,000,000 | 200 | `vector` | IVFFlat | 20 K in 1.33 s | 0.07 ms | 50.0 s | 2.7 % |

**Read this per row, not per batch.** HNSW insert is expensive *relative to a rebuild* — adding
5 % of the table costs half to two-thirds of rebuilding it, because each insert runs a full graph
search — but in absolute terms it is **4.6 ms per row on `halfvec`**, which disappears inside an
ingest pipeline where files arrive one at a time behind an essentia analysis that already costs
tens of seconds per file. **phaze never has to rebuild.** IVFFlat inserts are ~70× cheaper still,
but IVFFlat does not re-cluster on insert, so its recall drifts as the archive grows and it
*does* eventually need a rebuild — which is precisely the periodic-rebuild-and-hot-swap machinery
an in-process index forces unconditionally (E8, driver 5).

______________________________________________________________________

## Verdict

**Postgres + pgvector carries the track tier outright. It carries the bead's 10.3 M window tier
too, but only after a dimensionality reduction that costs 16 % of the true neighbours. It does not
carry P3's contiguous tier at any dimensionality — and that last failure is a *storage* verdict,
not a pgvector verdict, so no separate ANN component fixes it.**

That last clause is the finding that matters for the epic, so it is worth stating on its own:

> **Nothing in this bead erodes clean-room's "one stack" advantage.** Where pgvector fails, it
> fails because 725 M vectors × 1280 float32 is **3.7 TB of raw vectors before any index exists**,
> against **47 GB free** on the production host. Swapping pgvector for AudioMuse's disk-paged IVF,
> or for any other engine, does not change that arithmetic — the best case, their int8 storage, is
> still 145 GB of *vectors* at 200-d and no index at all. **The window/contiguous problem is a
> "how many vectors do we choose to keep" problem, not a "which index" problem.** For every tier
> phaze can afford to store, pgvector is sufficient, and a separate ANN component buys nothing.

### Tier by tier, against the production host's real envelope (host-prod: 125 GB RAM, **47 GB free disk**)

Sizes are measured per-row costs (E6) multiplied out; multiplication, not extrapolation.

| tier | rows | representation | data | index | **total** | vs 47 GB free | verdict |
| ---- | ---- | -------------- | ---- | ----- | --------- | ------------- | ------- |
| **Track** | 200,000 | 1280-d `vector` + HNSW | 1.19 GB | 1.64 GB | **2.83 GB** | 6 % | **✅ comfortable** — *measured* |
| **Track** | 200,000 | **1280-d `halfvec` + HNSW** | 0.57 GB | 0.82 GB | **1.39 GB** | **3 %** | **✅ comfortable, and no recall cost** — *measured* |
| Window (coarse only) | 3.47 M | 1280-d `halfvec` + HNSW | 9.8 GB | 14.2 GB | **24.0 GB** | 51 % | ⚠️ fits, but eats half the remaining disk |
| **Window (bead's tier)** | **10.3 M** | 1280-d `vector` + HNSW | 61.5 GB | 84.4 GB | **145.9 GB** | **310 %** | **❌ 3× the entire free volume** |
| **Window (bead's tier)** | **10.3 M** | 1280-d `halfvec` + HNSW | 29.2 GB | 42.2 GB | **71.4 GB** | **152 %** | **❌ still over** |
| **Window (bead's tier)** | **10.3 M** | PCA-200 `vector` + IVFFlat | 9.38 GB | 9.39 GB | **18.77 GB** | 40 % | **⚠️ fits — at 16 % of the top-10** — *measured* |
| Window (bead's tier) | 10.3 M | PCA-200 `vector` + HNSW | 9.38 GB | 12.05 GB | **21.4 GB** | 46 % | ⚠️ fits on disk; **build did not complete here** |
| Window (bead's tier) | 10.3 M | PCA-200 `halfvec` + HNSW | 4.69 GB | 7.67 GB | **12.4 GB** | 26 % | ⚠️ fits on disk; build untested at this tier |
| **Window (bead's tier)** | **10.3 M** | **PCA-200 `halfvec` + IVFFlat** | **4.69 GB** | **4.45 GB** | **9.14 GB** | **19 %** | **✅ the one comfortable window option** — *measured end to end* |
| P3 contiguous, 10 s hop | 72.5 M | PCA-200 `halfvec` + IVFFlat | 33 GB | 31 GB | **~64 GB** | 137 % | ❌ |
| **P3 contiguous, native 1 Hz** | **725 M** | 1280-d `vector` + HNSW | 4.33 TB | 5.94 TB | **10.3 TB** | 22,000 % | ❌❌ |
| P3 contiguous, native 1 Hz | 725 M | PCA-200 `halfvec` + IVFFlat | 331 GB | 313 GB | **~644 GB** | 1,370 % | ❌❌ |

*Rows marked "measured" are direct observations from E6. The rest are the measured per-row costs
in E6 multiplied by the row count — arithmetic on flat, verified constants, not a fitted curve.*

**The direct answer to the bead's headline question — can Postgres + pgvector carry ~10.3 M
window-level rows?**

> **Yes — measured — but only at ~200 dimensions, and the reduction is not free.**
>
> **Measured:** 10.3 M rows at PCA-200 `halfvec` occupy **9.14 GB total** (4.69 data + 4.45 index),
> build an IVFFlat index in **13.6 minutes**, and answer at **recall@10 = 0.862, p50 31 ms,
> p95 43 ms** — on synthetic padding that E5 shows is ~10 recall points harder than real data.
> That is **19 % of the production host's free disk** and comfortably inside S1's batch budgets.
>
> **At the native 1280-d it does not fit**: 146 GB against 47 GB free — 3× over — and `halfvec`
> alone does not rescue it (71 GB, still 1.5× over). PCA to 200-d brings it inside the envelope at
> a measured cost of **16 % of the exact top-10 and 22 % of the exact top-1** (E4). That is a
> **quality trade the molecule must make deliberately**, not a free win, and it is precisely what
> S2-O4 asked.
>
> **The caveat that goes with the yes:** at this tier HNSW did not build — abandoned incomplete at
> **57 min 48 s** once the ~13 GB graph exceeded the 2 GB budget on a 2 vCPU VM — and IVFFlat
> *refused outright* at the default `maintenance_work_mem`, demanding 2,136 MB by name. The window
> tier is reachable, but only by an operator who sets build memory deliberately (E9).

### On the other four questions

**Do `halfvec` / quantization change the answer?** **Yes, and it is the cheapest win available.**
At 1280-d, `halfvec` halves both the data and the HNSW index and makes queries ~1.8× faster, at
**no measurable recall cost** — recall@10 tracked the float32 arm to within 0.008 at every single
operating point tested (HNSW ef 40/100/200: 0.825/0.912/0.942 vs 0.824/0.914/0.950; IVFFlat probes
1..50: within 0.012 throughout). **There is no measured reason to store 1280-d embeddings as
`vector` rather than `halfvec`.** It does not, however, rescue the window tier on its own, and
16-bit is where pgvector's non-binary precision ladder stops — AudioMuse's int8 default is 2×
denser again, which is a real if narrow advantage of a bespoke store.

**Does pgvector need a base-image change?** **No.** S1's blocker **B1** is confirmed on its fact
(`vector` is unavailable in `postgres:18-alpine`, in the harness *and* in production) but **wrong
on its conclusion**. Alpine 3.24 community ships `postgresql-pgvector` built for `postgresql18`; it
installs to the wrong prefix for this image, and a 3-line Dockerfile fixes that for **+31 MB
(425 → 456 MB)**, staying Alpine and staying `postgres:18`. The real work is not the extension —
it is publishing that image so CI can reference it (GitHub Actions service containers cannot be
built), keeping the **nine** `postgres:18-alpine` pins in `justfile` (8) and `docker-compose.yml`
(1) in step — ten repo-wide once `.github/workflows/tests.yml:39` is counted — and adding a
`shm_size`.

**Does every purpose need ANN?** **No — two of the four do not**, and one of the two that does not
is P1, the purpose the molecule has been treating as the index's main customer. See the table
below.

**Was AudioMuse's index choice reasoning transferable?** **Mostly not.** Four of their five design
drivers are artifacts of their deployment envelope — an unknown-Postgres matrix spanning Docker,
Kubernetes and native desktop builds; an 8 GB RAM target; six vector spaces to amortize over; and
an in-process mmap'd index with no incremental-insert path. phaze has none of those. The fifth —
quantized coarse search with an exact float32 re-rank — is a genuinely good technique that phaze
should adopt and can adopt natively via `halfvec`, without adopting anything of theirs (E8).

### Per-purpose impact (S1 rubric, phaze-ytgo.1)

| Purpose | Verdict | Granularity delivered | vs EFB | Evidence |
| ------- | ------- | --------------------- | ------ | -------- |
| P1 dedup + rename | **SERVES — and the ANN index is NOT required for it.** The resemblance share's shape is *all-pairs candidate generation over 200,000 track vectors, offline*. Measured: an exact, index-free all-pairs top-10 over 1280-d real-distribution vectors costs **≈ 6 minutes** at 200,000 rows (E7), against ~2.8 GB and a 146 s build for an HNSW index that produces an approximation. The index is a *convenience* here, not an enabler — and if it exists for P2, P1 rides it for free. Storage at the track tier is **1.39 GB with `halfvec`**, 3 % of the production host's free disk. Per S1 rule 4, the **identity share remains REDUNDANT** (audfprint/Panako own it). | **track** — 200,000 rows, the tier that is comfortable on every axis | **n-m** — this bead measures retrieval fidelity, not embedding quality; S2's remaster delta is unchanged and unaffected by anything here | E4, E6, E7 |
| P2 discovery / playlists | **SERVES.** This is the one purpose that genuinely **needs** the index, and pgvector clears its bar with room to spare. S1's budget is p95 ≤ 200 ms for k=50 at N=200,000 in the ⌘K palette. Measured at 200,000 × 1280-d: HNSW **p50 5.1 ms / p95 7.3 ms** (`vector`), **p50 2.9 ms / p95 3.5 ms** (`halfvec`) — and on **REAL** vectors at 153,203 rows, **p50 2.4 ms / p95 3.2 ms at recall@10 = 0.991**. The index-free alternative measurably **fails** the bar: an exact Postgres seq scan at the same tier is **p50 538 ms** (`vector`) and **p50 196 ms / p95 207 ms** (`halfvec`) — E10. Build is 95–146 s and fully incremental at 4.6 ms/row (E10). | **track** | **n-m** — S1 rule 3 requires an operator-run EFB arm for any P2 *quality* claim and none was possible; the claim here is on **latency and fidelity**, which is what this bead owns | E5, E6, E10 |
| P3 set/tracklist | **BLOCKED — on storage, at every dimensionality, and not by pgvector.** On the archive's **real** duration distribution (E2: 85.9 % of files > 10 min, 39.4 % > 1 h, 60 min mean), a contiguous 1 Hz pass over 200,000 files is **725 M vectors** — 7× S1's estimate at a 10 s hop and **70× at the native rate**, because S1 assumed 5 % multi-hour sets and the archive is 39 %. That is **10.3 TB at 1280-d** and **~644 GB even at PCA-200 `halfvec`**, against **47 GB free**. Even a 10 s hop is ~64 GB, still over. Two S2 open questions resolve *favourably* and change nothing: contiguous decode is **13× cheaper** than S2's deep-seek figure (S2-O5, E3), and inference is only 1,340 CPU-hours. **The compute objection died; the storage objection is 1–3 orders of magnitude.** Note the boundary: the bead's **10.3 M** tier *does* fit (9.14 GB, measured) — what does not fit is a vector per second of a 60-minute-mean archive. | **window-contiguous is deliverable and unaffordable**; the *existing* strided rows remain wrong-shaped for P3 (S1 E3) | **better in principle, unreachable in practice** | E2, E3, E6, E7, verdict table |
| P4 archive QA | **SERVES — and the ANN index is NOT required for it.** The map surface queries a **precomputed 2-D projection**, which is a two-column btree/GiST bounding-box read, not a vector search; `phaze-ytgo.3` measured PCA fitting 200,000 × 200 in 0.11–4.6 s with sub-millisecond out-of-sample updates. Outlier detection is a batch job over the same 200,000 vectors — the E7 all-pairs number, ≈ 6 minutes. **Nothing in P4 issues an interactive top-k query.** S1's prior that P4 is `REDUNDANT` against EFB is untouched by this bead and is not contradicted. | **track** + a 2-column projection | **n-m** — no EFB quality arm was possible (S1 rule 3) | E7, `phaze-ytgo.3` |

**What this table deliberately does not say.** It makes **no** claim about embedding *quality* for
any purpose. S1's accuracy bars (P1's 50 operator-labelled pairs, P2's 20-seed blind A/B, P3's
±30 s tracklist agreement, P4's outlier yield) are exactly as `phaze-ytgo.2` left them:
**unmeasured**, and this bead could not and did not attempt them. Every "SERVES" above is a verdict
on **storage and retrieval feasibility** only. A capability that stores and retrieves perfectly and
recommends badly still fails S1's bar, and D1 must not read these tokens as quality evidence.

______________________________________________________________________

## Recommendation

### For `phaze-ytgo.7` (D1 — the verdict matrix)

1. **Strike "storage and indexing" from the clean-room risk column.** The track tier — which is
   what P1, P2 and P4 all actually need — is **1.39 GB of `halfvec` and a 95-second index build**,
   answering at p50 2.9 ms. It needs no new component, no new service and no base-image change. The
   epic's worry that clean-room would need a separate ANN component and lose its "one stack"
   advantage **does not survive measurement** for any tier phaze can afford to store.
2. **Move P3 from a compute problem to a storage problem, and re-scope it.** Both S1's blocker
   **B2** (per-file O(1) cost) and S2's decode concern are now measured and small: contiguous
   decode is 3.5 s per audio-hour, 13× cheaper than S2's deep-seek observation (E3); inference is
   1,340 CPU-hours for the whole archive. What replaces them is **725 M vectors**, which is
   unaffordable at every dimensionality tested. **A P3 that keeps a vector per second of a 200,000-
   file, 60-minute-mean archive is not on the table.** A P3 scoped to *boundary detection only* —
   compute the 1 Hz novelty curve during analysis, persist only the detected boundaries and
   perhaps a vector per detected segment — is a completely different and probably affordable
   proposition, and it is what a P3 bead should be re-planned around.
3. **Do not let S1's P1 framing survive.** P1 does not need the ANN index. Its shape is a 6-minute
   exact all-pairs pass (E7), and S1's own recommendation 5 — wire the existing fingerprint engines
   into the dedup surface — remains the larger available P1 win and needs none of this.
4. **Carry the two corrected scale facts forward.** The archive is **85.9 % over ten minutes and
   39.4 % over an hour with a 60-minute mean** (E2), not the "5 % multi-hour sets" S1 carried. Every
   P3 number in the molecule is 7–70× low. And `analysis_window` really averages **71.5 rows/file**
   (17.3 coarse), closing S1's **O1**.
5. **Record that the pgvector-availability blocker (B1) is downgraded, not cleared.** It is a
   Dockerfile plus a published image plus a `shm_size`, not a base-image change — but it is still
   four coordinated edits across `docker-compose.yml`, seven `justfile` pins, a CI workflow and an
   Alembic migration, and the CI half genuinely forces phaze to publish its own Postgres image.

### If a clean-room implementation molecule is filed

- **Store the track tier as `halfvec(1280)`, not `vector(1280)`.** Measured: half the bytes, ~1.8×
  faster queries, 1.5× faster builds, and recall within 0.008 of float32 at every operating point
  (E6). There is no measured argument for float32 at this dimension.
- **Index the track tier with HNSW at `m=16 ef_construction=64`; index the window tier, if it is
  ever built, with IVFFlat.** This is not a preference, it is what the two tiers permit. At the
  track tier HNSW is 3–10× faster to query than IVFFlat at equal recall and builds in 95 s, so
  IVFFlat's faster build buys nothing for an index maintained incrementally (E10). At the window
  tier HNSW **did not build at all** in 58 minutes on the benchmark VM while IVFFlat built in 13.6
  minutes — revisit that only with the memory budget E9's formula demands. Start the track index at
  `hnsw.ef_search = 100`: measured **recall@10 = 0.991 on REAL vectors at p50 2.4 ms**. Start a
  window index at 20 probes: measured recall 0.86 at p50 31 ms.
- **Set `maintenance_work_mem` above `N × index-bytes-per-row` before building, and add
  `shm_size` to the compose service.** Use E9's formula. Getting this wrong does not produce a slow
  index; it produces a build that fails outright (IVFFlat), dies on `/dev/shm` (parallel HNSW), or
  runs so long it looks hung (HNSW past the threshold). **This is the single highest-value operational
  note in this document.**
- **Do not reduce dimensionality at the track tier.** It costs 16 % of the true top-10 (E4) to save
  1.2 GB on a host with 47 GB free. The trade is only worth considering at the window tier, where
  it is the difference between fitting and not fitting.
- **If the window tier is built, reduce properly or not at all.** E4 shows the recall loss is paid
  almost entirely on the first step down (1280 → 512 costs 12 points; 512 → 200 costs 4 more).
  There is no cheap intermediate.
- **Never size a reduction from a scree plot.** At 200-d the projection keeps 88 % of the variance
  and 84 % of the neighbours; on the coarse corpus it keeps **98.5 %** of the variance and still
  only **85 %** of the neighbours (E4). Explained variance systematically overstates retrieval
  fidelity for this embedding, because its effective dimensionality is 27.8 and neighbour ordering
  is decided by the tail the projection discards.
- **Rely on incremental insert; do not design a rebuild-and-swap pipeline.** pgvector maintains
  HNSW on `INSERT` in the same transaction as the row (E10). The periodic-full-rebuild-plus-pub/sub
  machinery an in-process index forces is work phaze does not have to do.

### Open, and deliberately left unmeasured

| # | Question | Why it is open | Who should close it |
| - | -------- | -------------- | ------------------- |
| **S4-O1** | Does **binary quantization** (`bit` + Hamming + exact re-rank) rescue the window tier at 1280-d? | 1 bit/dim is 160 B/vector at 1280-d — 16× denser than `halfvec` and denser than AudioMuse's int8 default. It is the one representation that could put 10.3 M rows at full dimensionality inside 47 GB. **Not measured**; the machine time went to the tiers the bead named. | A follow-up spike, ~2 hours. This is the highest-value unmeasured item here. |
| **S4-O2** | Build time for the window tier on **production hardware** (14 cores, 125 GB RAM, `maintenance_work_mem` ≥ 16 GB). | Every build here ran on 2 vCPU / 8.3 GB. Above the memory threshold the graph cannot be resident and the build takes the on-disk path, so the two abandoned HNSW builds in E9 are lower bounds of unknown looseness — the sub-threshold rows are accurate for this hardware but pessimistic for 14 cores. | Anyone with an hour on host-prod, once disk is free. |
| **S4-O3** | Whether **47 GB free on a 98 %-full 1.7 TB volume** is the real production budget or an artifact of a full disk that will be cleaned. | Every ❌ in the verdict table is measured against that 47 GB. If the volume is grown, the coarse-only window tier (24 GB at `halfvec`) becomes comfortable and the 10.3 M `halfvec` tier (71 GB) becomes reachable *without any dimensionality reduction* — which would make S2-O4's 16 % quality loss avoidable. **This single fact moves more of the verdict than any other open question.** | Robert. |
| **S4-O4** | Recall at the window tier on **real** vectors. | The 10.3 M tier is necessarily synthetic; E5 shows synthetic understates real recall by ~10 points at 153 K, but that gap was not measured at 10 M. | A future spike with 10 M real embeddings, i.e. ~2,800 audio-hours through the E3 pipeline (~9 CPU-hours of inference — cheap; the constraint is disk). |
| **S4-O5** | Whether a source-built pgvector (pinned tag, multi-stage build into the `postgres:18-alpine` prefix) is preferable to Alpine's package. | Alpine ships **0.8.1**; measurements here are on **0.8.5**. The package pin floats with Alpine's release cadence. Not measured. | The implementation bead, if pgvector goes in. |
| **S4-O6** | Whether the **coarse-only** window tier (3.47 M rows, E2) is the right compromise for P3-adjacent work. | It is 24 GB at 1280-d `halfvec` — the largest tier that fits without any quality loss — but S1 E3's objection stands that 180 s coarse windows straddle 2–4 DJ-set tracks. Its *usefulness* was not evaluated here, only its cost. | D1, or a re-planned P3 bead. |
