# Reading bead feedback evidence

`scripts/bead_feedback.py` reads the existing Beadhive validation ledger, raw gate logs and
first-class lifecycle APIs. It produces a report on stdout and never creates events, changes a
selector verdict, imports an external runtime, or changes admission policy.

Run from a hive worktree, naming every bead explicitly:

```bash
uv run python scripts/bead_feedback.py --bead phaze-example
```

Repeat `--bead` for additional membership. This command is a readback inspection, not an approved
cohort or velocity pilot. Keep output outside tracked files: source records and selector reasons
can contain local identifiers. A report with unreadable records or lifecycle reads exits nonzero
and retains the errors alongside the available evidence.

The reader resolves the common Git directory, scans retained schema-1 `bh/validation/runs`
manifests and `uses` records separately, and joins raw selector decisions by the manifest's own
gate-log path. A reuse decision remains a use, never an additional execution. Failed, cancelled,
unfinished and unknown attempts retain the producer's verdict, lifecycle and reason. Manual or
unrecorded runs have an unknown denominator.

Lifecycle readback uses `bh work show --json`, `bh work issue --json` and the historical event
listing `bh work list --parent --all --include-infra --limit 0 --json`. Review acceptance requires
a closed convention review gate explicitly approved by policy, an unambiguous Git commit matching
an exact ledger revision, and a same-revision pending submission event preceding acceptance.
The last matching submission event measures the accepted round's review wait; the first observed
submission remains separate. Structured changes-requested events count observed bounces. A green
gate, first successful validation, gate creation, merge or generic failure message proves none of
these lifecycle transitions.

The current producer does not expose authoritative original work start, exact admission arrival,
admission and permit-release timestamps, or a coverage generation identifier in these records.
The reader preserves these fields as null rather than inferring them from a current claim, queue
duration, execution completion, branch name or nearby timestamp. Therefore accepted-bead velocity
remains unknown, even when review acceptance is joined. Existing queue duration and execution
duration are reported separately. Concurrent lifecycle reads are not an atomic snapshot, and
retention can remove earlier events; observed first submission and bounce counts are not claims
of complete history.

This adds no recording overhead to the validation path. Report wall time is measured separately;
retained output size can be measured on the redirected file. Producer instrumentation is an
external Beadhive capability limitation, requiring separately authorized work before complete
velocity claims. Cohort window and strata must be declared before any pilot. Selector generation
ownership remains the separate implementation task described in the approved assessment.
