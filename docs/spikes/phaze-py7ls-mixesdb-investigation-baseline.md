# MixesDB directed investigation baseline

Date: 2026-10-08. Bead: phaze-py7ls. Epic: phaze-ypcx6. Investigation only; no adapter or migration is implemented.

## Question

What evidence remains necessary before proposing a MixesDB adapter against the provider specification, after two historical spikes recommended the source?

## Method

Read the [public viability spike](phaze-2ov24-mixesdb-viability.md) and [historical archive coverage spike](phaze-q2v7w-mixesdb-archive-coverage.md). Separate measured results from assumptions and recommendations. Do not repeat archive sampling: the fresh authorization excludes archive access.

The operator's fresh access answer, recorded on the epic, is: “Up to 100 MixesDB requests including retries and policy checks; allow robots/policy reads, then REST-only, identified client, serialized ≥5-second pacing; no archive access”. Specification and investigation are authorized; production code, migrations, deployment and adapter planning remain gated.

One investigator owns transport. Narrow same-host policy reads precede REST search/page reads. Requests are charged durably before sending, with a persisted shared host clock. No Action API, category/HTML discovery fallback, proxy, challenge solving, media fetches or cross-site requests.

## Evidence

| Historical measurement | Exact result | Limitation or correction |
|---|---|---|
| Public viability run | 77 of 200 requests; all HTTP 200; 12/36 matching sample items | Selected public examples, not an archive coverage estimate; included HTML and bare API help routes outside this fresh investigation's scope. |
| Public tracklist pages | 10/11 nonempty; 3/11 with mm:ss syntax | Syntax did not establish elapsed-offset meaning; parser missed the alternate list markup. |
| Archive coverage run | 298 of 300 attempts; 148 sampled sets from 85,969 eligible unique sets | Historical read-only measurement; no fresh archive access. |
| Archive matching | 25/148 matched; 60/148 classified absent from search; 63/148 capped/undetermined | The 60 are search nonmatches, not proven provider-wide absence. Fewer than 100 search results do not demonstrate enumeration exhaustion or alias recall. |
| Archive precision | 22/25 titles consistent with artist plus event or year; 3/25 weak artist-only matches | No ground-truth match precision was measured. Never turn title consistency into a measured precision percentage. |
| Page acquisition | 24 distinct pages fetched; one matched page with slash-containing key returned 404 | Correct native-key encoding remains unresolved. |
| Track content | 21/24 nonempty; 16 pages numbered markup, 8 alternate list blocks | Both forms need parsing; recognition must not fabricate an empty successful tracklist. |
| Historical cues | 10/24 minute offsets; 0/24 second offsets judged usable | First-mark magnitude alone is insufficient to prove clock time. Preserve ambiguous interpretation. |
| Historical pacing | One 0.52-second gap; remaining 296 gaps at least 5.3 seconds | Lost clock across restart caused the breach. Persist the clock across every worker and retry. |

Historical extrapolations and GO recommendations represent the prior authors' judgements, not fresh measurements. For phaze-ypcx6 on 2026-10-08, the current scope is investigation/specification; the historical suggestion to file a build bead is outside that scope.

Fresh policy reads, the initial conservative stop and the subsequently authorized API phase are recorded in [REST access and pacing](phaze-6ehg6-mixesdb-rest-access.md). Final shared count is 13/100; REST search and ordinary page routes work, slash-key routing remains unresolved, and bare Action help identifies MediaWiki 1.46.1. On phaze-ypcx6, 2026-10-08, the followup answer was “Respect those robots restrictions; test REST and the bare Action API endpoint only”. This amends the initial access scope without permitting `?action=` queries. Matching and completeness are in [search semantics](phaze-s19tp-mixesdb-search-semantics.md); source interpretation is in [parsing and provenance](phaze-vwcsv-mixesdb-parsing-provenance.md).

## Verdict

The evidence supports a content source with modest measured historical yield and now verified REST search/page availability. It does not prove reliable automatic association, precise cues or complete negative discovery. Those claims must remain separate. The current Action evidence is help/version discovery only.

## Recommendation

Directed questions for this batch:

1. Can fresh policy/access checks proceed without hitting a mandatory stop? If stopped, report which live questions remain unknown.
2. Can allowed REST endpoints preserve native identity, revisions and slash-containing keys? Do not substitute other endpoints.
3. What does search actually prove about enumeration, alias recall, ambiguity and absence?
4. Can both tracklist syntaxes preserve unresolved tracks, remix/mashup text and timestamp uncertainty within finite parser limits?
5. Does the proposed provider port carry provenance, rights, source-scoped identity and typed failure outcomes without provider-specific storage or approval authority?
6. Which recommendation can be reviewed now, and which future implementation/activation questions require operator direction?

All tracked examples are synthetic or historical aggregate counts. No archive identifiers, responses or transport scratch files are committed.
