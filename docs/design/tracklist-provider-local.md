# Local companion and embedded tracklist provider

Date: 2026-10-09. Bead: phaze-hhkbk. Epic: phaze-uquqk. Status: specification for review; no adapter or parser implemented.

This is the first planned in-repo provider under the approved [contract](tracklist-provider-contract.md), [matching policy](tracklist-provider-matching.md) and [persistence design](tracklist-provider-persistence.md). It changes no runtime behavior. Inspection baseline is `3ee5bfdb5e1149fdd60eafa3b34e0ea3c636b2c0`: supported group claim refreshed the epic from main before creating this batch. The contract's earlier pending offline-review branch is historical; the recognition path is now present in this source baseline. No archive, production or remote-provider access was used.

## Current evidence and proposed effect flow

| Source / symbol | Current evidence |
|---|---|
| [FileCompanion](../../src/phaze/models/file_companion.py), [link_companion](../../src/phaze/services/companion_linking.py) | Scanner/association-produced many-to-many file links include reference, stem and broad rules. A stored pair alone has no creation-method provenance or automatic provider association authority. |
| [extract_content_features / read_companion](../../src/phaze/services/companion_features.py) | Recognizes track-like content and references; does not produce TracklistTrack rows. Reads up to 1,048,576 feature bytes, caps lines at 1024 characters and references at 200; reports truncation and streams the fingerprint when needed. These are current recognition bounds, not proposed complete-parser guarantees. |
| [post_companion_features](../../src/phaze/routers/agent_companion_features.py), [extract_companion_features](../../src/phaze/tasks/companion_features.py) | Agent-local extraction and controller storage supply revision-scoped recognition evidence. |
| [read_companion_bounded_sync](../../src/phaze/services/companion_read.py), [read_companion_files / _read_all_sync](../../src/phaze/tasks/companion_read.py) | Agent containment resolution precedes nofollow bounded reads; UTF-8-sig strips BOM, TXT/NFO can fall back to CP437. Proposal reads keep at most four times requested decoded characters per attempt. Failures currently skip entries; the future provider effect must report individual typed failures and truncation. |
| [ReadCompanionFilesPayload / CompanionReadItem](../../src/phaze/schemas/agent_tasks.py) | Existing wire payloads constrain authorized companion reads. New provider read metadata must be designed explicitly; do not pretend this payload already reports complete parse/revision outcomes. |
| [detect_embedded_tracklist / _local_sources](../../src/phaze/services/tracklist_review.py) | Descriptive tag heads bounded at 12,000 characters recognize at least three timestamped lines; CUE links and fingerprint-current TXT/NFO features are displayed. Recognition alone does not parse tracks or import versions. |
| [load_companion_targets / fetch_companion_contents / clean_companion_content](../../src/phaze/services/proposal_context.py) | Proposal context resolves targets by agent using database reads, then reads bounded chunks after releasing the database session. Prompt cleaning/truncation is unsuitable as a complete parser input. Preserve that existing context flow. |

Proposed flow, with all new ports illustrative:

```text
stored file identities + fresh association/features + embedded raw_tags
  -> core builds bounded RecordingContext and authorized source references
  -> local provider discover returns scoped candidates + completeness/evidence
  -> core-injected agent read effect checks owner, containment, budget, revision
  -> provider parses returned bytes/text or supplied bounded embedded metadata
  -> typed snapshot/outcome -> shared core matching -> review/authorized import
```

The controller remains fileless: it never opens agent paths, scans directories or follows a sidecar's `FILE` path itself. An authorized source reference is bound to the owning agent/file UUID; read effects resolve current containment and refuse outside-root/symlink access. CUE target text is untrusted evidence, never an instruction to open arbitrary paths. Providers get no database session and cannot schedule agent jobs or write approval/storage themselves. The effect layer owns routing, permissions, cancellation and finite read/request budgets.

## Planned identity, discovery and revisions

Propose one explicit built-in `local_tracklist` descriptor with discovery/load, sidecar, embedded, parsing and timestamp-evidence capabilities. This identifier is a design choice subject to registration review, not a shipped provider. Native keys distinguish companion UUID sources from recording UUID plus embedded channel; paths and content hashes are excluded from stable identity. File moves retain identity only while the file UUID survives; re-ingestion with a new UUID requires explicit continuity review. URL is optional and absent by default.

Discover enumerates supplied authorized references and bounded metadata channels; no recursive disk search. It returns exact scope, recognized format, revision evidence and incomplete reasons for truncated context/features or stale references. Recognition cannot establish complete track enumeration. Load checks the observed file/tag revision against discovery; changed bytes produce retry/re-discovery evidence, not a snapshot falsely labeled with the old revision. A full-content digest may be revision evidence, but a bounded head digest is explicitly partial. Missing revision evidence remains unknown and uses the persistence design's complete normalized comparison after a successful complete parse.

## Syntax and outcome matrix

The rows below specify intended initial support. **Current runtime supports recognition and proposal context, not the public local adapter or these complete parse promises.** Numeric parser budgets and exact grammar fixtures must be fixed and evaluated during separately approved implementation.

| Format / bounded input | Planned interpretation | Outcome and limits |
|---|---|---|
| CUE with FILE, TRACK AUDIO, optional PERFORMER/TITLE and INDEX01 `mm:ss:ff` | Ordered tracks, set-level versus track-level fields, referenced-file origin, rational frames/75. Missing artist/title remain NULL. | Found only when complete dialect parse/targets are validated. Frames 0–74 and seconds 0–59 required. Missing INDEX01 retains untimed tracks; malformed/conflicting indexes yield incomplete/ambiguous. |
| CUE INDEX00, REM comments and unsupported directives | Retain bounded original evidence; INDEX00 is not silently substituted for INDEX01. REM date text retains certainty. | Unsupported semantic directives produce explicit unsupported/incomplete reasons; harmless documented comments need not erase parsed tracks. No guessing track gaps or exact dates. |
| Multi-file CUE | Keep each track's referenced FILE origin and local index; positions remain ordered across the source. | Preserve source snapshot, but a single-recording association or export is review-required until target-specific part mapping is evidenced. Never sum durations or concatenate files by guesswork. |
| TXT/NFO explicit ordered lines such as `01. Example Artist - Example Title` or `01. [00:03:15] Example Artist - Example Title` | Proposed narrow documented grammars; preserve line order, unresolved NULL fields and original timestamp text. | Found only when the chosen grammar accounts for complete track enumeration. Mixed prose, conflicting numbering or unrecognized track-like lines produce incomplete/unsupported. `ID - ID` is unknown, not literal known artist/title. |
| TXT/NFO recognition flag without a supported grammar | Keep availability/recognition and bounded raw evidence, with no invented tracks. | Incomplete with recognition-only reason; neither found-empty nor semantic absence. Release notes and unrelated prose may be unsupported. |
| Embedded descriptive tags (comments, lyrics, chapters, tracklist and recognized aliases) | Bounded supplied raw tags plus stable channel discriminator; apply the same narrow text grammar with source evidence. Structured chapter support needs an explicit separate grammar. | Recognition-only until supported syntax is completely parsed; no automatic conversion of every timestamp line into a track. Conflicting channels return ambiguous alternatives. |
| M3U/playlist paths or arbitrary binary/unknown extension | References may support existing association; they do not imply a set tracklist. | Unsupported for initial track parsing; no fake titles from path strings. |

Timestamp kind is offset only when source semantics/origin demonstrate recording-relative meaning. A valid CUE INDEX01 supplies a file-relative frame origin, not necessarily a whole-set origin. Plain `mm:ss` requires interpretation evidence; minute-granular, clock and unknown cues never become exact offsets. A first cue later than ten minutes alone does not prove clock time. Preserve all source precision and rational frame values; exact export eligibility belongs to core consumers.

## Written local scenarios and safeguards

| Synthetic input / event | Expected evidence and behavior |
|---|---|
| UTF-8 BOM CUE with CRLF; INDEX01 `00:03:10` | Strip BOM through evidenced decoding, normalize line endings only for parsing, retain encoding/original evidence; offset is 235/75 seconds relative to FILE, never rounded to three seconds. |
| CP437 TXT/NFO with decorative separators and two supported track lines | Preserve encoding decision and source offsets; decorative stripping for prompts is not proof of complete track enumeration. Parse only documented grammar, retaining rejected-line reasons. |
| Invalid UTF-8 CUE; decode replacement would damage syntax | Report undecodable/unsupported or incomplete; do not import replacement-character text as a certain complete parse. Existing proposal fallback is unchanged and not a parse guarantee. |
| File larger than byte budget, one adversarial oversized line, too many tracks/references | Finite byte/character/line/track/hypothesis limits and deadline; explicit incomplete with cap evidence. Truncated tails cannot become empty-source absence or complete revisions. |
| Stored features fingerprint differs from file revision | Discover reports stale evidence or refresh needed; load verifies fresh bytes through effects. Old recognized flag cannot authorize current import. |
| Source disappears after discovery, no owning agent, or permission denied | Distinguish absent native file from unavailable agent/access refusal with evidence; preserve approved tracklists and manual selection. |
| Symlink switched or CUE references a path outside roots | Agent effect refuses read/target resolution, no controller fallback. Explicit unavailable/unsupported evidence; do not follow the untrusted reference. |
| Multi-file CUE points to two parts but only one target is supplied | Snapshot keeps per-FILE origins and unresolved targets; matching remains review-required, no exact global cue export. |
| Embedded tag contains three timestamped prose lines | Existing recognition can stay visible; unsupported grammar is recognition-only incomplete, not three fabricated tracks. |
| Embedded and TXT both valid but disagree | Distinct stable sources/versions, conflict shown to core matching/review; no silent preference or overwrite of approved content. |
| Proposal context already contains truncated NFO text | Preserve existing clean_companion_content/build_file_context behavior and bounds. Provider parsing uses its own authorized complete-input effect; do not repurpose shortened prompt text as a full snapshot. |

The storage model currently permits one file_id per Tracklist while companions can target many recordings. Resolve this extension in orchestration through separate source-object and recording-association identity; do not misuse propagated_from_set_key for generic local sharing. Rights evidence is unknown absent source statements; owning a local file is not an asserted export license. Planned parser/permission/budget/revision/encoding scenarios require later tests; none was implemented or executed here.
