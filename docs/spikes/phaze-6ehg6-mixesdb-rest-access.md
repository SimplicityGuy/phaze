# MixesDB REST access and restart-safe pacing

Date: 2026-10-08. Bead: phaze-6ehg6. Status: investigation completed with mandatory transport stop; live REST questions unresolved.

## Question

Can a bounded identified client verify allowed REST access, native identity, revision and slash-key behavior without repeating the historical restart pacing defect?

## Method

The [baseline](phaze-py7ls-mixesdb-investigation-baseline.md) records authorization. A scratch Python 3.14 runner, invoked through uv, used one serialized curl transport with `phaze-investigation/2026.10 (+https://github.com/SimplicityGuy/phaze)` identification. Proxy use, redirects and automatic retries were disabled. Timeout was 30 seconds; response cap was 2,000,000 bytes. There was no browser or script execution. No archive, database, production or SSH access occurred.

An exclusive file lock covered reservation, durable state update, attempt log and request completion. One durable counter and last-attempt wall timestamp were shared across all invocations. The state was written to a temporary file, fsynced, atomically replaced and its directory fsynced before send. Every reservation consumes budget even if sending is interrupted. Corrupt state fails closed rather than resetting the counter. Attempt spacing was configured at 5.1 seconds; a clock moving backwards extends the wait. A forward clock discontinuity remains a limitation for a future production-grade transport: detect it and fail closed or use a persisted boot/clock epoch in addition to monotonic in-process timing.

Paths were narrowly allowlisted to robots, necessary policy pages and REST search/page routes. The fresh wildcard robots rules were checked before subsequent reads. Policy disallow, 403, 429 or detected challenge-platform markers persist a stop, preventing a restart from continuing. Scratch responses and counters are outside tracked files.

## Evidence

| Fresh attempt | Route | HTTP | Body bytes | Result |
|---|---|---|---|---|
| 1 | `https://www.mixesdb.com/robots.txt` | 200 | 6,322 | Wildcard group has Crawl-delay 4; REST routes lack a matching disallow. |
| 2 | `https://www.mixesdb.com/w/MixesDB:Legal_stuff` | 200 | 40,839 | Readable policy content plus Cloudflare challenge-platform JavaScript; mandatory conservative stop. |

The page included a script reference under `/cdn-cgi/challenge-platform/scripts/jsd/main.js`. It was neither fetched nor executed. This is not evidence of an HTTP block or a challenge-only interstitial: normal content was present. The runner's conservative challenge detector nevertheless stopped traffic under the authorized stop condition. No more requests were made, and no marker was suppressed to continue.

**Final budget: 2 of 100 attempts; 98 unspent.** Both completed successfully at HTTP level. No live retries, REST requests, media requests, category pages, Action API calls or cross-site requests occurred. The two persisted attempt times differ by **49.555183887 seconds**, exceeding the authorized 5-second minimum.

Offline checks executed against scratch code, without external transport:

- Fake clock verified initial admission, same-host delay, backward-clock waiting and refusal at 100 reservations.
- Fake transport produced one timeout, an explicit retry and a newly loaded worker; all three were charged before send and separated by at least 5.1 simulated seconds.
- Reloaded state preserved the retry/restart clock and shared budget; reservation 101 was refused before transport.
- Persisted stop blocked a reloaded worker; disallowed API/category and cross-host routes were refused.

These checks exercise the investigation runner, not a production adapter. They do not constitute a concurrent-process stress test or guarantees under arbitrary host clock changes.

## Native identity and revision assessment

Historical REST search returned `key`, `title` and `excerpt`; page JSON exposed wikitext in `source` and an empty license object. Those observations are historical, not fresh endpoint guarantees. A future adapter must preserve the search-returned native key verbatim as identity, separately from display title and URL encoding. Revision, retrieval time, source URL and policy evidence belong to each snapshot; an unavailable revision remains unknown.

The historical slash-key 404 does not prove absence: percent-encoding may be altered by routing, or the endpoint may interpret slashes differently. After a later access decision, verify a public slash-containing search key using only documented REST page semantics, without double-encoding experiments, category/HTML fallback or Action API. Record the original key and response behavior. This batch performed no such probe because transport stopped first.

## Verdict

Fresh robots and readable policy evidence were obtained; fresh REST access, revision fields, slash-key handling and pagination were **not verified**. Mandatory stopping was honored. Current access must not be described as settled by historical HTTP 200 results.

## Recommendation

Keep the durable admission/pacing/stop design as investigation evidence. A later operator access decision must address the challenge-platform marker before resuming live REST probes; this report does not authorize resumption or require bypassing it. Continue specification work offline, carrying unavailable/unknown outcomes instead of invented successful access.
