# Recorded 1001Tracklists search-result pages (phaze-fq9h.6)

Real HTML, captured live on **2026-08-03** by `scripts/capture_tracklist_search.py`, from a
residential IP, paced through the same module-level `reserve_host_request_slot()` schedule the
scraper and renderer share (robots.txt `Crawl-delay: 8`, whole host), with phaze's honest
identifying User-Agent.

**Exactly 6 host requests were spent to produce this directory, and these bytes exist so nobody
has to spend them again.** The whole-host budget is one request per 8 s — the binding constraint on
the entire drain (phaze-fq9h) — so result scoring (phaze-fq9h.6) is developed and regression-tested
against these files, never against the live site. Re-capture only when the site's markup is
suspected of having changed, and say so in the bead.

No browser was used. phaze-fq9h.1 measured the search endpoint as **not Turnstile-gated**: a plain
HTTP POST to `search/result.php` returns the results page. All 6 requests returned HTTP 200 on the
first attempt. Detail pages still need the renderer; search does not.

| File | Query | Rows | Bytes | Why it was worth a request |
|------|-------|-----:|------:|----------------------------|
| `sven-vath-time-warp-2024.html` | `Sven Vath Time Warp 2024` | 30 | 152,854 | The wrong-match case (below) |
| `sven-vath-time-warp.html` | `Sven Vath Time Warp` | 30 | 153,439 | Recurring festival, no year discriminator |
| `carl-cox-space-ibiza-2016.html` | `Carl Cox Space Ibiza 2016` | 30 | 155,497 | Long residency: many same-artist, same-venue, near-dated decoys |
| `amelie-lens-awakenings.html` | `Amelie Lens Awakenings` | 30 | 154,854 | A second artist, so the row shapes aren't Sven Väth-specific |
| `time-warp-2024.html` | `Time Warp 2024` | 30 | 156,663 | Event-only: many artists, one event, one date — only artist separates them |
| `no-such-set.html` | `Zzyzx Quorum Nonesuch Festival 2019` | 30 | 154,354 | The below-threshold case (below) |

Each `.html` has a `.json` sidecar recording the query, the reason it was chosen, the final URL,
HTTP status, byte count and parsed-row count at capture time.

These are public search results from a public site about public events. No local-archive
identifier is involved — the queries are typed strings, not archive filenames.

## The two findings that changed the code

### 1. The site orders results CHRONOLOGICALLY, not by relevance

Each capture decomposes into at most two contiguous date-descending runs: two of the six are
descending throughout, and the other four contain exactly one inversion, where a second,
lower-relevance block begins. Within a block the order is purely chronological — there is no
relevance component at all. "The top row" therefore means "the most recent row that matched some
keywords", which is not "the row that is this file". That is the mechanism behind the spike's wrong
render, and it is why scoring exists at all.

Two captures make the consequence concrete:

- **`time-warp-2024.html` — the wrong-match case.** For a file that is the Sven Väth Time Warp set
  of 2024-10-25, the top row is `Miss Monique @ MiMo Radio 009 (Floor 1, Time Warp, Maimarkthalle
  Mannheim)` dated 2024-11-05. Same festival, same venue, same year, wrong artist. The correct row
  is at **rank 4**. This is not a planted decoy — it is what the site actually returns, ranked
  above the right answer by its own ordering. The scorer gives the correct row 84 and that top row
  32, and selects the correct one.

- **`no-such-set.html` — the below-threshold case.** A query for a set that does not exist returns
  **30 rows anyway**, topped by `Sunburn Festival - Official Aftermovie 2019`. There is no
  empty-page signal to detect: an unmatchable query and a matchable one are indistinguishable by
  row count. A blind top-row take renders an aftermovie video page, which has no tracklist to
  parse — the "wrong, empty set" the spike produced. The scorer's best candidate here is 35,
  and the verdict is `LookupOutcome.NOT_FOUND` with nothing rendered.

A third shape only these captures could have revealed: for a file carrying a year but no resolved
day, two rows dated **2002-04-06 and 2003-04-05** score **85** — *higher* than the correct 2024 row's
81 — because their event text (`Sven Vath @ Time Warp, Germany`) is a cleaner match than the correct
row's longer venue string. Nothing but the year gate stops the scorer picking a 2002 set for a 2024
file. That is why year disagreement disqualifies outright instead of merely subtracting points.

### 2. One search-row selector was dead — `TracklistSearchResult.date` was always `None`

Audited every search-row selector against all **180 captured rows** (this was worth checking:
phaze-fq9h.4 found every per-track *detail* selector matching zero nodes, filed as phaze-2akf).

| Selector / field | Rows matched | Verdict |
|---|---:|---|
| `_SEARCH_ITEM_SELECTOR` = `.bItm` | 180/180 | **live** |
| `_SEARCH_RESULT_LINK_SELECTOR` = `a[href*='/tracklist/']` | 180/180 | **live** |
| `_EXTERNAL_ID_PATTERN` | 180/180 | **live** |
| `_SEARCH_LINK_TEXT_ARTIST_SEPARATOR` = `" @ "` | 148/180 | **live, partial by design** |
| `_HREF_DATE_PATTERN` (as written) | **0/180** | **DEAD** |
| `_HREF_DATE_PATTERN` (fixed) | 180/180 | live |
| `_SEARCH_ROW_DATE_SELECTOR` = `div[title="tracklist date"]` (new) | 180/180 | live |

The href pattern ended `(?:[/?#]|$)`, but the site now appends `.html` to the slug
(`…-germany-2024-10-25.html`), so it could never match a live href — the date field was silently
`None` on every result. Fixed by consuming an optional extension before the tail anchor, and the
row's own displayed date cell is now the primary source with the href as fallback.

This one mattered more than the count suggests: date is the only signal separating a recurring
festival's editions, so a scorer trusting that field would have ranked completely date-blind while
looking like it was checking dates. `test_tracklist_result_scorer.py` asserts the two date sources
agree on all 180 rows, so a future drift in either is caught rather than silently degraded.

The 32 rows with no `" @ "` separator are not a defect: they are promo/aftermovie video entries
(`Sunburn Festival - Official Aftermovie 2019`) that genuinely have no artist. They are parsed with
`artist`/`event` as `None` and disqualified from selection, rather than being coerced into a
plausible-looking string the scorer would then score against.
