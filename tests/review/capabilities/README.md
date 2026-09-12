# Review and proposal capability tests

This directory owns the 236 scenarios that previously lived in six review-read-model files and
five proposal files. Test bodies remain explicit; shared setup comes only from existing fixtures
or assertion-free local helpers.

| Capability | Scenarios | Production boundary |
| --- | ---: | --- |
| `changes/` | 63 | `review_changes.py` and the changes-review workspace |
| `tag_write/` | 26 | `review_tagwrite.py` and tag-write review actions |
| `dedupe/` | 10 | `review_dedupe.py` and keeper review |
| `cue/` | 7 | `review_cue.py` and CUE eligibility/preview |
| `proposal_parsing/` | 14 | `proposal_parsing.py`, including salvage policy |
| `proposal_context/` | 36 | `proposal_context.py`, prompt and companion context |
| `proposal_persistence/` | 13 | `proposal_persistence.py` and idempotent storage |
| `proposal_provider/` | 67 | `proposal_provider.py`, wire shapes, rate limits, and task orchestration |

`scenario_node_map.tsv` records every pre-move node ID beside its post-move node ID. Its 236 data
rows are the audit trail for the unavoidable path portion of each node-ID change.
