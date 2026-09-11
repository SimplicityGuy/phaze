# Historical Evidence Reconciliation Audit — 2026-09-11

This report records the reproducible reconciliation performed by `phaze-sfofk.15`. It compares
the complete historical corpus at source revision
`829594c6546498fff92270d4c569bb5f5bcc8b62` with the reconciled worktree. The checker is generic:
it recognizes approved placeholder shapes and encoded local-root shapes without embedding the
prohibited source identifiers in tracked code.

Run the audit from the repository root:

```bash
uv run python scripts/audit_historical_evidence.py \
  --base 829594c6546498fff92270d4c569bb5f5bcc8b62
```

## Corpus and identifier equivalence

| Check | Result |
| --- | ---: |
| Historical files scanned | 1,313 |
| Binary historical files compared byte-for-byte | 3 |
| Files containing approved identifier-only changes | 202 |
| Local absolute-root substitutions | 41,853 |
| Encoded local-root substitutions in graph IDs/references | 10,012 |
| Distinct source host/account identifiers derived from the baseline diff | 5 |
| Host/account substitutions | 729 |
| Archive-directory substitutions | 3 |
| Total approved substitutions | 52,597 |
| Prohibited identifier occurrences after reconciliation | 0 |
| Files differing beyond approved substitutions | 0 |

The encoded graph-root substitutions were applied consistently to node IDs, `source`/`target`,
`_src`/`_tgt`, and hyperedge references. Both tracked graph snapshots parse, contain unique node
IDs, and have zero dangling edge or hyperedge references. The checker derives the five prohibited
host/account values from identifier-only baseline changes, then verifies none remain anywhere in
the current historical corpus; it does not store or print those values.

## Numeric evidence equivalence

The audit normalizes only the changed identifier spans, then extracts every remaining numeric
token in order from the baseline and reconciled text.

| Check | Result |
| --- | ---: |
| Files compared | 1,313 |
| Ordered non-identifier numeric tokens compared | 319,770 |
| Files with a numeric-token mismatch | 0 |

Therefore no row count, duration, latency, sample size, percentage, date, port, version, or other
non-identifier quantity changed. A replacement inside a historical quotation changes only the
prohibited identifier; its wording and quantities remain point-in-time evidence.

## Archive, link, diagram, and graph checks

All five historical scopes have an explicit archive boundary: `.planning/`, `docs/spikes/`,
`docs/superpowers/specs/`, `docs/telemetry/measurements/`, and the dated documentation-audit
snapshot. Across the corpus, 123 repository-local links resolve in the current tree. One link to
`multi-compute.md` is retained at its original nested planning location and classified
`historical_by_boundary`; it records the path understood by that plan rather than current
navigation. The one Mermaid block is structurally valid. Both graph snapshots are structurally
valid and referentially closed.

## ADR reconciliation

- `docs/design/0010-colour-contrast-tokens.md` now records the shipped implementation and the
  later semantic-token supersession boundary without altering the accepted contrast target.
- `docs/design/0018-set-projection-and-file-viewer.md` now distinguishes its planning origin,
  filing date, retrospective synthesis, merge date, and later dated amendments.

No historical verdict, measurement, conclusion, or operator quotation was reinterpreted. The
historical-content comparison permits only the identifier substitutions enumerated above; ADR
status and provenance metadata are maintained records outside that immutable comparison.
