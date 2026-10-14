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
Phaze currently uses local landing: only the merger runs `bh work merge <id>` after every required
gate is resolved. Developers stop at submission and never approve or merge their own work.

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

## Queued seats and lease expiry

Keep expensive gates serial when needed. A queued developer may be idle for tens of minutes;
`bh worktree status` labels such as `ABANDONED`, inferred from absent file changes, and an expired
claim lease are not evidence that its agent stopped. Use the dispatcher's agent layer to establish
liveness before abandoning or reassigning work: **a record that reads as a status is not one**.
For a still-assigned `in_progress` bead, treat lease expiry as informational for that liveness
decision. Current tooling also uses stale leases to guard explicit foreign-claim recovery via
`bh work abandon`; expiry alone does not authorize reaping a live queued seat.

Decision: option (c) is chosen. Lease expiry is informational for a still-assigned `in_progress`
bead, and the agent layer is the authoritative liveness read. Option (c) chosen; re-claim fix
tracked upstream at beadhive/beadhive#95.

The historical measurement on `phaze-48ghg.4` (2026-08-26): a lease of ~4 minutes between
heartbeats, `Lease: expires expired (heartbeat 26 mins ago)`, and a same-owner re-claim that left the
heartbeat at exactly "26 mins ago", HEAD unchanged at `0bdfd198`, and the worktree clean. The bead
remained `in_progress`, assigned to its developer, and absent from `bh work ready`. Serialization was
intentional: an escalating bead cost ~27 min of gate plus ~23 min of submit re-validation, and four
seats hitting that together drove a 32 GB machine into swap.

The same-owner re-claim defect still reproduces in the installed tooling (bh 0.24.4, re-checked
2026-10-14): re-claiming prints `claimed` while the lease heartbeat is unchanged, and `bh work` has no
heartbeat or touch verb. Do not use re-claim as a heartbeat or take that success line as evidence of
renewal. The CLI correction (renew the lease with ownership guards, or report reattachment without
renewal) is outside this repo and is tracked upstream at beadhive/beadhive#95.

## Test isolation

Every concurrent worktree needs its own PostgreSQL databases and Redis logical database. Allocate
a seat and copy all three exports exactly as printed; the recipe starts the shared containers when
needed:

```bash
just test-db-for <seat>
```

Do not run two pytest processes against one seat. The session advisory lock refuses the second
process, while separate seats can validate concurrently. Do not stop the shared test containers
while another seat is active. When the worktree is finished, run `just test-db-release <seat>`;
never use `just test-db-down` as seat cleanup because it targets the shared containers.

Isolation is not only about services. Any writable path two seats can open is a collision surface,
and the scratchpad the harness advertises as "session-specific" is per-session, not per-seat —
every seat of one dispatch is handed the same directory. Write scratch output under a path carrying
the bead id, or inside the bead's own worktree. Read a gate's verdict from its own pytest summary
and coverage lines, never from a wrapper's exit code or a task-completion status. CLAUDE.md carries
the measured incidents behind both rules.

## Boundaries

Phaze's current validation mapping keeps the cost and authority boundaries distinct:

| Boundary | Recipe |
| --- | --- |
| `bh work check` and `bh work submit` | `just check-fast` |
| per-bead `merge` / `merge-main` configuration | `just check-fast` (molecule-bound merges are inert under relaxed validation) |
| assembled molecule (`bh work finish`) | `just check-all` |
| `postland` and conflict-resolution `union` | `just check` |

`just check-fast` is change-selected and may escalate to the full suite; `just check` is the manual
full coverage gate; `just check-all` adds all pre-commit hooks. See
[Gates and isolation](gates-and-isolation.md) for measured evidence and edge cases, and the
[public Just recipe contract](just-recipe-contract.md) for the complete recipe inventory.

- File epics through the planner; do not hand-roll dependency graphs with raw `bd` commands.
- Use `bh work` for bead reads and lifecycle mutations. `bv --robot-*` is read-only scheduling and
  triage support, not the source of truth for live assignment state.
- Never push directly to `main` and never combine unrelated beads in one PR.
- Preserve conventional history and the no-local-identifiers rule in files, commits, and PR text.
- A green validation or CI run does not grant a developer permission to approve or merge.
