# 🕸️ 1001Tracklists Scraping

This document covers the **operational behavior** of the 1001Tracklists integration: the SSRF
guard, the shared rate limiter, the render engine's Turnstile handling, the poisoned-result skip,
and the caches. It does not restate the parsing selectors (see the module docstrings in
`services/tracklist_parser.py` and `services/tracklist_scraper.py` for those) — this is about
*how requests get made safely and politely*, not *what gets extracted from them*.

Every claim below carries a `path:line` reference into the checked-out tree. If a reference looks
stale, the code — not this page — is authoritative; re-verify against current line numbers before
trusting either.

## Two request paths, one shared schedule

The integration is two cooperating clients, not one:

- **`TracklistScraper`** (`src/phaze/services/tracklist_scraper.py`) — plain `httpx`, POSTs the
  search page (`SEARCH_URL`) and parses the results list. No browser, no JavaScript.
- **`TracklistRenderer`** (`src/phaze/services/tracklist_render.py`) — a headful Patchright
  (CDP-patched Playwright) Chrome, under Xvfb on a headless worker, that navigates a detail page
  and waits for its track container to render. Detail pages inject their track listing via
  JavaScript behind a Cloudflare Turnstile challenge, so an `httpx` GET of a detail page returns an
  empty shell — this is why a real browser exists at all
  (`src/phaze/services/tracklist_render.py:1-22`).

The legacy code path that once fetched **and parsed** detail pages with `httpx` alone
(`scrape_tracklist` / `_parse_track_item`) no longer exists (phaze-2akf). It is documented here
only as a boundary marker: do not look for it, and do not resurrect an httpx-only detail-page
parser — every per-track selector it used matched zero nodes against a real capture
(`src/phaze/services/tracklist_scraper.py:3-16`).

Both clients are driven end-to-end by the resumable drain (`src/phaze/services/tracklist_drain.py`,
SAQ entry point `src/phaze/tasks/tracklist_drain.py`), which is **operator-triggered with no
cron** — there is no `CronJob` registration for `drain_tracklists`, deliberately: the epic's ethics
bound is that a headful browser scraping a public host from a residential IP is a decision the
operator makes, not something that starts on container boot
(`src/phaze/tasks/tracklist_drain.py:16-20`, `src/phaze/tasks/controller.py:6-7`). The separate
`refresh_tracklists` sweep (`src/phaze/tasks/tracklist.py`) — which re-arms specific pages for
re-lookup rather than scraping anything itself — is likewise operator-triggered; the old monthly
`refresh_tracklists` CRON was retired in the same change (phaze-2akf,
`src/phaze/tasks/controller.py:345-346`).

## SSRF allow-list, with the final-response-URL recheck after redirects

`_ALLOWED_HOSTS` is the complete host allow-list: `1001tracklists.com` and
`www.1001tracklists.com`, exact match only
(`src/phaze/services/tracklist_scraper.py:200`). `_is_allowed_url` compares against
`urlsplit(url).hostname` — never `.netloc`, which can carry userinfo (`https://evil@
1001tracklists.com` would report a different apparent netloc) — and lower-cases the comparison so
neither case tricks nor a lookalike subdomain (`www.1001tracklists.com.evil.com`, which a
suffix/substring check would wrongly allow) can pass
(`src/phaze/services/tracklist_scraper.py:303-312`).

The guard has to run **twice** because `httpx.AsyncClient` is configured with
`follow_redirects=True` (`src/phaze/services/tracklist_scraper.py:285`) — a deliberate choice,
since the bare apex host (`1001tracklists.com`) is on the allow-list *because* it 301s to `www`,
and an unfollowed redirect used to come back as a permanent retry loop instead of a result
(`src/phaze/services/tracklist_scraper.py:277-284`). Following redirects means a malicious or
compromised redirect chain could otherwise land the request off-host and hand its (attacker
controlled) HTML to the parser as if it were a legitimate results page. `TracklistScraper.search`
re-checks `_is_allowed_url` against the response's **final** `response.url`, after redirects have
already been followed, and raises `DisallowedScrapeHostError` if it fails
(`src/phaze/services/tracklist_scraper.py:357-359`, raised at
`src/phaze/services/tracklist_scraper.py:329-330`). The same allow-list check also runs on any
absolute href embedded *in* a parsed search-results page, before it is ever turned into a
`TracklistSearchResult.url` — guarding against a poisoned response page that embeds an
attacker-controlled absolute URL (e.g. `http://169.254.169.254/tracklist/x/`) as a result link
(`src/phaze/services/tracklist_scraper.py:403-414`).

`TracklistRenderer.render` enforces the identical check before ever navigating a detail page —
deliberately reusing `TracklistScraper._is_allowed_url` rather than restating the policy
(`src/phaze/services/tracklist_render.py:511-515`). This matters *more* here than in the httpx
path: a browser told to visit an attacker-chosen URL executes that page's JavaScript with the
worker's network position and the browser profile's cookies, not merely fetches inert bytes.

## The rate limiter: one whole-host schedule, not one per module or per worker

1001Tracklists' `robots.txt` publishes `Crawl-delay: 8` (verified live 2026-07-18, re-confirmed
2026-07-24); `MIN_DELAY = 8.0` / `MAX_DELAY = 12.0` are the resulting floor and jitter ceiling
(`src/phaze/services/tracklist_scraper.py:187-188`). The scheduling primitive is
`reserve_host_request_slot()` — a **module-level** function, not a method on either client class
(`src/phaze/services/tracklist_scraper.py:477-510`). It holds a shared `asyncio.Lock`
(`_rate_limit_lock`) just long enough to atomically read and advance a shared monotonic
`_next_request_at` timestamp, then releases the lock **before** sleeping — so concurrent callers
never race each other onto the same slot, but the request/sleep itself never holds the lock (which
would otherwise turn a 30s-timeout HTTP call into a hard serial bottleneck rather than merely a
rate floor). Each successive slot is `MIN_DELAY`–`MAX_DELAY` after the previous one; a caller that
falls behind (e.g. after a slow request) catches up from "now" rather than compounding a backlog of
reservations into the future
(`src/phaze/services/tracklist_scraper.py:483-510`).

**Both** clients draw from this one function:

- `TracklistScraper._rate_limit` is a thin delegate to it
  (`src/phaze/services/tracklist_scraper.py:314-320`).
- `TracklistRenderer._attempt_loop` calls `_paced`, which calls it before **every** navigation and
  every Turnstile reload — not just once per detail page
  (`src/phaze/services/tracklist_render.py:685-696`, called at
  `src/phaze/services/tracklist_render.py:605`).

This is deliberate, not incidental: the published `Crawl-delay: 8` is a budget for the whole host,
so a search POST and a browser navigation are both "one request" against the same ceiling. Two
independent limiters — one per module — would each individually honor 8s while the host observed
requests every ~4s, exactly the bug `reserve_host_request_slot` exists to prevent
(`src/phaze/services/tracklist_scraper.py:489-497`). **Do not add a second limiter anywhere in this
subsystem** for that reason; route any new call site that reaches the host through this same
function.

**Why this stops scaling past one replica.** The state (`_rate_limit_lock`, `_next_request_at`) is
a Python-process-local `ClassVar` on `TracklistScraper`
(`src/phaze/services/tracklist_scraper.py:268-269`) — an in-memory lock and timestamp, not
anything shared across processes. It was specifically built to fix concurrency *within* one
process: before phaze-wb1o, each of the controller worker's concurrently-running SAQ jobs (up to
`worker_max_jobs`, default 8, all in one process) constructed its own `TracklistScraper()` and slept
its own independent 8–12s, so the *aggregate* request rate scaled roughly N× past the floor
(`src/phaze/services/tracklist_scraper.py:255-267`). The fix is correct **only** because every
`docker-compose*.yml` in this repo runs a single `worker` container for the controller role (no
`replicas:` / `scale:`). If the controller is ever scaled to more than one replica, this
process-local lock stops being sufficient — two replicas would each independently pace at 8–12s
while the host again saw roughly double the agreed rate, invisibly, because nothing here can see
across a process boundary. The fix at that point is a Redis-backed limiter (the `cache_redis`
handle is already wired onto the controller queue; `check_rate_limit` in `services/proposal.py`
has the existing atomic-Lua-script pattern this would follow) — noted as a deliberate, not-yet-
needed enhancement, not a currently-open defect
(`src/phaze/services/tracklist_scraper.py:255-267`).

## The render engine: Turnstile, retries, and the outcome taxonomy

`TracklistRenderer.render` runs a bounded navigate → classify → (reload → classify)\* loop per
detail page (`src/phaze/services/tracklist_render.py:582-628`), reusing one page across retries
(a Turnstile clearance cookie the challenge may already have set is what the retry is hoping to
build on — throwing the context away between attempts would restart the challenge from zero every
time, `src/phaze/services/tracklist_render.py:458-461`). Each render ends in exactly one of five
outcomes (`RenderOutcome`, `src/phaze/services/tracklist_render.py:84-107`):

| Outcome | Meaning | Retryable / cacheable |
|---|---|---|
| `OK` | Track container rendered; HTML worth parsing | n/a — positive |
| `NO_TRACKLIST` | Page rendered fine, genuinely no track container | Cacheable negative |
| `INTERSTITIAL_PERSISTED` | Every attempt served the Turnstile challenge | Retryable, never a negative |
| `TIMEOUT` | Hard per-page wall clock expired | Retryable, never a negative |
| `NAVIGATION_FAILED` | Browser could not load the URL at all | Retryable, never a negative |

`RenderResult.is_retryable` is the single property that encodes the right-hand column
(`src/phaze/services/tracklist_render.py:135-146`) — only `NO_TRACKLIST` is a fact about
1001Tracklists; the other three failure modes are facts about *this attempt*, and the drain must
never write a negative cache entry for them.

### Classification order: the track container wins, always

`_classify` checks for the track container **first**; only when it is absent does it consult the
interstitial marker set (`src/phaze/services/tracklist_render.py:630-644`). This ordering is not
arbitrary — it is fixing a real, measured failure mode:

> **A successfully rendered detail page can still contain Turnstile widget markup.** 4 of 16
> captures did, including the spike's anchor page, because 1001Tracklists embeds the widget on
> pages it has *already* let a client through (`src/phaze/services/tracklist_render.py:65-81`).
> Marker-first classification would have reported those 4 of 16 good renders as blocked —
> intermittently, on otherwise-identical pages, which reads as flaky site-side blocking rather than
> a classifier bug. Verified against
> `tests/identify/fixtures/tracklist_render/README.md:47-53`.

### What compliant pacing measured about Turnstile — and what it did not prove

A capture run on 2026-08-02, 16 renders across 15 distinct pages with one browser reused across the
run, paced at the published crawl-delay with an honest User-Agent, recorded **16 of 16 clearing on
the first navigation — 0 interstitials, 0 retries, 0 failures**
(`tests/identify/fixtures/tracklist_render/README.md:26-34`). That is *not* the same as "Turnstile
is gone": the earlier spike (phaze-dmvs, 2026-07-24) measured 6 of 8 clearing on first navigation
under otherwise similar conditions, n is small in both runs, the two runs are a week apart, and
Cloudflare's scoring is not published or guaranteed stable
(`tests/identify/fixtures/tracklist_render/README.md:36-41`). The correct reading is: **not
observed in 16 consecutive renders; the retry path is retained and remains untested against a live
challenge.** The bounded reload/retry loop (`tracklist_render_turnstile_attempts`,
`src/phaze/services/tracklist_render.py:597`) stays in place on that basis and should not be
removed or shortened on the strength of this one favorable sample.

## The per-item poisoned-result skip

A render that succeeds (`RenderOutcome.OK`) but whose parse yields zero track rows is **not**
treated as a found tracklist. `perform_lookup` checks for an empty `tracks` sequence after parsing
and, if empty, returns `LookupOutcome.PARSE_FAILED` (a transient failure) rather than `FOUND`
(`src/phaze/services/tracklist_drain.py:476-485`). This is the direct fix for the same class of
defect that killed the legacy httpx detail-page parser: a page whose selectors have drifted against
a site redesign renders successfully and looks, superficially, like a set with genuinely no
tracklist (`src/phaze/services/tracklist_drain.py:402-406`).

The skip is enforced **per candidate, in its own short transaction** — `drain_once` commits each
candidate's outcome immediately after its own lookup, so one candidate's `PARSE_FAILED` cannot roll
back or otherwise affect any other candidate's already-committed result
(`src/phaze/services/tracklist_drain.py:61-62`; the historical defect this guards against,
phaze-gfyr / phaze-g2j3, is catalogued in
`tests/identify/tasks/test_tracklist.py:22-29`).

**The zero-track guarantee is structural, not merely a convention.** `_append_version` — the only
function that writes a `TracklistVersion` and its tracks, and the only place `Tracklist
.latest_version_id` is set — is never reached for a track-less render, because `perform_lookup`
diverts to `PARSE_FAILED` before `persist_lookup` ever calls into the write path
(`src/phaze/services/tracklist_drain.py:759-766`, the empty-write path being structurally
unreachable rather than merely unlikely). This is the invariant worth stating explicitly, because
it was a real review defect: **a cache hit requires the row's latest version to actually have
tracks — a non-null `latest_version_id` alone is not sufficient**, since a zero-track first scrape
that *was* allowed to write a version would be indistinguishable from a real success and would
never be retried. The guarantee holds because the write path that sets `latest_version_id` is
categorically never invoked with an empty track list, not because some downstream reader happens to
also check track count.

## A transient failure must never be cached as a real negative

`perform_lookup` (`src/phaze/services/tracklist_drain.py:373-487`) runs entirely with **no
database session in scope** — it is documented as producing its `LookupAttempt` "with no database
in scope, so the outcome can be asserted directly in a test against a recorded page, and
`persist_lookup` has no room to reinterpret it"
(`src/phaze/services/tracklist_drain.py:342-349`). `persist_lookup` then records that outcome
**verbatim** via `record_outcome` (`src/phaze/services/tracklist_drain.py:583-593`) — there is no
step where the recording layer re-derives or reinterprets what happened. The mapping from situation
to outcome, and which outcomes are cacheable as a negative, is stated as a table in
`perform_lookup`'s own docstring (`src/phaze/services/tracklist_drain.py:383-394`):

| Situation | Outcome | Cacheable as "no"? |
|---|---|---|
| Search raised / selectors stale | `SEARCH_FAILED` | No (transient) |
| Scorer refused, definitively | `NOT_FOUND` | Yes (negative TTL) |
| Scorer refused, ambiguously | `SEARCH_FAILED` | No (transient) |
| Page rendered, no track list | `NOT_FOUND` | Yes (negative TTL) |
| Turnstile survived the retries | `BLOCKED` | No (transient) |
| Timeout / navigation failure | `RENDER_FAILED` | No (transient) |
| Parse partial or empty | `PARSE_FAILED` | No (transient) |
| Tracks parsed | `FOUND` | n/a (positive) |

Only a clean search that scores no candidate above threshold, and a rendered page with genuinely
no track container, are cacheable negatives — both are facts about 1001Tracklists itself. Every
other row in the table is a fact about *this attempt* (a flaky Turnstile challenge, a timeout, our
own selector drift) and must re-enter the queue on its own backoff rather than being suppressed for
the 180-day negative TTL. `LookupOutcome.is_transient` / `.is_definitive_negative` are the two
properties this distinction turns on downstream (`src/phaze/enums/tracklist_candidate.py`, consumed
at `src/phaze/services/tracklist_lookup_cache.py:75-101`).

## The two TTL caches

There are exactly two, at different layers, for different reasons:

1. **In-process search-result cache** (`_TTLCache`, `src/phaze/services/tracklist_scraper.py:117-
   157`, instantiated as `TracklistScraper._search_cache` with a 6-hour TTL,
   `src/phaze/services/tracklist_scraper.py:192,253`). Deliberately DB-oblivious and
   process-lifetime only: since each drain call site constructs a fresh `TracklistScraper()` per
   job, this is what makes repeated lookups of the same query within one running process (e.g. a
   long drain slice) cost zero requests, without adding a storage dependency. It is a convenience
   cache, not a correctness mechanism — losing it (e.g. on a process restart) costs requests, not
   correctness.

2. **The persistent lookup cache** (`tracklist_lookup_cache` table,
   `src/phaze/models/tracklist_lookup_cache.py`; decision logic in
   `src/phaze/services/tracklist_lookup_cache.py`). This is the "never spend a request twice, ever,
   across restarts and months" mechanism — one row per unique set. `FOUND` rows never expire
   (`expires_at IS NULL`); `NOT_FOUND` rows suppress a re-query for `NEGATIVE_TTL_DAYS = 180`
   (`src/phaze/services/tracklist_lookup_cache.py:33-41`) because 1001Tracklists gains tracklists
   continuously, so a negative cannot be permanent; transient outcomes get an exponential backoff
   starting at `TRANSIENT_BACKOFF_BASE_MINUTES = 30`, doubling per attempt, capped at
   `TRANSIENT_BACKOFF_MAX_HOURS = 24`
   (`src/phaze/services/tracklist_lookup_cache.py:43-51,123-127`) until
   `TRANSIENT_MAX_ATTEMPTS` is reached, at which point the set is parked for an operator rather than
   silently reinterpreted as a negative (`src/phaze/services/tracklist_lookup_cache.py:80-99`).

The 180-day figure is sized deliberately, not arbitrarily: at the host-imposed ceiling
(`DAILY_LOOKUP_CEILING`, `src/phaze/services/tracklist_candidate_queue.py:75`) a full corpus pass
takes months, so six months is roughly "once per corpus pass" — the re-check costs a request only
after every never-asked set has already had its own turn
(`src/phaze/services/tracklist_lookup_cache.py:33-41`).

## Compliance posture, briefly

`robots.txt` (fetched live 2026-07-18, re-confirmed 2026-07-24) grants `User-agent: *` an `Allow:
/` at `Crawl-delay: 8`, with `Disallow: /js/`, `/user/`, `/action/`, `/projects/`; a separate block
blanket-disallows ~30 named commercial/AI crawlers phaze is not one of. The site publishes no
Terms of Service (confirmed by the operator 2026-07-18). `MIN_DELAY`/`MAX_DELAY` honor the
crawl-delay; `_build_headers` sends an honest, identifying `User-Agent` (`phaze/<version>
(+<contact url>)`, `src/phaze/services/tracklist_scraper.py:44-57`) instead of a spoofed browser
UA; and the `Disallow` list is honored **by construction** rather than by a runtime `robots.txt`
parse — this class only ever requests `SEARCH_URL` and hrefs matching
`a[href*='/tracklist/']` pulled from a search-results page, so a `/user/` profile link (which does
appear on search rows) is never followed (`src/phaze/services/tracklist_scraper.py:160-178`). The
render engine's own honesty stance is symmetric but inverted from the httpx client's: rather than
replacing the browser's UA (there being no browser to contradict, in the httpx case), it **appends**
phaze's identifying token to the real Chrome UA the browser already presents, because a Chrome that
claims not to be Chrome is an inconsistency Turnstile can score
(`src/phaze/services/tracklist_render.py:397-427`). The headful requirement is not a preference —
headless Patchright does not clear Turnstile at all — so a headless worker gets a virtual X display
via `XvfbDisplay` rather than ever passing `headless=True`
(`src/phaze/services/tracklist_render.py:9-14,235-254,386-393`).
