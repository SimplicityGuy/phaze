# Companion persistence and consumer hunt: third bounded round

Assessment bead `phaze-6mipc`, within `phaze-3l0q6`; inspected 2026-10-11 UTC.
This is the one companion round authorized at `phaze-7h65l`. One P2 attribution defect
survived independent source verification and a bounded synthetic persistence observation.
A separate local-directory candidate remains source-only, unrun and deferred. The frontend
pass found no concrete candidate, with browser execution unavailable. Stop further hunts
and complete the already approved quality plan; no fourth round or implementation is authorized.

## Provenance and complete slice

Hunt label: `bughunt-2026-10-11-phaze-6mipc`. Frozen source and initial leaf commit:
`c3ba36174eefa753999f020c7c78c855f40dada7`; production bytes match `42a0e5f4`.
The earlier coverage and runtime assessments measured `d1835b022f99397ed6be69359c57938a9ceaffd9`
and remain historical evidence. Main had incorporated **19 commits touching 69 files**,
including CUE, NFO, TXT and browser consumer repairs. Those repairs were included in this
round's source and historical dedup; old line/context measurements were not refreshed or
represented as measurements of these changes. No additional full baseline or model download ran.

All three independent lenses read the same **36 files, 5,068 physical lines** completely.
That is 108 file-lens reads and 15,204 physical line-lens reads, not 108 unique files.
Supporting callers and tests were read at relevant seams and do not enlarge the complete slice.

| Complete slice | Files |
| --- | --- |
| Services (13) | `provider_persistence.py`, `companion_capture.py`, `companion_capture_worker.py`, `companion_import_backfill.py`, `companion_content.py`, `companion_availability.py`, `source_workspace.py`, `selected_cue.py`, `selected_discogs.py`, `selected_tag_sources.py`, `selected_source_consumers.py`, `local_source_import.py`, `companion_features.py` under `src/phaze/services/` |
| Routers (3) | `agent_companion_capture.py`, `companion.py`, `cue.py` under `src/phaze/routers/` |
| Tasks (3) | `companion_import.py`, `companion_features.py`, `companion_capture.py` under `src/phaze/tasks/` |
| Migrations (4) | `085_provider_source_observations.py`, `086_local_source_decisions.py`, `087_companion_import_progress.py`, `088_discogs_provider_track_binding.py` under `alembic/versions/` |
| Record templates (10) | `_action.html`, `_history.html`, `_observation.html`, `_overview.html`, `_parsed.html`, `_refresh.html`, `_reverse.html`, `_review.html`, `_selected.html`, `_text.html` under `src/phaze/templates/record/companions/` |
| Pipeline templates (3) | `_selected_cue_preview.html`, `_recording_discogs.html`, `_local_source_sets.html` under `src/phaze/templates/pipeline/partials/` |

Lenses: data-integrity/persistence, security/review-authority, and frontend/consumer-contract.
Finders used the inherited session model; final adversarial source verifiers were fresh-context
`gpt-6-astra`, a different model. Verification was source-only; the completing lead separately
executed the benign attribution observation below. No finder was rerun during completion.

Task-owned artifacts live at `assessment-artifacts/phaze-6mipc` beneath the Beadhive worktrees
root, outside tracked source. They retain the frozen `slice.json`/`slice.txt`, `plan.md`,
finder/coverage JSONs, historical issue corpus, fingerprints, verdicts, append-only journal,
scratch reproduction, raw logs, JUnit, exact report body and filing records. Finder-time fields
describe source-only work; the final log and this report describe the later execution.
Only this report and its documentation index change; application, tests and CI remain untouched.

## Results and evidence limits

| Lens | Candidates | Source-verified novel | Reproduced | Filed | Disposition |
| --- | ---: | ---: | ---: | ---: | --- |
| Data integrity | 1 | 1 | 1 | 1 | P2 sanctioned untriaged intake `phaze-g7x85` |
| Security/review authority | 1 | 1 | 0 | 0 | Source-only local-directory candidate, unrun and deferred |
| Frontend/consumer contract | 0 | 0 | 0 | 0 | Static complete-slice review; no browser runtime proof |
| Round total | 2 | 2 | 1 | 1 | One runtime-supported finding filed after dispatcher review |

Source verification rate: 2/2, **100%**; reproduced share: 1/2, **50%**. Source-verified
P0/P1 share: 0/2, **0%**. These denominators do not turn the deferred candidate into a
runtime finding. No exact fingerprint drops or judged duplicates were retained as candidates.
Actual spend and cost per bug are **unavailable**; elapsed test seconds are not spend estimates.
The prior two rounds filed **5 defects: 4 P2 and 1 P3**. Cumulative
filed intake is now **6 defects: 5 P2 and 1 P3**; the deferred candidate is excluded.
Novelty among reproduced/filed candidates is 1/1, **100%**.

### Equal interpretations lose current raw-parent attribution — P2

Root: [provider persistence](../../src/phaze/services/provider_persistence.py) `_store` returns
an equal payload without comparing the requested `parent_id`.
[Local import](../../src/phaze/services/local_source_import.py) supplies the current raw ID,
but [review context](../../src/phaze/services/companion_view.py) and
[classification](../../src/phaze/services/companion_import_backfill.py) require that exact
raw parent. Semantic reuse is intentional; its interaction with exact-parent consumers loses
the current acquisition's interpretation relationship.

The synthetic input is **170,000 ASCII characters** of invented music metadata, below the
262,144-byte default read bound and below the line cap. Actual capture reads all bytes and
hashes the full unchanged revision, then retains 140,000 characters on the first acquisition
and 160,000 on the second. Both supported acquisitions are incomplete. Default import bounds
both retained texts to the same **131,072-character** prefix and records input-cap evidence.
Different raw payloads create distinct raw observations; equal normalized parsed payloads
reuse the first interpretation while its immutable parent remains the first raw.

Actual `source_review_context` returns **0 current interpretations** for the second raw and
`classify_source` returns `("parser_old", "needs_current_parser")` despite completed extraction.
Repeating import returns the same observation IDs retaining the stale parent; the consumer
helpers were not independently reread after retry. The
same-140,000-budget control reuses the raw, shows **2 interpretations**, classifies incomplete,
and remains idempotent on retry. This distinguishes intended replay reuse from missing
attribution across distinct captures; it does not claim a complete interpretation or unlimited import.

`test_attribution_repro.py`, `completion-repro.log`, `completion-repro.xml`: **2 passed,
10 warnings in 1.09 s**, Python 3.14.7. The header confirms the allocated completion-seat test
database on localhost:5433 and its **exclusive** lock; all three recipe exports, including
migrations and dedicated Redis, were copied exactly. The scratch assertions intentionally
confirm the incorrect behavior and its control; they are not a passing fix regression.
Actual `read_capture`, `store_capture_report`, `import_local_source`, PostgreSQL persistence
and the two current consumer helpers ran. No live worker delivery, browser, provider, model,
production archive or external service ran. The session schema uses ORM metadata; applying
migration 085 and exercising its database immutability trigger was not part of this observation.

Existing persistence/import tests cover exact retries, parent validity and multiple
interpretations of one parent. Proposed regression oracle: distinct valid captures with
equal normalized interpretations retain durable current-parent attribution visible in review
and classification, while same-parent replay remains idempotent. Preserve immutable history,
ownership, live links, revision checks, bounds, current-parent guards and review authority.
Either parent-scoped observations with correct conflict handling or a durable attribution edge
could satisfy this; changing historical parent IDs or relaxing the current-parent guard cannot.

Fingerprint: `data-integrity:src/phaze/services/provider_persistence.py:_store:raw-parent-attribution-loss`.
Independent verdict: `attribution-verdict.json` (high confidence; source-only).
Exact reviewed intake body: `attribution-report-body.md`; sanctioned untriaged intake
**`phaze-g7x85`**, filed after dispatcher review and verified by actual issue readback.
The refreshed open corpus has no additional near-duplicate candidate title; an unrelated ADR
attribution item was excluded. This intake is not implementation approval.

### Deferred and unavailable checks

The separate local-directory candidate has source-only verification and remains **unrun,
deferred and unfiled**. Completion did not investigate or reproduce it. No operational
experiment or runtime conclusion is attributed to it.

The frontend finder fully read all 36 files and relevant viewing, mutation-origin, selected-CUE
and parser tests. It checked actual consumer targets, native forms, parser generations,
pagination and guard-release paths statically. Playwright was absent; markup and source
inspection do not establish browser behavior. No concrete independent frontend candidate
survived. This empty static result and any stopped measurement are not zero-defect convergence.

## Historical dedup, errors and next step

The supplied historical corpus contains **3,951 issues**, the open-bug-title corpus **19**,
and the stored hunt fingerprint corpus **5**. Full relevant feature and recent repair bodies
were inspected alongside fingerprints and candidate mechanisms. The open `phaze-4ywkm`
companion-file dedup/delete policy remains unresolved; this proposal preserves existing
observation idempotency and does not decide or alter that policy. `phaze-5jo4c` remains the
broad artifact-seam neighbor. Idempotent import features `phaze-gq28d`/`phaze-rbqca` and
repairs `phaze-jwals`/`phaze-antgr` do not cover this equal-payload/different-parent case.
Three dependency bodies absent from the verifier corpus (`phaze-7ljit`, `phaze-8uk2k`,
`phaze-veuy5`) were subsequently read through `bh work issue`; all are closed human review
gates, resolving that corpus limit without identifying an overlapping repair.

All three finder passes completed; **0 finder errors** and **3 lead interruptions** are
distinct. Source-read recoveries include truncated broad output, guessed missing paths and
an unmatched glob; corrected bounded reads retained complete slice coverage. Completed fresh
verdicts remain separate from those tooling errors. During completion, sandbox restrictions
required ordinary local-service escalation. One external scratch import attempt failed before
collection; one fixture attempt failed both cases because its default report budget could not
accept 170,000 decoded characters. Correcting only the scratch import path and constructing
synthetic inventory before supported real captures produced the accepted run above. Failed
logs are retained; neither failed attempt counted as reproduction or a defect-free result.

Recommend stopping hunts now and finishing the authorized coverage, consumer-oracle and
selection-evidence proposals. Remediation filing and implementation kickoff remain separate
gates. Earlier **>95% line coverage** establishes executed lines at its historical commit,
not semantic assertions, current-context reachability or browser behavior after these repairs.
Measure progress by **accepted-bead velocity**, gate reuse, total agent/CI time and preserved
failure oracles, rather than reducing test seconds alone. No fourth hunt, silent fix or new
baseline is included in this assessment.
