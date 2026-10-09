# ADR-0024: Retire external acquisition while preserving tracklists

Date: 2026-10-08. Status: accepted. Durable operator record: beads phaze-1soo9 and phaze-tqoty.

## Decision and authority

The operator volunteered the removal instruction; no new question was put. The original statements,
including the provider's response and the operator's full removal scope, are preserved verbatim in
phaze-1soo9 and phaze-tqoty. They contain source identifiers that the operator explicitly requires
absent from tracked files, so this document points to that durable record rather than altering a quote.
Source-neutral statement, verbatim: "also, remember we have the tracklists that live alongside some files."

The retired provider's integration, calls, references, captcha handling, render support and acquisition
scripts are removed. Ajax JSON search is **CANCELED**, not deferred. Captcha re-queue is **CANCELED**,
not scheduled after a release. The earlier timing authorization is superseded; neither path has a
revival condition or permission to run.

Stored tracklists, unresolved NULL artist/title rows, versions, manual data, local companions,
embedded tags, Discogs matching, CUE generation, tag proposals and segment joins remain supported.
The deleted artist/event/date scorer had only the retired acquisition caller; removing it does not
remove Discogs matching or local tracklist consumers. A future provider can define its own matching
requirements when its investigation is authorized.

On 2026-10-07, the operator placed MixesDB in backlog bead phaze-cwyuq for a deeper investigation
(phaze-rs4x6): "for mixesdb, let's move it to the backlog. i want to do a deeper investigation there".
There was no question; the operator volunteered that statement. Nothing is
implemented or requested from that source by this change. Existing evidence is retained with exact
quantities in `docs/spikes/phaze-2ov24-mixesdb-viability.md` and
`docs/spikes/phaze-q2v7w-mixesdb-archive-coverage.md`.

## Historical records and schema

`docs/design/0022-tracklist-acquisition-spike-verdicts.md` and
`docs/design/0023-tracklist-phase2-verdicts.md` retain useful historical measurements; their acquisition
GO/deferral decisions are superseded by this retirement. Source-only implementation plans and orphaned
parser/query fixtures are removed. Historical source identifiers and URLs are removed from surviving
records; those records are source-neutral synopses, not verbatim copies of operator statements.

Migration revision 082 stays after 081, under the filename
`alembic/versions/082_retire_external_tracklist_acquisition.py`. Both forward replay and rollback use
the manual default. Rollback recreates empty historical acquisition tables disarmed, solely to keep
the revision graph replayable; no acquisition runtime is restored. Existing stored source provenance,
versions and track rows are preserved. Changing the baseline default affects fresh schema replay,
not existing production values. No production database writes are part of this follow-up.

Tracked-tree content and filenames are audited independently. Git history is unchanged.
