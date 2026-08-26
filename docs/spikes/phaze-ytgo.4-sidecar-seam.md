# S5 — AudioMuse-as-sidecar: integration seam and capability envelope

- **Bead:** `phaze-ytgo.4` (epic `phaze-ytgo` — AudioMuse-AI: clean-room vs sidecar, per purpose)
- **Date:** 2026-07-25
- **Tree:** `c01f36d` (base includes S1, `phaze-ytgo.1`)
- **Status:** investigation only. No product code, no dependency change, no compose change, no migration.
- **Upstream examined:** `github.com/NeptuneHub/AudioMuse-AI` at `49747c3` (post-`v3.0.5` `main`), image
  `ghcr.io/neptunehub/audiomuse-ai:latest`, digest `sha256:d6b2553…` (linux/arm64), pulled 2026-07-25.

> **Licence / clean-room statement.** This spike **did** read AudioMuse-AI's Python source, as the epic
> design explicitly permits for S5. **No AudioMuse source is transcribed, quoted or paraphrased
> line-by-line anywhere in this document.** Everything below is either (a) behaviour *observed by running
> the software*, (b) statements from its own published prose (`README.md`, `docs/ALGORITHM.md`,
> `docs/MULTI_SERVER.md`, `docs/DEPLOYMENT.md`, `docs/PLUGIN.md`), or (c) the shape of data it wrote into
> a database I controlled. Where a finding could only come from reading source, it is marked
> **[source-derived]** and stated as behaviour, never as implementation.
>
> **To the sealed spikes (`phaze-ytgo.2`, `.3`, `.6`): this document is not a design source.** It
> describes what a *deployment* of AudioMuse does at its boundaries so that D1 can price an integration.
> It deliberately contains no algorithm that a clean-room implementation would need. If you find
> yourself reaching for a paragraph here as a specification, stop — that is the contamination the seal
> exists to prevent.

______________________________________________________________________

## Question

Five questions, from the bead:

1. **Ingestion.** Does AudioMuse support a filesystem or local-path source, or is a media-server API the
   only way in? If the latter: what is the minimum viable shim, by endpoint, with an effort estimate?
2. **Result egress.** Does phaze read results through a documented HTTP API, or by reaching into
   AudioMuse's own Postgres tables? What is the coupling and the upgrade fragility?
3. **Identity mapping.** How does an AudioMuse track id map back to a phaze file UUID — and **does that
   survive phaze renaming and moving files?** phaze exists to relocate files. An integration keyed on
   path breaks on phaze's primary action.
4. **Capability envelope.** Which of P1..P4 can a sidecar serve at all, applying S1's blocker **B3** to
   P3.
5. **Operational cost.** Added CPU / RAM / disk on a home server already running phaze's stack, and
   whether a second Postgres instance is required against phaze's pinned `postgres:18-alpine`.

______________________________________________________________________

## Method

**The sidecar was actually run.** Every reachability claim below marked *demonstrated* was produced by a
working end-to-end deployment on this machine, not inferred from documentation. Claims that could not be
demonstrated are marked *documentary* or `UNMEASURED` and are never presented as measurements.

### The rig

Four containers on a dedicated Docker network, all on non-default host ports so nothing collided with
phaze's test harness (which holds 5433 and 6380):

| container | image | note |
| --------- | ----- | ---- |
| `am-pg` | **`postgres:18-alpine`** | **deliberately phaze's pinned image, not AudioMuse's `postgres:15-alpine`** — this is the experiment that settles the "second Postgres?" question |
| `am-redis` | `redis:7-alpine` | AudioMuse's own RQ broker |
| `am-flask` | `ghcr.io/neptunehub/audiomuse-ai:latest` | `SERVICE_TYPE=flask`, host port 18000 |
| `am-worker` | same image | `SERVICE_TYPE=worker` — supervisord runs `rq-worker-default`, `rq-worker-high`, `rq-janitor` |

Host: Apple M1 Pro, macOS 25.5, Docker 29.5.2, **arm64** (no AVX2 — see [E8](#e8--operational-cost)).

### The shim

Since there is no filesystem ingestion path ([E2](#e2--ingestion)), I wrote a throwaway ~170-line Python
stdlib HTTP server that impersonates a Subsonic-compatible server and pointed AudioMuse's Navidrome
provider at it. It served **phaze-shaped UUID track ids** (`uuid5`, stable across restarts) so that
identity round-tripping could be observed directly. The shim lived in the scratchpad and is not part of
this branch.

### The corpus — real archive files, not synthetic audio

Per the epic's "measure on REAL archive files" rule. Six items derived from four real files on this
machine (`<scratch>/audio`, `<scratch>/watch-test`):

| shim name | source | duration | bitrate | purpose |
| --------- | ------ | -------- | ------- | ------- |
| `track1_320` | `<track-04>.mp3` | 441.18 s | 365 kbps | single track |
| `track1_192` | re-encode of the above (`libmp3lame -b:a 192k`) | 441.18 s | 192 kbps | encode-level near-duplicate, **analysed after** `track1_320` was committed |
| `track1_96` | re-encode of the above (`libmp3lame -b:a 96k`) | 441.18 s | 141 kbps | encode-level near-duplicate |
| `set_79min` | `<set-01>.mp3` | 4,721.30 s | 320 kbps | full set |
| `set_79min_b` | byte-identical copy of `set_79min` (same md5) | 4,721.30 s | 320 kbps | exact-duplicate control |
| `set_368min` | `<set-02>.mp3` | 22,098.51 s | 128 kbps | 6-hour concert recording |

Small **n**. Six items cannot establish an accuracy rate and this document never claims one. What six
items *can* establish — and did — is **structural** behaviour: whether ingestion works at all, whether
identity survives a move, whether duplicates collapse, and what the analysis actually stores. Those are
yes/no facts, and a yes/no fact does not need a large sample.

### What was deliberately NOT measured

- **No existing-features-baseline (EFB) arm.** S1's EFB needs a populated phaze database; none was
  reachable (S1 hit the same wall — its open question O1). Under S1's binding scoring rule 3, **P2 and
  P4 therefore score `UNMEASURED`, not `SERVES`**, however well the mechanism demonstrably works.
- **No live-vs-studio / cover / remix pairs.** The corpus has no rendition pairs, so P1's *resemblance*
  share is unmeasured. Only the *identity* share (same recording, different encode) was exercised.
- **No x86 timing.** All timings are arm64 on an M1 Pro and are not portable to an Intel home server.

______________________________________________________________________

## Evidence

### E1 — It ran

Demonstrated, in order:

```console
$ curl -s -X POST :18000/api/servers/test -d '{"server_type":"navidrome","url":"http://host.docker.internal:14040",…}'
{"error":null,"ok":true,"path_format":"absolute","sample_count":4,"warnings":[]}

$ curl -s -X POST :18000/api/analysis/start -d '{"num_recent_albums":0,"top_n_moods":5}'
{"status":"queued","task_id":"d6b155bf-…","task_type":"main_analysis"}
```

AudioMuse enumerated the shim's catalogue, downloaded every file over HTTP, ran its analysis, stored
results and built its IVF index. Final state: **6 provider tracks → 6 `score` rows, 6 `embedding` rows,
6 `clap_embedding` rows**, task status `SUCCESS`.

One deployment note: `POST /api/analysis/start` returns `403 {"error":"Setup required"}` until the setup
wizard completes or auth is disabled. The wizard is a genuine first-run gate, not an optional step.

### E2 — Ingestion

#### There is no filesystem ingestion path. This is definitive.

- The provider surface consists of exactly **five media-server implementations** — Navidrome, Jellyfin,
  Emby, Plex, Lyrion — selected by a single `MEDIASERVER_TYPE` setting whose documented values are those
  five. There is no `local`, `filesystem`, `folder` or `path` provider. **[source-derived]**
- A repo-wide search for filesystem-ingestion vocabulary returns only: path *resolution* helpers for the
  three native desktop builds, a Lyrion helper that resolves `file://` URIs *returned by Lyrion*, and
  the provider-migration feature's requirement that a media server report absolute paths. None is an
  ingestion source. **[source-derived]**
- There is **no audio upload or push endpoint**. The only multipart file intake in the entire HTTP
  surface is the database-backup restore endpoint. **[source-derived]**
- **The plugin system cannot add one.** `docs/PLUGIN.md` enumerates fifteen plugin capabilities — pages,
  menu items, settings, database access, own tables, an on-song-analyzed hook, cron tasks, extra pip
  packages, background jobs, named tasks, startup hooks, media-server *playlist creation*, and an extra
  ONNX execution provider. **Registering a media-server provider is not among them.** A filesystem
  source is a fork, not a plugin.

**Conclusion: a media-server API is the only way in, and phaze must impersonate one.**

#### The minimum viable shim, by endpoint

Transport shape (needed to build anything at all): requests go to `{base}/rest/{endpoint}.view` with
query-string auth (`u`, `p` as `enc:<hex-of-password>`, `v`, `c`, `f=json`); every response is a JSON
object wrapping a `subsonic-response` envelope carrying `status` and `version`; a `status` of `failed`
plus error code 40–44 is read as an auth failure. **[source-derived, and confirmed by the shim working.]**

**Demonstrated minimum — the endpoints AudioMuse actually called during a full analysis run:**

| # | endpoint | called for | required? |
| - | -------- | ---------- | --------- |
| 1 | `search3` (empty query, `songCount`/`songOffset` paging) | connection test; also the unfiltered whole-library enumeration | **yes** — the Test Connection button uses it, and it is where `path_format` is judged |
| 2 | `getMusicFolders` | library list + library filtering | **yes** — setup lists libraries from it |
| 3 | `getAlbumList2` (`type=newest`, `size`, `offset`, optional `musicFolderId`) | album enumeration; **the analysis run's unit of work** | **yes** |
| 4 | `getAlbum` | tracks within an album | **yes** |
| 5 | `stream` (`id`) | **downloads the audio** | **yes** |
| 6 | `getLyricsBySongId` | lyrics pipeline | no — may return empty |

Additionally present in the provider but not exercised by an analysis-only run: `getSong`,
`getPlaylists`, `getPlaylist`, `createPlaylist`, `updatePlaylist`, `deletePlaylist`. **[source-derived]**
The playlist five are only needed if phaze wants AudioMuse's clustering to write playlists back — which
means AudioMuse would be *writing into* phaze, a direction worth refusing on its own merits.

**Three things about the shim that are not obvious and are load-bearing:**

1. **AudioMuse's unit of work is the album, not the track.** The analysis task enumerates albums and
   then fetches each album's tracks. phaze has no album entity — it has files, metadata and tracklists.
   The shim must therefore **synthesise stable album groupings**, and they must stay stable, because an
   album is also the retry/progress unit. Grouping 200,000 loose files into synthetic albums is a design
   decision phaze does not currently have to make.
2. **It downloads every file in full.** `stream` responses are written whole to the worker's temp
   volume. It issued no range requests. The 354 MB six-hour set was transferred in full — and then only
   its first 600 seconds were decoded ([E5](#e5--the-600-second-wall)). Ingesting an *N*-terabyte archive
   moves *N* terabytes through the shim.
3. **Absolute paths matter for a feature phaze does not want.** Reporting relative or absent `path`
   values produces a first-class warning in the connection test, because path matching backs the
   provider-migration and align features. My shim reported absolute paths and scored
   `path_format: "absolute"`.

#### Effort estimate

| stage | estimate | basis |
| ----- | -------- | ----- |
| Working shim, single-user, small library | **~1 day** | demonstrated: 170 non-blank lines of stdlib Python, 5 endpoints, built and working inside this spike |
| Production shim at phaze's scale | **1–2 weeks** | album synthesis over 200k loose files; paging; correct `Content-Type`/`Content-Length` streaming off phaze's storage; scan-root → music-folder mapping; Subsonic auth; error envelope; deciding what to do with playlist write-back |
| **Ongoing** | **permanent, unbounded** | see below |

**The ongoing cost is the real one, and it is not a rounding error.** The shim does not implement "the
Subsonic API"; it implements *the subset of Subsonic that AudioMuse's Navidrome provider currently
happens to call, in the shapes it currently happens to parse.* That is internal code in another project,
with no compatibility contract to phaze. Any upstream change to enumeration strategy, paging, an
expected field, or the album/track model silently breaks ingestion, and it breaks it at the point where
phaze looks like a *misbehaving media server* — a bad failure mode to debug. This is a permanent
integration surface owned by phaze and controlled by someone else.

### E3 — Result egress

**A documented HTTP API exists, and it is good. phaze would not need to read AudioMuse's tables.** This
is the most favourable finding in the document.

Three read paths, all demonstrated against phaze-shaped UUIDs:

| endpoint | returns | demonstrated |
| -------- | ------- | ------------ |
| `GET /api/sync` | **bulk paginated export**: per track — `embedding`, `clap_embedding`, `umap_x`/`umap_y`, `tempo`, `key`, `scale`, `energy`, `mood_vector`, `other_features`, plus `album`/`artist`/`year`/`rating` and an opaque change fingerprint `fp`. `?fields=index` returns an `{id, fp}` manifest for diffing | yes — keys listed above are the literal response keys observed |
| `GET /external/get_score`, `GET /external/get_embedding` | one track's stored analysis / its **200-float** vector as JSON | yes |
| `GET /api/similar_tracks` | ranked neighbours with a `distance`, straight off the in-memory IVF index | yes |

```console
$ curl -s ':18000/api/sync?fields=index&limit=10'
{"has_more":false,"next_page":null,"provider_type":"navidrome","total_tracks":6,
 "tracks":[{"fp":"29622f9fdd628884","id":"<uuid-1>"}, …]}

$ curl -s ':18000/external/get_embedding?id=<uuid-1>'
item_id <uuid-1>   dim 200
```

Note the `id` values: **those are my shim's UUIDs, not AudioMuse's internal ids.** Egress is expressed
in phaze's own identifiers ([E4](#e4--identity-mapping)). And `fp` is a change fingerprint over the
analysis columns, so phaze can poll the manifest and pull only what changed rather than re-exporting
200,000 rows.

#### Coupling and upgrade fragility

**Do not read their tables.** The observation that makes this categorical is not aesthetic:

- **AudioMuse has no versioned migration tool.** Its schema is created and evolved by idempotent DDL
  executed at process startup — add-column-if-missing, and **drop-column-if-exists**. During my run's
  startup it dropped three columns from its own `score` table and dropped five legacy index tables
  outright, and it relaxed a primary key on the identity mapping table. **[source-derived, and visible
  in the startup log.]** A consumer reading those tables is coupled to a schema that mutates on *their*
  container restart, with no version number to pin and no deprecation window.
- **The identity scheme is versioned and has already moved.** Their published prose documents the
  content id as `fp_2…`; the running build minted `fp_4…`. Two scheme bumps between the prose and
  `main`. Anything phaze persists that embeds an AudioMuse id inherits that churn.
- The HTTP surface, by contrast, is Swagger-annotated, has an explicitly external-integration blueprint,
  and its multi-server document states an API-stability intent ("There is no `v2` API. Every existing
  endpoint gains one optional `server` parameter").

So: **read via HTTP, and treat the tables as private.** If a future need forces table access anyway,
record it as a known maintenance liability with a named owner — reading another project's unmigrated
tables is a durable liability, and this one is worse than average because the DDL demonstrably drops
columns.

Residual coupling even on the good path: phaze would depend on `/api/sync`'s field names, on the
embedding remaining 200-dimensional, and on the ids continuing to round-trip. Those are ordinary API
risks, not schema-archaeology risks.

### E4 — Identity mapping

**This was expected to be the decisive problem. It is not. Identity survives phaze renaming and moving
files — demonstrated.**

Three layers, all observed directly:

1. **AudioMuse's own catalogue id is content-derived, not path-derived and not provider-derived.** Its
   published prose (`docs/MULTI_SERVER.md`, `docs/ALGORITHM.md`) states this outright: the id is a
   similarity hash computed from the track's own embedding, written as a scheme-versioned `fp_…` string.
   Confirmed in the running system — every `score.item_id` was an `fp_4…` value, never a path and never
   my shim's UUID.
2. **A mapping table records the provider's id per server.** Exactly what phaze needs:

   ```console
   $ psql -c 'select item_id, provider_track_id, file_path from track_server_map'
    fp_<hash-1> | <uuid-1> | <fixtures>/original/track1_320.mp3
    fp_<hash-2> | <uuid-2> | <fixtures>/original/track1_192.mp3
    fp_<hash-3> | <uuid-3> | <fixtures>/original/track1_96.mp3
    fp_<hash-4> | <uuid-4> | <fixtures>/original/set_79min.mp3
    fp_<hash-5> | <uuid-5> | <fixtures>/original/set_368min.mp3
   ```

   `provider_track_id` is **phaze's file UUID, stored verbatim**. `file_path` sits alongside as a
   descriptive column, not as the key.
3. **The API translates in both directions.** Every id in and out of `/api/sync`, `/external/*` and
   `/api/similar_tracks` is the provider's id, not the catalogue id (E3).

#### The rename-and-move test

I changed **every path** in the shim — new directory, new filename, ids unchanged — and re-ran a full
analysis, which is exactly the shape of phaze executing a batch of approved moves.

```console
$ curl -s ':14040/__move'    # every file: <fixtures>/original/X -> <fixtures>/renamed/<rand>-X
$ curl -s -X POST :18000/api/analysis/start -d '{"num_recent_albums":0,"top_n_moods":5}'
… main_analysis | SUCCESS | 100

score rows           : 5   (unchanged — nothing re-analysed, nothing re-downloaded)
track_server_map rows: 5   (all five mappings intact, all item_ids unchanged)
/external/get_embedding?id=71c97b1b-…  ->  item_id 71c97b1b-…  dim 200
```

**Verdict on Q3: yes, identity survives — conditional on one thing phaze controls.** Because phaze
*is* the media server in this topology, phaze chooses the track id. If the shim emits the phaze file
UUID (stable across rename and move), identity is stable by construction. If the shim were to derive ids
from paths — the obvious lazy implementation — every phaze move would orphan the analysis. **This is a
shim design requirement, and it should be written down as one.**

Two caveats, both real:

- **`file_path` goes stale and stays stale.** After the move, both `score.file_path` and
  `track_server_map.file_path` still held the *pre-move* paths, and a full analysis run did not refresh
  them (it correctly skipped every already-mapped track). Any phaze feature reading a path out of
  AudioMuse would read a lie. Consume ids, never paths.
- **A re-analysis can change the catalogue id.** The catalogue id is a function of the embedding, so a
  model or scheme change re-mints it (`fp_2` → `fp_4` upstream). phaze must key on its **own** UUID and
  treat the `fp_…` id as an opaque, disposable internal detail — never as a stored foreign key.

### E5 — The 600-second wall

**The single most consequential finding for phaze's actual archive, and it is not in AudioMuse's
documentation.**

A configuration value named as a *timeout* (`AUDIO_LOAD_TIMEOUT`, default **600**) is applied as a
**decode duration limit**: only the first 600 seconds of any file are loaded, in both the primary decode
path and its fallback. **[source-derived]** The effect is visible without reading anything:

```console
$ psql -c 'select title, duration from score'
 <track-04> [320]       | 441.1765     <- 7:21 track, real duration
 <track-04> [192]       | 441.1765
 <track-04> [96]        | 441.1765
 <set-01> (79 min set)       |      600     <- real duration 4 721 s
 <set-02> (368 min set) |      600     <- real duration 22 099 s
```

The shim advertised the true durations (4,721 s and 22,099 s) in its Subsonic responses. AudioMuse
stored **600** for both. So this is not merely "analysis is truncated" — **the truncated value is what
gets persisted as the track's duration.**

Consequences for phaze, in ascending order of severity:

1. **Every multi-hour set is represented by its first ten minutes.** For phaze's stated core corpus —
   "primarily full sets from events like Coachella" — the stored embedding describes the opening of the
   set and nothing else. Two completely different Coachella sets that both open with an ambient intro
   are near-neighbours; the same set and its own second hour are not comparable at all.
2. **Every long file reports the identical duration, 600.** Duration agreement is one of the three
   checks AudioMuse uses to decide whether two tracks are the same recording (its own prose calls this
   "the AcoustID rule"). That check is *degenerate* across phaze's entire set collection: every set
   agrees with every other set to the second.
3. **Raising the cap is not free, and the arithmetic is unfavourable.** Audio is decoded to mono
   float32 at 16 kHz. At 600 s that is 38.4 MB of waveform. At the six-hour set's true length it is
   22,099 × 16,000 × 4 ≈ **1.41 GB of waveform for one file**, before the derived mel-patch tensor. The
   cap is what keeps the worker inside AudioMuse's stated 8 GB envelope. (RSS at a raised cap was not
   measured — the arithmetic is derived, the peak is `UNMEASURED`.)

This finding does not depend on model quality, tuning, or sample size. It is a fixed property of the
deployment, it applies to the majority of what phaze exists to organise, and **any sidecar verdict that
ignores it is wrong.**

### E6 — P3 is structurally blocked (S1's B3, confirmed twice over)

S1's B3: *a capability producing one averaged vector per track is structurally incapable of P3 — no
averaging of patch embeddings can emit a timestamp.* The bead instructed me to confirm the per-track
shape and then stop measuring P3. Confirmed by three independent observations:

1. **The pipeline averages patches and discards them.** Audio is cut into mel-spectrogram patches, the
   embedding model emits one vector per patch, and the patches are **averaged into a single
   200-dimension track vector**. This is stated verbatim in their own published `docs/ALGORITHM.md`
   (chapter 3, "MusiCNN") — no source reading required. The per-patch vectors are consumed and never
   persisted. **[source-derived confirmation of the same fact.]**
2. **The storage shape has no time axis, at all.** The `embedding` table's entire definition is
   `(item_id text primary key, embedding bytea)`, foreign-keyed to `score`. Row counts after the run:
   `score 6 / embedding 6 / clap_embedding 6` — exactly one vector per track. And a sweep of **all 27
   tables** for any column whose name suggests a time offset returns **only task-bookkeeping columns**:

   ```console
   $ psql -tAc "select table_name||'.'||column_name from information_schema.columns
                where table_schema='public' and column_name ~* 'time|offset|start|stop|second|position|segment|window'"
   task_history.duration_seconds
   task_status.end_time
   task_status.start_time
   task_status.timestamp
   ```

   There is nowhere in AudioMuse's schema for a timestamp-bearing audio result to live.
3. **The 600-second wall (E5) independently forecloses it.** Even a hypothetical time-localised output
   would cover only the first ten minutes of a three-hour set.

**P3's deliverable is `tracklist_tracks` rows carrying a `timestamp`. A sidecar cannot produce one.
P3 = `BLOCKED`. No P3 benchmarking was performed, per the bead's instruction and S1's recommendation 1.**

#### The one workaround, and why it is also blocked

There *is* a way to get window-level vectors out of an unmodified AudioMuse: because phaze **is** the
media server, the shim could fan a long set out into many synthetic "tracks" — one per window — each
with an id encoding `(file_uuid, start_sec)`. AudioMuse would then emit one vector per window and phaze
would recover the timestamp from its own id. The timestamp comes from phaze, not from AudioMuse, so B3
is not violated.

It is priced out by three orders of magnitude. Using S1's own P3 sizing (~1.1 × 10⁷ window vectors at a
10 s hop across the archive) and this spike's measured throughput (~20–26 s per analysed track,
[E8](#e8--operational-cost)):

> 1.1 × 10⁷ windows × ~22 s ≈ 2.4 × 10⁸ CPU-seconds ≈ **7.7 years single-threaded**, ≈ **3.8 years** on
> the two RQ workers measured here, ≈ **175 days** at 16-way concurrency.

Plus 1.1 × 10⁷ rows in `score`, `embedding`, `clap_embedding` and `chromaprint`, and 1.1 × 10⁷ HTTP
downloads. **P3 stays `BLOCKED` — now structurally *and* on measured cost.** D1 should treat P3-via-
sidecar as closed.

### E7 — Near-duplicate behaviour: what its dedup does and does not do for P1

The bead flagged a possible collision: AudioMuse's v3 duplicate detection collapses content across
servers, and phaze has its own dedup. **Measured answer: within a single media server, AudioMuse does
not collapse anything — not even a byte-identical file.**

Six provider tracks produced **six distinct catalogue ids**:

| pair | Hamming (of the 200-bit content signature) | cosine distance (raw 200-d embeddings) | collapsed? |
| ---- | ------------------------------------------ | -------------------------------------- | ---------- |
| `track1_320` vs `track1_192` | **2** / 200 | **0.000070** | **no** |
| `track1_320` vs `track1_96` | **4** / 200 | **0.001043** | **no** |
| `track1_96` vs `track1_192` | 2 / 200 | 0.000682 | no |
| `set_79min` vs `set_79min_b` *(byte-identical)* | **0** / 200 | — | **no** |
| `track1_320` vs `set_79min` | 17 / 200 | 0.050556 | n/a |
| `track1_320` vs `set_368min` | 16 / 200 | 0.100617 | n/a |
| `track1_96` vs `set_79min` | 15 / 200 | 0.050349 | n/a |
| `track1_96` vs `set_368min` | 18 / 200 | 0.106210 | n/a |
| `track1_192` vs `set_79min` | 15 / 200 | 0.049631 | n/a |
| `track1_192` vs `set_368min` | 16 / 200 | 0.101679 | n/a |
| `set_79min` vs `set_368min` | 25 / 200 | 0.140179 | n/a |

Three things follow, and the first is the one D1 needs.

**(a) The collapse is a cross-server mechanism only.** The `track1_192` re-encode was analysed in a
*separate, later run*, well after `track1_320` was committed — so this is not a race. Its signature was
2 bits away, its cosine distance 0.00007, its duration identical to four decimal places, and its
Chromaprint agreed (I called AudioMuse's own agreement function on the two stored fingerprints inside
the container: `True`). Every published identity criterion was satisfied, and it still minted a new id.
The byte-identical copy is even starker: an **identical** signature, and it minted the *next free id*
(`…762a` → `…762b`, i.e. the trailing digits are a collision counter, not signature bits).

> **So: AudioMuse's dedup neither helps nor collides with phaze's.** It does not solve phaze's
> encode-level near-duplicate problem, and it will not silently merge two phaze files into one row. The
> bead's collision worry can be closed as unfounded — with the corollary that the hoped-for P1 benefit
> is also absent.

**(b) The embedding *is* discriminative at the identity end.** Same recording, different encode:
0.00007–0.001. Unrelated tracks: 0.050–0.140. That is a two-orders-of-magnitude gap, and a phaze-side
near-duplicate detector could exploit it. But under S1's binding scoring rule 4 this is the **identity
share** — same recording, different encode — which phaze already owns via audfprint and Panako. Per S1
that scores `REDUNDANT`, and per S1's recommendation 5 the highest-value P1 action available is wiring
phaze's *existing* fingerprint engines into the dedup surface, which needs none of this.

**(c) There is a discrimination-margin warning worth carrying forward.** Wholly unrelated electronic
tracks sat at only 15–25 Hamming bits out of 200, and 0.86–0.95 cosine *similarity*. AudioMuse's own
prose acknowledges this ("a homogeneous library puts genuinely different recordings inside the cosine
threshold"), which is why it needs duration and Chromaprint as additional gates. phaze's archive is
exactly such a homogeneous library — one genre family, heavily. Combined with E5 (every long set reports
duration 600, so the duration gate is degenerate), a P1 detector built on these embeddings would be
leaning on a narrow margin precisely where phaze's corpus is least separable. Six items cannot quantify
that; it is a flag for whoever runs the real P1 arm, not a measurement.

### E8 — Operational cost

#### Does phaze need a second Postgres? No — demonstrated.

AudioMuse's compose pins `postgres:15-alpine`. I ran it against **phaze's pinned image** instead:

```console
$ docker exec am-pg psql -tAc 'select version()'
PostgreSQL 18.4 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit

$ docker exec am-pg psql -tAc "select count(*) from information_schema.tables where table_schema='public'"
27
$ docker exec am-pg psql -tAc 'select extname, extversion from pg_extension order by 1'
pg_trgm|1.6
plpgsql|1.0
unaccent|1.1
```

**Full schema initialisation, migration and a complete analysis run all succeeded on `postgres:18-alpine`.**
The version range overlaps phaze's pin. AudioMuse needs its **own database and role** on that instance —
it creates `unaccent` and `pg_trgm` in its own database, and takes a database-scoped advisory lock during
schema setup — but it does not need a second server process, a second image or a second port.

Two caveats: (i) upstream tests against 15, so running on 18 is *unsupported-but-working*, and a future
AudioMuse release could regress it; (ii) their backup/restore feature shells out to `pg_dump`/`psql` and
their own source notes a GUC incompatibility when moving dumps between major versions — so backups taken
on 18 are not portable back to a 15 deployment.

**Redis is likewise shareable**: the broker is configured by a single `REDIS_URL`, so it can point at
phaze's existing Redis on a dedicated logical database index rather than running `redis:7-alpine`
separately. phaze's own convention (`CLAUDE.md`) already mandates per-consumer logical-DB isolation, so
this fits the house rule rather than fighting it.

#### Measured footprint

Idle, after the analysis run completed:

| container | idle RSS | peak RSS during analysis | peak CPU |
| --------- | -------- | ------------------------ | -------- |
| `am-flask` | 178.9 MiB | 178.9 MiB | 0.1 % |
| `am-worker` | 279.7 MiB | **729.2 MiB** | **244 %** (≈ 2.4 cores) |
| `am-pg` | 37.2 MiB | 42.9 MiB | 4.5 % |
| `am-redis` | 4.3 MiB | — | 0.5 % |

- **Idle RAM: ≈ 500 MiB** for the four containers; **≈ 460 MiB** if Postgres and Redis are shared with
  phaze.
- **Peak RAM: ≈ 950 MiB** with one worker active. This is at the default 600 s decode cap; raising that
  cap raises it (E5).
- **Peak CPU: ~2.4 cores** from a single worker container running two RQ workers. AudioMuse's stated
  4-core minimum is a real requirement, and those cores are *in addition to* phaze's own analysis
  workers, which are doing essentially the same kind of work on the same machine.
- **Image: 5.36 GB on disk** (arm64, `latest`; 1.79 GB compressed / 15 layers). Both `linux/amd64` and
  `linux/arm64` are published, so an ARM home server is fine; on x86 without AVX2 an experimental
  `-noavx2` image exists. Add ~425 MB for `postgres:18-alpine` and ~59 MB for `redis:7-alpine` if not
  shared.
- **Database growth: small.** `audiomusedb` was 9.7 MB with six tracks — almost all fixed overhead. Per
  track it stores a 200 × float32 embedding (**800 B**), a Chromaprint (**1.1–3.0 KB** measured; longer
  files, larger prints), a CLAP embedding and a lyrics embedding (sizes not separately measured), and a
  `score` row. The audio-embedding column alone at 200,000 files is ≈ **160 MB**, matching S1's estimate;
  Chromaprints add roughly **220–600 MB**. Disk is not the constraint here.
- **Temp storage**: each analysed file is downloaded whole to the worker's temp volume before decoding,
  so the volume must hold the largest file in the archive (354 MB in this corpus) per concurrent worker.

#### Throughput, and what it means at 200,000 files

Per-track wall clock from the worker's own timeline, two albums processed in parallel across the two RQ
workers:

| track | download + analyse |
| ----- | ------------------ |
| `track1_320` (7:21 track, 20 MB) | ~26 s |
| `set_79min` (79 min set, 189 MB, decode capped at 600 s) | ~20 s |
| `set_79min_b` (repeat probe, cold) | 16 s end-to-end |

**≈ 20–26 s per track on an M1 Pro (arm64), at 2-way concurrency.** Naïve extrapolation:

> 200,000 files × ~22 s ÷ 2 workers ≈ 2.2 × 10⁶ s ≈ **25 days of continuous running** for one full
> initial ingest — *plus* transferring the entire archive byte-for-byte over HTTP through the shim
> (E2), *plus* the same CPU cores phaze's own essentia analysis wants.

Treat 25 days as an order-of-magnitude figure, not a forecast: it is arm64, n = 6, and a 4-core x86
minimum-spec box with more workers would land differently. What it is *not* is a background detail —
initial ingest is a multi-week campaign on a home server, and it is the dominant one-time cost of the
sidecar option.

______________________________________________________________________

## Verdict

**The sidecar seam works and is buildable. It is also, for phaze specifically, the wrong shape — and the
reason is the corpus, not the software.**

Point by point:

| Question | Answer |
| -------- | ------ |
| **Q1 Ingestion** | **No filesystem path exists.** A media-server API is the only way in; phaze must impersonate a Subsonic server permanently. Minimum shim: `search3`, `getMusicFolders`, `getAlbumList2`, `getAlbum`, `stream` (+ `getLyricsBySongId` optional). ~1 day to a working shim (demonstrated), 1–2 weeks to production, **permanent unbounded maintenance** because the shim tracks another project's internal client code, not a spec. |
| **Q2 Egress** | **A documented HTTP API — no table reading required.** `/api/sync` is a paginated bulk export with a change manifest, carrying embeddings, CLAP vectors, UMAP coordinates and the interpretable scalars, keyed on phaze's own ids. Coupling is ordinary API coupling. Their tables are the fragile path (startup DDL that drops columns, no versioned migrations, an id scheme already bumped twice) and should be treated as private. |
| **Q3 Identity** | **Survives renaming and moving — demonstrated.** Every path changed, no mapping lost, nothing re-analysed, egress still resolved by phaze UUID. Conditional on one shim design rule: **the shim must emit phaze's file UUID as the track id**, never a path-derived one. `file_path` in their schema goes stale after a move and must never be consumed. |
| **Q4 Envelope** | P3 `BLOCKED` structurally and on cost. P1's identity share `REDUNDANT`; its resemblance share unmeasured and degraded for sets. P2/P4 mechanically demonstrated but `UNMEASURED` against the EFB, and degraded for sets. See the table below. |
| **Q5 Cost** | **No second Postgres** — full schema and analysis verified on phaze's pinned `postgres:18-alpine`; needs its own database and role, not its own server. Redis shareable via a dedicated logical DB. **+5.36 GB image, ≈ 500 MiB idle RAM, ≈ 950 MiB peak, ~2.4 cores under load**, plus a ~25-day initial ingest that also moves the whole archive over HTTP. |

**And the finding that outranks all five: the 600-second wall (E5).** AudioMuse analyses and stores only
the first ten minutes of any file. phaze's archive is "primarily full sets". Every Coachella set in the
archive would be represented by its opening ten minutes and would report a duration of exactly 600
seconds. That is not a tuning problem; it is what the deployment does, it applies to the majority of
phaze's corpus, and it silently degrades P1, P2 and P4 for exactly the files phaze cares most about.

### Per-purpose impact (S1 rubric, phaze-ytgo.1)

| Purpose | Verdict | Granularity delivered | vs EFB | Evidence |
| ------- | ------- | --------------------- | ------ | -------- |
| P1 dedup + rename | `UNMEASURED` — with the identity share `REDUNDANT` and sets `BLOCKED` | track (**first 600 s only** for files > 10 min) | n-m | Identity share (same recording, different encode) is cleanly separable — cosine 0.00007–0.001 vs 0.050–0.140 unrelated (E7) — but S1 rule 4 scores that `REDUNDANT` against audfprint/Panako, which phaze already owns. AudioMuse's own dedup does **not** collapse encode-level duplicates within one server (E7a), so it adds nothing there. The **resemblance** share (live vs studio, covers, remixes) had no pairs in the corpus: unmeasured. For any file > 10 min the comparison is over its first ten minutes only (E5), so for phaze's sets P1 is structurally blocked. No EFB arm run. |
| P2 discovery / playlists | `UNMEASURED` | track (**first 600 s only** for files > 10 min) | n-m | Mechanism demonstrated end-to-end: `/api/similar_tracks` returned ranked neighbours with distances, keyed on phaze UUIDs, off a built IVF index (E3). Quality **not** measured — no EFB arm was possible without a populated phaze database, and S1 rule 3 makes that `UNMEASURED`, not `SERVES`. Severe caveat: for concert sets the neighbour relation is computed over opening ten minutes (E5), and unrelated tracks in this homogeneous corpus sat at 0.86–0.95 cosine similarity (E7c). |
| P3 set/tracklist | **`BLOCKED`** | n-a | n-a | **S1's B3 confirmed three ways.** Patch embeddings are averaged to one 200-d track vector (their own `docs/ALGORITHM.md` ch. 3); the `embedding` table is `(item_id, bytea)` with one row per track and **no time-bearing column exists anywhere in the 27-table schema** (E6); and the 600 s wall forecloses it independently (E5). The shim-fanout workaround — phaze emitting each window as a synthetic track — is not structurally forbidden but is priced at ~1.1 × 10⁷ analyses ≈ 3.8 years on the measured hardware (E6). No P3 benchmarking performed, per the bead's instruction. |
| P4 archive QA | `UNMEASURED` | track (**first 600 s only** for files > 10 min) | n-m | `/api/sync` demonstrably serves `umap_x`/`umap_y` plus the full mood/genre scalars per track (E3), so the map exists without extra work. But S1 already rates P4 the strongest `REDUNDANT` candidate against the EFB, no EFB arm was run (rule 3), and the map coordinates for every concert set would be derived from its first ten minutes (E5) — i.e. the outliers it surfaces on phaze's most important files would be artefacts of truncation. |

Scoring notes, against S1's six binding rules: rule 1 — scored independently, no average given. Rule 2 —
granularity stated in every row; nothing claims P3. Rule 3 — no EFB arm was possible, so P2 and P4 are
`UNMEASURED`, not `SERVES`, despite the mechanism working. Rule 4 — P1's identity/resemblance split is
stated explicitly. Rule 5 — not applicable; P3 is `BLOCKED`, not claimed. Rule 6 — sample size (n = 6)
and file provenance are in [Method](#the-corpus--real-archive-files-not-synthetic-audio).

______________________________________________________________________

## Recommendation

1. **Do not adopt the sidecar for P1 or P3.** P3 is closed — structurally and on cost (E6). P1's only
   demonstrated strength is the identity share phaze already owns; S1's recommendation 5 (wire the
   existing fingerprint engines into the dedup surface) delivers more, needs no shim, no container, no
   AGPL analysis and no 25-day ingest.

2. **If D1 wants a sidecar cell at all, scope it to P2 and restrict it to files ≤ 10 minutes.** That is
   AudioMuse's native use case, the mechanism is demonstrated working, and the restriction follows
   directly from E5. State the restriction in the verdict — a P2 sidecar that silently mis-serves every
   concert set is worse than no P2 at all. Under S1's `SERVES-WITH-CAVEAT` this is the caveat to name.

3. **Before any sidecar GO, run the EFB arm.** It is the one measurement this spike could not make and
   the one D1 most needs. It requires a populated phaze database (S1's open question O1) and nothing
   else. Without it, P2 and P4 cannot rise above `UNMEASURED` and a GO would be unjustified on the
   evidence.

4. **If a sidecar is ever built, three seam rules are non-negotiable, and all three are cheap if written
   down now and expensive if discovered later:**
   - **The shim emits phaze's file UUID as the track id.** Anything path-derived breaks on phaze's
     primary action (E4).
   - **Read results only over HTTP — `/api/sync` for bulk, `/external/*` for point lookups.** Never read
     their tables: the schema is evolved by startup DDL that drops columns, with no version to pin (E3).
   - **Never consume a path from AudioMuse.** `file_path` is stale the moment phaze moves a file and is
     not refreshed by re-analysis (E4).

5. **Record the Postgres finding as a positive.** No second database server is needed —
   `postgres:18-alpine` runs AudioMuse's full schema and pipeline, demonstrated. This removes one of the
   commonly assumed sidecar costs, and it is worth carrying into D1 so the sidecar option is not
   penalised for a cost it does not have. Redis can likewise be shared on a dedicated logical DB.

6. **Carry the 600-second wall into D1 as a first-class finding, not a footnote.** It is undocumented
   upstream, it was only visible by running the software, and it interacts with phaze's corpus worse
   than any other property of the system. If D1 renders a sidecar GO on any cell without pricing it, the
   verdict is unsound.

### Open, and deliberately left unmeasured

| # | Question | Why it is open | Who should close it |
| - | -------- | -------------- | ------------------- |
| O1 | **EFB delta for P2 and P4.** | No populated phaze database was reachable; S1 hit the same wall (its O1/O3). Binding rule 3 makes both cells `UNMEASURED` without it. | Whoever first has archive DB access; it needs no AudioMuse deployment at all. |
| O2 | **P1's resemblance share** — live vs studio, covers, remixes. | The corpus had no rendition pairs; only encode-level pairs, which are the identity share. | A P1 arm with an operator-labelled seed set (S1's P1 accuracy bar). |
| O3 | **Worker RSS at a raised `AUDIO_LOAD_TIMEOUT`.** | The 1.41 GB waveform figure for a 6-hour set is derived arithmetic; actual peak RSS with the cap raised was not measured. | Anyone re-running the rig; it is a one-setting change and a `docker stats` sample. |
| O4 | **x86 throughput.** | All timings are arm64 M1 Pro. The 25-day ingest figure does not transfer to a 4-core Intel home server. | Re-run the rig on the target hardware if a sidecar reaches GO. |
| O5 | **Whether upstream keeps working on Postgres 18.** | Demonstrated working, but upstream pins and tests 15. This is unsupported-but-working, not supported. | Re-verify at each AudioMuse upgrade, if a sidecar reaches GO. |
