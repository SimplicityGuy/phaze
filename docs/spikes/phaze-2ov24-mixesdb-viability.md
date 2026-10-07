# phaze-2ov24: is MixesDB a viable second tracklist source?

Spike under epic phaze-5d0wg. Measured 2026-10-06. No product code was written or committed.

## Question

Can phaze read MixesDB (`www.mixesdb.com`) set pages with an honest, identified client inside the
site's published rules? Is the content licence compatible with storing the data in a personal
catalogue? What share of sets does MixesDB cover?

## Method

Request budget, as given in this bead's assignment on 2026-10-06 (the question put to the approver is not
quoted here, so this is not an attributed decision): at most 200 requests to `mixesdb.com`, each at
least 4 s after the last. Every request went through one throwaway `curl` wrapper that:

- sent `User-Agent: phaze/2026.10.2 (+https://github.com/SimplicityGuy/phaze)`, which is exactly what
  `honest_user_agent_token()` in `src/phaze/services/tracklist_scraper.py` returns (run, not
  re-typed);
- enforced a 5 s minimum gap, over the 4 s `Crawl-delay`;
- appended every request to a counter log and refused at 200.

No UA rotation, no browser, no challenge solving, no proxy. No URL matching a `Disallow` rule was
requested. In particular no `?title=`, no `?action=`, no `Special:` page, no `/db/`, `/tools/`,
`/w/Talk`, `/w/Template`. Parsing scripts were throwaway and are not committed.

Coverage procedure, per sampled set (artist, festival, year):

1. One request to the site's own REST search, `/w/rest.php/v1/search/page?q=<artist event year>&limit=5`.
   A hit needs a title that starts with the year and contains the artist and the festival name.
2. For a search miss, one request to the artist's category page `/w/Category:<Artist>`. It lists
   that artist's sets. A listing that shows `(N out of N)` is complete. A listing that shows
   `(200 out of M)` with M above 200 is capped, so a miss against it proves nothing.
3. For each hit, one `GET /w/<Page>` to inspect the tracklist and any cue marks.

## Evidence

### Access: an honest client is served

All 77 requests returned HTTP 200. None returned a challenge page, 403, 429 or a captcha. Plain
`GET /w/<Page>` pages, `Category:` pages, `robots.txt`, the REST endpoints and the bare `api.php`
help page were all served to the identifying UA.

This contradicts the earlier note that a fetch tool was "bot-blocked on the legal page". The
honest client read it with a single plain GET. That block was specific to the earlier tool, not to
`mixesdb.com`.

### Request tally (77 of 200 spent, 123 unspent)

| Purpose | Requests |
|---|---|
| `robots.txt` | 1 |
| `MixesDB:Legal_stuff`, `MixesDB:About` | 2 |
| Bare `/w/api.php` (path only, no query) | 1 |
| `Category:1001Tracklists` | 1 |
| REST search: 36 sample queries, 2 format probes, 1 search for `1001tracklists.com` | 39 |
| One malformed probe query I wasted (empty result) | 1 |
| Artist category pages (1 format probe + 19 distinct artists) | 20 |
| Set pages, `GET /w/<Page>` | 11 |
| REST page JSON (`/w/rest.php/v1/page/<key>`) | 1 |
| **Total** | **77** |

Outcome classes: 77 HTTP 200. 12 of them were an empty `{"pages":[]}` body (10 sample searches, one
format probe, the wasted probe). Zero blocked or throttled.

### robots.txt (fetched this run), the rules relied on

The group for `User-agent: *` carries `Crawl-delay: 4` and, among others:

```
Disallow: /db/
Disallow: /tools/
Disallow: /*?title=
Disallow: /*?action=
Disallow: /*.mp3
Disallow: /MixesDB_Search.php
Disallow: /w/Template
Disallow: /w/Special
Disallow: /w/Talk
Disallow: /w/Category:Date
Disallow: /w/Category:Mixcloud   (and a list of player, chart and hidden categories)
Disallow: /*?*&action=history   (also info, edit, submit, purge)
Disallow: /*?*oldid=
Disallow: /sitemap.txt
```

A second group (`ia_archiver`, `AhrefsBot`, `CCBot`, `HTTrack`, `libwww`, and others) gets
`Disallow: /`. There is also a separate wget entry:

> Sorry, wget in its recursive mode is a frequent problem. Please read the man page and use it
> properly; there is a --wait option you can use to set the delay between hits, for instance.

`User-agent: wget` gets `Disallow: /`. A `phaze/...` UA matches none of the named groups, so the
`*` group governs. `Category:1001Tracklists` and ordinary artist categories are not disallowed.
`rest.php` and `api.php` have no rule of their own.

### `api.php` and the REST API

- Bare `GET /w/api.php` returns the "MediaWiki API help" page (200).
- Every real `api.php` call needs `?action=...`. That textually matches `Disallow: /*?action=`, so
  it was **not** exercised. Reordering parameters (`?format=json&action=query`) would dodge the
  pattern while breaking its intent. I did not do that. The third-party claim that `api.php` works
  is true as to the help page. Whether it is usable under the robots rules is **no**.
- The MediaWiki REST API under `/w/rest.php/v1/` is served, has no `?action=`, and is linked from
  every page by the site itself (`/w/rest.php/v1/search`). It gave:
  - `search/page?q=...&limit=N` returns JSON with `key`, `title`, `excerpt`. Search is full-text
    and fuzzy. It often ranks body matches above the set you want.
  - `page/<key>` returns JSON whose `source` field is the page wikitext. The tracklist is a
    numbered list like `# [00:20] Artist - Title [Label]`. This is a much easier parse than HTML.
    The JSON's `license` field is empty (`{"url": "", "title": ""}`), so it cannot be used as the
    licence statement.

### Licence text (`MixesDB:Legal_stuff`, retrieved this run), quoted verbatim

> If not stated otherwise, all tracklists on MixesDB created by the community are released under
> the Creative Commons Attribution-Share Alike 3.0 United States license: You may copy and alter
> parts of it and distribute it online without attributing MixesDB as long as the content is
> distributed under the same or a similar license.
>
> I.e. you can copy-paste tracklists from MixesDB without giving credit to MixesDB as long as the
> website you post on also allows copying them without giving credit.
>
> All other text like this page, interface text, the "Help", "MixesDB" and "User" namespaces and
> comments are owned by their respective authors (see the "history" of each page). Copying such
> text requires crediting either the author or MixesDB in general.

The page links `creativecommons.org/licenses/by-sa/3.0/us/deed.en`. Other clauses that bear on a
client:

> Images, trademarks, service marks ... are the property of their respective owners ... MixesDB can
> not grant any rights to use any otherwise protected materials.

> The database is stored on a server in United States ... User agrees ... (2) not to use the Service
> for illegal purposes; (3) not to interfere or disrupt networks connected to the Service ...

> MixesDB ... may terminate your password or account, block access and remove and discard any
> content ... for any reason ...

`MixesDB:About` adds: "We don't offer any downloads or secret ways to get download links." The
footer reads "This site is not for downloading sets."

What the 11 fetched set pages show: no per-page licence override ("if not stated otherwise" was
never triggered), and no per-page licence line at all. I did not fetch the CC legal code, so
anything beyond what MixesDB's own statement says is **unverified** here.

### Coverage sample: public sets only, NOT the archive's distribution

I have no archive access. The sample is 36 headline sets I chose from memory across Ultra, Tomorrowland,
EDC, Creamfields, Glastonbury, Coachella, Awakenings and Burning Man, 2006 to 2023. I believe each
took place. I did not verify that, so a "miss" may be a set that never happened or was never
filmed or recorded. The selection leans toward big mainstage sets. The archive is mostly
full sets of this kind, but its real mix is unknown.

Sample (artist, event, year): Swedish House Mafia Ultra 2018 · Tiesto Ultra 2018 · Martin Garrix
Ultra 2018 · David Guetta Ultra 2017 · Hardwell Ultra 2015 · Armin van Buuren Ultra 2019 · Martin
Garrix Tomorrowland 2019 · Dimitri Vegas Tomorrowland 2018 · Hardwell Tomorrowland 2014 · Afrojack
Tomorrowland 2014 · Alesso Tomorrowland 2018 · Charlotte de Witte Tomorrowland 2022 · Amelie Lens
Tomorrowland 2022 · Adam Beyer Tomorrowland 2019 · Tiesto EDC 2019 · Martin Garrix EDC 2018 ·
Kaskade EDC 2015 · Above & Beyond EDC 2017 · Zedd EDC 2017 · Carl Cox Creamfields 2019 · Paul van
Dyk Creamfields 2014 · Fatboy Slim Glastonbury 2019 · Eric Prydz Coachella 2018 · Swedish House
Mafia Coachella 2022 · Daft Punk Coachella 2006 · Skrillex Coachella 2023 · Fred again.. Coachella
2023 · Calvin Harris Coachella 2019 · Carl Cox Coachella 2019 · Richie Hawtin Awakenings 2018 ·
Nina Kraviz Awakenings 2018 · Solomun Burning Man 2018 · Black Coffee Coachella 2022 · Disclosure
Coachella 2019 · Marshmello Coachella 2019 · Kaskade Coachella 2018.

Results, as quantities (n = 36 sample items; items 26 and 27 resolve to the same MixesDB page, so
there are 35 distinct targets and 11 distinct matched pages):

| Outcome | Items | Share of 36 |
|---|---|---|
| Matching page found | 12 | 33% |
| No page, and the artist's category listing is complete, so an absence | 16 | 44% |
| No page by search, and the category listing is capped at 200, so undetermined | 8 | 22% |

So the share with a matching page is **12/36 = 33%** (a floor). Among the 28 items where the answer
is determinable it is 12/28 = 43%. If every undetermined item turned out to exist it would be
20/36 = 56%. The answer is somewhere in 33% to 56% for this sample, not a point estimate. Search
alone found 12 hits and 24 misses. The artist category listings then confirmed 16 of those misses
and left 8 open. A page filed under a different artist credit (a b2b, a VA page) would be missed by both
methods.

Of the 11 matched pages:

| Property | Pages |
|---|---|
| Has a non-empty tracklist | 10 of 11 (one page exists with an empty tracklist) |
| Has any cue mark on tracks (`[..]` before the track) | 8 of 11 (73%) |
| Has `mm:ss` cue marks | 3 of 11 (27%) |
| Has minute-only marks (`[05]`, `[008]`, `[0?]`) | 5 of 11 |
| No cue marks (plain ordered list) | 3 of 11 |

Cue marks that exist are mostly **minute-granular**, not second-granular. One page mixed both
(24 `mm:ss` and 9 minute-only on one page). A few have `?` placeholders (`[07:??]`, `[0?]`). Of
the 10 pages with a tracklist, the track counts ran 12 to 55 entries.

### 1001Tracklists mirroring

None of the 11 set pages contains the string `1001` anywhere in its HTML, links included.
`Category:1001Tracklists` exists, but it is not an ID mirror. It is a category of sets *produced by*
the 1001Tracklists site, its "Exclusive Mix" and "Spotlight Mix" podcast series: 16 mixes, 12
images, 2014 to 2025. A full-text search for `1001tracklists.com` returned only those mixes, whose
matches are SoundCloud and Mixcloud URLs of 1001Tracklists' own channel, not tracklist URLs. So
**MixesDB does not carry 1001Tracklists IDs or URLs as a cross-reference**, in this sample or in the
site's own search. Joining the two sources has to go by artist, event and date text.

## Verdict

**GO** for MixesDB as a second tracklist source, with two conditions.

- **Access: viable.** An honest identified client is served on every path tried, with no block, and
  the site publishes a documented REST API that is not covered by a `Disallow`.
- **Licence: compatible with storing the data in a personal catalogue.** Tracklists are CC BY-SA 3.0
  US "if not stated otherwise", by MixesDB's own statement on its legal page. A private catalogue
  that does not republish is not caught by ShareAlike. If the data is ever shared, ShareAlike
  applies. This is a reading of the site's statement, not legal advice.
- **Premise corrections.** (1) The "bot-blocked" report does not hold for an honest client. (2) The
  third-party `api.php` claim only holds for the help page, since real calls need `?action=`, which
  robots disallows. The REST API is the correct door. (3) `Category:1001Tracklists` is not a mirror
  of 1001Tracklists IDs.
- **Coverage: unknown for the archive.** The 33% to 56% above is a public mainstage sample of 36
  and must not be read as the archive's rate. It says the source is not empty for this kind of
  set and that most cues are minute-level, which limits its use as a cue-point source.

Condition 1: before a build bead is sized, measure the real archive. Take at least 100 archive
sets, derive artist, event and date from existing metadata, run the same two-step lookup (about 2
requests per set, so around 200 to 300 requests at 5 s spacing, under 30 minutes), and record
**counts only**: matched, absent-by-complete-listing, undetermined, with a cue and with `mm:ss` cues.
Condition 2: the build must stay on the REST API and honour the rules below.

## Recommendation

1. Build MixesDB as a second source **after** the archive measurement above. If the matched share
   on real archive sets is low, drop it. This spike cannot decide that.
2. Use `rest.php/v1/search/page` plus `rest.php/v1/page/<key>` (wikitext). Do not use `api.php`, do
   not use `?action=` or `?title=`, and never touch any path in the `Disallow` list. Treat a change
   in `robots.txt` as a stop.
3. Reuse `honest_user_agent_token()`. Throttle to at least 5 s between requests, which exceeds the
   `Crawl-delay: 4`. Make that a per-host setting. Cache every fetched page and its revision.
   Fetch each set once.
4. Resolve a set from artist, event and date, using the artist `Category:` listing when search is
   ambiguous. Category listings are capped at 200 entries, so large artists need search plus the
   date prefix of the title. Do not read a search miss as an absence.
5. Parse `# [cue] Artist - Title [Label]`. Model the cue as `(value, granularity)` with granularity
   `minute | second | unknown`, and tolerate `?` placeholders and no cue at all. Never treat a
   minute mark as a second-accurate cue.
6. Stop on any 403, 429 or challenge page and report it. Never evade.
7. Attribution and licence obligations a build must meet:
   - Store with every imported tracklist: the page URL, revision id, retrieval date and the licence
     string "CC BY-SA 3.0 US, as stated on MixesDB:Legal_stuff".
   - Show the source link in the admin UI wherever imported data appears. MixesDB says it does not
     need credit as long as the re-use is under the same or a similar licence. A visible link is
     cheap and protects against that clause being read narrowly.
   - Do not republish or export imported tracklists unless the recipient gets them under the same
     or a similar licence. ShareAlike is the term that bites.
   - Do not copy images, comments or the Help, MixesDB and User namespaces. They carry other terms.
   - Do not use the site to fetch audio or links to audio. MixesDB says it is "not for downloading
     sets".
   - Read the CC legal code for 3.0 US before any public redistribution. I did not.
8. Do not store 1001Tracklists IDs against MixesDB rows, since there are none to read.
