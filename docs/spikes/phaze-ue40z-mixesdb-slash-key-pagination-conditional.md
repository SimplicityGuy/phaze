# MixesDB slash-key routing, REST pagination and conditional requests

Date: 2026-10-12. Bead: phaze-ue40z. Epic: phaze-oprdu. Live-request spike; no product code.

## Question

Three facts about the MixesDB REST API were left unverified by [the access investigation](phaze-6ehg6-mixesdb-rest-access.md):

1. How does a page key that contains a slash route on the REST page endpoint, using only documented REST page semantics?
2. Does the REST page search paginate, using only documented parameters?
3. Do the `ETag` and `Last-Modified` response headers support working conditional requests?

## Method

**Authorization.** Operator request budget, recorded 2026-10-11 on phaze-ue40z (planning session for epic phaze-oprdu). Request as put, operator's words verbatim: "request budget, let's say 250 calls over an hour at most for the investigation, ok?" Clarifying question as put: "Does the 250-call, one-hour budget cover both probes combined, or each probe separately?" Selected option label, verbatim: "250 each". Resulting grant for this bead: at most 250 HTTP requests to MixesDB within any single hour, a ceiling and not a target. The earlier 100-request authorization (13 spent) does not carry forward. The access scope was not widened: the answer recorded on phaze-ypcx6 on 2026-10-08, "Respect those robots restrictions; test REST and the bare Action API endpoint only", still bounds every request. Durable record: the phaze-ue40z bead comment.

**Transport.** A scratch runner, not the production transport profile (which had not landed), outside the repository. It reproduces the investigation runner's durable design: an exclusive file lock held across reservation and send; a durable counter and last-attempt timestamp reserved before the request is sent; state written to a temporary file, fsynced, atomically replaced, directory fsynced; corrupt state fails closed; a persisted stop on 403, 429 or a genuine challenge. One request per process invocation, serial, curl with no redirects followed and no proxy, 30 s timeout, 2,000,000-byte response cap, no automatic retries, honest identifying User-Agent (`phaze-investigation/2026.10 (+https://github.com/SimplicityGuy/phaze)`), configured gap 5.1 s. A path allowlist admitted only `/robots.txt`, the bare `/w/api.php` and routes under `/w/rest.php/`; any `action=` query was refused. The runner also carried its own total cap of 60, tighter than the grant. Raw captures stayed in a session scratch directory and are not tracked.

**Probes.** Robots was fetched first; the wildcard group has `Crawl-delay: 4` and no rule matching `/w/rest.php/`. Two guesses at a machine-readable REST description were made at the standard MediaWiki locations (a discovery document and a module specification). Slash keys were sought through the documented `search/page` route with only its documented `q` and `limit` parameters. The slash-key page route was then requested in the two shapes the earlier investigation recorded (ordinary percent-encoding, literal slash). Conditional requests were made on a page already fetched, with ordinary `If-None-Match` and `If-Modified-Since` plus negative controls. No double-encoding, alternate encoding, category or HTML fallback, Action query, POST or invented parameter was used.

## Evidence

**Request budget.** 15 requests used against the 250-per-hour ceiling (and the runner's own cap of 60). All 15 fell within a 113.1 s window. Minimum inter-request gap 5.103 s (configured 5.1 s). No retries, redirects, transport errors, 403, 429 or challenge. Outcomes: 9 HTTP 200, 2 HTTP 304 and 4 HTTP 404.

| # | Purpose | Result |
|---|---|---|
| 1 | `robots.txt` | 200, 6,322 bytes; identical in size to the earlier investigation's capture. |
| 2-3 | Machine-readable REST description (discovery document, module specification) | Both 404 REST JSON `rest-no-match`. Not pursued further: guessing more locations would be probing, not reading a published description. |
| 4-8 | Five `search/page` queries, generic terms, `limit=100` | All 200; each returned exactly 100 records. |
| 9 | Page route, slash key, ordinary percent-encoding | 404, Apache/edge HTML page (not REST JSON). |
| 10 | Page route, same key, literal slash | 404 REST JSON `rest-no-match`. |
| 11 | Page route, key without a slash | 200 JSON, 1,115 bytes, with `ETag` and `Last-Modified`. |
| 12 | Same page, `If-None-Match` set to the returned `ETag` | 304, empty body, validators repeated. |
| 13 | Same page, `If-Modified-Since` set to the returned `Last-Modified` | 304, empty body, validators repeated. |
| 14 | Same page, `If-None-Match` set to a non-matching value (control) | 200, full 1,115-byte body. |
| 15 | Same page, `If-Modified-Since` set to a date before `Last-Modified` (control) | 200, full 1,115-byte body. |

Totals: 9 HTTP 200 (requests 1, 4-8, 11, 14, 15), 2 HTTP 304 (12, 13), 4 HTTP 404 (2, 3, 9, 10); 9 + 2 + 4 = 15.

**Q1, slash keys.** Slash-containing keys are rare in search output: of the 500 records returned by requests 4-8, exactly 1 key contained a slash (`<page-key-01>`, one separator). The page route behaved exactly as the earlier investigation recorded, now on a different key: the ordinary percent-encoded form (`%2F`) was answered by an HTML 404 before reaching the REST router, and the literal-slash form reached the router and matched no handler (`rest-no-match`, the message echoing the path with the slash intact). Neither response says the page is absent, and no documented route variant remains inside scope. The REST description locations tried (requests 2-3) do not exist, so no published route description could be read to show a different slash-bearing route.

**Q2, pagination.** Every one of the five `limit=100` searches returned exactly 100 records with `pages` as the only top-level field; response headers carried no `Link`, total or cursor-style header (only a request-scoped `x-search-id`). The documented parameters are `q` and `limit`; no documented parameter selects a later window, and none was invented. A saturated 100-record response therefore cannot be distinguished from an exhausted one, and no way to enumerate beyond 100 was demonstrated.

**Q3, conditional requests.** The page response carried a strong `ETag` (no `W/` prefix; a 40-character hexadecimal value) and a `Last-Modified` equal to the body's `latest.timestamp` to the second, and `Cache-Control: max-age=5`. Sending the exact `ETag` as `If-None-Match` returned 304 with no body; sending the exact `Last-Modified` as `If-Modified-Since` returned 304; a non-matching `If-None-Match` and an earlier `If-Modified-Since` each returned the full 200 body. A repeat fetch (request 14) returned the same `ETag` as the first. Conditional behavior is therefore working and discriminating on the page route. Not tested: whether the `ETag` changes when the page is edited (no edit was observable inside the budget), whether it is a content hash or derived from the revision, conditional behavior on the search route, or behavior after the 5 s freshness window beyond what these requests incidentally covered.

## Verdict

1. **Slash keys: cannot be served by the documented route within scope.** Both documented encodings fail, one at the edge and one in the REST router, reproduced on a second key. The failure is not evidence the page does not exist. No in-scope variant remains and no published route description is available.
2. **Pagination: not settled.** The documented search offers no continuation; 100 records with no total or cursor is the observed ceiling, and exhaustion is unknowable from the response.
3. **Conditional requests: settled for the page route.** `If-None-Match` and `If-Modified-Since` each produce 304 on an unchanged page and 200 on a non-matching validator. Whether the `ETag` tracks revision changes is not settled.

## Recommendation

- **Slash-key handling should not change toward a workaround.** The provider bead should treat a slash-containing native key as unloadable by the REST page route: preserve the search-returned key verbatim as identity, report load as unavailable/incomplete with the original key and both observed responses as evidence, and never treat the 404 as absence of the page or as a negative-cache entry for the recording. It should not attempt encodings, category or HTML fallback. Because such keys were 1 in 500 records in this sample, the cost of that limitation is small; a sample of 500 is not a measured archive-wide rate.
- **Pagination handling should not change.** Keep the existing rule: a response at the cap of 100 is incomplete with no cursor, and a count below the cap is not proof of exhaustion. Narrow with more specific artist/event/year queries rather than expecting continuation.
- **Conditional requests may be adopted for page refresh, conservatively.** The provider may send `If-None-Match` (preferred) with the stored `ETag` and treat 304 as "unchanged since the stored snapshot". This refines the current record that the transport sends no conditional requests. A 304 should still count as a request against the pacing budget, and the stored `ETag` should remain recorded evidence, not a revision, until a bead observes an `ETag` change coinciding with a `latest.id` change.
