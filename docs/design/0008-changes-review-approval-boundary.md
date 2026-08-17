# ADR 0008: Changes Review Approval Boundary

**Status:** Accepted

## Decision

Changes Review is the only UI workspace that authorizes filename, destination, and tag changes.

Filename and destination are one atomic decision. Both values are persisted on one
`RenameProposal`, governed by one `status`, one optimistic-concurrency token, and one execution/audit
chain. Approving that row authorizes both dimensions, including a rename-in-place when
`proposed_path` is null. No approval control may hide either value.

Tag changes remain an independent decision in the same workspace. They cannot share the proposal
approval without inventing persisted state: proposed tags are computed after an executed move, and
the first durable authorization record is the append-only `TagWriteLog` created when the operator
dispatches the write. That log stores before/after snapshots, verification discrepancies, and the
undo boundary. A rename approval therefore never authorizes tags, and a tag approval never changes
the proposal status.

The canonical operator vocabulary is:

| UI state | Persisted proposal state | Tag-write meaning |
| --- | --- | --- |
| Needs Review | `pending` | computed change has not been dispatched |
| Approved | `approved` or `executed` | completed writes are retained in the audit log |
| Blocked | `failed` | failed, verification-failed, or discrepant write |
| Rejected | `rejected` | not applicable; tags have no persisted rejection state |

`All` is the union of those states. The UI mapping does not migrate or collapse persisted statuses;
`approved` and `executed` must remain distinct for dispatch and audit.

## Consequences

- Propose remains generation and inspection only.
- The former destination and tag-write pages are compatibility aliases to Changes Review, not
  separate approval surfaces.
- Filename and destination are always rendered in full before proposal approval.
- Tag before/after values, discrepancies, and reversibility are shown in the same workspace, but
  only after execution makes a tag write eligible.
- Bulk proposal approval is limited to selected rows that are still `pending` and still at or above
  90% confidence when the server executes the update. Missing and low confidence remain individually
  reviewable. Disabled and excluded reasons are visible text, never tooltip-only.
- Existing proposal state, tag audit rows, execution logs, and undo snapshots require no migration.
