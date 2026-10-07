# phaze-lm73u — what companion files contain, by type, across the whole archive

- **Bead:** `phaze-lm73u` (spike, molecule `phaze-gyj6i` — companion-file intelligence)
- **Date:** 2026-10-06
- **Tree:** branch `wt/bead/issue/phaze-lm73u`, forked at `e9c5ed86`
- **Code under test:** none. This is a content survey of the files themselves. **No product code,
  test code or build config changed.**
- **Production access:** read-only. Every Postgres session ran `SET default_transaction_read_only =
  on; SET statement_timeout = '60s'` and issued `SELECT`s only. Every file was opened read-only
  inside an agent container whose archive mounts are themselves mounted `ro`. Nothing was written,
  moved or deleted on any host. The probe scripts were removed from both containers and both hosts
  afterwards.
- **Scrubbing:** archive roots are `<root-1>` … `<root-8>`, hosts are `host-prod` (control plane) and
  `host-store` (the fileserver agent). Release-site stamp files are `<site-a>.nfo` and
  `<site-b>.txt`, a radio-show series is `<series-a>`. The raw samples, paths and the
  placeholder-to-name mapping are kept in the per-bead scratch directory only. All quantities are
  exact.

______________________________________________________________________

## Question

The epic (`phaze-gyj6i`) asks, as its first question: *what do companion files actually contain, by
type, and which content signals tie a companion to a specific media file?* This bead answers it for
`.cue`, `.nfo`, `.txt`, `.m3u`, `.m3u8` and `.pls` (the ingestible companion set,
`INGESTIBLE_COMPANION_EXTENSIONS` in [`src/phaze/constants.py`](../../src/phaze/constants.py)) across
every agent root. The verdict is GO or NO-GO on one claim: **"content is a usable linking and junk
signal."**

The operator's feedback on the epic (2026-10-06, recorded in epic `phaze-gyj6i`'s description) gives
six concrete examples to confirm or refute. §4.6 takes them one at a time. The scope decision,
operator decision 2026-10-06 (`AskUserQuestion` in the dispatch session; durable record: epic
`phaze-gyj6i` description), is as follows. Question as put: *"How wide should the content sample
be?"* Answer as given (selected option label, verbatim): **"Whole archive (Recommended)"**.

______________________________________________________________________

## Method

### 1. Population, by query (counted)

I ran three read-only queries against production Postgres from inside the API container on
`host-prod`, fetching narrow columns and computing in Python. No `LIKE`/`EXISTS` joins were run over
`files`.

1. `agents`, `files` grouped by `(agent_id, file_type)`, and `scan_batches` grouped by
   `(agent_id, configured_root)`.
2. For every `files` row on the fileserver agent: `id, file_type, current_path, original_path,
   file_size, sha256_hash`, plus the distinct `companion_id`s in `file_companions`. That is 145,057
   rows.
3. `scan_batches`, and every row of `orphan_companion_diagnostics`.

The third query matters. **Since `phaze-ehryj` the archive's companions live in two tables, not
one.** A companion is ingested into `files` (written below as *admitted*) only when its own folder
holds media, or when its folder or stem name-matches media in the parent folder. Every other
companion is recorded only as an `orphan_companion_diagnostics` path (*orphan* below). A survey of
`files` alone would never have seen the operator's `NNN.txt` example: all 40 such files are orphans.

### 2. Every file read, by an extractor on the owning agent (counted)

There is one fileserver agent (on `host-store`). Its worker containers mount every root `ro`. I
streamed a small Python feature extractor into the `phaze-agent-worker-meta` container and ran it
with `nice -n 19`. It read the paths from stdin and, for **every** one of the 45,390 companion paths
(40,810 admitted plus 4,580 orphans), it:

- opened the file read-only and read at most 1 MiB. The largest companion is 40,940 bytes, so every
  file was read whole;
- returned per-file features only:
  - size and sha256 of the bytes read;
  - the detected encoding and how it was detected (§4.5);
  - line and alphanumeric counts;
  - CUE keyword counts and `FILE` values;
  - M3U/PLS entries;
  - tokens ending in a media extension;
  - URL domains and ad-vocabulary hits;
  - labelled-field names (`Artist ....:`);
  - counts of timestamped, numbered and `Artist - Title` lines, and the longest consecutive run of
    such lines;
- for files in the stratified sample (§3) only, also returned up to 30 non-blank lines, each cut to
  120 characters, with box-drawing characters stripped, plus the lines of the longest track-like run.

Nothing else left the host. The returned features and excerpts sit in per-bead scratch. None of
them are in this document: every excerpt shape quoted below is rewritten with placeholders.

The extractor was refined over three full passes, about 4 minutes each. The third pass is the one
reported. The changes between passes:

- Pass 2 added all-NUL detection and a Windows-1251 test, and tightened tracklist runs.
- Pass 3 tested CP437 before Windows-1251 (pass 2's order mislabelled 452 `.nfo`, §4.5) and
  stripped leading box-drawing art before matching labelled fields and track lines.

A sample-only pass then added the track-run excerpts. It changed no feature on any of the 1,073
sampled files.

19 rows could not be opened because their `current_path` no longer exists on disk: 10 `.nfo`, 6
`.txt` and 3 `.cue`. Every figure below that says *read* excludes them.

### 3. Stratified sample, manually reviewed (sampled)

I stratified the sample by **extension × table (admitted/orphan) × root**:

- **Allocation:** proportional within each extension, toward targets of 150 `.cue`, 250 `.nfo`,
  250 `.txt`, 200 `.m3u` and all 3 `.pls`.
- **Floors:** each admitted stratum took at least 10 files, and each orphan stratum at least 40,
  because the orphans are where the operator's questions sit. A stratum smaller than its floor was
  taken whole.
- **Dedupe:** within a stratum, a second file with the same database sha256 was skipped, so one
  stamp file could not fill a stratum.
- **Forced in:** all 40 numerically named `.txt` files.
- **Seed:** the random seed was fixed (`73`).

| Extension | Sample size | Strata (population → sampled) |
| --- | ---: | --- |
| `.cue` | 197 | admitted `<root-1>` 80→10, `<root-4>` 35→10, `<root-5>` 2,146→136; orphan `<root-1>` 1→1, `<root-5>` 109→40 |
| `.m3u` | 256 | admitted `<root-1>` 674→10, `<root-4>` 7,130→88, `<root-5>` 6,645→82, `<root-6>` 254→10; orphan `<root-1>` 21→21, `<root-5>` 1,552→40, `<root-6>` 5→5 |
| `.nfo` | 319 | admitted `<root-1>` 1,149→14, `<root-4>` 7,139→89, `<root-5>` 9,845→122, `<root-6>` 256→10; orphan `<root-1>` 63→40, `<root-5>` 1,680→40, `<root-6>` 4→4 |
| `.txt` | 298 | admitted `<root-1>` 215→10, `<root-4>` 132→10, `<root-5>` 5,107→193; orphan `<root-1>` 4→4, `<root-5>` 1,141→43 (+ the 40 numbered files) |
| `.pls` | 3 | admitted `<root-4>` 2→2, `<root-5>` 1→1 |
| **Total** | **1,073** | |

I used the sample to **validate the automated classifier, not to estimate shares**. The shares in §4
are counted over the whole population. The sample answers *how often is that count right*. I read
the excerpts of these files and labelled each one by hand:

- 30 random `.nfo`;
- 30 `.nfo` and 30 `.txt` that the classifier labelled tracklist;
- every one of the 32 sampled `.txt` not labelled tracklist;
- 8 `.cue`;
- the 24 sampled `.m3u` without a resolvable media entry;
- all 52 sampled files labelled junk;
- all 40 numbered files.

I also read 21 further files to settle specific questions: the 17 `<site-a>`-named variants not
caught by repetition, 3 stamp-family variants and 1 template NFO.

### 4. Classifier (counted)

Each read file gets one primary label, applied in this order:

1. **`junk:empty`** — zero bytes, every byte `0x00`, or fewer than 20 alphanumeric characters.
2. **`junk:repeated-stamp`** — the file's exact content (sha256) appears in at least 3 folders, and
   **those folders hold different media**. That means the media sets of the folders that hold any
   media have an empty intersection, and the folder names differ. When the content's folders hold
   no media at all, it means at least 3 differently named folders. This is the rule that separates a
   stamp from a **duplicated release**. The same release folder downloaded three times also yields 3
   identical NFOs, but beside the same media. Those 251 rows (`.m3u` 128, `.nfo` 123) are a dedup
   matter, not junk, and are not counted here.
3. **`junk:site-ad`** — carries a URL domain, has ≤ 40 non-blank lines, contains at least one ad
   word, and has none of the useful signals below.
4. **`tracklist`** — for `.cue`, at least 2 `TRACK` and at least 2 `TITLE` lines. For anything else,
   at least 3 consecutive track-like lines (timestamped, numbered, or `Artist - Title`), or at least
   5 timestamped or numbered lines anywhere.
5. **`media-ref-only`** — has at least one explicit media reference (§4.2) and none of the above.
6. **`release-info`** — at least 3 labelled release fields (artist, title, genre, source, year, size,
   quality, length, label, encoder, rip/release/air date and similar).
7. **`other`** — none of the above.

______________________________________________________________________

## Evidence

### 4.1 Population (counted, by query)

There is one fileserver agent (on `host-store`) and two compute agents, which own no roots. Nine scan
batches exist: eight configured roots plus the live watcher batch.

| Root | Last scan's `total_files` | `.cue` | `.m3u` | `.nfo` | `.txt` | `.pls` | Admitted | Orphan | Media rows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `<root-1>` | 6,095 | 80 + 1 | 674 + 21 | 1,149 + 63 | 215 + 4 | — | 2,118 | 89 | 4,102 |
| `<root-2>` | 8,811 | — | — | — | — | — | 0 | 0 | 8,813 |
| `<root-3>` | 0 | — | — | — | — | — | 0 | 0 | 0 |
| `<root-4>` | 25,866 | 35 | 7,130 | 7,139 | 132 | 2 | 14,438 | 0 | 11,428 |
| `<root-5>` | 103,338 | 2,146 + 109 | 6,645 + 1,552 | 9,845 + 1,680 | 5,107 + 1,141 | 1 | 23,744 | 4,482 | 79,594 |
| `<root-6>` | 820 | — | 254 + 5 | 256 + 4 | — | — | 510 | 9 | 310 |
| `<root-7>`, `<root-8>` | 0, 0 | — | — | — | — | — | 0 | 0 | 0 |
| **Total** | | **2,371** | **16,281** | **20,136** | **6,599** | **3** | **40,810** | **4,580** | **104,247** |

How to read the table:

- Cells read *admitted + orphan*.
- **`.m3u8`: 0 rows in either table.**
- `<root-5>` is the scan the epic cites. Its 4,482 orphans include 246 `<site-a>`-named files. The
  epic gave 245 for that scan; my name filter also catches spelling variants.
- `<root-7>` and `<root-8>` have scan batches but are not in the agent's current `scan_roots`.

### 4.2 Explicit media references and how they resolve (counted)

A reference is any of the following:

- a CUE `FILE` value;
- an M3U non-comment entry ending in a media extension;
- a PLS `FileN=` value;
- in NFO and TXT, any token ending in a media extension.

I resolved each reference by its basename against the `files` table's media rows:

- **sibling** — a media row in the companion's own folder, exact byte-for-byte name;
- **parent** — the same, in the parent folder;
- **ci** — the same as sibling, but case-insensitive;
- **elsewhere** — no sibling or parent match, but a media row with that exact basename exists
  somewhere else on the agent.

A file counts once, by its best outcome. *All exact* means every reference in the file resolved
sibling or parent.

| Extension | Table | Read | Has a reference | Sibling exact | +ci | Elsewhere (unique basename) | Unresolved | All exact |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `.cue` | admitted | 2,258 | 2,258 (100%) | **2,150 (95.2%)** | 12 | 0 | 96 | 2,149 |
| `.cue` | orphan | 110 | 110 (100%) | 0 | 0 | 94 (94) | 16 | 0 |
| `.m3u` | admitted | 14,703 | 14,514 (98.7%) | **14,222 (98.0%)** | 217 | 21 (21) | 54¹ | 14,192 |
| `.m3u` | orphan | 1,578 | 1,548 (98.1%) | 0 | 0 | 838 (788) | 710² | 0 |
| `.pls` | admitted | 3 | 3 | **3** | 0 | 0 | 0 | 3 |
| `.nfo` | admitted | 18,379 | **113 (0.6%)** | 6 | 3 | 0 | 104 | 6 |
| `.nfo` | orphan | 1,747 | 8 (0.5%) | 0 | 0 | 0 | 8 | 0 |
| `.txt` | admitted | 5,448 | 1,453 (26.7%) | **1,412 (97.2%)** | 0 | 0 | 41 | 1,403 |
| `.txt` | orphan | 1,145 | 948 (82.8%) | 0 | 0 | 933 (933) | 15 | 0 |

Percentages in the *Sibling exact* column are of the files that have a reference.
¹ 53 unresolved, plus 1 whose only entries are stream URLs. ² 709 unresolved, plus 1 whose entry
is a relative path into another folder that does hold the media.

**No reference anywhere resolved to a parent-folder media row.**

**What the unresolved references are.** They were characterised, not sampled:

- **`.cue` admitted, 96 unresolved:** 51 name the right stem with a different extension (a `.wav`
  sheet beside an `.mp3`), and 44 name a file that was renamed after the sheet was cut (`cd1.mp3`
  and similar). 1 has a mis-decoded name.
- **`.m3u` admitted, 53 unresolved** (plus the stream-URL file): 1 names the right stem with a
  different extension. In the other 52 the media was renamed after the playlist was written: a
  different group suffix, a site tag appended, or a trailing `.` in the entry.
- **`.nfo` and `.txt` admitted:** the free-text token regex also captures the words before the
  filename (`Files.: <name>.mp3`, `Tracklist for: <name>.opus`). Accepting a token that **ends with**
  a sibling or parent media filename resolves 79 of the 104 unresolved `.nfo` and 10 of the 41
  unresolved `.txt`. That brings `.nfo` to 88 of 113 and `.txt` to 1,422 of 1,453. The `.nfo`
  remainder includes track titles that happen to end in `.wav`, which are not filenames.

**Orphans: references point to media, but in another copy of the release.** By construction an
orphan's folder holds no media, and (beyond the scan's exact-key rule) nothing in the parent folder
matches. Yet orphan references resolve to an exact, **unique** media basename elsewhere in the
archive for 94 of 110 `.cue`, 788 of 1,548 `.m3u` and 933 of 948 `.txt`. The media's folder carries
**the same release-folder name** as the orphan's folder (compared alphanumeric-only, case-folded)
for 82 of 94 `.cue`, 682 of 838 `.m3u` and 928 of 933 `.txt`.

Independently, **2,797 of the 4,580 orphans are byte-identical to an admitted companion**: `.cue` 9,
`.m3u` 791, `.nfo` 1,002, `.txt` 995. And only 838 of the 4,580 orphans have any media row in their
parent folder. The orphans are mostly **leftovers of duplicated release folders whose media copy was
kept elsewhere**, not companions waiting for a parent-folder match. The Recommendation section
hands this to the matching spike.

A smaller orphan cause: **26 orphan companions** (`.cue` 4, `.m3u` 22) reference **`.mp2`** media,
and `.mp2` is not in `EXTENSION_MAP`. That media is never ingested, so no rule could link those
references.

### 4.3 Tracklists (counted, with sampled precision)

| Extension | Read | Labelled tracklist | Formats (counted) | Sampled precision |
| --- | ---: | ---: | --- | --- |
| `.cue` | 2,368 | **2,354 (99.4%)** | CUE `TRACK`/`PERFORMER`/`TITLE`/`INDEX`: all 2,354 have ≥ 2 `INDEX`, 2,332 have ≥ 2 `PERFORMER` | 8 / 8 |
| `.txt` | 6,593 | **5,800 (88.0%)** | numbered 3,333 · plain `Artist - Title` 1,908 · timestamped 559 | **30 / 30** |
| `.nfo` | 20,126 | **3,440 (17.1%)** | numbered 2,718 · plain 539 · timestamped 183 | **23 / 28** (82%) |
| `.m3u` | 16,281 | 25 (0.2%) | numbered 16 · plain 9 | 0 / 3 — all three are `.mp2` file lists |

The formats seen in the read excerpts, as shapes:

- **Timestamped:** `[00:19] <artist> - <title> [<label>]`, `00:00 <artist> - "<title>" (<label> <year>)`,
  `21:06<TAB><artist><TAB><title>`, `[00:00:00] 01. <artist> - <title> [<label>] (2:50)`.
- **Numbered:** `01. <artist> - <title> [<label>]`, `1) <artist> - <title>`, `1. <artist> "<title>" (<label>)`,
  `01 - <artist> - <title>`.
- **Plain:** `<artist> - <title>` or `<artist> – <title>` one per line, often under a header block
  `<show> #<n> - <dj>` / `TX: <date>` / `..::Tracklisting::..`.
- **CUE:** a header with `PERFORMER`/`TITLE`/`FILE "<audio>" MP3`, then per track `TRACK nn AUDIO`,
  `PERFORMER`, `TITLE`, `INDEX 01 mm:ss:ff`. `REM DATE`/`REM GENRE` appear in some.

Where the heuristic is wrong (sampled):

- **NFO false positives (5 of 28):** a multi-part *file listing* (`01. <set>-<date> [60:00]` repeated
  per part) is not a set tracklist, and neither is a compilation pack's list of `.mp2` files. Scene
  NFOs very often carry a one-line listing (`01. <title> [61:51]`), which correctly does *not* reach
  the 3-line threshold.
- **TXT false negatives:** 4 of the 32 non-tracklist `.txt` read are tracklists in formats the
  heuristic misses: `<artist> '<title>' (<label>)`, `artist-title` with no spaces, a `/`-separated
  list, and lowercase titles with no artist. Of the 6 sampled `.txt` labelled `other`, 4 are these.

**Release information in NFO (counted).** 11,616 `.nfo` (57.7%) carry release fields but no
tracklist. Counting non-exclusively, 15,005 `.nfo` (74.6%) have at least 3 release fields. In the
random-30 review all 20 labelled `release-info` did carry artist/title/genre/source/date/length
fields. One `other` was release info in a `<Field....> value` layout the field regex misses.

### 4.4 Junk (counted, with sampled precision)

| Extension | Read | `junk:repeated-stamp` | `junk:empty` | `junk:site-ad` | **Junk total** |
| --- | ---: | ---: | ---: | ---: | ---: |
| `.cue` | 2,368 | 0 | 0 | 0 | **0** |
| `.m3u` | 16,281 | 0 | 196 | 0 | **196 (1.2%)** |
| `.pls` | 3 | 0 | 0 | 0 | **0** |
| `.nfo` | 20,126 | 3,892 | 199 | 21 | **4,112 (20.4%)** |
| `.txt` | 6,593 | 311 | 8 | 34 | **353 (5.4%)** |
| **All** | **45,371** | **4,203** | **403** | **55** | **4,661 (10.3%)** |

**Sampled precision: 52 of 52.** Every sampled file labelled junk was junk on reading. Recall was
not measured. The `other` bucket (1,057 files) is mostly not junk. The `other` files I read were
release information or tracklists in layouts the heuristics miss, `.mp2` file lists, and one
internet-radio station playlist (4 copies) that names no media.

The signatures found:

- **Exact repeated files, by hash.** 3,310 sha256 values occur 2 or more times, covering 11,058 rows.
  That figure includes duplicated releases. Only **27** of those contents meet the stamp rule, and
  they cover **4,203 rows**. The largest stamp groups hold 3,653, 87, 81, 79, 44, 34, 33, 32, 31, 20
  and 13 rows. An operator reviewing stamps by content therefore faces **27 decisions, not 4,203**.
- **Site stamps.** The 27 stamp contents break down as follows:
  - the `<site-a>.nfo` family: 8 contents, 3,845 rows;
  - the `<site-b>.txt` family: 5 contents, 246 rows;
  - usenet and torrent-board adverts: 6 contents, 59 rows. One of them is a 25,848-byte UTF-16
    "info" file;
  - a per-show **template NFO**: 5 contents, 32 rows. Its bytes are identical across different
    episodes, so it carries only the show name and one release date. **This is the one borderline
    family**: it is not wrong about the show, it just says nothing about the episode;
  - an IRC "sorry no .nfo available" placeholder: 10 rows;
  - a "please help with the tracklist!" placeholder: 7 rows;
  - a one-line recording credit: 4 rows.

  I read at least one file from 19 of the 27 contents. The 8 unread contents each share a filename
  and size class with a read one.
- **Empty or near-empty.** There are 368 **all-NUL** files (`.nfo` 187, `.m3u` 181): non-zero size
  but every byte `0x00`. 358 of them are in `<root-5>`, which looks like an interrupted download or
  copy. 23 are zero-byte files and 12 have fewer than 20 alphanumeric characters.
- **Ads.** 55 small files carry only a site pitch: torrent-proxy notices, "downloaded from …"
  readmes and RSS-feed adverts.
- **Corrupt or misnamed content inside a stamp name.** 31 copies of a `<site-a>`-named file are
  binary garbage, 21 are all-NUL, and 5 are an HTML "page not found" document saved as `.nfo`. All
  are caught by repetition or emptiness.

### 4.5 Text encodings (counted)

I detected encodings on the raw bytes, in this order:

1. a UTF-8 BOM, then a UTF-16 BOM;
2. NUL bytes at every other offset, read as UTF-16-LE with no BOM;
3. every byte `0x00`, read as all-NUL;
4. strict UTF-8 decoding (ASCII when no byte is ≥ 0x80);
5. **CP437** when at least 20 high bytes are present and ≥ 50% of the bytes ≥ 0x80 fall in
   0xB0–0xDF, the CP437 box-drawing and block range (`░▒▓│┤╡ … █▄▌▐▀`);
6. Windows-1251 when at least 3 Cyrillic lowercase words decode and they outnumber accented-Latin
   words;
7. otherwise **Windows-1252** as the fallback.

| Extension | ASCII | UTF-8 | UTF-8 BOM | UTF-16 (BOM / no BOM) | CP437 | Windows-1252 (fallback) | Windows-1251 | All-NUL |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `.nfo` | 1,958 | 84 | 81 | 0 / 1 | **13,468** | 4,342 | 5 | 187 |
| `.txt` | 4,457 | 897 | 327 | 130 / 0 | 0 | 782 | 0 | 0 |
| `.cue` | 1,454 | 18 | 448 | 117 / 0 | 0 | 331 | 0 | 0 |
| `.m3u` | 16,096 | 0 | 0 | 0 / 1 | 0 | 3 | 0 | 181 |
| `.pls` | 2 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |

The ASCII column includes the zero-byte files.

**The NFO CP437 case is confirmed.** 13,468 of 20,126 `.nfo` (66.9%) are CP437 art by the rule
above. In 21 UTF-8 companions the art had already been converted to Unicode box-drawing
characters. Two caveats, both measured:

- **CP437 and Windows-1252 cannot be told apart from bytes when there are few high bytes.** Of the
  4,342 `.nfo` that fell to the Windows-1252 fallback, 3,852 are the `<site-a>` stamp, whose border
  byte decodes as `¦` in Windows-1252 and `ª` in CP437. The other 490 include ASCII art that uses `¯`
  and other Latin-1 punctuation, which decodes plausibly either way. It makes no difference to
  classification: the features read ASCII letters, digits and punctuation.
- **Every Windows-1251 detection on `.nfo` was wrong.** I read all 5, and all were CP437 art whose
  block bytes decode to Cyrillic capitals (`█` → `Ы`, `▄` → `Ь`, `▀` → `Я`). No Windows-1251 NFO
  was found. The 13 `.txt` files with Cyrillic **names** contain ASCII or Windows-1252 text.
  An earlier extractor pass, which tried Windows-1251 before CP437, mislabelled 452 `.nfo` this way.

### 4.6 The operator's examples

| Operator's example (epic `phaze-gyj6i`) | Verdict | Evidence |
| --- | --- | --- |
| A `<site>.nfo` stamp file "holds no information: junk" | **Confirmed, with 3 exceptions** | 3,895 rows carry a `<site-a>` name (3,892 `.nfo`, 3 `.txt`). 3,845 are `junk:repeated-stamp`, 23 `junk:empty` (all-NUL), 12 `junk:site-ad` and 10 missing on disk. I read the remaining 5 and all 12 site-ad variants. Every one starts with the same site advertisement, but **4 have content appended after it: 3 carry a real tracklist or release fields for the release beside them**, and 1 carries a second group's NFO art. **A filename-only rule would delete those 3; the content rule keeps them.** |
| `00-<artist>-<event>-<date>-<group>.m3u` "is the playlist for that set's mp3" | **Confirmed** | 16,078 scene-style `00-*.m3u`; 15,862 carry media entries. Across all admitted `.m3u`, 98.0% of files with an entry resolve it exactly to a sibling media row. The operator's "pretty useless" is right about *metadata* (an M3U adds nothing but the filename) but **wrong about linking**: the entry is the single strongest exact link signal measured here. |
| `00-<artist>-<event>-fm-<date>-<group>.nfo` "may be junk, but it may include the tracklist … format inside the file is unknown" | **Mostly release info, sometimes a tracklist, rarely junk** | 860 `00-*-fm-*.nfo`: 752 `release-info`, 48 `tracklist`, 58 `other`, 1 `media-ref-only`, 1 `junk:empty`. Across all 15,885 scene-style `00-*.nfo`: 11,421 release-info, 3,393 tracklist, 766 other, 176 empty, 97 media-ref-only, 32 repeated-stamp. The format is CP437 art around a labelled block (`Artist ....: <artist>`, `Genre`, `Source`, `Air date`, `Length`), followed by a one-line file listing or, in 3,393 cases, a numbered or timestamped tracklist. |
| `<Artist> - Live @ <Festival> - <date>.cue` "is the CUE for the mp3 in its directory: 1:1 naming, but that may not always be the case" | **Confirmed, including the caveat** | 2,150 of 2,258 admitted `.cue` (95.2%) name a sibling media file exactly in `FILE`, and 12 more case-insensitively. The 96 that do not are mostly the caveat: the same stem with a different extension (51), or a renamed file (44). CUE is the richest companion: 99.4% are tracklists with per-track `INDEX` timing. |
| `Tracklist.txt` is the tracklist; "tracklists may not always be tracklist.txt" | **Both confirmed** | 466 files are named exactly `Tracklist.txt` (any case), and 461 are tracklists by content. 682 `.txt` are named like a tracklist (`tracklist`, `tracklisting`, `playlist`, `tracks`, the Cyrillic equivalent), and 665 of them are. But **5,135 of the 5,800 `.txt` tracklists (88.5%) have some other name**, usually `<show> #<n>.txt` or `<artist> - <set> (<date>).txt`. |
| `<name>.txt` site stamps are "likely garbage" | **Confirmed** | `<site-b>.txt`: 247 rows. 246 are `junk:repeated-stamp`, from 5 distinct contents of 64–614 bytes: an RSS-feed advert, one variant with the site's category list. 1 is a 45-byte variant labelled `other`. Other stamp families are in §4.4. |
| `046.txt` (any number) is "likely the tracklist for that episode of a series" | **Confirmed** | §4.7 |

### 4.7 Numbered files (`NNN.txt`)

**All 40 numerically named companions in the archive are `.txt` orphans in one folder**:
`<series-a> 000-049/<sub-folder>/`, numbers 003–050. Nothing in the `files` table has a purely
numeric stem.

- **Content:** all 40 classify as tracklists, numbered (`1. <artist> "<title>" (<label>)`).
- **Episode in the header:** 36 open with a header carrying `#<n>` equal to the filename's number
  (`<series-a> with <dj> #<n>` / `<date>`). 1 carries the number without `#`. 3 have no header and
  open straight on track 1.
- **Mapping:** **the number is the episode.** The series comes from the grandparent folder's name
  (`<series-a> 000-049`), and the header confirms it in 37 of 40.
- **Resolution:** the parent folder (`<sub-folder>/`) holds no media. The **grandparent** folder
  holds exactly **one** media row, episode 003 (`<SERIES-A>_003.mp3`), and `003.txt` is its
  tracklist. **1 of 40 resolves to a media row, and only two levels up.** The scan's companion rule
  consults one level, so not even that one can link under the current rule. No other episode from
  000 to 049 of `<series-a>` is in the archive as a media row. The only other numeric match for
  episode 004 is an unrelated podcast. So the files are informative, but 39 of the 40 describe audio
  phaze does not have.

______________________________________________________________________

## Verdict

**GO — content is a usable linking signal and a usable junk signal**, with the boundaries below.

- **Linking: GO for `.cue`, `.m3u`, `.pls` and reference-bearing `.txt`.** Among admitted files with
  a reference, the reference names a sibling media file exactly in **95.2%** of `.cue`, **98.0%** of
  `.m3u`, **3 of 3** `.pls` and **97.2%** of `.txt`. Case-insensitive and suffix matching add a
  little more. **`.nfo` is not a linking signal:** only 0.6% name a media file at all.
- **Orphans: GO, as a duplicate signal rather than a link.** References in orphan companions do
  **not** resolve to sibling or parent media: 0 of 2,614 orphans with a reference. They do resolve to
  a **unique** exact basename elsewhere in 1,815 of them, overwhelmingly in another folder of the
  same release name.
- **Junk: GO.** Exact content repetition across folders holding different media, all-NUL or empty
  bytes, and small ad-only files together flag **4,661 of 45,371 read companions (10.3%)**. That is
  `.nfo` 20.4%, `.txt` 5.4%, `.m3u` 1.2%, `.cue` 0%. Sampled precision is **52 of 52**. The 4,203
  stamp rows reduce to **27 distinct contents**.
- **Tracklists: strong for `.cue` and `.txt`, weaker for `.nfo`.** Sampled precision is 8/8, 30/30
  and 23/28 respectively. The `.nfo` misses are multi-part file listings. Not linking- or
  junk-relevant, but it matters to any tracklist ingestion that follows.

______________________________________________________________________

## Recommendation

1. **For the linking-accuracy spike (`phaze-9aker`):**
   - Use content references before name similarity, in this order: CUE `FILE`, M3U/PLS entries, then
     NFO/TXT tokens that *end with* a media filename.
   - Resolve exact against the sibling folder, then the parent folder, then case-insensitively.
     Measure the stem-match-with-a-different-extension case (51 `.cue`) as its own rule, not under
     close-name matching.
   - **Re-examine the orphan premise before measuring close-name matching on orphans.** The orphans
     sampled here are mostly copies of releases whose media sits in another same-named folder, and
     2,797 of 4,580 are byte-identical to an admitted companion. Close-name matching against the
     *parent* folder cannot link them, because their media is not there. They are a dedup and junk
     population as much as a linking one.
2. **For the junk-deletion design (`phaze-ib3ub`):**
   - Propose deletion **by content group, not by file.** The 27 stamp contents, the all-NUL files
     and the ad files map naturally onto a review queue with one approval per content group. This
     follows operator decision 2026-10-06 (`AskUserQuestion` in the dispatch session; durable record:
     epic `phaze-gyj6i` description). Question as put: *"\"Automatically mark useless files for
     deletion\": how should a marked file surface?"* Answer as given (selected option label,
     verbatim): **"Review queue, you approve (Recommended)"**.
   - **Do not key on filename alone.** 3 of the 3,895 `<site-a>`-named files carry the release's
     real tracklist or release info appended after the stamp.
   - Keep duplicated releases (identical content beside the same media) out of junk. They are the
     dedup feature's business.
3. **Encoding:** decode NFO with the detection order above. **Drop Windows-1251 detection, or gate
   it behind CP437 detection:** it produced 5 false positives and no true positive on `.nfo`.
   Expect CP437 and Windows-1252 to be indistinguishable on low-high-byte files, and do not let any
   decision depend on telling them apart.
4. **Numbered series files:** map `NNN.txt` to episode `NNN` of the series named by the grandparent
   folder, and use the content header as confirmation. In today's archive this links 1 file, and
   only by looking two levels up. It is not worth a dedicated rule unless the series audio arrives.
5. **Two observations outside this bead's scope, offered as candidate beads:**
   - `file_companions` holds links **only for `<root-4>`**: all 14,438 of its admitted companions and
     no others. The 26,372 admitted companions under `<root-1>`, `<root-5>` and `<root-6>` have none.
     `associate_companions` runs only on the operator-triggered `POST /associate`, so it appears not
     to have been run since those scans.
   - `.mp2` media is not ingested at all. 26 orphan companions reference `.mp2` files, and so do 2
     admitted `.m3u` (16 entries).

______________________________________________________________________

## Lessons and their general form

*A survey of one table describes only what that table admits.* Since `phaze-ehryj`, the scan
filters companions before they reach `files`. The epic's examples came from its list of unmatched
companions, which live in `orphan_companion_diagnostics`. A `files`-only population would have
reported 0 numbered files and only 3,604 of the 3,895 `<site-a>` copies. The general form is: **before sampling
a population, find every table a producer can route a member of it to.** It is written down here
because it is specific to this survey's design rather than to a code path.

*Byte-identical is not junk, and "looks like a tracklist" is not useful.* An exact-hash repetition
rule (at least 3 folders) was the first junk heuristic tried. Measured against this archive, it also
catches real release NFOs and playlists duplicated across copies of the same release folder: 251
rows. Exempting "useful-looking" content to protect those rows let 79 copies of a `<site-b>`
advertisement through, because its category list is numbered. The rule that discriminates asks
whether the identical bytes sit **beside different media**. The general form is the one `CLAUDE.md`'s
verification-fidelity rule 3 already states: **an independent signal is not automatically a
discriminating one; feed it the wrong case.**
