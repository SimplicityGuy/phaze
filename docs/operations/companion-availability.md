# Companion availability and reconciliation

Companion inventory records history, rather than certifying that every stored path still exists.
Root scans upsert observed files; a completed scan does not remove absent inventory.

`phaze backfill companion-features` retains the total `companions` inventory count. Its `current`,
`missing` (features absent), `stale_content`, and `stale_extractor` counts describe only unmarked
inventory. `unavailable_missing` means an explicit reconciliation confirmed no copy under the
configured roots. `unavailable_ambiguous` means the original path was absent and several verified
copies or several competing claims prevented a unique destination. These two populations are
reported separately and are **not extracted coverage**. An unmarked row is not verified readable.

`phaze backfill companion-links` separately reports both unavailable counts and `awaiting features`
for unmarked inventory. Unavailable companions are not decided; their existing links and review
history stay intact. Link added/removed/kept counts describe the decided population, so preserved
links on unavailable rows are not included in `kept`. Junk detection leaves existing unavailable
review rows untouched and creates no new candidates from unavailable inventory. Unavailable copies
do not serve as linked duplicates or count toward a known-stamp decision for available copies.

A report alone does not change availability. Only the explicit stale-row reconciliation pipeline
sets these markers. Any scan/upsert, re-point, or reconciliation reporting the original path present
again clears both markers, making the companion eligible for extraction and association again.
There is no automatic deletion or reaper.

## Operator sequence

Production writes require separate authorization. Implementation approval is not merge, deployment,
reconciliation apply, feature enqueue, or association apply approval.

1. Verify the deployed schema includes revisions 083 and 084 and the consumers implementing these
   counters. The previously measured release 2026.10.3 did not contain those migrations; do not infer
   deployment from source or a PR state.
2. Produce a scoped `phaze backfill stale-row-candidates <agent>` document. Keep actual archive
   identities, hashes and paths in private scratch only; scope the document to the reviewed rows.
3. On the owning agent, pass that document to `python -m phaze.agent_watcher locate-stale`. Confirm
   **every configured scan root** is mounted and appears in `walked_roots`, with `walk_errors=0`.
   Companion reads resolve and containment-check the exact NFC/NFD twin opened with no-follow and
   nonblocking flags, require a regular file, and cap full-byte destination verification at 1,048,576
   bytes (64 KiB chunks, expected-size plus one guard). Over-cap or unreadable candidates produce
   walk errors, not a missing verdict. An incomplete search cannot establish companion absence or a unique destination. Present files
   never become ambiguous merely because their bytes have copies elsewhere.
4. Pass the resulting document to `phaze backfill reconcile-stale-rows <agent>` without `--apply`.
   Review its exact missing, ambiguous, repoint/merge, retained-reviewed, changed and unverifiable
   counts using placeholder identities. For companions, multiple same-byte copies remain ambiguous
   even when only one candidate keeps the original filename. The existing reconciliation's guarded
   history-preserving merge/repoint handles only a unique verified destination.
5. Obtain explicit authorization for the concrete reconcile apply. Re-run the dry run against fresh
   owning-agent evidence immediately before applying; a stale path, hash or size is refused. Inventory
   and history of missing/ambiguous source rows remain retained.
6. Run the real feature and link dry runs. Report total inventory, current features, pending readable
   extraction, both unavailable counts, links and retained history separately. Any feature enqueue or
   link apply remains another explicitly authorized step. Verify quarantine exclusion and repeat dry
   runs after authorized work finishes; unavailable counts must never be described as extracted files.

Synthetic filesystem/Postgres tests verify this implementation and its consumer boundaries. They do
not replace the separately authorized production reconciliation and post-repair verification on
phaze-st1ty.
