# Quality implementation outcome (A0)

Task **phaze-8d5bs (A0)** reviews the adoption criteria of implementation epic **phaze-75qpj** and
publishes its **measured** outcome. The approved design is the
[implementation plan](../quality-assessment/implementation-plan.md) (assessment PR #716). Inputs are
the [pilot disposition manifest](pilot-disposition-manifest.md) (PD, the pilot ledger) and the
[final union verification](final-union-verification.md) (G0's developer evidence and the
assembled-gate checklist). Main at the time of writing is `0f7a81afc92807bbcf2138ca04c85b8a30d4de05`.

**Bottom line.** The planned quality and coverage work has landed. The planned *runtime* and
*velocity* improvements are mostly not proven:

- All 6 canonical repairs and 15 of the 18 quality tasks have landed on main. The remaining three
  (PD, G0, A0) are this batch.
- One runtime change was adopted: **R4**, measured on one matched pair of CI runs.
- No selection authority was adopted. Full escalation remains the recovery path for every
  unsupported case.
- **Accepted-bead velocity, stable p95 and paired fast-gate gains are NOT proven.**

The final assembled gate verdict is **not recorded here** (see
[Final gate result](#final-gate-result)).

## Recorded decisions this outcome rests on

Each citation gives the question as put, the answer as given, the date and the durable record.

| Decision | Question as put | Answer as given | Date | Durable record |
| --- | --- | --- | --- | --- |
| Coverage criterion | "For the plan's acceptance criteria, should "over 95%" apply to overall line coverage only, or to both line and branch coverage? I recommend both overall, with explicit targets for the highest-risk modules." | "Line coverage only" (selected option label; the other option was "Both line and branch coverage") | 2026-10-10T23:07:13Z | recovered-decision comment on `phaze-8d5bs` and its follow-up |
| Runtime targets | "Which runtime matters most for optimization? I recommend measuring all three, while prioritizing the full local validation gate." | "all 3" (free text, verbatim; it matches none of the offered options "Full local validation gate", "CI completion time", "Fast per-bead feedback") | 2026-10-10T23:07:27Z | recovered-decision comment on `phaze-8d5bs` and its follow-up |
| Observation cohort | "Before the velocity pilots run, which observation cohort should they use? Repairs and coverage backfills can proceed without this choice." | "Next 20 completed beads, grouped by change type" (selected option label) | 2026-10-11T19:03:57Z | recovered-decision comment on `phaze-x45ht` and its follow-up |
| R4 adoption | Quoted in full in the [PD manifest](pilot-disposition-manifest.md#r4-adopted-on-one-matched-ci-run) | "Adopt, gate on matched CI" (selected option label) | 2026-10-12 local (2026-10-13T01:30Z) | operator-decision comment on `phaze-l5uve` |

**Citation limits:**

- **Recovery path.** The first three were recovered by grep from local session transcripts.
  `scripts/recover_operator_decisions.py` does not read that transcript format. Each reply records
  only the answer text, but the matching question calls list the offered options. Comparing them
  confirms two exact option labels and one free-text answer ("all 3"). The durable comments, and
  follow-up comments recording the offered options, were written during this batch, not when the
  decisions were made.
- **Design GO (2026-10-11).** The plan records it at its decision section: answer **GO**, selected
  option "GO design + bounded follow-on planning", assessment bead `phaze-9dxnh`. **The question as
  put is not recorded there and was not recovered**, so this citation is incomplete.
- **Filing and kickoff authorization.** The epic description records this ("User explicitly
  authorized implementation filing and kickoff on 2026-10-11 after merging #716"). It was an
  instruction, not a recorded question/answer pair.
- **Open citation repair.** The four existing disposition documents that cite the cohort decision
  without a resolvable record are tracked by `phaze-p65lt` (open, depends on this bead).

## Requirement audit

### Canonical repairs (6)

Each repair's own A-row control was re-executed on final source in G0: one semantic RED at the
intended oracle, then exact restore and a cold GREEN. Receipts are in the
[final union verification](final-union-verification.md#a1-a6-receipts).

| Bead | Repair | Landed on main | A-row |
| --- | --- | --- | --- |
| `phaze-g7x85` | parsed-observation reuse across raw captures | PR #717, merge `652c76f0`, 2026-10-11 | A6 |
| `phaze-zjwp4` | tag timeout leaves a live mutation thread | PR #718, merge `a1aeddb1`, 2026-10-11 | A1 |
| `phaze-ct6l1` | partial tag save excluded from Undo | PR #719, merge `f0ab9ae6`, 2026-10-11 | A2 |
| `phaze-33c4p` | boot-only orphan reaping never revisits | PR #720, merge `63b27206`, 2026-10-11 | A4 |
| `phaze-83205` | durable tag enqueue after a revision change | PR #721, merge `f3416afe`, 2026-10-12 | A3 |
| `phaze-jpm1y` | capacity waiters expire without heartbeat | PR #722, merge `3d5ebbad`, 2026-10-12 | A5 |

All six are closed `merged`. None was replanned.

### Quality tasks (18)

| Task | Bead | Outcome | Landed on main |
| --- | --- | --- | --- |
| M0: joined acceptance and selector records | `phaze-k04p7` | Delivered readback. Velocity remains unknown: 0 original-start joins | PR #724 (merge `49971332`) |
| SB: coverage-generation ownership | `phaze-a393q` | EXCLUDE generation publication; safe full-escalation fallback retained | PR #724 |
| SF: branch diagnostics | `phaze-ihvi5` | Delivered diagnostics; branch policy unchanged (policy deferred) | PR #724 |
| C1: duplicate HTTP refusal and rollback | `phaze-kl9i4` | Delivered controls | PR #724 |
| C2: capture eligibility and SQL recovery | `phaze-n78df` | Delivered controls | PR #724 |
| C3: configuration failure and cancellation cleanup | `phaze-ola2z` | Delivered controls | PR #724 |
| C4: pipeline/lane unknown-state rendering | `phaze-63cuy` | Delivered controls | PR #724 |
| C5: path fallback and root termination | `phaze-b6csj` | ADOPT (two test controls, no speed claim) | PR #724 |
| C6: selected CUE/Discogs HTTP refusals | `phaze-q8j9r` | Delivered HTTP/U+P scope; browser portion C6_B deferred | PR #729 (merge `c2e7737e`) |
| SC: selected/full shadow equivalence | `phaze-tmf6u` | EXCLUDE | PR #729 |
| SD: mixed prose/code authority | `phaze-al4ct` | EXCLUDE | PR #729 |
| SE: files-table / revision-085 mappings | `phaze-mbtzg` | EXCLUDE (non-browser); SE_B deferred | PR #729 |
| R1: immutable parse reuse | `phaze-2jvjd` | EXCLUDE | PR #729 |
| R3: shared telemetry capture | `phaze-axfxb` | EXCLUDE | PR #729 |
| R4: seven-shard rebalance | `phaze-l5uve` | ADOPT on one matched CI run | PR #730 (merge `0f7a81af`) |
| PD: optional disposition manifest | `phaze-x45ht` | [manifest](pilot-disposition-manifest.md) | this batch (not yet on main) |
| G0: final assembled union | `phaze-47gba` | Developer evidence delivered; **root's assembled gate pending** | this batch (not yet on main) |
| A0: this outcome review | `phaze-8d5bs` | this document | this batch (not yet on main) |

Per-pilot reasoning is in the PD manifest and the reports it links. It is not restated here.

### Acceptance criteria

| Criterion (approved plan) | Status | Evidence / gap |
| --- | --- | --- |
| Real fault detection for all six canonical repairs | **Met by targeted receipts** | Six A-row RED/restore/GREEN receipts in G0 |
| Overall LINE coverage strictly above 95% | **Met in every run cited below; final assembled run pending** | See [Coverage by run](#coverage-by-run) |
| Existing true branch checks unchanged | **No floor changed by this epic; final run pending** | One combined arc lost at the R4 head (`phaze-0r4p0`), see Limitations |
| Lower matched fast/full/CI complete-gate medians, no tail/failure/bounce/starvation regression | **NOT proven** | Only CI shard balance was measured, on one matched pair; no fast or full medians were compared |
| Seven-shard max/mean at most 1.5, per the R4 option's conditions "<=1.5 max/mean incl. setup/browser/combine, lower critical wall, no missing contexts/skips" | **Met on one run** | Test step 1.130; whole shard jobs incl. setup 1.112; critical wall 712 s to 469 s; skips 15 to 15; contexts 20245 to 20252 with one teardown context re-attributed and no arc lost (see the PD manifest) |
| Accepted-bead velocity only with authoritative joins | **NOT proven; no claim** | 0 of 20 cohort beads have an original work start |
| Adopt only proven supported selection authority | **Nothing adopted** | SC/SD/SE excluded; full escalation retained |
| Deferred branches recorded | **Met** | PD manifest deferred table |
| Original nodes, skips, exclusions and floors preserved | **Met in the cited runs; final run pending** | R4 pair: no baseline node missing, skips 15 to 15. G0: +6 nodes, none removed |

## Measured outcomes

**A count is a property of the run that produced it.** The figures below come from different trees,
recipes and populations. None is a delta of another. Unmatched populations prove no gain.

### Coverage by run

| Run (tree / head) | Recipe and population | Lines covered/valid | Line % | Source |
| --- | --- | ---: | ---: | --- |
| PR #724 final `just check-all`, head `d871bb62` | local two-lane full, 11,279 passed + 7 skipped, ordered disjoint 6,111 + 5,175 union (11,286) | 28,031 / 28,324 | 98.96554 | PR #724 body |
| PR #726 normal finish `just check-all`, head `2a4cf9af` | local two-lane full, 11,279 passed + 7 skipped, 11,286-case union | 28,031 / 28,324 | 98.96554 | PR #726 body |
| PR #726 CI run 38276598801, same head `2a4cf9af` | seven CI shards, 11,267 passed + 15 skipped (11,282 cases) | 28,029 / 28,324 | 98.95848 | PR #726 body |
| R3 normal provider submit, source `7e4b322e` | local two-lane full, 11,297 passed + 7 skipped (11,304-case union) | 28,045 / 28,324 | 99.01497 | retained R3 `coverage.json` (re-read for this document) |
| PR #729 normal group submit, head `a93154e5` | local full, 11,317 unique identities | not stated | 99.02 | PR #729 body |
| PR #730 normal submit, head `2e410625` | local full, 11,324 unique identities (11,317 + 7 new) | not stated | 99.02 | PR #730 body |
| R4 baseline CI run 38317442319, head `a35be942` | seven CI shards, 11,313 executed, 15 skips | 28,051 / 28,332 | 99.00819 | combined `coverage.xml` artifact (re-read) |
| R4 CI run 38335835547, head `413e1e1b` | seven CI shards, 11,320 executed, 15 skips | 28,051 / 28,332 | 99.00819 | combined `coverage.xml` artifact (re-read) |

The two R4 rows report line rate as `0.9901` in the XML; the percentages above are computed from
the covered/valid counts. Branch coverage at the R4 pair is 6,505 / 6,768 at the baseline and
6,504 / 6,768 at the R4 head.

**Same-source local/CI line delta.** On one head, `2a4cf9af`, local covered 28,031 lines and CI
covered 28,029. That 2-line difference comes from two different execution populations. It is
**not** a source improvement or regression.

**Local/CI node population delta.** On the same head, local ran 11,286 cases and CI ran 11,282.
The 4-case difference comes from platform-dependent parameter generation: an errno set whose
members alias differently on Darwin and Linux. PR #726 states this explanation; the full analysis
is in private evidence that was not re-derived for this document. Native node ids are kept as they
are; this difference must **never** be normalized away.

### Runtime

| Comparison | Status |
| --- | --- |
| **CI**, matched pair: R4 run 38335835547 vs baseline run 38317442319 | Shard step max/mean **2.326 to 1.130**; whole shard jobs incl. setup **2.080 to 1.112**. Longest test-workflow part **712 s to 469 s**, now the browser job. **One run, not p95.** Re-derived in the PD manifest |
| **Full local gate** | **No matched comparison.** Observations only: PR #724's full-test recipe took 1,052.64 s after a 1,440.339 s host queue (PR #724 body). Runs on other trees under other host load are not comparable |
| **Fast gate** (`just check-fast`) | **No paired measurement.** The approved three-alternating-pair warm protocol was not run, because no selection or runtime change that it governs was adopted |
| Stable p95, tail, bounce and starvation | **Not measured** |

### Velocity

The operator-selected cohort is the next 20 completed beads after 2026-10-11T19:04:16Z, grouped by
change type. All 20 are accepted-review joins. **0** have an authoritative original work start, so
there are **0** velocity joins. **No accepted-bead velocity, paired-gain or stable-p95 claim is
made.**

## Final gate result

**Recorded in the PR/bead at landing.** Root runs the legitimate assembled `just check-all` on this
batch's final HEAD and records its own provider verdict, pytest summaries and coverage line in the
landing PR and a bead comment. This document deliberately contains no number for that run. The
[final union verification](final-union-verification.md#what-roots-assembled-just-check-all-must-show)
lists what it must show. Until then, G0, and therefore this outcome, is not complete.

## What is NOT proven

- Matched full-gate or fast-gate improvement, stable p95, tail, bounce or starvation non-regression.
- Accepted-bead velocity, of any kind.
- That R4's CI balance holds beyond the one measured pair.
- The final assembled population, coverage and branch result (pending root's gate).
- Coverage equivalence of the excluded pilots. They were not run as experiments; exclusion keeps
  current authority.
- **Unexplained first full-run failure in PR #724.** That PR's first grouped full run failed one
  ledger-reaper assertion on source unchanged from main. A retry passed. The original cause remains
  unproven (PR #724 body).
- **SD's validation lost evidence.** It lost its final combined coverage (binary, JSON and XML) and
  lane-B JUnit to an observer full-disk error (ENOSPC). They are not reconstructed. The durable
  record is the bead comments on `phaze-t4pmg` and `phaze-tpv01` ("observer ENOSPC"); PR #729 says
  only that the data was lost.

## Open follow-ups

| Bead | Priority | Subject |
| --- | --- | --- |
| `phaze-0r4p0` | P2 | Deliberate test for the `backfill.py:114 -> 121` arc that only cross-test state covered before R4 |
| `phaze-l18wo` | P2 | Make the set-similarity synthetic scale test independent of host load |
| `phaze-p65lt` | P2 | Repair the cohort citations in four quality disposition docs (depends on `phaze-8d5bs`) |
| `phaze-5f1sv` | P2 | COND: global condition policy (deferred) |
| `phaze-28883` | P2 | ENV-M: 34-graph coarse model proof (deferred) |
| `phaze-5jo4c` | P3 | ENV-B: browser/artifact fidelity (deferred); also gates SE_B and C6_B |
| `phaze-pms85` | P2 | SF branch-ratio policy (deferred) |
| none filed | n/a | R2 admission fairness policy, R5 factory/registry seeding, ENV-R remote lifecycle: deferred with no bead in this epic |
