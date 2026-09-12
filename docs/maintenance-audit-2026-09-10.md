# Repository Maintenance Audit — 2026-09-10

This is the final reproducible audit for the `phaze-sfofk` maintenance sprint. It applies the
[repository maintenance contract](repository-maintenance-contract.md) to the repository state
after implementation beads `.1` through `.18` and the pre-publication integrity implementation
from `.19`. It does not replace or rewrite the point-in-time [2026-08-19 documentation
result](documentation-audit-2026-08-19.md).

## Revisions and counting rule

| Role | Revision |
| --- | --- |
| Sprint baseline | `560d964a` |
| Audited source | `e316c78ccfcea85cb9bd770a8fb3e43bab76bcb2` |

The reviewed population is every path tracked at the audited source. A path is **changed** when
its source-revision path appears in `git diff --name-status 560d964a..e316c78c`; the single rename
is counted once at its destination. Every other tracked source path is **unchanged**. This rule
keeps the totals reproducible and avoids turning line counts into file-disposition counts.

| Disposition | Files |
| --- | ---: |
| Reviewed | 2,648 |
| Changed | 689 |
| Unchanged | 1,959 |

The equality is exact: **689 + 1,959 = 2,648**.

## Exhaustive disposition

| Inventory class | Reviewed | Changed | Unchanged | Disposition |
| --- | ---: | ---: | ---: | --- |
| Code: Dockerfiles | 3 | 1 | 2 | Current build rationale retained or normalized |
| Code: GitHub automation | 9 | 6 | 3 | Workflow comments reconciled with current gates |
| Code: JavaScript | 1 | 1 | 0 | Maintained UI commentary normalized |
| Code: Justfile | 1 | 1 | 0 | Public recipe surface reconciled and guarded |
| Code: Python | 1,013 | 423 | 590 | Comments/docstrings normalized; executable changes belong only to separately scoped refactors and this integrity gate |
| Code: runtime templates | 125 | 0 | 125 | Reviewed; no change required |
| Code: shell | 19 | 8 | 11 | Operational comments normalized without changing commands |
| Code: stylesheet | 1 | 0 | 1 | Reviewed; no change required |
| Docs: accepted decisions | 18 | 7 | 11 | Accepted status and durable rationale preserved |
| Docs: generated at source | 4 | 4 | 0 | Historical generator records retained as point-in-time evidence |
| Docs: historical evidence | 1,313 | 202 | 1,111 | Identifiers scrubbed with numeric evidence preserved; archive boundaries added |
| Docs: maintained at source | 77 | 34 | 43 | Present-tense guidance reconciled; ten unsupported generator markers, two stale anchors, and two invalid Mermaid diagrams corrected |
| Docs: runtime-loaded | 1 | 0 | 1 | Runtime prompt reviewed; no change required |
| Docs: tool-local | 1 | 1 | 0 | Tool guidance normalized |
| Excluded: captured/binary fixture | 22 | 0 | 22 | Classified, retained, and not interpreted as prose |
| Excluded: declarative config/data | 31 | 1 | 30 | Classified; the changed alert configuration was validated by its owning guard |
| Excluded: generated runtime asset | 16 | 0 | 16 | Classified, retained, and not hand-edited |
| Excluded: repository marker | 3 | 0 | 3 | Classified and retained |

The class rows sum to the reviewed, changed, and unchanged totals above. “Unchanged” means that
review found no correction warranted; it does not mean the path was omitted from the inventory.

## Reproduction commands

Run from a clean checkout containing the audited source:

```bash
uv run python scripts/maintenance_inventory.py --rev e316c78ccfcea85cb9bd770a8fb3e43bab76bcb2 --paths
git diff --name-status 560d964a..e316c78ccfcea85cb9bd770a8fb3e43bab76bcb2
uv run python scripts/audit_historical_evidence.py --base 829594c6546498fff92270d4c569bb5f5bcc8b62
uv run alembic heads
uv run phaze --help
just docker-compose-validate
just docs-check
uv run pytest tests/shared/test_documentation_integrity.py tests/shared/test_historical_evidence_audit.py tests/shared/core/test_docs_ia_current.py
uv run pre-commit run --all-files
just check-all
```

`just docs-check` is the deterministic maintained-document entrypoint. It reuses the maintenance
inventory classifications and the historical audit's link, forbidden-identifier, and Mermaid
parsers. It verifies index completeness, percent-decoded local paths and anchors, supported
generator provenance, migration-head claims, and every maintained Mermaid fence. Each Mermaid body
is rendered in a fresh temporary directory and isolated process group by Mermaid CLI `11.12.0`, the
version pinned and verified by this audit, so one browser lifecycle cannot hide another block's
failure. A render succeeds only after a complete closing `</svg>` is durable. The JSON field
`mermaid_terminated_after_render` names blocks whose browser did not exit during the grace period
and whose isolated process group was therefore cleaned up. External HTTP targets are listed in the
JSON report but are not contacted by the deterministic gate; `--check-external` performs that
separate, network-dependent check and uses a distinct result channel and exit status.

## Limitations

- The source revision includes the integrity-gate implementation and every correction it exposed,
  but predates this audit file and its index link. That is intentional: an audit cannot contain its
  own final commit identifier. The table excludes only its self-referential publication machinery.
- Local-anchor validation models GitHub-style Markdown heading identifiers and explicit HTML
  `id`/`name` anchors. It does not execute arbitrary Markdown extensions or JavaScript-generated
  anchors.
- External availability changes independently of repository content. External targets are
  reported, but their reachability is not part of the deterministic local pass.
- On the validation host, the system browser sometimes completed an SVG before its close promise
  returned. The renderer records that case explicitly, terminates the isolated process group, and
  verifies that the group is gone; missing or partial output remains a hard failure. The final
  validation rendered 24 of 24 complete SVGs, and all 24 required this recorded post-render cleanup.
- Captured/binary fixtures, generated runtime assets, declarative data, and repository markers are
  dispositioned by the inventory but are not decoded as maintained prose.
- The executable-AST comparison proves Python syntax equivalence for comment/docstring-only paths;
  it does not replace focused behavior tests for the separately authorized pure refactors. Each
  comment-only bead records its path-scoped comparison command in its submission evidence; one
  sprint-wide comparison would be invalid because those authorized refactors intentionally change
  executable ASTs.
