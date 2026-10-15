# Operator copy inventory

Developer review record for phaze-6q6x2, 2026-10-14. This inventories the authored presentation
boundary; no user data is filtered or rewritten.

## Removed or rephrased

| Surface | Change and retained contract |
| --- | --- |
| Settings fields and generated settings schemas | Removed bead/phase/decision suffixes from shared, agent and control metadata. Retained keys, defaults, limits, credential rules, measured validation values and reload guidance. |
| Startup/backend validation | Production TLS/Redis and registry cardinality errors describe the violated requirement without internal decision codes. |
| CLI help and output | Queue status, backfill, reconciliation and analysis-child help use operation names. Dry-run behavior, step counts, 2,400-row incident magnitude and safety instructions remain. |
| Operator diagnostics | Queue/recovery/enqueue/startup logs retain action, failure and remediation details. Logging keys and task identifiers remain unchanged. |
| Telemetry catalogue | Public instrument/label descriptions retain measured quantities and dimensional meaning without tracking references. Metric names and labels remain unchanged. |
| OpenAPI routes | Explicit operation descriptions replace auto-published developer genealogy. Detailed original endpoint docstrings remain in source. |
| OpenAPI/settings component schemas | Public summaries describe payloads. Original detailed rationale is retained in adjacent source comments. Wire shapes are unchanged. |
| UI and template help | Static authored HTML/SVG and attributes are checked after excluding developer comments. Representative empty/settings/error fragments are rendered by the regression check. |
| Maintained user/operator docs and example environment | Functional headings, tables, rollout instructions and descriptions use operation names. Evidence destinations, versions, commands, thresholds and measured quantities remain. Changed TOCs and tracked inbound anchors are repaired; a compatibility anchor preserves the frozen historical spike link. |
| Kubernetes deployment, retry and queue-worker lineage | Rephrased KDEPLOY-01/03/06, WR-01, the 35-05 canvas reference and quick-task labels as operational descriptions. Quick-task suffixes in pool-setting descriptions and public retry help, plus the WR-03 push diagnostic suffix, are removed. Defaults and diagnostic remediation stay intact. |

The maintained-document scope is the explicit `CURRENT_DOCS` tuple in
`tests/shared/core/test_operator_copy.py`, plus `.env.example`. It includes README, architecture,
API/database/configuration references, deployment/cloud/analysis guides and operational telemetry
and companion guides. New guides should join this scope when they become current product guidance.

## Intentional remaining matches in current guides

Each remaining tracking match below is a support target or an exact historical operator record,
not a label in current functional guidance. Link labels use the measurement/decision subject.

| Location | Match | Purpose |
| --- | --- | --- |
| `docs/analysis-evaluation.md` | `/home/datum/phaze-iq64v` | Exact existing evaluation root used by staged launchers and report paths; the path prefix alone is exempt in this guide. |
| `docs/analysis-evaluation.md` | `docs/evaluation-phaze-iq64v-corpus.json` | Real tracked corpus manifest; the exact filename is exempt and its existence is tested. |
| `docs/analysis-evaluation-upcloud-runbook.md` | `/home/datum/phaze-iq64v` | Exact existing launcher/model/report root consumed by shell commands; only this path prefix is exempt in this guide. |
| `docs/k8s-burst.md:248` | `SCHED-03` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:254` | `D-04` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:255` | `phaze-1q4g` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:265` | `phaze-wcrb` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:270` | `phaze-1xngw` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:272` | `phaze-79mu7` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:280` | `D-14` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:282` | `phaze-j0ixx` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:285` | `phaze-79mu7` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:296` | `phaze-j0ixx` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:299` | `phaze-j0ixx` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:301` | `phaze-d28sn` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:303` | `phaze-79mu7` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:315` | `phaze-2mwyo` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:316` | `D-14` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:323` | `phaze-9sqa` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:334` | `phaze-6ck1` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/k8s-burst.md:538` | `phaze-3j67` | Support/evidence path or link target |
| `docs/k8s-burst.md:538` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:539` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:540` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:567` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:590` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:600` | `phaze-3j67` | Support/evidence path or link target |
| `docs/k8s-burst.md:666` | `phaze-3j67` | Support/evidence path or link target |
| `docs/k8s-burst.md:667` | `phaze-3j67` | Support/evidence path or link target |
| `docs/k8s-burst.md:667` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:668` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:669` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:716` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:781` | `phaze-3j67` | Support/evidence path or link target |
| `docs/k8s-burst.md:863` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/k8s-burst.md:983` | `phaze-d2hgv.6` | Support/evidence path or link target |
| `docs/k8s-burst.md:1094` | `phaze-8r6t4` | Support/evidence path or link target |
| `docs/analysis-evaluation.md:14` | `phaze-b2qs9` | Support/evidence path or link target |
| `docs/api.md:254` | `phaze-l1j35` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/stranded-analysis-recovery.md:4` | `phaze-zaf2l` | Support/evidence path or link target |
| `docs/stranded-analysis-recovery.md:4` | `phaze-spends` | Support/evidence path or link target |
| `docs/essentia-analysis.md:127` | `phaze-b2qs9` | Support/evidence path or link target |
| `docs/runbook.md:344` | `phaze-0jpe` | Compatibility anchor for the frozen historical spike link |
| `docs/degenerate-audio.md:9` | `phaze-nkucu` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/telemetry/overhead.md:33` | `phaze-u1n7j` | Support/evidence path or link target |
| `docs/telemetry/overhead.md:51` | `phaze-m1drf` | Verbatim historical operator record (paragraph-scoped exemption) |
| `docs/telemetry/overhead.md:123` | `phaze-u1n7j` | Support/evidence path or link target |
| `docs/telemetry/overhead.md:173` | `phaze-b2qs9` | Support/evidence path or link target |
| `docs/telemetry/overhead.md:176` | `phaze-zaf2l` | Support/evidence path or link target |
| `docs/telemetry/overhead.md:176` | `phaze-spends` | Support/evidence path or link target |
| `docs/telemetry/alerting.md:17` | `phaze-zaf2l` | Support/evidence path or link target |
| `docs/telemetry/alerting.md:17` | `phaze-spends` | Support/evidence path or link target |
| `docs/telemetry/alerting.md:89` | `phaze-u1n7j` | Support/evidence path or link target |
| `docs/telemetry/alerting.md:131` | `phaze-m1drf.5` | Verbatim historical operator record (paragraph-scoped exemption) |

## Canonical retained operator records

| Surface | Retained match | Purpose |
| --- | --- | --- |
| `src/phaze/tracklist_providers/trackid.py:RIGHTS_EVIDENCE` | `phaze-gyr3l` | Exact historical authorization tuple serialized into provider snapshots and retained observations; independently pinned by the provider test. |
| `src/phaze/tracklist_providers/mixesdb.py:RIGHTS_EVIDENCE` | `phaze-gyr3l` | Exact historical authorization tuple retained as canonical source-read evidence. |

The negotiated `X-Phaze-Config-Version` header is an exact machine API identifier, not a
development label. The guard exempts only that complete, case-sensitive header token, while
similar prose and suffixed header names remain checked. Integration preparation follows the
reviewed configuration implementation `c4608099`, assembled as `956eed9a`: the supported-input
paragraph retains per-control reload timing, and the inference-batch row retains the exported
host ceiling, reduce-only live policy and 11-default/33-ensemble graph distinction. Only the
development tracking suffixes are removed from those reviewed descriptions.

## Excluded by purpose

- `tracklist_providers/trackid.py` and `mixesdb.py` retain the exact named `RIGHTS_EVIDENCE` tuple as a canonical historical operator record. These values are serialized into provider snapshots and retained observations, so rephrasing them can change persisted semantic identity. The guard exempts only those top-level record assignments, independently pinned by provider tests; current help/config/error literals in the same modules remain guarded.


- Python implementation comments and nonpublished docstrings retain debugging and decision traceability. Generated public schema summaries are separately checked.
- Jinja comments, HTML comments and JavaScript comments explain implementation rather than visible/help/attribute copy; they retain provenance. The guard does not exclude whole templates.
- `docs/design/**`, `docs/spikes/**`, `.planning/**`, dated audit/verification/quality artifacts and `docs/postgres-jit.md` retain original historical evidence and decision records. The JIT document is a measured decision report, including verbatim questions/answers, not a current configuration how-to.
- Developer workflow material (AGF, gates/isolation, Git topology, validation recipes, maintenance contracts, bead-feedback/coverage workflows) necessarily teaches bead/epic lifecycle and keeps legitimate examples.
- Machine identifiers (settings names, routes, metric/task names, migration revisions, container/secret/resource names, support file paths) remain byte-identical; the test explicitly recognizes the product-token families.
- Caller-supplied recording names, errors and other data are not scanned at runtime. A render test confirms a recording name containing a bead-like substring remains escaped and otherwise intact.

## Regression evidence

The focused guard checks authored source literals/template content, generated OpenAPI/settings
summaries, recursive CLI help, current guides and representative rendered fragments. Four injected
examples (including a title attribute and all three reported leaks) must fail detection. Internal
comments and legitimate migration/revision numbers are accepted.

Executable examples are a separate preservation boundary. The regression executes the documented
pagination block against the real pagination helpers and an isolated database. It executes the
onboarding INSERT and quarantine UPDATE against transaction-local temporary tables, so removing
`now()` parentheses fails as invalid SQL. Evaluation shell argv is tokenized, its exact launcher and
model paths are checked, inline code delimiters are balanced, and repository JSON/Markdown references
must exist. These checks preserve callable syntax and machine references rather than exempting
whole fenced blocks from the public-copy guard. Review of every changed fenced block against the
pre-cleanup version found the restored evaluation paths, SQL calls and pagination calls; remaining
changes affect comments, whitespace or diagram labels. Inline callable references such as
`configure_logging()` and scientific expressions such as `sum(increase())` also retain their original
syntax.

Numeric review compares original/current numeric tokens after excluding removed genealogy labels;
all operational quantities, dates, revisions and measured figures are preserved. Added digits in
functional names such as S3/arm64 describe the same existing technologies; removed digits belong to
bead/phase/decision labels. Tests and local-link verification are recorded in the developer handoff.
