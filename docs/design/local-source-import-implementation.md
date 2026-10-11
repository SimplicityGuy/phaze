# Local source import and reviewed authority

Implemented 2026-10-10 for phaze-67q4e, following the companion plan and source-neutral provider design. Imports and explicit target-specific review are separate operations.

## Import boundary

`services/local_source_import.py` consumes an immutable raw companion observation from the authenticated capture report, or an explicitly chosen stored descriptive tag key. It uses the injected local provider registry and local tracklist interpreter for both source channels. Release extraction produces a separate observation so complete release notes never grant approval to an incomplete tracklist. Original raw observations remain the parent of parsed observations. Reimport appends or reuses candidates; it never selects a source or changes legacy tracklist pointers.

Stored embedded keys `comment`, `comments`, `description`, `lyrics`, and `tracklist` are accepted in their exact original case. The caller chooses an actual key; case aliases are not merged. Native identity is `embedded:<recording UUID>:<actual key>`. Its revision hashes the stored channel text, never the media file. Native ID3 `COMM`/`USLT` frame keys and MP4 namespaced keys are outside this initial grammar. They remain stored in raw tags without automatic import.

The capture report retains `capture:inventory_sha256:<expected digest>` evidence. This validates inventory freshness for bounded reads independently from a bounded-prefix digest. Historical bounded observations without this evidence require recapture; a full companion SHA with explicit `revision:sha256:full` evidence remains supported. No controller disk or network read occurs during imports.

The injected interpreter can retain partial track rows as an incomplete candidate. Recognition-only default behavior remains unchanged. A truncated or incomplete read can never become a complete parsed outcome. Raw text, partial facts and errors remain available for detail readers, but incomplete data cannot be selected.

## Explicit decisions

The admin APIs under `/api/local-sources` expose import, reimport, decision and selected-content reads. They use the repository's existing private-LAN reverse-proxy operator boundary (`docs/design/0019-runtime-config-hot-reload.md` section 10 and `routers/admin_runtime_config.py`), with server-owned actor identity and audit logging. Agent credentials are reserved for agent reports. These routes do not write media, tags, rename proposals or approved Changes Review state.

A decision names the recording, immutable observation, kind, expected source revision, expected source and recording inventory SHA, expected current selection token, and unique decision UUID. The immutable audit payload includes the full request and actor. Reusing a UUID for different intent fails. Only an identical still-current replay is a no-op; an older decision cannot resurrect authority after a later choice. Legacy choices receive a deterministic read token without rewriting their history. A pending candidate can be rejected without clearing another reviewed choice. Rejecting an active selected observation requires choosing a replacement.

Tracklist selection requires complete nonempty tracks. Release selection requires complete structured release facts; an exhaustively scanned note with no recognized fields remains viewable text but cannot replace reviewed metadata. Conflicting source content requires explicit acknowledgement. Unknown/conflicting facts retain their original certainty; selecting a source does not invent trusted values.

## CUE target applicability

A reviewed CUE decision explicitly maps one intrinsic FILE ordinal to one recording. The source snapshot retains all tracks, original FILE literals, rational 75-fps offsets and intrinsic origins. The selected DTO filters only the reviewed FILE part and qualifies its intrinsic offsets against the recording while retaining original origin evidence. It never concatenates multiple files, opens a FILE literal, guesses a target from its name, or mutates a canonical source version.

`get_selected_recording_source(session, media_id, kind=...)` returns the pinned immutable snapshot and separate target-projected tracks. The selected observation is authority; arrival order and newer pending candidates are not. The DTO is DB-only and keeps selected text/facts readable when inventory becomes stale, missing or unlinked. Detailed list projections should select narrow summary columns rather than duplicate full snapshots for each list row. The shared detail projection and consumer cutover beads reuse this port.

## Association evidence and transactions

Migration 086 adds nullable link derivation method, revision, evidence and time; historical values stay NULL (unknown). Only actual `LinkDecision` reruns populate these fields, including retained links. No historical method is inferred from current filenames. Association locks the globally sorted participating inventory before changing companion pairs and rechecks source features against the locked inventory revision.

Decision UUID locks precede source buckets. Composed import/decision writes then acquire source buckets, all source/recording inventory rows sorted by original path and UUID, source objects/candidates/selections and append-only audit events. Network work is absent. Imports do not create physical compatibility Tracklist rows or abuse duplicate propagation fields. Selection event updates/deletes are rejected by the database. Every populated expansion field makes downgrade lossy and therefore refused; forward recovery preserves evidence.

## Validation

Synthetic seam tests exercise capture report → immutable raw storage → local parse/extraction → pending candidates → explicit review → selected projection, shared CUE targets, embedded channel revisions, replay/stale review rejection and retained offline history. Migration tests exercise unknown historical links, append-only events and evidence-preserving downgrade refusal. No production archive or backfill is involved.

## Remaining recognition gaps (phaze-dcfho)

Read-only investigation on 2026-10-10 revisited the phaze-6fud5 v2 results using its in-memory parser source and seeded reservoir sampling procedure (seed 6155, sorted traversal, six scan roots). The 483-file sample contained 120 each of CUE, TXT, NFO and M3U plus 3 PLS. The remaining cohorts reproduced exactly: 2 CUE tracklist failures, 49 TXT tracklist failures, and 38 readable NFOs with no release facts. Private source text and paths stayed on the owning host; only aggregates, structural row prefixes and anonymized syntax returned to scratch. No files, tags, database rows, source selections or deployment were changed. The investigation examined every remaining failure's anonymized layout, not every previously successful extraction's accuracy.

### What 100 percent means

Recognition is not accuracy, and a file extension is not proof that the requested information exists. Measure exhaustive enumeration of the available track rows and extraction of explicit release facts separately from absent information, unresolved field meanings, usable timing and applicability to a recording. Never manufacture a tracklist or release identity to make a denominator pass. These cohort targets are proposed acceptance criteria, not implemented results or evidence of archive-wide recall.

| Cohort | v2 result | Proposed outcome |
| --- | --- | --- |
| CUE, 120 files | 118 complete; 2 blocked by invalid frame components | Retain all 120 tracklists; keep the 2 invalid offsets unusable until reviewed correction |
| TXT, 120 files | 71 complete; 49 incomplete | Enumerate all 108 track-bearing documents by section; report 12 documents without tracks explicitly |
| NFO, 118 readable files | 80 with facts; 38 without | Extract facts from all 90 metadata-bearing documents; report 28 promotional notices without release facts explicitly |

Two all-NUL NFOs remain unreadable and are outside the 118 readable denominator. Previously successful TXT/NFO results still need an accuracy audit; a `found` status alone cannot establish correct row boundaries, field attribution or recording applicability.

### CUE: isolated invalid timing blocks otherwise retained tracklists

Each remaining CUE has exactly one `INDEX 01` with a frame component greater than 74: 75 in one file and 77 in the other. The parser retains 21 and 20 track records respectively but reports the entire snapshot incomplete. Other INDEX values in the two sheets use normal 75-fps syntax. The [libodraw CUE format specification](https://github.com/libyal/libodraw/blob/main/documentation/CUE%20sheet%20format.asciidoc) defines 75 frames per second. These two offsets are invalid under that grammar; their intended correction cannot be determined just from the text.

Do not silently carry excess frames into the next second or assume the author used hundredths. The proposed repair separates exhaustive track enumeration from timing validity: retain the original invalid timestamp, a visible diagnostic and an unusable offset; allow explicit review of the track identities while valid intrinsic offsets retain their original FILE origins. This is a recovery/review gap, rather than evidence that rejecting the two offsets is wrong.

The downstream seam matters: `services/cue_generator.py` omits tracks without qualified timing, and `services/selected_cue.py` counts them. A recovered metadata review must not imply that regenerated CUE output contains every track. Preview must expose exclusions, and writing should require correction or explicit acknowledgement of the missing timed row. Verify capture, import, immutable persistence, decision, projection and generated artifact behavior with synthetic invalid-frame fixtures. Reported as **phaze-f1wvr — Preserve CUE tracklists when isolated INDEX frame values are invalid**.

### TXT: sections, row grammar and false positives

The 49 rejected documents divide into disjoint investigation cohorts:

| Primary cohort | Documents | Required work |
| --- | ---: | --- |
| No tracklist present | 12 | Report absent content; do not turn promotional text, categories, empty templates or an explicit unknown tracklist into tracks |
| Multiple sets, discs or parts | 18 | Retain section boundaries, restart numbering within a section, and review which section belongs to which recording |
| Numeric/time prefix conventions | 2 | Support bracketed minute cues and zero-based ordinals followed by minute cues; never infer a recording-relative origin from syntax alone |
| Other row/evidence conventions | 17 | Broaden bounded grammar and retain localized unknown fields rather than discard identifiable rows |

Error counts overlap across documents: conflicting numbering affects 20 documents, title-only rejection 14, unsupported track numbering 6, ambiguous artist/title splitting 8, unsupported timestamps 2, and no supported tracks 15. Counts of individual rejected lines are not counts of files. Three of the 15 documents with no retained tracks contain a title-only list, a slash-separated list, or a bullet-separated list; the other 12 genuinely lack track enumeration.

Observed row grammars include title-only tables without a literal `Tracklist` heading; slash and tab columns; bullet separators; missing separator whitespace; timestamp-before-number and number-before-timestamp forms; `Setlist` headings; annotated `ID` and question-mark rows; trailing missing titles; and quoted or parenthesized hyphens that are not artist/title separators. Some rows encode compound tracks or additional label columns. Preserve their source text and unresolved structure instead of guessing a split. Digit-bearing artist tokens are also mistaken for unsupported side numbering in otherwise unnumbered lists.

Six of the multipart documents retain numbered media filenames from a framed `File(s)` header as track rows before the actual list begins. This creates false positives and a spurious numbering conflict even in the first real section. Suppress typed header blocks before row classification; relaxing only the numbering check would hide the defect.

Multi-set collection notes also contain sections explicitly without a tracklist. They cannot become a complete tracklist for one linked recording merely by flattening the sections that do have rows. The existing CUE FILE mapping is the pattern for explicit target applicability, but TXT currently lacks its equivalent section mapping. This needs parser/domain/import/review seam work, not just additional regular expressions. Keep genuinely duplicate positions in one section, truncated inputs and unresolved target applicability visible. Reported as **phaze-5cnfj — Recover TXT track sections and grammar without promoting headers to tracks**. Related **phaze-cdoft — Collection folders: non-tracklist companions (release notes, info .txt) still link to every episode** addresses association policy separately and is not a duplicate parser repair.

### NFO: ten real metadata layouts and twenty-eight notices

Of the 38 readable NFOs with no extracted release facts, 28 are generic promotional notices, including repeated site banners. They contain no release-specific field block. Broadly extracting artist names or genre mentions from that prose would attribute promotional examples to the linked release.

The remaining 10 contain explicit metadata missed by v2:

| Layout | Files |
| --- | ---: |
| Colon-decorated frames around dotted labels | 3 |
| Additional art, bar or arrow frames around labels | 3 |
| `LABEL..[VALUE` scene fields | 1 |
| Whitespace-delimited labels without a colon | 2 |
| `Runtime` alias with dot-separated duration | 1 |

An in-memory known-label syntax probe found candidates in all 10 and none of the 28 notices. This establishes recognizable field syntax, not a tested production extractor: normalization, exact source spans, frame removal and false-positive precision still need regression coverage. Keep dates with ambiguous locale order uncertain; preserve compound quality values; trim structural art without removing punctuation from values; require a bounded known-label grammar for whitespace fields. The existing raw-value/normalized-value/certainty contract can represent these facts without an LLM. Reported as **phaze-szf3h — Recognize framed and non-colon NFO release metadata layouts**.

### Recommended implementation order and validation

1. Repair CUE metadata recovery and timing warnings, with end-to-end review and generated-artifact checks. This is the smallest cohort and must retain all valid 75-fps behavior.
2. Broaden NFO framing and explicit-label grammar. Re-run the same cohort and require facts in all 90 metadata-bearing files, no invented facts in all 28 notices, and correct original spans on synthetic layout regressions.
3. Repair TXT header detection and row grammar, then introduce section-aware target review. The 108 track-bearing-document target is conditional on correctly representing sections and unresolved fields; it is not a promise that every document applies to every associated recording.
4. Reaudit successful positives and expand the corpus beyond this seeded sample. Record recall on documents that contain the requested information, precision of extracted rows/facts, absent-content outcomes, localized warnings, truncation and target applicability independently. A 100 percent sample recognition result must not be presented as 100 percent semantic accuracy or archive-wide coverage.

This bead contains investigation evidence only. The three reports are intake awaiting triage, and phaze-6fud5 remains the prerequisite v2 repair awaiting independent review. No follow-up implementation or deployment is included here.
