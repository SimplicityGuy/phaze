# MixesDB REST access and restart-safe pacing

Date: 2026-10-08. Bead: phaze-6ehg6. Status: amended API investigation complete; REST tested, Action help only under clarified scope.

## Current API findings after scope amendment

On 2026-10-08, phaze-ypcx6 records the new instruction verbatim: “for phaze-ypcx6 please proceed to test the API endpoints both Action API and REST API.” It superseded the initial REST-only/no-api.php exclusion for ordinary read-only API investigation. The later policy clarification question, summarized rather than quoted here, asked whether normal read-only Action queries were authorized despite robots restrictions. The answer on the same dated epic was verbatim: **“Respect those robots restrictions; test REST and the bare Action API endpoint only”**. No `?action=` calls or reordered, encoded or POST workarounds were used. Total budget remained 100, with the two initial attempts retained.

The prior conservative stop was preserved in the ledger history, then released only after the new authorization was persisted and the review batch reopened through the lifecycle. The new phase distinguishes readable endpoint content with background JavaScript from an actual challenge/interstitial; 403, 429 or an actual challenge still stops transport. No background script or hidden link was executed or fetched.

| Attempt(s) | Endpoint test | Outcome |
|---|---|---|
| 3 | Fresh robots | HTTP 200, 6,322 bytes; identical to initial policy. |
| 4 | Bare `/w/api.php`, no query | HTTP 200 HTML API help, 53,382 bytes; generator identifies **MediaWiki 1.46.1**. No challenge marker. |
| 5 | `/w/rest.php/v1` | HTTP 404 JSON `rest-no-match`; root has no handler. This does not mean the REST family is unavailable. |
| 6–8 | Three bounded public `search/page` queries with `q` and `limit=5` | HTTP 200 JSON; counts 3, 5 and 5. |
| 9 | One existing public artist query with `limit=100` | HTTP 200 JSON; exactly 100 results; top-level response only `pages`, with no total or continuation field. |
| 10, 13 | `page/<key>` for two non-slash public native keys already returned by search | HTTP 200 JSON with `id`, `key`, `title`, `latest`, `content_model`, `license`, `source`. |
| 11 | Slash-containing native key with ordinary percent-encoded path component | HTTP 404 Apache HTML; background challenge-platform marker, readable 404 content, no actual challenge. |
| 12 | Same key with literal slash, remaining characters ordinarily encoded | HTTP 404 REST JSON `rest-no-match`; route did not match a handler. Neither failure proves the source page absent. |

Successful page JSON exposes `latest.id` and `latest.timestamp`, supplying current revision evidence; `content_model` is wikitext. Both `license` objects contain empty URL/title strings, so separate policy evidence remains necessary. A successful response also carried ETag and Last-Modified headers; their conditional-request semantics were not tested. Search entries expose numeric page ID, native key, title and excerpt, plus nullable display fields. Numeric page ID is additional identity evidence, not proof that a numeric-ID load route is supported.

The two successfully loaded pages contain 12 and 21 numbered entries. The first contains mashup/remix text; the second has section labels separating groups of tracks. Neither page supplied cue marks, and neither used `<list>`. Alternate list markup and timestamp interpretation remain supported by historical evidence and synthetic cases, not fresh parser coverage. Preserve section labels as bounded provenance; flattening them silently loses source context.

**Final shared budget: 13 of 100 attempts, 87 unspent.** Of the 11 new attempts, 8 returned HTTP 200 and 3 HTTP 404. Across both phases: 10 HTTP 200 and 3 HTTP 404. There were no transport errors, retries, redirects followed, 403, 429 or actual API challenge responses. The minimum of all 12 persisted inter-attempt gaps is **5.140897035598755 seconds**. Transport remains available within the clarified scope, but further requests were unnecessary for this bounded investigation.

API query-module behavior, Action pagination/revisions and Action search remain untested by deliberate policy restriction. REST pagination beyond returned results, conditional caching and a functioning slash-key route remain unresolved. No unknown pagination parameters, double-encoding probes, Action fallback or broad crawling were attempted.

The current verdict is that ordinary REST search and page access, source format and revision fields are verified. The Action family is verified only as a served help/version endpoint. Remaining discovery, routing and parser uncertainties must be carried into human review rather than filled with endpoint guesses. This evidence does not authorize an adapter plan or implementation.

## Initial policy phase retained as historical evidence

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

**Initial phase budget: 2 of 100 attempts; 98 then unspent.** Both completed successfully at HTTP level. No live retries, REST requests, media requests, category pages, Action API calls or cross-site requests occurred in that phase. The two persisted attempt times differ by **49.555183887 seconds**, exceeding the authorized 5-second minimum.

Offline checks executed against scratch code, without external transport:

- Fake clock verified initial admission, same-host delay, backward-clock waiting and refusal at 100 reservations.
- Fake transport produced one timeout, an explicit retry and a newly loaded worker; all three were charged before send and separated by at least 5.1 simulated seconds.
- Reloaded state preserved the retry/restart clock and shared budget; reservation 101 was refused before transport.
- Persisted stop blocked a reloaded worker; disallowed API/category and cross-host routes were refused.

These checks exercise the investigation runner, not a production adapter. They do not constitute a concurrent-process stress test or guarantees under arbitrary host clock changes.

## Initial native identity and revision assessment

Historical REST search returned `key`, `title` and `excerpt`; page JSON exposed wikitext in `source` and an empty license object. Those observations are historical, not fresh endpoint guarantees. A future adapter must preserve the search-returned native key verbatim as identity, separately from display title and URL encoding. Revision, retrieval time, source URL and policy evidence belong to each snapshot; an unavailable revision remains unknown.

The historical slash-key 404 does not prove absence: percent-encoding may be altered by routing, or the endpoint may interpret slashes differently. After a later access decision, verify a public slash-containing search key using only documented REST page semantics, without double-encoding experiments, category/HTML fallback or Action API. Record the original key and response behavior. This batch performed no such probe because transport stopped first.

## Initial verdict before the amendment

Fresh robots and readable policy evidence were obtained; fresh REST access, revision fields, slash-key handling and pagination were **not verified**. Mandatory stopping was honored. Current access must not be described as settled by historical HTTP 200 results.

## Initial recommendation before the amendment

Keep the durable admission/pacing/stop design as investigation evidence. A later operator access decision must address the challenge-platform marker before resuming live REST probes; this report does not authorize resumption or require bypassing it. Continue specification work offline, carrying unavailable/unknown outcomes instead of invented successful access.
