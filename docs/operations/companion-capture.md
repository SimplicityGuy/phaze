# Companion source capture

Date: 2026-10-09. Bead: phaze-p2qah. Epic: phaze-kf7jr.

Capture persists companion text independently of the shortened text used in proposal prompts.
It reads no archive files on the controller and performs no parsing, tracklist selection, tag
writes or approval. Imported tracklists and release facts are produced by subsequent workflows.

`services.companion_capture.enqueue_companion_capture` resolves one inventoried companion and
optional recording association in a short read session. After closing that session, it dispatches
`capture_companion_source` to the owning agent's metadata queue. The typed request binds the agent,
source file UUID, path, expected whole-file SHA-256 and byte size. Agent startup's token-derived
identity must match the payload before any read begins.

The agent invokes a dedicated subprocess. It resolves the NFC/NFD twin inside configured roots,
pins every directory and the regular source file with nofollow descriptors, and reads at most the
byte budget once. Before/after descriptor and path checks detect replacement or modification.
Timeout or cancellation kills and reaps the subprocess, including when filesystem I/O is blocked.
The worker never reads the remainder of an oversized file merely to compute a full digest.

Defaults are 262144 source bytes, 131072 decoded characters, 4096 lines, 1048576 serialized report
bytes and a 30-second subprocess deadline. Accepted maxima are 1048576 source bytes, 262144
characters, 16384 lines, 2097152 report bytes and 120 seconds. A single request captures one source.
BOM, UTF-8, UTF-16, CP437 and Windows-1252 decoding uses existing encoding detection and retains
original line endings and formatting. Invalid encodings and binary NUL content produce explicit
unsupported evidence because PostgreSQL text cannot retain NUL. Character/line/wire truncation is
visible; nothing is promoted to a parsed tracklist.

Only complete source bytes receive revision scope `full` and `revision:sha256:full` evidence.
Byte-capped prefixes receive `bounded` scope. Text can be truncated even when all source bytes were
read and their complete revision is known. Changed revisions produce retry evidence. Missing,
refused, unreadable and deadline outcomes remain separate stored observations.

`POST /api/internal/agent/companion-captures` validates the typed report, bearer ownership and
live inventory/association before calling `provider_persistence.store_source_read`. Source identity
is `local` / `companion:<file UUID>`. The report transaction follows provider-bucket/inventory/source-object
lock ordering; no network wait occurs inside it. Changed inventory is retained as
stale retry evidence rather than current success. Replays reuse identical observations and failures
leave earlier text intact. No agent-supplied report can change a reviewed source selection.

Stored decoded text remains available without the agent online. Its observed revision and current
inventory/link availability are separate facts: an old successful read does not certify that the
source still exists today. Subsequent parser and viewer workflows consume these immutable database
observations. The legacy `read_companion_files` proposal RPC remains unchanged.
