# ADR-0025: Remote provider sources (channel, authorization rule, contract version)

Date: 2026-10-11. Bead: phaze-6dg0b. Epic: phaze-oprdu. Status: accepted. The contract stays at
version 1.0, extended in place, per the operator's answer recorded under "Contract version".

This record extends the provider contract in `docs/design/tracklist-provider-contract.md` so a provider
can read a remote source. It changes no code. The implementation is bead "Implement the remote-source
extension in the provider domain and registry" in the same epic, which implements exactly what this
record decides and stops to ask where it leaves a gap.

## Why the contract cannot express a remote read today

Read from the source at `49971332`, not assumed:

| Source | What it does today | Consequence |
|---|---|---|
| `SourceReference.channel` in `src/phaze/tracklist_providers/domain.py` | `Literal["companion", "embedded"]` | No value names a remote source, so no reference to one can be built. |
| `ProviderRegistry.discover` in `src/phaze/tracklist_providers/registry.py` | Authorizes exactly the `RecordingContext.sources` whose provider ID matches. | A read is authorized only if the caller already put the reference in the context. |
| `ProviderRegistry.load` | Authorizes exactly `candidate.source`, or nothing. | Same: the candidate carries its own authorization. |
| `_BoundedEffects.read` | Equality membership in that allowed tuple; a miss sets a sticky `unauthorized_read`. | The mechanism is sound; only the source of the allowed set must change for remote reads. |
| `Capability` | No member for remote transport, although the contract document lists "remote transport" as a capability. | A descriptor cannot declare it. |
| `SUPPORTED_VERSION = (1, 0)` | Registry construction refuses any descriptor whose `contract_version` is not exactly `(1, 0)`; cursors in and out must carry exactly `(1, 0)`. | See "Contract version" below. |
| `provider_source_objects.channel` (`src/phaze/models/provider_source.py`, revision 085) | `CHECK (channel IS NULL OR channel IN ('companion', 'embedded'))`, `String(16)`. | A remote source object cannot be stored until that check is widened. |

Both authorization rules derive permission from data the caller hands in. That is safe for local sources
because a local reference names an agent-owned file UUID that core resolves by containment. It is not safe
for a remote source, which is why this record replaces it for the remote channel rather than reusing it.

## Decision

### 1. The channel and what a remote reference carries

`SourceReference.channel` gains the value `"remote"`. A remote reference carries:

| Field | Content |
|---|---|
| `identity` | `(provider_id, native_id)`. For a fetch, the candidate's provider-scoped native identity. For a search, the provider ID and `search:<context_key>`, where `context_key` is the registry's existing digest of the recording context. A search identity is never stored as a source object. |
| `request` (new, required when `channel == "remote"`, forbidden otherwise) | `kind`: `"search"` or `"fetch"`. `terms`: for a search only, at most 4 `(field, value)` pairs, `field` one of `artist`, `event`, `date`, `value` at most 256 characters; empty for a fetch. `page`: a positive integer, at most 64; always 1 for a fetch. |
| `format` | The response format the host profile declares for that route. |
| `revision` | Always `None` on a remote reference. A revision is a property of a response, so it lives on `SourceRead`. |

A remote reference has **no field that can hold a URL, host, path, method, header or credential**.
`DomainModel` already forbids extra fields, so one cannot be smuggled in. Query values are opaque text
that the transport encodes as parameter values; they never become a request target. Only
`RecordingContext` facts (artist, event, date) can become terms, which is operator record 3 below: no
local audio or file content is ever sent to a provider.

`Capability` gains `REMOTE_TRANSPORT = "remote_transport"`.

### 2. Who holds the host profile

The host profile (allowed `https` host, a route template per request kind, the native-ID syntax a fetch
accepts, which query fields a search accepts, pacing, budget, User-Agent, credential slot, stop
conditions) is a **core** object, registered with the registry at the composition root and keyed by
provider ID. Its full contents belong to the bead "Generic remote transport effect".

The descriptor declares only the `REMOTE_TRANSPORT` capability. It names no host. A provider-authored
host list would let an adapter authorize its own destinations, which is the thing this record exists to
prevent. Registry construction fails with a contract error when a descriptor declares `REMOTE_TRANSPORT`
without a core profile, when a profile is registered for a provider that does not declare it, and when a
profile names an unknown provider.

This is the implementer's reading of the bead's phrase "the descriptor's declared allowed host profile":
the profile is declared *for* the descriptor, by core, not *in* it.

### 3. Who mints authorized remote references

Only the registry mints, at the start of `discover` or `load`, before the provider runs:

- **discover**, provider declares `REMOTE_TRANSPORT` and its profile supports search: one search
  reference, page 1, whose terms are the context's artist, event and date facts with certainty `known`
  or `inferred` and a non-empty value, restricted to the fields the profile accepts. With no usable term,
  nothing is minted and the provider has nothing remote to read.
- **load**, provider declares `REMOTE_TRANSPORT` and its profile supports fetch: one fetch reference for
  `candidate.identity`, but only if `native_id` fully matches the profile's native-ID syntax. Otherwise
  `load` returns `contract_error` with code `unauthorized_identity` before any effect runs.

The guarded effects view that the registry hands the provider exposes the minted references for that
invocation, read-only. The injected core effect keeps its `read(source, budget)` signature. A provider
reads the next search page by copying a minted reference with a different `page`.

#### Amendment (2026-10-12): a fact longer than a term is skipped

Rule: a fact that is otherwise usable but whose value is longer than the 256-character term bound in
section 1 is **skipped**, so it is not used as a search term. It is never truncated, and it is never a
contract error. If every usable fact is skipped, nothing is minted, as above. The guarded effects view (`skipped_terms`)
names each skipped fact by its **field name only** (for example `artist`), never by its value, so a
provider can say in its discovery evidence that a term was not used and keep its completeness claim
honest.

Reason: a truncated value would be a query that core never saw as a fact. A contract error would let one
pathological artist string make discovery unavailable for that recording.

This is an implementer's decision, made by the dispatcher of epic phaze-oprdu on 2026-10-12 without
putting it to the operator, for bead phaze-0wn41, where this record left the case open.

### 4. How the registry checks them

In `_BoundedEffects.read`, a reference with `channel == "remote"` is authorized if and only if, with
`page` reset to 1, it equals a reference minted for this invocation, and its `page` is within the
profile's page bound. Everything else about reads is unchanged: each remote read counts against
`max_reads`, cumulative byte, character and line bounds still apply, and a miss sets the existing sticky
`unauthorized_read` violation, which survives a caught exception.

Remote references from the caller are refused, never silently dropped:

- A `RecordingContext.sources` entry with `channel == "remote"`: `discover` returns `contract_error`
  `caller_remote_reference` before any effect.
- A `candidate.source` with `channel == "remote"`: `load` returns the same, before any effect.
- A discovery candidate whose `source` is a remote reference: the existing "candidate reference was not
  explicitly authorized" contract error, because minted references never enter the candidate-source
  allowed set.

Local authorization (context sources and `candidate.source` with channel `companion` or `embedded`) is
unchanged.

The transport effect re-validates the reference's shape as defence in depth and builds the request only
from the profile's host and route template, the percent-encoded native ID as one path segment, and the
encoded terms and page. It does not follow a redirect off the profile host. It cannot verify that a
reference was minted; that is the registry's check.

### 5. What `SourceRead` gains

One optional field, `validators`, default `None`, holding:

| Field | Content |
|---|---|
| `etag` | The response `ETag` header exactly as sent, including any `W/` prefix, at most 512 characters, or `None`. |
| `last_modified` | The response `Last-Modified` header exactly as sent, at most 128 characters, or `None`. |

Status (`status`, `code`), truncation (`truncated`, `bytes_read`), revision and `retrieved_at` already
exist and are reused. The transport effect sets `code` to name the HTTP outcome; the typed status mapping
belongs to the transport bead.

Validators are **recorded evidence only** under this record. A weak `ETag` is never a full revision. A
`Last-Modified` value alone is never a revision. A strong `ETag` may become `revision` with
`revision_scope="full"` only where the host profile declares that route's `ETag` a content validator.
The transport sends no conditional requests: conditional requests are unverified for MixesDB (epic
phaze-oprdu), and enabling them is a later decision.

**Serialization invariant.** An unset `validators` is omitted from every serialization of a
`SourceRead`. Two consumers make this load-bearing:

- `store_source_read` in `src/phaze/services/provider_persistence.py` persists
  `read.model_dump(mode="json", exclude={"retrieved_at"})` and reuses an observation only on an equal
  payload digest and equal payload. A new `"validators": null` key in every local read's payload would
  stop reuse, so each unchanged re-capture would append a duplicate observation.
- `CaptureReport.read` in `src/phaze/schemas/agent_companion_capture.py` carries a `SourceRead` from the
  agent's capture worker to the controller, and `SourceRead` forbids extra fields. An agent emitting
  `"validators": null` to a controller one deploy behind would be rejected.

This requirement holds even though the contract itself is unshipped, because the stored observations and
the `CaptureReport` wire payload already exist. Which beads own it is an implementer's decision, made
by the dispatcher of epic phaze-oprdu on 2026-10-11 without putting it to the operator: the contract bead implements the
omission and its blast-radius tests (see "Blast radius").

### 6. Storage

The contract bead changes no schema. The orchestration bead ("Operator-triggered lookup: discover,
candidates, load, import, select, for any registered provider"), which is the first to store a remote
source object, widens the `provider_source_channel` check to include `'remote'` in its own migration,
allocated at the implementation head. The transport-effect bead owns only its own ledger migration. This
assignment is an implementer's decision, made by the dispatcher of epic phaze-oprdu on 2026-10-11
without putting it to the operator. A remote source object has `source_file_id` NULL. Until that migration lands, an attempt to
store one fails at the check constraint, loudly.

## Why a caller-supplied URL can never authorize a read

Authorization here means "core decided this read may happen". The registry's mint step is the only place
that decision is made, and it is made from a core-held host profile, never from data a caller handed in.
A URL is the wrong thing to authorize by, for five reasons:

1. **It is a destination, not a decision.** A URL carries scheme, user-info, host, port, path, query and
   fragment. Checking one against an allowlist means parsing attacker-shaped input: IP literals,
   alternative IPv4 and IPv6 spellings, internal names, DNS that resolves differently at check and at
   connect, and redirects. A minted reference has no destination to parse; the transport builds the
   destination from the profile.
2. **Every URL in this system is untrusted content.** `ProviderCandidate.url` and `Snapshot.url` are
   provider-interpreted remote content; a remote page can put any URL in them. The contract document
   already says `url` is "not an identity requirement or automatic permission to fetch", and a cursor is
   "never an arbitrary executable URL".
3. **Credentials are attached per host profile.** A read steered by a URL could send a provider's secret
   to another host.
4. **Pacing and budget are keyed by profile.** A URL-driven read escapes both, which breaks operator
   record 1.
5. **Caller-supplied structure is the same hazard, whatever its type.** That is why section 4 refuses a
   remote reference placed in the context or on a candidate, rather than trusting it. The registry trusts
   only what it minted.

A URL can still reach a provider as a *query value*. For example, TrackId's `url=` filter takes a
canonical URL as a parameter. That would need a new term field whose value comes from core-held recording
data and is sent as an encoded parameter on an allowlisted route, never dereferenced. This record does not
add that field; it is a later decision if the measurement in operator record 6 justifies it.

## Contract version

The contract stays at version `(1, 0)`. There is exactly one contract shape, the remote-capable one
this record describes, and it replaces the earlier 1.0 shape in place. `SUPPORTED_VERSION`, the
descriptor and cursor defaults, and the registry's exact-equality checks are unchanged. The local
provider keeps declaring `(1, 0)`, and remote providers declare `(1, 0)` too.

Operator record (Rule 2 of `docs/design/0012-verification-fidelity-and-operator-attribution.md`):

- **Question as put** (by the dispatcher, through `AskUserQuestion`, with the options "Minor bump to 1.1
  (Recommended)" and "Stay 1.0, additive"): "How should the provider contract version change to allow
  remote sources?"
- **Answer as given**, typed in place of an option, so it is not an option label: "Stay 1.0. We haven’t
  shipped the provider contract yet. No need to support both contract types, just the new one."
- **Date:** 2026-10-11.
- **Durable record:** this section, and the design section of epic phaze-oprdu.

The answer covers versioning only. Nothing else in this record rests on it.

No data migration follows from it, because no contract version is stored: no column holds one, the
stored observation payloads are `SourceRead` and `Snapshot` dumps with no version field, and cursors are
not stored. Staying at 1.0 does not relax the serialization invariant in section 5, which protects data
and a wire payload that exist today.

## Operator records

Rule 2 of `docs/design/0012-verification-fidelity-and-operator-attribution.md` requires the question as
put, the answer as given, the date and a durable record. All seven were given on 2026-10-11 (local time;
the session transcript stamps them 2026-10-12 UTC) in the planning session for phaze-5lovg. Each is
restated from the design section of epic phaze-oprdu, the durable record until this file. Each answer
below, and the questions of records 1 to 3, were checked verbatim against that session's transcript with
`scripts/recover_operator_decisions.py` on 2026-10-11. The questions of records 4 to 6 were asked inside
longer assistant messages and are quoted as the epic records them. Records 2 to 7 were asked in prose, not through `AskUserQuestion`, so each answer is the
operator's typed text, not an option label.

1. **Pacing.** Question, through `AskUserQuestion`: "Terms of service and caching rights can't be
   determined from the API. How should that gate the plan?" Answer, typed in place of an option: "no rate
   limit currently, but please be responsible, let's not innundate their servers". It extends only to
   request pacing. It is not an answer on storing snapshots.
2. **Contract extension.** Question: "Should I file a new bead, ahead of transport and provider, to add a
   remote channel and an authorization rule? It would need an ADR, and you would decide whether it stays
   version 1.0 or becomes 1.1. How do you want to handle it?" Answer: "yes". This record is that ADR. The
   version choice was put to the operator separately and is recorded under "Contract version".
3. **Matching context.** Question: "What should link a local recording to a TrackId stream: a stored
   source URL, artist/event/date keywords, or something else? If you don't know, I'll read
   docs/design/tracklist-provider-matching.md." (The epic records the question without its last
   sentence; the transcript has it.) Answer: "i don't know. for sure we're not
   uploading the local recording up to them." Binding constraint: no local audio or file content is ever
   sent to a provider. Only `RecordingContext` facts (artist, event, date, duration) may be used in
   queries. Section 1 limits search terms to artist, event and date.
4. **MixesDB.** Question: "No operator GO for a MixesDB adapter is recorded; the recommendation says
   'conditional GO, pending'. I'll build the foundation so it fits and file no MixesDB adapter beads until
   you record a GO." Answer: "MixesDB is a GO". Scope: adapter planning and implementation for the
   content-only provider in `docs/spikes/phaze-x6zxl-mixesdb-recommendation.md`. It does not widen the
   recorded access scope (REST and the bare Action help endpoint only, robots restrictions respected), and
   it approves no live-request budget.
5. **Matcher scope.** Question: "Minimal evidence-only, always review-required, or the full matcher?"
   Answer, typed after the prefix "Matcher scope:": "Minimal evidence-only, always review-required (my
   recommendation)". The full matcher stays out of scope.
6. **Measurement.** Question: "May I run a read-only count query against the archive database to see how
   many recordings carry a platform URL? I'd report counts only." Answer, typed after the prefix
   "Measurement query:": "yes". Counts only; no identifiers.
7. **Naming.** No question was put; the operator gave an instruction. As typed: "yes, re-file as v2. but
   don't bake "v2" into API, or provider". No version suffix appears in any provider ID, module, class,
   setting, route or table name. A contract version number is not a name, and the contract stays at
   1.0 in any case.

## Blast radius

This changes the path for every `SourceRead` producer and consumer: the agent capture worker
(`src/phaze/services/companion_capture_worker.py`), the capture task and service, `local_source_import`,
and `provider_persistence`, plus the local provider through the registry. What could break: duplicate
stored observations for unchanged local re-captures, rejected agent capture reports during a mixed
deploy, and local discovery or load refused by the new authorization branch. The tests that must prove
otherwise, named for the contract bead:

- `test_local_source_read_payload_is_unchanged_without_validators`: a local `SourceRead` dumps
  byte-identically before and after the change.
- `test_unchanged_local_recapture_reuses_its_observation`: `store_source_read` on an unchanged capture
  returns the existing observation.
- `test_local_provider_behaviour_is_unchanged`: the existing local-provider test modules pass unmodified,
  named in the submit report.

The number of stored observations in the deployed database was not measured for this record. The
byte-identity test is what protects them, whatever the count.

## Lesson

The general form of the serialization invariant is: **an additive optional field is additive only if its
unset value is invisible to every consumer that compares or validates the serialized form.** The
persisted digest and the agent wire are this repo's instances. Pydantic's default dump writes unset
optional fields as `null`, so "optional with a default" is not enough. It is written here because this
is where the next field added to a provider DTO will be designed.
