# ADR-0022: Tracklist acquisition spike verdicts (looser merging, ajax search, MixesDB, parser fixes)

Date: 2026-10-07. Bead: phaze-i5sp6 (decision bead, epic phaze-5d0wg). Status: accepted.

## Decision

One verdict per spike-gated path. Each cites its spike doc by filename and the measured quantity that decided it.

| # | Path | Verdict | Operator selected (phaze-i5sp6, 2026-10-06, question and answers recorded below) | Spike recommendation (not operator-decided) | Spike doc |
|---|---|---|---|---|---|
| 1 | Looser event+date set merging | **NO-GO** | operator ruled NO-GO ("NO-GO (Recommended)") | n/a (the operator's ruling is the verdict) | `docs/spikes/phaze-5rhjq-collapse-ratio.md` |
| 2 | ajax JSON search (`/ajax/search_tracklist.php`) | **GO** | operator selected "ajax JSON search" to carry forward | spike: the JSON search works for an honest client | `docs/spikes/phaze-gnbct-ajax-endpoints.md` |
| 3 | MixesDB as a second source | **GO**, two conditions | operator selected "MixesDB source" to carry forward | spike: the two conditions, measure real archive coverage first and use the REST API only | `docs/spikes/phaze-2ov24-mixesdb-viability.md` |
| 4 | Parser fixes (mark unresolved `ID - ID` rows; row-count canary) | **GO** | operator selected "Parser fixes" to carry forward | spike: the specifics, mark unresolved `ID - ID` rows and add a row-count canary against numTracks | `docs/spikes/phaze-gakqn-parser-gap-audit.md` |
| 5 | Deferred items (below) | **DEFERRED, not decided, not a NO-GO** | not selected | implementer / planner deferral | various |

The operator's authority in this table is limited to the NO-GO ruling in row 1 and the selection of which paths to carry forward in rows 2 to 4.
The MixesDB conditions and the parser-fix specifics are the recommendations of `docs/spikes/phaze-2ov24-mixesdb-viability.md` and
`docs/spikes/phaze-gakqn-parser-gap-audit.md`, per CLAUDE.md rule 2 and `docs/design/0012-verification-fidelity-and-operator-attribution.md`.

### 1. Looser event+date set merging: NO-GO

Deciding evidence, from `docs/spikes/phaze-5rhjq-collapse-ratio.md`:

- The measured collapse ratio is 94,533 / 85,964 = **1.0997** (about 1.10), so merging cannot recover much. The most event+date
  merging can save is 4,264 sets (4.96% of the queue, about 1.2 days of 24.9).
- Of the 1,718 event+date groups holding 2 or more sets, **1,571 (91.4%)** join sets with differing artist text, and **4,103 of the
  4,264 extra sets (96.2%)** sit in those groups. This is the festival-day shape (several artists, one event, one day) and would
  propagate one set's tracklist onto another's files.
- The spike labels 4,103 an **upper bound** on true false merges, not a count: differing artist text can be a spelling variant of one
  artist, and no ground truth exists without spending lookups. The safe part, groups with a single artist text, is 147 groups saving
  161 sets (0.19% of the queue, about 0.05 days), and 144 of those 147 are already plain same-query duration splits.
- Event-only merging is worse: 1,342 groups (11,629 sets) are the same event on different dates.

The risk is not paid for by the saving. Do not build event+date or event-only merging. The narrower duration-tolerance lever the spike
mentions (1,936 same-query splits, 3,394 extra sets) is **not** decided here; see section 5.

### 2. ajax JSON search: GO

Evidence from `docs/spikes/phaze-gnbct-ajax-endpoints.md`: `search_tracklist.php` returned valid JSON on 2 of 2 requests with 0
challenges (all 8 of 8 `/ajax/` calls answered HTTP 200 JSON, 10 of 30 budgeted live requests spent). Each result carries `id_unique`,
`id_tracklist`, `url_name` and a `tracklistname` from which artist, event and date parse (the `Artist @ Event, ... YYYY-MM-DD` shape held
for 18 of 20 results). Limits recorded by the spike: the search cannot be narrowed by year (query 4 returned the same 4851-byte body as
query 3), and track count and duration are absent, so confirmation can still need a detail page.

**Carry-forward condition (from the spike's own recommendation 5):** before the build depends on it, first test whether the endpoint
works with **no prior page navigation** and from **plain `httpx` with the honest UA**, using **at most 3 requests**. The spike did not test
either (its 8 calls were all in-page `fetch` on a session whose navigation had itself been captcha'd). If both fail, the render session
is still needed for discovery and the build is sized accordingly.

### 3. MixesDB as a second source: GO with two conditions

Evidence from `docs/spikes/phaze-2ov24-mixesdb-viability.md`: all 77 of 77 requests returned HTTP 200 to the honest UA (no challenge,
403 or 429; 77 of 200 budgeted requests spent); the REST API is served and not covered by a `Disallow`; tracklists are CC BY-SA 3.0 US
"if not stated otherwise" per the site's legal page. Public-sample coverage was 12/36 = **33%** (a floor), within a 33% to 56% range, and
explicitly **not** the archive's rate.

Conditions, from the spike:

1. **Measure real archive coverage first**: at least 100 archive sets, artist/event/date derived from existing metadata, the same
   two-step lookup, recording **counts only** (matched, absent-by-complete-listing, undetermined, with a cue, with `mm:ss` cues). If the
   matched share is low, drop the source.
2. **Build on the REST API only** (`rest.php/v1/search/page` and `rest.php/v1/page/<key>`). Not `api.php`, not `?action=` or `?title=`,
   no `Disallow`ed path; a `robots.txt` change is a stop. The spike's other build requirements (5 s spacing, caching, attribution and
   licence storage, minute-granular cue modelling) carry forward as that spike's recommendations 3 to 8.

### 4. Parser fixes: GO

From `docs/spikes/phaze-gakqn-parser-gap-audit.md`, two items:

- **Mark unresolved `ID - ID` rows** instead of storing the literal `ID` as artist and title. 12 rows are affected (10 of 52 in
  `25fhn7c9-ok.html`, 2 of 12 in `19h6nw7t-ok.html`); the discriminator is exact in both captures (`data-isided="true"` is absent on
  exactly those rows).
- **Add a row-count check against `itemprop=numTracks`** as a markup-drift canary. It equals the container count in both captures
  (52 and 12), and an under-matching selector with all rows parseable passes today.

The representation of an unresolved row is a design choice for the build bead; the spike notes it "needs a decision on representation".

### 5. Deferred: explicitly not decided, and not a NO-GO

These are the **implementer's and planner's deferral** (phaze-i5sp6, 2026-10-07), not operator decisions. Each is open and can be revived by the evidence named.

- **ajax media links (`/ajax/get_medialink.php`).** The spike verdict was GO (5 of 5 valid JSON, 4 with data, 1 `noLinkFound`), but the
  operator did not select it in the question recorded below, so it is not carried into the implementation molecule.
- **Turnstile and captcha retry tuning.** There is no measured retry data, and bug phaze-a6n3e must land first so that retries act on
  correctly classified blocks.
- **The other parser extras:** the inline `cueValuesEntry` cue map, `w/` mashup rows, `span.trackStatus`, and the `/source/` taxonomy.
  The two recorded captures contain no evidence for them (`trackStatus` 0 / 0, real `w/` mashup hits 0 / 0, cue map a placeholder in both,
  stage 0 / 0); cued-page captures are needed first, each as its own capture bead. Row genres, set genres, recording duration, set headers
  and set-level links were also not selected and remain unbuilt.
- **A wider duration tolerance for same-query sets** (1,936 splits, 3,394 extra sets), which `docs/spikes/phaze-5rhjq-collapse-ratio.md`
  says only counts the splits and does not show the folded pairs are the same recording. Not filed or decided.

## Operator decisions (phaze-i5sp6, 2026-10-06/07, session crossed midnight)

Durable record: this section, per [ADR-0012](0012-verification-fidelity-and-operator-attribution.md) rule 2. Only the selected option
labels are quoted as answers. The attribution extends no further than the questions asked.

**Looser event+date merging.** Question as put:

> The spike evidence is in. How should the decision bead rule on looser event+date set merging? The collapse spike found 96% of the groups it would fold join sets with different artist text (festival-day shapes), and a measured collapse ratio of 1.10.

**Correction (phaze-5d0wg.3, 2026-10-07):** The spike `docs/spikes/phaze-5rhjq-collapse-ratio.md` measured 1,571 of 1,718 groups (91.4%) join sets with differing artist text; 96.2% (4,103 of 4,264) is the share of the extra sets in those groups. The NO-GO verdict is unaffected: merging could save at most 4,264 sets, about 1.2 days of the 24.9-day drain.

Answer as given (selected option label): "NO-GO (Recommended)".

**Which paths to carry into implementation** (multi-select). Question as put:

> The ajax spike found the JSON search and media-link endpoints work for an honest client, and MixesDB is a conditional GO. Which spike-gated paths should I carry into a /bh:replan implementation molecule?

Answer as given (selected option labels): "ajax JSON search", "MixesDB source", "Parser fixes".

**Image-captcha bug.** Question as put:

> The ajax spike found that detail pages returned an image captcha that the renderer does not recognise, so it would be recorded as 'set has no tracklist' and cached as NOT_FOUND for 180 days. Should I file this as a bug bead now, and at what priority?

Answer as given (selected option label): "File as P1 (Recommended)". Filed as bug phaze-a6n3e.

What is **not** operator-decided (phaze-i5sp6, 2026-10-07): the two GO conditions in sections 2 and 3 are the spikes' own recommendations, and the whole of
section 5 is deferral. The media-link endpoint was not selected; that is recorded as a fact about the answer, not as a NO-GO.

## Findings (not decisions)

**Measured archive scale** (`docs/spikes/phaze-5rhjq-collapse-ratio.md`, 2026-10-07, read-only, production grouping, all-MISS cache):

- 145,057 files scanned; 104,247 media; 94,553 set-like (`LIVE_SET`); 94,533 candidate files; **85,964 unique sets**.
- Collapse ratio **1.0997**. `docs/design/0014-tracklist-candidate-sets.md` assumes **2.0**; that assumption does not hold. 80,067 of 85,964
  sets (93.1%) are singletons.
- Drain time at A1 to A4 of the spike (8 to 12 s per host request, mean 10 s; about 2.5 requests per lookup): about **24.9 days**
  continuous at 10 s (19.9 to 29.9 days across 8 to 12 s), against about 13.7 days if 2.0 held. Because `DEFAULT_PROPAGATION_MIN_CONFIDENCE`
  is `EXACT`, only 1,867 of 8,569 collapsed links (21.8%) propagate, giving 92,666 lookups and about **26.8 days** at 10 s.
- Zero query signal is negligible: 70 files (0.074%), 8 unique sets, already skipped at zero host cost. That weakens the case for a
  query-derivation bead justified by rescuing them.
- Correcting the 2.0 row of `docs/design/0014-tracklist-candidate-sets.md` is prose and goes through its own docs PR; this ADR does not edit it.

**Image-captcha misclassification** (`docs/spikes/phaze-gnbct-ajax-endpoints.md`): 2 of 2 detail-page navigations returned the site's own
image captcha ("We need to validate your are real human!", `id="captcha"`), not Cloudflare Turnstile. `looks_like_interstitial` returned
`False` and `_classify` falls through to `NO_TRACKLIST`, so the renderer would store a cacheable "no tracklist" instead of a retryable
block. The intended behaviour is classification as `INTERSTITIAL_PERSISTED`, not cached. Filed as bug phaze-a6n3e (P1). The rate at which
the captcha fires is unmeasured (2 of 2 in this run only).

## General forms of the lessons recorded

- **A measured ratio replaces a design assumption.** The 2.0 collapse assumption was priced into a design table and survived because no
  one had measured it over the whole candidate population; a figure from a smaller earlier run (1.0307 on 9,708 lookupable sets in
  `docs/design/0014-tracklist-candidate-sets.md`) did not vouch for the full archive. General form: cite a ratio with the population it
  was measured on, and re-measure when the population changes.
- **A prior-run result does not vouch for a new session.** The detail page cleared in the 2026-08-03 capture and was captcha'd on
  2026-10-06; the `/ajax/` endpoints answering inside one captcha'd session does not show they answer with no navigation or from plain
  `httpx`. General form: a behaviour of a third-party site is a claim about one session until re-run in the shape the build will use,
  which is why the carry-forward test in section 2 exists. See also [ADR-0016](0016-transferred-model-verification.md).
- **A sample drawn from public data is not the archive's distribution.** The MixesDB 33% is a public mainstage sample; section 3 requires
  the archive measurement before sizing. General form: a coverage figure names its sample, and the build is gated on the real population.
- **An upper bound is not a count.** The 4,103 false-merge figure is labelled as such by its spike and is quoted as such here. General
  form: carry the bound label with the number or the number will be read as a measurement.
