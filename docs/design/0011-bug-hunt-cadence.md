# ADR-0011 — Review the fix push, and scope routine lenses to the diff

| | |
| --- | --- |
| **Status** | Accepted — decided 2026-08-11, recorded 2026-08-18 |
| **Date** | 2026-08-11 (bug-hunt retrospective) |
| **Bead** | `phaze-tyrmc` |
| **Applies to** | the deep bug-hunt process (`.claude/bughunt-playbook.md`, gitignored) and the dispatcher flow for `fixgroup:*` molecules |

## Why this is committed and the playbook is not

The operating playbook lives at `.claude/bughunt-playbook.md` and `.gitignore:152` excludes all of
`.claude/`. It is never committed, so it does not survive a fresh clone, a new machine, or its own
next rewrite. This file exists so the two conventions below, **and the measurements that justify
them**, outlive that drift. The playbook holds the mechanics; this holds the decision.

## Decision

1. **A hunt's fix push gets the same adversarial treatment the hunt itself got.** Before a
   `fixgroup:*` integration branch merges, run a diff-scoped adversarial verification pass — the
   §6 verifier contract, pointed at the fix diff instead of at a finder's candidate.
2. **Between full hunts, lens passes are scoped to the diff since the last hunt.** Whole-tree
   passes are reserved for occasional deep hunts. They are **reduced in frequency, not retired** —
   see the 67% number below for why retiring them would be wrong.

## The measurements, which are the whole argument

From the bug-hunt retrospective of 2026-08-11. Line-level provenance was taken on a **stratified
sample** of the 2026-08-08 hunt's bugs, tracing each defect to the commit that introduced it.

- **~25% of the 2026-08-08 hunt's bugs were introduced, or first made reachable, by the
  2026-07-27 hunt's own fix push.** None were revert-style regressions — every one was a *new*
  defect born inside a fix commit. Two from the sample:
  - an idle-in-transaction lock connection, introduced by a lock fix;
  - a re-mounted endpoint, exposing error handling that was already broken but unreachable.
- **~67% were latent bugs predating the previous hunt.** Whole-tree hunting still pays.
- **P1s per hunt: 15 → 2 → 5 → 3.** Severity is draining, so the marginal value of each successive
  whole-tree pass is falling — which is what makes diff-scoped routine passes the better default,
  and what makes the fix push (a quarter of the yield, concentrated in a diff you already have)
  the highest-density target left.

Both rates are estimates from a sample, not a census of all 131 beads labelled
`bughunt-2026-08-08`. They are precise enough to choose a cadence and not precise enough to quote
as a defect rate.

> **Do not "simplify" the numbers out of this document.** A convention stripped of its evidence
> reads as arbitrary process and gets dropped by the next person to touch it. If a later hunt
> re-measures, add the new series next to this one rather than overwriting it.

**Footnote on the P1 series.** Recounted 2026-08-18 against the live beads DB, `priority == 1` per
`bughunt-*` label gives **19 → 2 → 5 → 3** (2026-07-17, 07-20, 07-27, 08-08); the retrospective
recorded the first hunt as 15. The discrepancy is not reconciled here — priorities are editable
after filing, and the retrospective's own figure is preserved above as the number the decision was
actually made on. The trend is the same either way.

## What this catches, and what it does not

Stated plainly, because a control described as closing a loop when it only narrows one is worse
than no control: it stops people looking.

**It catches:** a defect introduced by the fix diff itself, in the lines the diff changed — the
larger share of the 25%, and the class the "idle-in-transaction lock connection" example belongs
to. This is a review control operating on a small, freshly-written, high-churn diff, which is the
condition adversarial verification is strongest under.

**It does not catch, and these remain open:**

- **Defects the diff makes reachable without touching.** The second sampled example — a re-mounted
  endpoint exposing dormant broken error handling — has its bug in code the diff never edits. A
  reviewer reading only changed lines will not see it. Mitigated, not closed, by requiring the pass
  to ask what the diff newly makes reachable; that reasoning is weaker than reading the code, and
  it is the residual this convention is least able to argue away.
- **Interactions between fixgroups.** Each `fixgroup:*` diff is reviewed against the integration
  base. Two fixgroups developed concurrently can each be individually sound and jointly wrong, and
  neither diff-scoped pass sees the other.
- **Latent bugs predating the last hunt** — the 67%. Diff-scoped lenses structurally cannot find
  these. This is the reason clause 2 says *reduced in frequency*, and the misreading to guard
  against: "we run diff lenses now" must never be heard as "we no longer need whole-tree hunts".
- **Anything outside the fix diff's own files** that the fix depended on being true.

**It is a convention, not a gate.** Nothing in CI blocks a `fixgroup:*` merge that skipped the
pass; enforcement is the dispatcher seat following the flow in `CLAUDE.md`. Treat a missing pass as
a process miss to be noticed in review, not as something the tooling will catch.

**"Injection" here means defect injection.** The originating bead is titled "close the fix-push
injection loop" — the loop is *hunt → fix → new bugs → next hunt*. This is not a control against
untrusted content influencing a commit, and nothing here should be cited as one.

## How to run the fix-push pass

The verifier contract is playbook §6 with its target swapped. The claim under refutation changes
from a finder's candidate to the fix itself:

> This diff fixes the beads it claims to fix, and introduces no new defect.

Rules carried over from §6 unchanged: a **different model** from whoever wrote the fix; **default
to refuted**; open the **current** files rather than reasoning from the patch alone; cite exact
line contents as evidence; right-size severity. Added for this target:

- Feed it the **bead bodies of every bug in the fixgroup**, so "does it actually fix them" is
  answerable rather than assumed.
- Ask explicitly **what the diff newly makes reachable** — the dormant-code class above is invisible
  to a reviewer who only reads changed lines.
- Findings become **new beads**, not silent edits. A fix-push pass that quietly rewrites the fix
  destroys the provenance the next retrospective needs.

## Consequences

- Each hunt-fix molecule costs one extra verification pass before merge, sized by the diff rather
  than the tree.
- Routine passes get cheaper and more frequent; whole-tree deep hunts get rarer and stay budgeted.
- The next retrospective can measure whether the 25% fell. That is the test of this ADR, and it
  requires fix-push findings to be filed as beads (above) to be measurable at all.
