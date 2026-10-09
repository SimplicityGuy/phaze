# phaze-movg3: does `/ajax/search_tracklist.php` answer plain httpx with no browser session?

> Superseded: external acquisition and all proposed follow-ups are canceled. Recommendations and method descriptions below are historical measurements, not instructions or authorization. See `docs/design/0024-tracklist-source-retirement.md`.


Date: 2026-10-07. Follow-up to `docs/spikes/phaze-gnbct-ajax-endpoints.md`, which only exercised the endpoint inside a
browser session that had already navigated to a detail page (and drawn an image captcha there).

## Question

Does `https://example.invalid/retired-source` answer a **plain httpx** request carrying only the honest
`phaze/<version> (+<contact url>)` User-Agent, with no browser, no cookies from a browser session and no prior page
navigation, and return its JSON without a challenge page?

Note: this is a DIFFERENT endpoint from the one `TracklistScraper` uses today. `src/phaze/services/tracklist_scraper.py`
POSTs to `/search/result.php` (`SEARCH_URL`) and parses HTML rows; `/ajax/search_tracklist.php` is a GET returning JSON.
This spike says nothing about `/search/result.php`.

## Method

Operator-approved budget (2026-10-07, "phaze-movg3: yes, no browser"; terms: at most 3 requests): at most 3 live requests, only
to `/ajax/search_tracklist.php`. Spent: **2**.

- Throwaway probe, not committed, run with `uv run python -I`. One `httpx.AsyncClient`, `follow_redirects=False`, 30 s timeout,
  a fresh cookie jar, no Referer, no cookies, no prior request to any other path.
- Every request drew a slot from `reserve_host_request_slot()` (8-12 s whole-host limiter) first. A counter was written to
  scratch before each send. The probe stops at the first response that is not clean JSON with `success: true` and at least
  one entry; that rule was not triggered.
- User-Agent: `honest_user_agent_token()` alone (`phaze/<version> (+<contact url>)`), no Chrome prefix, no rotation.
- Request shape, reused exactly from the first spike: `GET /ajax/search_tracklist.php` with query parameters
  `p=sven vath time warp`, `noIDFieldCheck=true`, `fixedMode=true`, `sf=p`. The set is the public anchor
  `25fhn7c9` (Sven Vath @ Time Warp, 2024-10-25), so the expected first hit was known.
- Request header NAMES sent (httpx defaults plus the UA, nothing added): `accept`, `accept-encoding`, `connection`, `host`,
  `user-agent`. (Computed offline from the same client configuration after the run; the live run did not record request
  headers beyond confirming the probe set none other.)
- Never requested: `/js/`, `/user/`, `/action/`, `/projects/`, any other path. No worker or drain was started.

## Evidence

Requests 1 and 2 were the same query (request 2 as a repeat, to check stability).

| Class | Count of 2 |
|---|---|
| JSON ok (`success: true`, 20 entries) | 2 |
| Interstitial / captcha / challenge page | 0 |
| Empty body | 0 |
| Error / block / redirect | 0 |

- Both bodies parsed as JSON, `success: true`, `data` a list of exactly **20** entries; each body was 4851 bytes and the two
  were byte-identical (`cmp`), which is also the size the first spike recorded for the same query inside the browser.
- Each entry is `{"object": "tl", "properties": {...}}` with exactly five properties: `tracklistname`, `id_tracklist`,
  `id_unique`, `url_name`, `is_live`. The set was exactly as in the first spike.
- The anchor `25fhn7c9` was result 1 of 20: `tracklistname` "Sven Väth @ Time Warp, Maimarkthalle Mannheim, Germany
  2024-10-25", `id_tracklist` 540785, `url_name` "Sven Vath Time Warp, Maimarkthalle Mannheim, Germany 2024-10-25",
  `is_live` "1".
- No cookies were set by either response (`Set-Cookie` absent, jar empty).
- Response header names seen: `access-control-allow-origin`, `connection`, `content-encoding`, `content-length`, `content-type`,
  `date`, `keep-alive`, `server`, `strict-transport-security`, `upgrade`, `vary`, `x-content-type-options`,
  `x-frame-options`, `x-xss-protection`.
- Gap in the record: the probe printed the HTTP status line and `content-type` value for each response, but that part of the
  output was cut off by a `tail` in the wrapper and not preserved, and I chose not to spend the third request to recapture
  it. What is established is the parse: a 4851-byte body that is valid JSON with `success: true`. The first spike recorded
  that this endpoint labels JSON `text/html`; parse the body, do not trust the type.

## Verdict

**GO: plain httpx is served the JSON.** 2 of 2 requests returned the JSON search result with the honest UA only, no browser,
no cookies and no prior navigation; 0 of 2 met a challenge.

Limits on the claim: a sample of 2 requests, about 10 s apart, from one IP, in one minute. It shows the endpoint is not
gated behind a prior page visit or a cookie; it does not bound a rate at which a challenge would appear, and it says nothing
about `/ajax/search_track.php` or `/ajax/get_medialink.php`. The first spike's premise, that the ajax path needed the session
that had navigated to a detail page, is contradicted for this endpoint.

## Recommendation

A build bead can implement the search leg in plain httpx, with no Patchright for search. It would need:

- `GET https://example.invalid/retired-source` with params `p=<query>`, `noIDFieldCheck=true`,
  `fixedMode=true`, `sf=p`; the honest UA only (what `TracklistScraper` already sends); httpx default `Accept`; no cookies,
  no Referer needed.
- Pacing through `reserve_host_request_slot()` like every other request to the host.
- Parse the body, not the content type. Treat any non-JSON body, or `success` not true, as a hard stop for that host
  (retryable block, not a cacheable "no tracklist"), per the stop rule in the first spike.
- Shortlisting is client-side: `tracklistname` carries artist, event and date as text (`Artist @ Event, Place YYYY-MM-DD`
  in 18 of 20 results in the first spike); the year cannot narrow the query.
- Detail URL rebuild: lowercase `url_name`, drop punctuation, hyphenate, and join with `id_unique` as the first spike
  verified (`sven-vath-time-warp-maimarkthalle-mannheim-germany-2024-10-25` for `25fhn7c9`). The detail page itself still
  draws the image captcha, so the track list is still behind the render session; this only removes the browser from search.
- Before building: a rate-bounded follow-up (needs its own operator-approved budget) to learn how many plain requests the
  endpoint tolerates, since 2 requests cannot say.
