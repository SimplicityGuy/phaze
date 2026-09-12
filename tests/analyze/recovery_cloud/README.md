# Recovery and cloud-reconciliation capability tests

This package owns the 241 scenarios moved from the recovery/cloud structural-maintenance closure. Each test body lives in exactly one state or ownership family; setup helpers remain local and assertion-free. `scenario_node_map.tsv` records every preserved pre/post pytest node id, including parametrized cases.

| Family | Scenarios | Ownership |
| --- | ---: | --- |
| `pending` | 5 | Pending confirmation, quota holds, and observation without transition |
| `admission` | 9 | Admission, quota reservation, and inadmissibility state transitions |
| `terminal` | 8 | Successful and terminal outcomes, cleanup, and callback primacy |
| `redrive` | 19 | Retry budgets, redrive, spillover, and node-loss accounting |
| `wedge` | 25 | Pod/workload wedge and phantom-workload observation |
| `orphan_replay` | 126 | Orphan classification, replay safety, reaping, and crash idempotency |
| `owner_routing` | 27 | Owner-specific fileserver, compute, and controller routing |
| `ledger_backfill` | 17 | Saq ledger parsing, backfill, and startup recovery ordering |
| `locking` | 5 | Transaction release, commit ordering, and advisory-lock ownership |
| **Total** | **241** | |

Cluster-wide PostgreSQL catalogue assertions remain scoped to `current_database()` through the repository's shared guard/query conventions.
