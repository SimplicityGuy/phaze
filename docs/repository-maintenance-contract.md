# Repository Maintenance Contract

This contract defines the scope and evidence rules for the `phaze-sfofk` maintenance sprint. It
is an inventory and reading contract, not a rewrite of the corpus. A later audit may update a
maintained document or annotate historical evidence, but it must apply the rules below and record
its own source revision.

## Reproducible baseline

The baseline is commit `560d964a74f1d88027134ca4f0cf8bdf6a5d9cea`. It is the committed tree
from which the maintenance worktrees were provisioned. The inventory counts files, not comment
lines: every file in a code surface is reviewed even if it currently contains no comment, so a
zero-comment file cannot disappear from the audit population.

Run the exact count command from any descendant containing the inventory script:

```bash
uv run python scripts/maintenance_inventory.py --rev 560d964a74f1d88027134ca4f0cf8bdf6a5d9cea
```

Add `--paths` to emit every path behind every count. The command exits nonzero and prints an
`unclassified` list if a tracked path is neither an audited surface nor a justified exclusion.
It resolves the revision to a commit, reads its tree with `git ls-tree`, and therefore does not
silently include untracked files or somebody else's working-tree changes.

### Code-comment surfaces

| Surface | Inclusion rule | Files |
| --- | --- | ---: |
| Python | Every tracked `*.py` | 997 |
| Shell | Every tracked `*.sh` | 14 |
| JavaScript | Every tracked `*.js` | 1 |
| Stylesheet | `assets/src/app.css` | 1 |
| Runtime template | `src/phaze/templates/**/*.html` and `alembic/script.py.mako` | 125 |
| Dockerfile | Every basename equal to `Dockerfile` or beginning `Dockerfile.` | 3 |
| GitHub automation | Workflow YAML plus `.github/actions/**/action.{yml,yaml}` | 9 |
| Just | The root `justfile` | 1 |

The stylesheet is included in addition to the surfaces named by the epic because its comments
carry build and accessibility rationale. Captured HTML test fixtures, dated HTML design
prototypes, SVG assets, and compiled/static assets are not runtime templates; their dispositions
are covered by documentation or the exclusions below.

### Documentation classes

Documentation includes every path below `.planning/`, `docs/`, and `design/`; every tracked
Markdown, MDX, reStructuredText, AsciiDoc, and text file anywhere else; `LICENSE`;
`alembic/README`; and both `.env.example*` files. Classification uses the following precedence,
so every documentation path has exactly one class:

| Class | Contract | Files |
| --- | --- | ---: |
| Maintained | Current human/operator/developer guidance, reference material, archive-boundary READMEs, and documentation assets not owned by another class | 59 |
| Accepted decision | `docs/design/*.md`; later accepted/superseding records win over earlier ones | 18 |
| Historical evidence | Planning archives, spikes, dated specifications/prototypes, measurements, and the 2026-08-19 audit; read as point-in-time evidence | 1,313 |
| Runtime-loaded | `src/phaze/prompts/naming.md`; changing it changes product behavior | 1 |
| Generated | Markdown containing a line-start `<!-- generated-by:` ownership marker; do not hand-edit until the generator is identified or the marker is dispositioned | 17 |
| Tool-local | Tracked agent steering (`CLAUDE.md`); it governs its tool, not product runtime | 1 |

Generated ownership is classified before historical/current semantics. A generator marker is
provenance, not proof that the contents are current. The later documentation audit must either
verify the real generator and regenerate the file or remove a misleading marker. The archive
boundary files `.planning/README.md` and `docs/superpowers/specs/README.md` remain maintained even
though the trees they explain are historical.

Ten spike scripts belong to both populations: they are executable Python/shell comment surfaces
and historical documentation evidence. That intentional overlap explains why the surface totals
must not be added to obtain the repository total.

### Justified class-level exclusions

The baseline has 2,622 tracked files. The two audited populations cover all but 72 paths; the
remaining paths are assigned to these exclusions by the script:

| Exclusion | Reason | Files |
| --- | --- | ---: |
| Captured or binary fixture | Vendor schemas, captured HTML/JSON, golden payloads, images, and audio are compared as fixture data; rewriting prose-like bytes would invalidate their evidentiary value | 22 |
| Declarative configuration or data | TOML/YAML/JSON/INI/lock/ignore files are authoritative inputs to factual checks, not a prose-normalization population (GitHub workflow/action YAML is explicitly included above) | 31 |
| Generated runtime asset | Favicons, social images, the web manifest, and the template poster SVG are output/assets rather than comment-bearing sources | 16 |
| Repository marker | `.gitkeep` and `py.typed` communicate directory/package state and contain no prose to normalize | 3 |

These exclusions are from comment normalization, not from correctness checks. A maintained-doc
claim about a Compose file, lock file, dashboard, fixture, or static asset must still be verified
against that file. A new suffix or path shape is not automatically excused: it appears under
`unclassified` until this contract and the classifier gain a specific disposition.

## Source-of-truth precedence

For a claim about what ships now, use this order:

1. Executable code, committed configuration/data, migrations, and tests that enforce the behavior.
2. The newest accepted decision that governs the behavior. If code conflicts with it, record a
   reconciliation defect; do not silently rewrite either side to make the conflict disappear.
3. Maintained documentation, which explains the verified implementation but is not independent
   evidence for its own claims.
4. Historical evidence, generated files, and tool-local guidance. These can explain why a choice
   was made, but an old use of “current” does not override the live tree.

Use the narrow authoritative source for each fact:

| Claim | Authoritative source |
| --- | --- |
| Runtime and dependency constraints | `pyproject.toml` plus the resolved `uv.lock`; Docker system packages from each `Dockerfile*` |
| Environment names, aliases, defaults, validation, and secret precedence | `src/phaze/config*.py`, `.env.example*`, and focused configuration tests |
| Database schema and current migration head | SQLAlchemy models, `alembic/versions/*.py`, and `uv run alembic heads` |
| HTTP/API behavior | FastAPI routers, request/response schemas, templates, and router contract tests |
| Queue ownership, retries, timeouts, heartbeat, and transaction ordering | Task/service implementations, queue configuration, and focused task/integration tests |
| Deployment topology, images, ports, mounts, and health checks | Compose files, Dockerfiles, deployment manifests, and their validation tests |
| Developer and CI workflows | `justfile`, scripts invoked by recipes, `.github/workflows/`, composite actions, and `tests/buckets.json` |
| UI structure and tokens | Jinja templates, `assets/src/app.css`, JavaScript, and accessibility/browser guards |

Repowise is orientation and impact evidence, not a replacement for those sources. If its response
reports a stale index, verify against the current worktree before changing a claim.

## Comment dispositions

Every in-scope comment or docstring receives one of three dispositions:

- **Keep:** concise rationale for surprising ordering, transactions, concurrency, safety,
  authorization, protocols, performance/memory bounds, compatibility, failure isolation, or the
  condition that makes a regression test non-vacuous. It must use current names and present-tense
  behavior.
- **Migrate:** long incident histories, measurements, alternatives, and chronology move to an
  accepted design record, maintained runbook, or explicitly historical evidence. Leave a short
  invariant and a resolvable filename link at the code site. Preserve every measured quantity.
- **Delete:** code restatement, narration of setup/assertions, obsolete chronology, stale paths or
  names, empty section separators, and comments whose only content is obvious from the statement.

Do not use this taxonomy to smuggle in a product change, new default, migration, or architectural
decision. Such a discovery returns to planning. Public/import surfaces and observable behavior
remain unchanged unless a separate bead explicitly authorizes otherwise.

### Protected directives and citations

Never delete or relocate a pragma merely because it resembles a comment. Verify it with the tool
that consumes it and preserve its exact semantic scope. Protected forms include:

- Ruff/flake/type directives such as `noqa`, `type: ignore[...]`, `type: ...`, `pyright`,
  `mypy`, `ruff: noqa`, `fmt: skip`, and formatter on/off regions.
- Security suppressions such as `nosec` and their test ids. Ruff and Bandit may attach findings
  to different physical lines; follow `CONVENTIONS.md` rather than coalescing them.
- Coverage and test directives such as `pragma: no cover` and pytest markers encoded in comments.
- ShellCheck, shfmt, Hadolint, yamllint, Docker syntax/check directives, workflow annotations,
  template/linter suppressions, and generated-file ownership markers.
- Bead/date attribution, ADR filename links, decision identifiers, and compatibility/safety
  citations that make a retained invariant traceable.

The directive's consumer passing is necessary but not sufficient: an unused blanket suppression
is deleted or narrowed, while a live suppression keeps the smallest scope the tool recognizes.

## Operator attribution and ADR citations

Attributing a completed choice to the operator requires a citation, not emphasis. The claim
carries the question asked, the option label selected, the ISO date, and a durable bead or
accepted record in the same paragraph. The operator owns the selected label, not the option
description written by the assistant. `scripts/recover_operator_decisions.py` can recover local
transcript evidence, but its output is an unversioned forensic aid and is not itself the durable
record.

Authority statements (“this choice belongs to the operator”) and pending states (“awaiting an
operator decision”) are not claims that a decision already occurred. Do not add a date/bead merely
to appease the guard; phrase the actual meaning accurately. The wrap-tolerant, paragraph-scoped
guard remains `tests/shared/test_operator_attribution_citations.py`.

Cite accepted decisions by filename, for example
`docs/design/0012-verification-fidelity-and-operator-attribution.md`, not by a bare ADR number.
`tests/shared/test_adr_citation_resolution.py` checks that numbers resolve and H1s agree with
filenames, but cannot prove that a reused number still means the decision the author intended.

## Local identifiers and numeric preservation

`CONVENTIONS.md` applies to every tracked artifact, plus commit messages and review/PR text. Never
commit real archive filenames or directories, archive/scratch paths, content digests, file UUIDs,
or personal host/account identifiers. Use the approved placeholders such as `<track-01>`,
`<set-01>`, `<archive-mount>`, `<scratch>/...`, `fp_<hash-1>`, `<uuid-1>`, and role names such as
`host-prod`.

Replace identifiers, never quantities. Row counts, durations, latencies, sample sizes,
percentages, dates, ports, versions, and other measured values remain exact. After scrubbing,
compare numeric tokens before and after and confirm that any removed digit belonged only to the
identifier being replaced. Group prose quantities with commas according to `CONVENTIONS.md`; do
not reformat identifiers that merely look numeric.

For the complete historical corpus, run the generic checker documented in the
[2026-09-11 historical evidence audit](historical-evidence-audit-2026-09-11.md). It verifies
identifier-only transformation, ordered numeric-token equivalence, archive boundaries, local
links, Mermaid fences, and encoded graph-reference integrity against an immutable source revision.

## Executable-AST proof for Python comment work

For uncommitted comment/docstring edits relative to the bead's starting commit:

```bash
uv run python scripts/compare_python_ast.py --base <starting-commit>
```

For a committed branch, compare two revisions:

```bash
uv run python scripts/compare_python_ast.py --base <starting-commit> --target HEAD
```

Explicit Python paths may follow the options. With no paths, the script obtains changed `*.py`
files from `git diff`. An added, deleted, copied, or renamed Python file fails as non-comment-only.
For modified files it parses with `type_comments=True`, removes only leading module/class/function
docstring nodes, discards source positions, normalizes moved `type: ignore` line numbers, and
compares the remaining AST. Executable statements, annotations, ordinary string expressions,
type comments, and `type: ignore[...]` tags therefore remain checked.

This proves executable/typing syntax equivalence; it does not prove comment accuracy, preserved
pragmas, imports that happen dynamically, generated output, or runtime behavior. Review the diff,
run the operator/ADR guards, Ruff, mypy, and the bead's focused tests as separate evidence.
