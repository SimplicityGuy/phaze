# Historical Planning Archive

This directory is a frozen record of the GSD planning process used through 2026-07-29. Its
top-level `PROJECT.md`, `STATE.md`, `ROADMAP.md`, `MILESTONES.md`, `REQUIREMENTS.md`, and
`RETROSPECTIVE.md` describe the state known at that cutoff; labels such as “current,” “active,”
and “pending” are historical statements, not the live backlog.

Current implementation work is tracked in Beadhive beads. Use the repository [README](../README.md)
and the current [documentation index](../docs/README.md) for shipped behavior and operator
guidance. Dated plans, measurements, milestone audits, and retrospectives remain valuable evidence
and are intentionally preserved without rewriting their conclusions to match later releases.

Repository-local links and Mermaid diagrams in this tree describe the repository shape known when
each record was written. `scripts/audit_historical_evidence.py` checks every local target and every
Mermaid fence. A missing target reported as `historical_by_boundary` is intentionally retained as a
point-in-time path, not promised as current navigation; valid targets are reported separately.
