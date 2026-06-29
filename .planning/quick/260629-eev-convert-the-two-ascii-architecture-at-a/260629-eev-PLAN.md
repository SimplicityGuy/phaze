---
phase: quick-260629-eev
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - docs/cloud-burst.md
  - docs/k8s-burst.md
autonomous: true
requirements: [DOC-MERMAID-01]
must_haves:
  truths:
    - "docs/cloud-burst.md renders its 'Architecture at a glance' topology as a mermaid flowchart instead of ASCII box-drawing"
    - "docs/k8s-burst.md renders its 'Architecture at a glance' topology as a mermaid flowchart instead of ASCII box-drawing"
    - "Every host, service, object name, port, and edge label from the original ASCII is preserved verbatim — zero semantic loss"
    - "The PHAZE_CLOUD_TARGET=local caption is an italic markdown line below each mermaid block, not inside the diagram"
  artifacts:
    - path: "docs/cloud-burst.md"
      provides: "mermaid flowchart LR replacing the ASCII 'Architecture at a glance' block"
      contains: "```mermaid"
    - path: "docs/k8s-burst.md"
      provides: "mermaid flowchart LR replacing the ASCII 'Architecture at a glance' block"
      contains: "```mermaid"
  key_links:
    - from: "docs/cloud-burst.md mermaid block"
      to: "edge labels"
      via: "mermaid arrow labels -->|\"...\"|"
      pattern: "rsync over SSH"
    - from: "docs/k8s-burst.md mermaid block"
      to: "edge labels"
      via: "mermaid arrow labels -->|\"...\"|"
      pattern: "the ONLY result channel"
---

<objective>
Convert exactly two ASCII "Architecture at a glance" diagrams to mermaid `flowchart LR`
blocks — one in `docs/cloud-burst.md`, one in `docs/k8s-burst.md`.

Purpose: ASCII box-drawing diagrams do not render in Markdown viewers and are hard to
maintain. The repo already uses mermaid for architecture diagrams (`docs/architecture.md`).
These two docs are the last holdouts. The conversion must be lossless: every host, service,
object name, port, and edge phrase survives verbatim.

Output: Two edited Markdown files, each with its ASCII topology block replaced by a
`mermaid` block plus an italic config-note caption beneath it.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@./CLAUDE.md

<scope_lock>
This is a surgical, Markdown-only change. Touch ONLY the two fenced blocks named below.

DO edit:
- `docs/cloud-burst.md` — the plain ``` ASCII block under "## Architecture at a glance" (lines ~30-47)
- `docs/k8s-burst.md` — the plain ``` ASCII block under "## Architecture at a glance" (lines ~32-50)

DO NOT touch (out of scope, leave byte-for-byte unchanged):
- `docs/project-structure.md`, any directory tree, any ASCII table
- any bash / yaml / hcl / jsonc / sql / text CLI fenced block in either file
- `docs/architecture.md` (reference only — read it to match mermaid style)
- anything under `docs/superpowers/`
- the prose, headings, blockquotes, "Key invariants", step sections, or "See also" of either file
</scope_lock>

<mermaid_style_reference>
The project's existing mermaid convention (from `docs/architecture.md`):
- Fenced as ```mermaid
- Node labels are double-quoted: `API["🚀 FastAPI :8000<br/>UI + /api/v1"]`
- Cylinder (DB) nodes use `[("...")]`: `PG[("🐘 PostgreSQL 18<br/>:5432")]`
- Edge labels use the pipe form: `API -->|HTTP /api/internal/agent| AGENT`
- `<` / `>` inside labels are escaped as `&lt;` / `&gt;` (e.g. `phaze-agent-&lt;id&gt;`)

For THIS conversion use `flowchart LR` (left-to-right) with `subgraph id["Title"] ... end`
groupings for the host/cluster boundaries. Emoji are optional — match the spirit, not a
mandate. Edge labels with special chars MUST be quoted: `A -->|"rsync over SSH"| B`.
</mermaid_style_reference>

<source_block_cloud_burst>
The CURRENT ASCII block in `docs/cloud-burst.md` (the verbatim content to convert — every
token below must survive into the mermaid):

  Outer frame title: `Tailscale tailnet (default-deny grants ACL)`
  Group 1 — `nox (file server)`: runs `docker-compose.agent.yml`
    (worker+watcher+fprint+media)
  Group 2 — `OCI A1 (compute agent)`: runs `docker-compose.cloud-agent.yml`,
    `worker (kind=compute`, `no media, scratch volume`, `-arm64 image)`
  Group 3 — `lux (application server)`: `api(:8000)` ·
    `Postgres(:5432 app ORM + saq_jobs broker)` · `Redis(:6379)`;
    `controller worker (stage_cloud_window cron)`;
    `broker role 'phaze_broker' → saq_jobs ONLY (least-privilege)`
  Edges:
    - `nox → A1:22` labeled `rsync over SSH`
    - `A1 → lux:{5432,6379,8000}` labeled `HTTP API + saq_jobs + cache`
  Caption (move OUT of diagram to italic line below):
    `PHAZE_CLOUD_TARGET=local ⇒ long files route LOCAL, staging cron no-ops,
     backfill rejected, A1 idle. (all-local)`
</source_block_cloud_burst>

<source_block_k8s_burst>
The CURRENT ASCII block in `docs/k8s-burst.md` (the verbatim content to convert):

  Outer frame title: `transport-agnostic mesh (Tailscale OR WireGuard)`
  Group 1 — `lux (application server / control plane)`: `api(:8000)` · `Postgres` · `Redis`;
    `controller worker:` with children:
      `s3_staging`, `submit_cloud_job`, `reconcile_cloud_jobs (*/5 cron)`,
      `LocalQueue probe (startup)`
  Group 2 — `x64 Kueue cluster`, `namespace: phaze`:
    `ResourceFlavor phaze-cpu`, `ClusterQueue phaze-cq`, `LocalQueue phaze-lq`,
    `SA/Role/RoleBinding`, `Secret phaze-agent-token`,
    `one-shot pod (presign GET → analyze → POST result → exit)`
  Edges:
    - `s3_staging` labeled `presign PUT/GET` → `S3 bucket`
    - `submit_cloud_job` labeled `kube POST` → `suspended batch Job`
    - `Kueue admits` → `one-shot pod`
    - `one-shot pod` labeled `POST /api/internal/agent/analysis/{file_id} (the ONLY result channel)` → `controller` (the `POST /api/internal/agent/analysis/{file_id}` callback target in the lux control plane)
  Caption (move OUT of diagram to italic line below):
    `PHAZE_CLOUD_TARGET=local ⇒ long files route LOCAL, no kube submit, no S3 staging. (all-local)`
</source_block_k8s_burst>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Replace both ASCII "Architecture at a glance" blocks with mermaid flowcharts</name>
  <files>docs/cloud-burst.md, docs/k8s-burst.md</files>
  <action>
Edit each file's "## Architecture at a glance" fenced block ONLY (per the scope_lock and
source_block_* contracts in context). For each of the two files:

1. Replace the plain ``` ASCII box-drawing block with a ```mermaid block using
   `flowchart LR`. Use `subgraph id["Title"] ... end` for each host/cluster grouping named
   in the source_block contract. Use the outer-frame title as the diagram's framing — either
   as a top-level comment line `%% Tailscale tailnet (default-deny grants ACL)` immediately
   after `flowchart LR`, or as an enclosing subgraph; do not drop it.

2. PRESERVE VERBATIM every host name, service name, object name, port, and config token from
   the source_block contract. The edge phrases — "rsync over SSH", "HTTP API + saq_jobs + cache"
   (cloud-burst); "presign PUT/GET", "kube POST", "Kueue admits",
   "POST /api/internal/agent/analysis/{file_id} (the ONLY result channel)" (k8s-burst) —
   become mermaid edge labels via the `-->|"label"|` pipe form (always double-quoted because
   they contain spaces, slashes, parens, and braces). For the cloud-burst node-to-node edges,
   keep the destination tokens `A1:22` and `lux:{5432,6379,8000}` somewhere in the label or
   adjacent node text so the routing detail is not lost (e.g. edge label
   `"rsync over SSH (nox → A1:22)"` and `"HTTP API + saq_jobs + cache (A1 → lux:{5432,6379,8000})"`),
   matching the original semantics.

3. Any node OR subgraph-title text containing special chars — `{file_id}`, `:`, `/`, `(`, `)`,
   `·`, `→`, `'`, `{`, `}`, `*`, `+`, `=` — MUST be a double-quoted label:
   `n1["api(:8000)"]`, and subgraph titles use the quoted id form
   `subgraph lux["lux (application server)"]`. Escape any literal `<`/`>` as `&lt;`/`&gt;`
   per the project mermaid style. Use the `→` and `·` characters verbatim inside quoted
   labels (they are fine inside quotes). For the controller-worker children in k8s-burst
   (s3_staging / submit_cloud_job / reconcile_cloud_jobs / LocalQueue probe) and the lux/cluster
   one-shot-pod callback, model them as nodes inside their subgraph with the edges above.

4. Move the `PHAZE_CLOUD_TARGET=local ⇒ ...` caption OUT of the diagram. Place it as a single
   italic Markdown line directly BELOW the closing ``` of the mermaid block, wrapping the exact
   wording in `_..._` (e.g.
   `_PHAZE_CLOUD_TARGET=local ⇒ long files route LOCAL, staging cron no-ops, backfill rejected, A1 idle. (all-local)_`
   for cloud-burst, and the k8s-burst variant verbatim). Preserve the `⇒` and the `(all-local)`
   suffix exactly. It is a config note, not topology — it does NOT belong inside the flowchart.

5. Do NOT alter the surrounding prose, the "## Architecture at a glance" heading, "Key
   invariants", any other fenced block, or anything else in either file. Leave the
   `<!-- generated-by: gsd-doc-writer -->` line on line 1 untouched.

Do NOT inline-execute pre-commit with `--no-verify` at any point.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze && \
      test "$(grep -c '```mermaid' docs/cloud-burst.md)" -ge 1 && \
      test "$(grep -c '```mermaid' docs/k8s-burst.md)" -ge 1 && \
      test "$(grep -c 'flowchart LR' docs/cloud-burst.md)" -ge 1 && \
      test "$(grep -c 'flowchart LR' docs/k8s-burst.md)" -ge 1 && \
      ! grep -q '─\|│\|┌\|┐\|└\|┘\|▶\|├' docs/cloud-burst.md && \
      ! grep -q '─\|│\|┌\|┐\|└\|┘\|▶\|├' docs/k8s-burst.md && \
      grep -q 'rsync over SSH' docs/cloud-burst.md && \
      grep -q 'the ONLY result channel' docs/k8s-burst.md && \
      grep -q '_PHAZE_CLOUD_TARGET=local' docs/cloud-burst.md && \
      grep -q '_PHAZE_CLOUD_TARGET=local' docs/k8s-burst.md && \
      echo "STRUCTURE_OK"</automated>
  </verify>
  <done>
Both files have a ```mermaid `flowchart LR` block under "## Architecture at a glance"; no
box-drawing chars (─│┌┐└┘▶├) remain anywhere in either file; every host/service/object/port/edge
token from the source_block contracts is present verbatim; each mermaid block is well-formed
(balanced `subgraph`/`end`, every edge uses `-->`, every special-char label and subgraph title
is double-quoted); the PHAZE_CLOUD_TARGET=local caption sits as an italic `_..._` line directly
below each mermaid block (not inside it); `pre-commit run --files docs/cloud-burst.md docs/k8s-burst.md`
passes with no `--no-verify`; nothing else in either file changed.
  </done>
</task>

</tasks>

<verification>
Final phase checks (run from repo root `/Users/Robert/Code/public/phaze`):

1. Mermaid blocks present and ASCII gone:
   `grep -c '```mermaid' docs/cloud-burst.md docs/k8s-burst.md` — each ≥ 1.
   `grep -nE '─|│|┌|┐|└|┘|▶|├' docs/cloud-burst.md docs/k8s-burst.md` — no matches.

2. Lossless tokens — spot-check a sample survives verbatim in each file:
   cloud-burst: `docker-compose.agent.yml`, `docker-compose.cloud-agent.yml`, `kind=compute`,
   `-arm64 image`, `scratch volume`, `api(:8000)`, `Postgres(:5432 app ORM + saq_jobs broker)`,
   `Redis(:6379)`, `stage_cloud_window cron`, `phaze_broker`, `saq_jobs ONLY`, `rsync over SSH`,
   `A1:22`, `lux:{5432,6379,8000}`, `HTTP API + saq_jobs + cache`.
   k8s-burst: `namespace: phaze`, `ResourceFlavor phaze-cpu`, `ClusterQueue phaze-cq`,
   `LocalQueue phaze-lq`, `SA/Role/RoleBinding`, `Secret phaze-agent-token`,
   `s3_staging`, `submit_cloud_job`, `reconcile_cloud_jobs (*/5 cron)`, `LocalQueue probe (startup)`,
   `presign PUT/GET`, `kube POST`, `Kueue admits`,
   `POST /api/internal/agent/analysis/{file_id} (the ONLY result channel)`,
   `presign GET → analyze → POST result → exit`.

3. Mermaid validity (manual scan): `subgraph` count == `end` count in each block; every edge is
   `-->`; every label containing `:` `/` `(` `)` `·` `→` `{` `}` `*` `+` `=` is double-quoted;
   no literal unescaped `<`/`>`.

4. Captions relocated: `grep -n '_PHAZE_CLOUD_TARGET=local' docs/cloud-burst.md docs/k8s-burst.md`
   shows each as an italic line; confirm by eye it is BELOW the closing ``` of the mermaid block.

5. Out-of-scope untouched: `git diff --stat` lists ONLY `docs/cloud-burst.md` and
   `docs/k8s-burst.md`. `docs/architecture.md`, `docs/project-structure.md`, and
   `docs/superpowers/` are unchanged.

6. Hooks: `pre-commit run --files docs/cloud-burst.md docs/k8s-burst.md` passes
   (trailing-whitespace, end-of-file-fixer, etc.). NEVER use `--no-verify`.
</verification>

<success_criteria>
- Both "Architecture at a glance" sections render as `mermaid` `flowchart LR` diagrams with
  `subgraph`/`end` host groupings.
- Zero semantic loss: every host, service, object name, port, and edge label preserved verbatim.
- Both PHAZE_CLOUD_TARGET=local captions are italic Markdown lines below their mermaid blocks.
- No box-drawing characters remain in either file.
- `git diff --stat` touches only the two target files.
- pre-commit hooks pass with no `--no-verify`.
</success_criteria>

<output>
Create `.planning/quick/260629-eev-convert-the-two-ascii-architecture-at-a/260629-eev-SUMMARY.md` when done.
</output>
