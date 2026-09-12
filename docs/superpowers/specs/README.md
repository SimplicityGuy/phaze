# Historical Design Specifications

The dated specifications in this directory record the design context available when their work
was proposed. They are retained as evidence and may describe interfaces, routes, architecture, or
constraints that later decisions changed.

For shipped behavior, start with the repository [README](../../../README.md), the current
[documentation index](../../README.md), and the accepted records under [`docs/design/`](../../design/).
When a dated specification conflicts with live code or a later accepted decision, the live code
and later decision take precedence. Do not silently rewrite a historical specification to make it
look predictive; add a supersession note or update current documentation instead.

Repository-local links and Mermaid diagrams preserve the shape known when a specification was
written. `scripts/audit_historical_evidence.py` distinguishes valid current targets from missing
targets intentionally retained as `historical_by_boundary`, and validates every Mermaid fence.
