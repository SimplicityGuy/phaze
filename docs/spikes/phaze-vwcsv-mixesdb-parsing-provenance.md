# MixesDB parsing, timestamps and provider provenance

Date: 2026-10-08. Bead: phaze-vwcsv. Status: offline assessment against proposed provider contract; no parser implementation.

## Question

Can a MixesDB content adapter preserve source meaning, identity and rights without losing unresolved tracks or inventing recording-relative cues?

## Method

Use historical markup observations and synthetic fragments. Compare with the proposed descriptor/discover/load provider boundary drafted by phaze-drv39 under phaze-uquqk. That contract is a parallel proposed specification, not a landed runtime API. The initial [access stop](phaze-6ehg6-mixesdb-rest-access.md) preceded REST requests; the subsequently authorized API phase obtained two public REST page bodies. No actual parser or consumer changes were tested here.

## Evidence and interpretation cases

Historical pages used numbered `#` entries on 16/24 pages and `<list>` blocks on 8/24. The original public spike recognized only the numbered form. Preserve ordered entries from both supported forms; reject unsupported nested structures explicitly rather than silently returning an empty list.

The two fresh pages have 12 and 21 numbered entries, respectively; neither has cue marks or `<list>` markup. One contains remix/mashup text, the other section labels dividing track groups. These are bounded source observations, not executed parser coverage. Preserve section labels and the source's ordering/credit context as provenance rather than silently flattening every section into one confident set identity.

All examples in this table are synthetic:

| Source fragment or case | Proposed result |
|---|---|
| `# [05] Example Artist - Example Title [Example Label]` | Ordered track, minute precision; preserve original `05`. Offset kind requires source/origin evidence; never promote to exact seconds. |
| `<list>` with `[00] Example Artist - Opening Track` and `[08] ID - ID` | Two ordered rows; second artist/title nullable, original unresolved text retained. Recognition does not imply all tracks resolved. |
| `# [07:??] Example Artist - Example Title` | Unknown/partial timestamp, original preserved, unusable for exact cues. |
| `# [0?] ID - Unknown` | Unknown precision/value, nullable fields, no fabricated zero offset. |
| `[12:35]` with no timing-origin context | Ambiguous clock/offset interpretation. Syntax alone proves neither kind. |
| Explicit clock sequence `23:58`, `00:03` | Clock kind with midnight wrap evidence; no duration/offset inferred without recording start and day mapping. |
| Valid offset at `15:00` after a documented long intro | Relative offset can be valid; magnitude above ten minutes is not a clock detector. |
| Hour-form `01:12:30` with documented elapsed origin | Offset with second precision, retaining the original text. Check order/range against independently known duration. |
| CUE `INDEX 01 00:01:37` | CUE-specific 75fps rational offset only when that format is actually supplied; never parse a wiki colon tuple as CUE frames by guessing. |
| `Example Artist - Example Title (Example Remix) w/ Other Artist - Other Title` | Preserve remix and mashup text; optional structured components need explicit parse evidence. Do not split into two chronological tracks automatically. |
| Missing separator, nested templates or an unterminated list | Incomplete/unsupported with bounded fragment and affected positions; no invented clean artist/title. |
| Repeated track text | Preserve separate ordered occurrences; identical text is not duplicate-removal authority. |
| No recognized track lines | Distinguish demonstrated trackless page from unrecognized/truncated content; recognition failure is not found-empty. |
| Response truncated at byte/track/line limit | Incomplete with limit and scope; no full-content digest or complete-track claim. |

These are written acceptance scenarios, not executed parser tests. Minute cues can help approximate display and ranking when their origin is evidenced, but exact CUE export must reject minute, clock and unknown entries. A mixed page is evaluated per entry; one qualified cue cannot upgrade the others. Existing consumer behavior requires separate specification/implementation before qualified timestamp eligibility is enforced.

## Proposed field mapping

| Source observation | Provider domain representation |
|---|---|
| REST search-returned key | Stable `(mixesdb, native_id)` without truncation; distinct from display title, encoded URL and content revision. Never place it into a retired provider's ID namespace. |
| Page title/category/date evidence | Nullable set artist/event/date plus known/inferred/unknown/conflicting certainty and bounded origins. Do not normalize a partial date into an exact day. |
| Page source | Format and parser revision, track enumeration completeness and bounded interpretation evidence. Native markup stays source evidence, not matching authority. |
| Fresh `latest.id` and `latest.timestamp` | Current opaque revision and revision-time evidence; explicit unknown for sources without it. Retrieval time is independent and timezone aware. |
| Ordered lines | Positive unique positions, nullable artist/title, optional label/remix/mashup metadata, timestamps with original/kind/precision/interpretation evidence/usability. |
| Original page reference | Source URL for attribution; page key remains identity. URL presence never grants permission to fetch media or linked sites. |
| Empty REST license object | Unknown API license evidence, supplemented by separately retrieved policy statement; never interpret blank strings as rights clearance. |

The proposed contract supports these requirements without source-specific ORM DTOs or acquisition scheduling. Core owns effects, allowed routes, retry budgets, matching, storage/version import and approval. Provider output can propose content; it cannot associate, apply, export or replace manual data autonomously. A revision change after discovery must be explicit before import/review.

## Rights evidence

The fresh [MixesDB legal page](https://www.mixesdb.com/w/MixesDB:Legal_stuff) states community tracklists normally use CC BY-SA 3.0 US, subject to exceptions. Other text and namespaces have separate author-credit terms; images and trademarks carry distinct rights. This report records the source's statement; it does not decide whether a particular import/export is legally permitted. The linked external license deed/code was not fetched, as cross-site requests are outside authorization.

A future snapshot must preserve page URL, native ID, revision or unknown state, retrieval time, applicable policy URL/statement, per-page overrides when observed, and parser version. Show the source reference where imported content appears. Keep redistribution/export gated until source statements, exceptions and license obligations have been reviewed. The historical claim that private storage categorically resolves all license questions is not adopted as a legal determination.

Do not copy policy prose into track content, fetch images/audio, follow player URLs or import comments/other namespaces as tracklists. Policy reads are investigation evidence, not permission to broaden acquisition.

## Verdict

The proposed provider port can represent MixesDB's formats and uncertainty. Current REST revision schema and two numbered-list sources were observed. No production parser coverage, fresh alternate-list/timestamp coverage or per-page rights override behavior was verified. Qualified timestamp consumption remains a separate implementation concern.

## Recommendation

Require bounded dual-format parsing, nullable unresolved fields, immutable provenance, scoped identity and conservative timestamp qualification in any later plan. Before product activation, execute synthetic parser cases and obtain separately authorized fresh allowed REST fixtures. Keep rights evidence distinct from an export permission verdict.
