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
