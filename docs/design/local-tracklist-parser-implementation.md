# Local tracklist syntax implementation

Date: 2026-10-09. Bead: phaze-2r87l. Epic: phaze-kf7jr.

The pure [LocalSourceInterpreter](../../src/phaze/tracklist_providers/local_parser.py)
implements `interpret(candidate, content: SourceRead, budget: LoadBudget) -> LoadOutcome`.
It receives supplied decoded text, revision and encoding evidence. It never opens paths,
resolves CUE targets, reads the database, selects content or changes tags. Callers explicitly
inject the interpreter into `local_registry`; the default recognition-only adapter is unchanged.
The interpretation version is `local-tracklist-v1`.

## Initial grammar

CUE supports quoted `FILE "name" WAVE|MP3|AIFF`, `TRACK number AUDIO`, quoted set/track
`PERFORMER` and `TITLE`, and `INDEX 01 minutes:seconds:frames`. Seconds are 0–59, frames
0–74. `INDEX 00` is retained as evidence and never substituted for `INDEX 01`. Missing
INDEX01 leaves an untimed row. Blank lines and `REM` comments/release text are harmless
uninterpreted evidence in the original source; directives such as FLAGS, ISRC, PREGAP,
POSTGAP or CDTEXTFILE change semantics outside this grammar and make the parse incomplete.
Malformed quoting, unsupported FILE/TRACK dialects, conflicting numbering or indexes,
orphan fields and nonmonotonic indexes are incomplete with line-specific reasons.

Positive unique increasing positions preserve declared gaps. Conflicting declarations use
stable increasing internal positions and remain incomplete; legitimate numbering resets
in a new FILE are rebased without guessing time concatenation. Declared track numbers
remain in row evidence even when duplicate or conflicting. Set PERFORMER is inherited only
when a track has no explicit PERFORMER; explicit ID/unknown values stay NULL. Set TITLE is
retained in source text and is not guessed to be an event. Missing values remain NULL.

Valid CUE INDEX01 semantics demonstrate offsets relative to the intrinsic referenced FILE.
Offsets preserve `(minutes * 60 * 75 + seconds * 75 + frames) / 75` exactly. Each FILE
directive has a source-identity-derived ordinal origin and its literal untrusted reference
is retained as evidence. A qualified intrinsic FILE offset does not prove target-recording
applicability. Multi-FILE offsets reset independently, with no concatenation, duration
guess or controller path lookup. Source parse completeness is separate from recording
association; import orchestration must validate FILE mappings before precise consumers
use them. Missing FILE origins and conflicting/nonmonotonic values remain unusable.

TXT/NFO and supplied descriptive embedded channels support `number. body` or `number) body`,
optionally with a leading `[mm:ss]`, `[hh:mm:ss]`, `[clock hh:mm:ss]` or `[number min]`.
A single spaced hyphen/en dash/em dash divides artist and title. Multiple separators leave
both fields NULL with ambiguity evidence. A title-only body requires explicit `Title:`
notation or an unknown placeholder; numbered prose/bare titles remain rejected raw evidence.
Explicit terminal `[label: value]`, `(remix: value)` and `(mashup)` are supported, with at
most three annotations. ID, unknown, unidentified and question-mark placeholders become NULL.
No fuzzy artist splitting, remix inference or lookup is performed.

Plain colon-shaped times retain original text and second precision, but their kind is
unknown with no offset or origin. Explicit clock notation remains clock; minute notation
remains minute precision and unusable for exact placement. Syntax or a first cue beyond
ten minutes never qualifies recording-relative timing. Malformed time fields stay visible
and make the source incomplete. Untimed enumerations can be complete.

Blank/decorative lines and the documented `artist`, `album`, `title`, `date`, `genre`,
`label`, `venue`, `event`, `year`, `quality`, `bitrate`, `encoder`, `length`, `tracklist`
and `tracks` headers are ignored for track enumeration and remain in source text. Other
prose or unrecognized track-like lines make the parse incomplete. M3U/PLS and unknown
formats are unsupported references, never fabricated tracks. Recognition-only, empty or
all-prose inputs are incomplete, not found-empty sources.

## Bounds and preserved evidence

The parser is synchronous bounded string work: it does not claim that an asyncio timeout
can interrupt CPU work. It respects negotiated character, line, byte-read and track limits.
The supplied parsing view also obeys the byte budget in UTF-8, conservatively bounding decoded
input even if a caller supplies zero read-byte accounting. Fields are at most 4096 characters
and evidence at most 64 entries of 4096 characters.
Annotation scans have a fixed count. Input truncation, read incompleteness or any cap
always produces an incomplete snapshot. Unterminated partial last lines remain raw evidence
and cannot create factual fields. Text is retained up to negotiated character/UTF-8 byte
limits; original BOM, line endings and encoding are retained, with BOM stripped only from
the parsing view. Excluded lines remain in that text and rejection evidence names their
line numbers. Each output row is serialized once against the total 4MiB DTO budget, with
reserve for cap evidence. Repeated inherited fields that would exceed it yield an explicitly
incomplete bounded track prefix and retain source text, rather than an oversized snapshot.

The [synthetic fixtures](../../tests/identify/services/test_local_tracklist_parser.py)
assert ordered rows, excluded-line reasons, nullable metadata, exact rational timing,
distinct multi-FILE origins, BOM/CRLF, malformed syntax, prose/playlist refusal and bounds.
Release metadata extraction and target-specific imports are separate epic beads.
