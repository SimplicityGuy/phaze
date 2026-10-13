# Mixed prose and code: retain complete validation authority

Task **phaze-al4ct (SD)** records the mixed-validation disposition for implementation epic
**phaze-75qpj**. The approved assessment is PR #716, source
`75e586ef9d1d412dfcf8ecb34818ecdda2576e70`.

**Disposition: EXCLUDE new mixed prose/code selection from current adoption.** Keep the existing
selector, strict prose-only classification, complete prose floor, normal always-run floor,
coverage and branch evidence, and whole-report refusals. No executable authority changes here.
An explicitly reviewed exclusion is a permitted completion path for this conditional pilot.

## Prerequisite decision

The [selection proposal](../quality-assessment/selector-proposal.md) requires selected/full
shadow equivalence (C) before new mixed authority (D). The
[SC disposition](selector-equivalence-disposition.md) excludes that comparison from adoption:
installed Repowise 0.55.0 does not provide the verified immutable generation, pinned reader and
atomic publication protocol described in the [SB disposition](coverage-generation-disposition.md).
Neither a mutable task-local store nor a stale shared report supplies that missing prerequisite.

The chosen observation cohort is the next **20 completed beads**, grouped by actual change type.
The [M0 readback](../bead-feedback-readback.md) retains unknown original work-start timestamps.
A few review waits or full-suite durations from different trees cannot establish accepted-bead
velocity. This task therefore stops at bounded feasibility and explicit exclusion; it does not
execute three paired fast-gate screens or publish a new coverage generation.

## Boundaries preserved by the existing implementation

The actual authority remains `scripts/select_impacted_tests.py` and its `just test-fast` consumer.
The following table describes retained behavior, not a proposed mixed-mode implementation.

| Input or evidence | Existing boundary retained |
| --- | --- |
| Regular Markdown entirely within the strict allowed locations | Prose-only `docs` verdict runs every module in `tests/docs_floor.txt` |
| Markdown plus any Python or other executable artifact | Whole-diff prose shortcut is unavailable; the normal selector evaluates the whole change |
| Executable Markdown, symlink, submodule or nonregular type/mode | Raw-diff mode checks refuse classification as prose |
| Rename from or to a runtime prompt or executable location | Both rename ends must qualify; checking only the destination is insufficient |
| Runtime prompt Markdown, unknown configuration, shell, template or build artifact | No suffix or directory exemption; unknown authority escalates to the full suite |
| Unsupported inference, stale source coordinates or absent mapped test file | Existing whole-report escalation remains; no entry is silently dropped |
| Dirty tree, malformed raw record or invalid selector invocation | Existing refusal remains; no accidental prose verdict authorizes a weaker run |
| Empty diff | Not a prose-only diff; the normal floor remains applicable |

In particular, this task does not subtract Markdown paths from a Repowise report and run what is
left. A future mixed mode would independently authorize **every** code/artifact path, run the
**entire** prose floor, and preserve all whole-report refusals. A successful Python route test
does not by itself authorize changed Jinja, migration, prompt, workflow or native-browser behavior.
The current prose transcript remains distinct from measured selection and full escalation.

## Evidence and unmeasured outcomes

The existing `tests/shared/test_fast_gate.py` includes raw-mode and both-rename-end negative
controls, one Python file among twenty Markdown files, unknown-artifact and inference refusal,
missing mapped test escalation, and a real selector subprocess for a mixed diff. Its prose-floor
inventory guard checks that every test module reading tracked prose is included. Those existing
controls can establish that this documentation change preserves the shipped boundary; they do
not demonstrate a new selected/full shadow oracle or new mixed consumer equivalence.

No new mixed verdict, path mapping, manifest, context import or threshold is adopted. Legitimate
skips, canonical populations, branch evidence, line floors, and service/browser isolation remain
unchanged. No fault was injected into an unsupported mixed pilot, and no matched gate runtime,
stable p95, coverage gain or accepted-bead velocity gain is claimed. Targeted control results,
hooks and the eventual exact-tree provider verdict belong to the task handoff and Beadhive;
this document does not predeclare them green.

## Downstream work

PD must record SD as excluded, with selection and runtime outcomes unmeasured. G0 still runs the
actual assembled full population, including adopted repairs and coverage backfills. A0 reviews
that coverage and the declared lifecycle cohort before any adoption or speed claim. SC's exclusion
does not prohibit separately scoped feasibility work, but it does not authorize mixed selection.
A future proposal needs the missing generation protocol, demonstrated shadow equivalence, real
consumer fault detection and fresh review before changing this authority.
