---
title: Tag/CUE bulk builders apply .limit() before the qualifying-change filter — 200K starvation risk
created: 2026-07-10
severity: major
found_by: Phase 85 code review (85-REVIEW.md WR-01..04) + Phase 85 verification (85-VERIFICATION.md)
owner: follow-up (accepted as tracked debt per operator decision, 2026-07-10)
blocks: "practical usability of the newly-live tag-write / CUE bulk tools at 200K applied scale"
resolves_phase: null
---

# WR-01: `.limit()` applied before the Python "has a qualifying change" filter

Phase 85 turned on the tag-write and CUE review workspaces against the live `applied()` predicate.
Both the code review and the phase verifier independently confirmed a real WARNING-severity scaling
defect in the three bulk builders. Accepted as tracked follow-up debt (operator decision 2026-07-10)
— the phase's two success criteria (predicate revival + mutation-verified behavior change) are fully
met independent of this; files remain individually writable so it is not data loss.

## WR-01 (primary)
`services/review.get_tagwrite_review_rows` and `routers/tags.bulk_write_no_discrepancies` apply the
SQL `.limit(_MAX_*)` to a filename-ordered candidate set **before** the Python "has ≥1 change" filter.
Zero-change applied files never qualify, never receive a COMPLETED log, and so are never evicted by
`completed_subq` — they re-occupy the same alphabetically-first `.limit()` slots on every submit. At
the 200K applied backlog this phase targets, a wall of >2000 non-qualifying files can permanently hide
/ starve qualifying files behind them, and re-submitting the bulk tool makes no forward progress.

Proper fix likely needs a SQL-expressible "has-change" predicate (push the comparison into the query)
so the `.limit()` bounds *qualifying* rows — not a naive filter-before-limit, which would reintroduce
the full-scan the limit was protecting against. That trade-off is why this is its own follow-up.

## WR-02
`tags._get_tag_stats` double-subtracts files that have BOTH a COMPLETED and a DISCREPANCY log — a
latent counting bug the `applied()` revival newly exposes (previously masked because the executed
count was always 0).

## WR-03
`review.get_cue_review_cards`'s eligible half relies on a Python loop-break over
`_get_eligible_tracklist_query`, which materializes ALL eligible pairs into memory first — only the
gated half has a real SQL `.limit`, so the D-03 memory bound is only partial.

## WR-04
Total review cards can reach 2×`_MAX_REVIEW_ROWS` (per-set caps; documented, but the constant reads
as a single budget).

See `.planning/phases/85-executed-gate-revival/85-REVIEW.md` for full findings and
`85-VERIFICATION.md` for the independent confirmation.
