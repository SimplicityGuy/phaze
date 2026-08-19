# Agentic Git Flow

Phaze uses Beadhive's Agentic Git Flow (AGF). Work is represented by beads, performed in
Beadhive-provisioned worktrees, validated before review, and integrated by the role that owns the
merge boundary. Raw Git is still used to inspect and commit the implementation inside a worktree;
`bh work` owns the lifecycle around it.

Check that the checkout is attached to a healthy hive before starting:

```bash
bh hive ready
bh work ready
bh work issue <id>
bh work brief <id>
```

## Roles

- A **planner** turns a new idea or a materially changed design into an approved bead molecule.
- A **dispatcher** coordinates an epic and assigns its child beads; in fanout mode it does not
  implement those children.
- A **developer** owns one bead from claim through a reviewable submission. It does not approve or
  merge its own work.
- A **reviewer** validates intent, acceptance criteria, behavior, and the proposed diff, then
  approves or bounces the review gate.
- A **merger** serializes approved changes onto the integration branch and preserves history.

Load the corresponding `bh` role skill before acting in one of these seats. Do not substitute a
generic feature branch for the worktree and identity that `bh` provisions.

## Developer lifecycle

```bash
bh work claim <id> --as dev/<seat>
# Work only in the path printed by claim. Inspect and commit with Git there.
bh work show <id>
bh work check <id>
bh work submit <id>
```

`submit` requires a clean worktree, conventional commit subjects, and a passing validation of the
exact proposed commit from a clean checkout. It opens a review gate and leaves the bead submitted;
it does not mean merged. If review requests changes, use `bh work resume <id>`, address the feedback,
and submit again. `bh work refine <id> --autosquash` can remove local checkpoint noise while
preserving the resulting tree.

When a hive uses GitHub PR landing, publish the bead branch and open the PR after submission. Once
GitHub reports the PR merged, `bh work land <id>` verifies that external state and closes the bead.
For local landing, only the merger runs `bh work merge <id>` after every required gate is resolved.

## Review and integration

```bash
bh work review <id> --run
bh work approve <id> --as <reviewer>  # or: bh work bounce <id>
bh work merge <id>
```

The reviewer should exercise the feature, not only read the diff. The merger owns conflict
resolution and the integration slot; it escalates conflicts rather than discarding a bead's work.
Epic dispatch and grouped batches have additional molecule-level commands, so use the dispatcher
role guide rather than applying the leaf sequence mechanically.

## Test isolation

Every concurrent worktree needs its own PostgreSQL databases and Redis logical database. Start the
shared containers once, allocate a seat, and copy all three exports exactly as printed:

```bash
just test-db
just test-db-for <seat>
```

Do not run two pytest processes against one seat. The session advisory lock refuses the second
process, while separate seats can validate concurrently. Do not stop the shared test containers
while another seat is active.

## Boundaries

- File epics through the planner; do not hand-roll dependency graphs with raw `bd` commands.
- Use `bh work` for bead reads and lifecycle mutations. `bv --robot-*` is read-only scheduling and
  triage support, not the source of truth for live assignment state.
- Never push directly to `main` and never combine unrelated beads in one PR.
- Preserve conventional history and the no-local-identifiers rule in files, commits, and PR text.
- A green validation or CI run does not grant a developer permission to approve or merge.
