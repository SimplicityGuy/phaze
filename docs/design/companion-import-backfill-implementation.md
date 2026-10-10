# Durable companion capture and import

Implemented 2026-10-10 for phaze-rbqca. Capture, import and reviewed source selection remain separate operations.

## Invocation and deployment

Deploy migration 087, the controller and owning agents together. Older agents omitting physical-read attempt UUIDs remain compatible, with unknown acquisition history for those reports. The controller needs its Postgres broker; owning agents need registered scan roots and the authenticated report API. Run companion feature extraction first for older inventories: `phaze backfill companion-features --apply`. Association reruns record fresh derivation evidence.

`phaze backfill companion-import` counts all companion inventory pages without writes, queues or filesystem reads. `--agent <agent-id>` scopes an owner. `--apply` requests durable per-agent controller jobs; it prints each run UUID even when enqueue acknowledgement fails. `--run <run-id>` displays aggregate progress and at most fifty source summaries. Use `--after <item-id>` and `--page-size <1..50>` for subsequent status pages. `--apply --run <run-id>` resumes the retained run. No production backfill is executed during implementation validation.

Automatic association commits before requesting the same controller pipeline. Scan and watcher requests never wait for all companion reads. Explicit backfill first re-derives association asynchronously; a running scan or another association owner defers this work.

## Durability and finite work

Each run freezes a server-created-at cutoff. Its UUID keyset cursor traverses only sources created at or before that cutoff; newer inventory is covered by a subsequent run. The cutoff is a creation boundary, not a long-lived transaction snapshot. A source committed late behind an already passed cursor is also covered by a subsequent run. Each page's items and cursor commit atomically. A crash before commit leaves neither; a crash afterwards resumes durable items. Enumeration and processing pages are bounded to fifty. Status uses grouped counts and explicit narrow pages rather than full retained payloads.

Per-item lease UUIDs fence delayed checkpoint writes. Each step imports exactly one target in a short transaction, taking its source bucket before globally sorted source/target inventory and companion pairs. An invalid target stays unresolved while other targets continue. Recording selections, approved tags, rename proposals and media bytes are untouched. Sources without fresh targets retain raw capture and an unresolved outcome.

Capture intent commits before enqueue. A stable backfill item key reconciles ambiguous acknowledgement and repeated dispatch; explicit operator refresh remains a distinct unkeyed read. Authenticated attempt evidence completes pending items. Queue calls have a finite deadline and occur without an open database session. Missing callbacks time out visibly; a new run can retry a recovered owner. Features not yet available are reported separately and covered after feature reports trigger fresh association. Stored current text can be reinterpreted by current parsers without rereading files.

Continuation identity is stored on the run. It rotates once transactionally before enqueue. Retrying an earlier job re-enqueues that stored identity instead of creating another continuation chain.

Interpretations inherit their raw read's timestamp. Classification therefore checks complete interpretation presence independently for tracklists and release facts, scoped to the exact current raw parent and current parser version. A smaller-budget partial interpretation does not erase a retained complete one; UUID order never implies parse chronology. This accounting does not select an observation or modify reviewed authority.

Eligibility checks missing/ambiguous inventory, both original and current quarantine paths, and junk features matching current inventory revision. Dispatch checks these again; the worker refuses requested or resolved quarantine paths before opening. Changed owners cannot silently transfer work to another owner. Retained text remains readable after quarantine or deletion.

## Acquisition versus content

An immutable observation represents semantic content, not a physical read attempt. Identical failure reports can reuse an old observation after a successful read. Each new physical read therefore mints a fresh attempt UUID; HTTP retransmission preserves it. The authenticated callback serializes the UUID before the source bucket and allocates a received-order ordinal under that bucket. Attempts are append-only bounded evidence pointing at observations. Reuse compares the full bounded envelope and collision-safe observation identity.

An unchanged report retransmission reuses its attempt. If inventory changes cause different normalized freshness/content, retransmission fails closed with 409 and rolls back; the original attempt stays intact. A new read gets a new UUID. Receipt, dispatch intent and capture timeout age use the same database wall clock; leases and re-enqueue intervals use controller time. Latest received attempt, latest stored content and selected authority are separate projections. Sources without attempt rows have unknown acquisition history; observation retrieval time never invents it.

`get_latest_source_attempts(session, object_ids)` accepts at most fifty IDs and reads narrow summary columns only. It omits stored text, observation payload and attempt envelope. Detail composition can inject this reader without contacting agents.

## Bounded retained history

Identity lookup pages digest buckets one full UTF-8 key at a time. Observation reuse filters by nonunique normalized-content digest, parser, revision scope, revision and status, then compares complete payload equality one row at a time. Full-revision conflict/reinterpretation checks page only relevant IDs, parser/status and decoded text. Digest collisions never grant identity or equality. Existing observation UUIDs, parent/conflict provenance and reviewed choices survive this query change.

Migration 087 adds attempts/runs/items and an indexed current source-file binding. Populated acquisition or progress evidence makes downgrade lossy and therefore refused. Forward recovery preserves history.
