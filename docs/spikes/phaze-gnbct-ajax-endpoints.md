# Spike phaze-gnbct: do the `/ajax/` JSON endpoints answer an honest-UA request without a challenge?

Date: 2026-10-06. Epic: phaze-5d0wg. Budget: operator-approved "Up to 30" live requests (2026-10-06). **Spent: 10.**

## Question

Do `/ajax/search_tracklist.php`, `/ajax/search_track.php` and `/ajax/get_medialink.php` on
`www.1001tracklists.com` return usable JSON to an honest-User-Agent request made from the render session, with no
Turnstile interstitial? Does the JSON search carry enough (set id, url name, title, artist, event, date) to pick a
set without a detail render? Does `get_medialink` return per-track Spotify / Beatport / Apple ids that the detail
page does not already carry?

## Method

Throwaway probe (not committed), `uv run python -I`, built from the repo's own pieces:

- Browser: `PatchrightLauncher` (headful Patchright Chrome), one context created with the launcher's derived
  honest UA, which is the browser's own Chrome UA plus the identifying token. UA sent:
  `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 phaze/2026.10.2 (+https://github.com/SimplicityGuy/phaze)`.
  No UA rotation, no extra headers set, no cookies from an account, nothing solved or spoofed.
- Pacing: every request, navigation included, drew a slot from `reserve_host_request_slot`
  (8-12 s, whole-host). A running counter was written to scratch notes before each request.
- Session: one `page.goto` (`domcontentloaded`) to the public anchor detail page for set `25fhn7c9`
  (Sven Vath @ Time Warp, 2024-10-25). Each `/ajax/` call was then an in-page
  `fetch(url, {credentials: 'include'})` (GET, same origin; headers set by the browser only: UA, Referer = the
  detail page, Accept `*/*`, the context's own cookies).
- Requests sent after the navigation (all GET, no body):

| # | Path | Query parameters |
|---|---|---|
| 3 | `/ajax/search_tracklist.php` | `p=sven vath time warp`, `noIDFieldCheck=true`, `fixedMode=true`, `sf=p` |
| 4 | `/ajax/search_tracklist.php` | `p=sven vath time warp 2024`, same three flags |
| 5 | `/ajax/search_track.php` | `p=sven vath ritual of life`, same three flags |
| 6-10 | `/ajax/get_medialink.php` | `idObject=5`, `idItem=` one of 251475, 655873, 961402, 1012909, 939154 |

  The `idItem` values are the first five numeric `div.mediaRow[data-trackid]` values from the recorded anchor
  capture `tests/identify/fixtures/tracklist_render/25fhn7c9-ok.html`. The request shapes came from third-party
  client code read (never run) as untrusted data.
- Never requested: `/js/`, `/user/`, `/action/`, `/projects/`. No phaze worker or drain was running.

Stop rule: a challenge on an `/ajax/` path would end the run and be recorded as that endpoint's verdict. None
occurred. The detail-page navigation did hit a challenge (see Evidence); that is not an `/ajax/` path, and I
continued to the `/ajax/` calls on that session deliberately because the stop rule is scoped to `/ajax/`.

## Evidence

**Live requests spent: 10 of 30.** Two were navigations, eight were `/ajax/` calls. The unspent 20 were left
unspent because the question was settled, not because of a stop.

| Request class | Count | JSON ok | Interstitial / captcha | Empty | Error |
|---|---|---|---|---|---|
| Detail-page navigation | 2 | 0 | 2 (site image captcha, see below) | 0 | 0 |
| `search_tracklist.php` | 2 | 2 | 0 | 0 | 0 |
| `search_track.php` | 1 | 1 | 0 | 0 | 0 |
| `get_medialink.php` | 5 | 4 | 0 | 1 (valid JSON `noLinkFound`) | 0 |
| **Total** | **10** | **7 + 1 empty-JSON** | **2 (both navigations)** | | **0** |

All eight `/ajax/` responses were HTTP 200 and parsed as JSON. No `/ajax/` response resembled a challenge page.

Note on content types: both search endpoints answered `Content-Type: text/html; charset=utf-8` with a JSON
body; `get_medialink.php` answered `application/json`. A client must parse the body, not trust the header.

### The detail navigation was captcha'd, and phaze's classifier does not see it

Both navigations (requests 1 and 2, in two separate browser launches, the second given a 45 s wait) returned a
62.6 KB / 64.8 KB document titled with the site's generic title, carrying `og:url` of the set, zero
`.tlpItem` rows, and the body text "We need to validate your are real human!" with a base64 image `<img alt="Captcha">`, a
text input `#captcha` and a Submit button. That is 1001Tracklists' own image captcha, **not** Cloudflare Turnstile.
It was not solved or retried. The anchor capture from 2026-08-03 (349 KB, 52 rows) shows the same URL cleared
then, so this is a changed behaviour, not a bad id.

`looks_like_interstitial` returned `False` on it and `_classify` falls through to `NO_TRACKLIST` when no container
is found and no marker matches (`src/phaze/services/tracklist_render.py`). So the production renderer, given this
page, would record a **cacheable negative ("this set has no tracklist")** instead of a retryable block. That is a
defect in its own right, independent of the `/ajax/` verdict (see Recommendation).

### `search_tracklist.php`

Both queries returned `{"success": true, "data": [...]}` with exactly 20 entries, every entry
`{"object": "tl", "properties": {...}}` with exactly five properties: `tracklistname`, `id_tracklist` (numeric),
`id_unique` (the short id used in detail URLs), `url_name`, `is_live`. Example (public data):

```json
{"object":"tl","properties":{"tracklistname":"Sven Väth @ Time Warp, Maimarkthalle Mannheim, Germany 2024-10-25",
 "id_tracklist":"540785","id_unique":"25fhn7c9",
 "url_name":"Sven Vath Time Warp, Maimarkthalle Mannheim, Germany 2024-10-25","is_live":"1"}}
```

- The anchor set `25fhn7c9` was result 1 of 20.
- Query 4 (`... time warp 2024`) returned a body byte-identical in content to query 3 (4851 bytes both): the extra
  year token did not narrow or re-rank. The year cannot be used as a search filter.
- `url_name` plus `id_unique` reconstruct the detail URL: lowercase `url_name`, drop punctuation, hyphenate,
  gives exactly `sven-vath-time-warp-maimarkthalle-mannheim-germany-2024-10-25`, the anchor slug.
- Of the 20 titles, 20 end in an ISO `YYYY-MM-DD` date and 18 contain the `Artist @ Event, ...` shape
  (the other two are `Artist - Event ... date`; a BBC entry carries two dates, a broadcast date and an event date inside parentheses).

### `search_track.php`

`{"success": true, "data": {<id_unique>: {...}}}`, an object keyed by track `id_unique` (not a list), 20 entries.
Each entry has `trackname`, `id_unique`, `id_track` (the numeric id `get_medialink` takes), `feature`, `urlname`,
`fulltrackname` ("Artist - Title (Mix)"), `shorttrackname`, `id_type`, `first_played`, `count`, `play_count`, plus
numbered sub-objects: `"0"` artist (name, `id_unique`, `id_artist`) on 20 of 20, `"1"` label on 18 of 20, `"2"` a
related original-track reference on 1 of 20 (a remix points back at its original). The first result for the query
was the exact track whose `id_track` (251475) is the first `data-trackid` on the anchor page.

### `get_medialink.php`

Five numeric ids: 4 answered `{"success": true, "data": [...]}` with 4, 5, 6 and 10 media entries; 1 (id 251475)
answered valid JSON `{"success": false, "noLinkFound": true, "message": "No links found!"}`. That id's detail row
shows only a Spotify "Pre-Save" badge, i.e. it is unreleased or unlinked, which `noLinkFound` explains.

Every entry carries 15 keys: `type`, `id`, `message`, `player` (an `<iframe>` HTML string), `buttons`, `info`,
`viewCount`, `isDeleted`, `playerId`, `duration`, `account`, `source`, `idObject`, `idItem`, `params`.
Source codes observed across the 25 entries, with the platform inferred from the `player` iframe `src` where one
was present: `1` = Beatport (embed.beatport.com `id=`), `2` = Apple Music (embed.music.apple.com `?i=`),
`36` = Spotify (open.spotify.com/embed/track/`<22-char id>`), `4` and `10` and `6` are further platforms
(`10` = SoundCloud and `4` = Traxsource per third-party documentation; I did not verify those three from the
iframe sources and list them as unverified). Several ids returned multiple entries for one source (id 939154:
three Beatport, three source-`4`), which are alternate releases and not duplicates.

Per-track ids in the four successful responses: Spotify 4 of 4, Beatport 4 of 4 (6 distinct Beatport ids),
Apple 4 of 4.

**Does the detail page already carry them?** The recorded anchor capture (76 `data-trackid` attributes, 38 media
rows) contains **0** occurrences of `playerId`, 0 `spotify:track:` URIs, 0 `beatport.com/track` URLs, 0
`get_medialink` strings, and its only Spotify/Apple links are one artist link each (`open.spotify.com/artist/...`,
`music.apple.com/artist/...`). Its media rows hold icons and an "add media link" handler, with ids resolved lazily
by script. So the track-level Spotify, Beatport and Apple ids are **not** in the rendered detail HTML.

## Verdict

| Endpoint | Verdict | Basis |
|---|---|---|
| `/ajax/search_tracklist.php` | **GO** (works; with a limit on usefulness, below) | 2 of 2 JSON ok, 0 challenges |
| `/ajax/search_track.php` | **GO** | 1 of 1 JSON ok, 0 challenges |
| `/ajax/get_medialink.php` | **GO** | 5 of 5 valid JSON (4 with data, 1 `noLinkFound`), 0 challenges |

Scope of the GO: 8 of 8 `/ajax/` calls, one session, about 3 minutes, all inside one page context whose
navigation had itself been captcha'd. A sample of 8 shows these endpoints were not challenged then; it does not
bound a rate at which they would be. The premise ("detail pages are Turnstile-gated, the ajax paths are not") is
half-contradicted: the detail page is now gated by a different mechanism, a site image captcha, and the ajax
paths did answer on a session that had not passed it. I did not test whether they answer with no prior
navigation at all, or from plain `httpx` with the honest UA.

**Does the search JSON alone pick a set without a detail render?**

- Present: set id (`id_unique`, plus numeric `id_tracklist`), `url_name`, a full title string, and in practice the
  artist, event and date, all but only as **text inside `tracklistname`**.
- Missing as structured fields: artist, event, date, venue, track count, duration. They must be parsed from the title
  by the shape `Artist @ Event, Place YYYY-MM-DD`, which held for 18 of 20 observed results (the other two use
  ` - `). Track count and duration are absent entirely, so a "does it have the right number of tracks" check still
  needs the detail page.
- The search cannot be narrowed by year (query 4 vs 3), so date matching is client-side over at most 20 hits.
- Verdict: **enough to shortlist and usually to pick** (id + reconstructable URL + date and event in the title),
  **not enough to confirm** when two candidates share artist, event and date or when track count matters.

## Recommendation

1. **Adopt the three endpoints as the primary discovery and enrichment path**, behind the existing whole-host
   limiter and honest UA. Search via `search_tracklist.php` first; pick by parsing artist, event and date from
   `tracklistname`; build the detail URL from `id_unique` and `url_name`. A single run of 10 requests reached a
   shortlist plus Spotify, Beatport and Apple ids for four tracks.
2. **Use `get_medialink.php` for per-track Spotify/Beatport/Apple ids.** It is the only source of them seen
   here, since the detail HTML carries none. Treat `noLinkFound` as a normal outcome and parse the `player`
   iframe `src` to confirm the platform; do not rely on `source` numbers beyond 1, 2 and 36.
3. **Parse the body, not the content type** (search endpoints answer JSON as `text/html`), and treat a non-JSON
   or captcha-bearing body on an `/ajax/` path as a hard stop for that session, the same stop rule used here.
4. **File a bug: the renderer classifies the site's image captcha as `NO_TRACKLIST`.** The real fix is to detect
   this variant (the "validate your are real human" form, `id="captcha"`) as `INTERSTITIAL_PERSISTED`, i.e. a
   retryable block, and not to cache it. Left alone, a detail render that is merely captcha'd is stored as "this
   set has no tracklist". This needs its own bead; no product code was touched here.
5. **Open questions to settle in a follow-up, cheaply (at most 3 requests):** whether the `/ajax/` calls answer
   with no prior page navigation, and from plain `httpx` with the honest UA (the third-party note that CPython UAs
   are blocked by robots.txt is unverified here). If yes, the render session is not needed for discovery at all.
6. The detail captcha is a changed behaviour since the 2026-08-03 capture. How often it fires, and whether it is
   the same at a later time or from another session, is **unmeasured**: this run saw 2 of 2.
