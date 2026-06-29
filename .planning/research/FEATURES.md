# Feature Research

**Domain:** Single-user self-hosted admin console — DAG-centric "hybrid console" IA over an existing pipeline backend (phaze v7.0 UI redesign)
**Researched:** 2026-06-29
**Confidence:** HIGH (locked design + verified existing endpoints) / MEDIUM (external UI-pattern conventions, web-verified)

> **Scope note for the roadmapper.** This is a UI/IA rewrite milestone: **v7.0 adds zero backend behavior.** Every feature below maps to an *existing* router/endpoint (verified by grepping `src/phaze/routers/`). "Table stakes / differentiator / anti-feature" here means *what to build vs. what to deliberately skip when implementing each locked requirement* — not new product capabilities. The design spine (`docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md`) and the interactive prototype (`…-assets/prototype.html`) are the canonical reference; do not re-litigate the IA.
>
> **One hard caveat surfaced by this research (read first):** the prototype's **Track-ID** workspace is labeled "AcoustID → MusicBrainz recording match." **No AcoustID/MusicBrainz capability exists in the codebase** — `grep -ri 'acoustid|musicbrainz' src/phaze` is empty. The only identity signals that exist are (a) audfprint+panako **acoustic-fingerprint** match/score and (b) **rapidfuzz tracklist** match confidence. Building AcoustID→MusicBrainz would be a net-new backend feature and would violate the milestone's "no backend behavior change" boundary. See IDENT anti-features. This is the single most important scope correction for phase 59.

## How to read the category tables

Categories map 1:1 to the requirement groups and their phases:

| Category | Reqs | Phase | "The pattern" |
|----------|------|-------|---------------|
| **SHELL** | SHELL-01..05 | 57 | Three-column console shell + DAG rail-as-nav |
| **WORK** | WORK-01..05 | 58 | Stage workspaces + execution-lane capacity cards |
| **IDENT** | IDENT-01..02 | 59 | Identity workspaces (match state + visible sub-chain) |
| **REVIEW** | REVIEW-01..05 | 60 | Unified before→after diff/approve gate |
| **RECORD** | RECORD-01..04 | 61 | Full per-file record + ⌘K palette + Agents + empty state |
| **CUT** | CUT-01..04 | 62 | a11y, narrow collapse, dead-code removal, docs |

---

## Feature Landscape

### SHELL — Three-column console + DAG rail-as-nav (Phase 57)

The established pattern is the **IDE / "console" three-pane shell** (VS Code, Linear, GitLab CI, Datadog pipeline views): a persistent left **spine** that is simultaneously navigation *and* live status, a center **workspace** that swaps in place (never a full-page reload), and an on-demand right **detail** pane. The rail-as-nav is the load-bearing idea — clicking a stage swaps the center via HTMX, exactly as CI/CD consoles let you click a pipeline stage to drill into it.

#### Table Stakes

| Feature | Why Expected | Complexity | Maps to existing capability |
|---------|--------------|------------|------------------------------|
| Persistent left rail listing every stage with a selected/active highlight | Spine must always show "where am I in the flow" | LOW | New template over existing stage set; rail config is static (prototype `RAIL`) |
| Click stage → center swaps **without full-page nav** (HTMX `hx-get` + target) | Core promise ("kills tab-jumping"); page reload would defeat it | MEDIUM | Existing per-tab routes already render the content; wrap as partials |
| Live per-stage counts on the rail | Console rails show counts (queue depth, done) so the operator triages at a glance | LOW | `GET /pipeline/stats` already returns stage counts (5s poll exists) |
| `/` renders the shell with **Analyze** selected (no `/pipeline` redirect, no land-on-tab-2) | SHELL-01; fixes the headline complaint | LOW | New `/` route renders shell; retire `/`→`/pipeline` redirect |
| Header with brand (wave logo, Jura, blue) + theme toggle preserved | SHELL-04; "evolve don't reskin" | LOW | Reuse `base.html` `.dark` store + brand tokens verbatim |
| Old routes (`/pipeline`,`/proposals`,`/tracklists`,`/tags`,`/cue`,`/duplicates`,`/search`,`/preview`) redirect into shell stage state | SHELL-05; bookmarks must survive | LOW | 8 known routes confirmed present; 301/302 → shell with stage param |
| Header status strip: per-agent status dots (nox/A1/k8s) | SHELL-03; CI consoles keep global health in the chrome | LOW | `admin_agents` liveness already drives `agents_table.html` dots |

#### Differentiators

| Feature | Value Proposition | Complexity | Maps to existing capability |
|---------|-------------------|------------|------------------------------|
| Rail nodes carry a **status dot** (done ✓ / live-pulse / idle) per stage, plus amber tint on the Review&Apply group | Turns the spine into an at-a-glance "where is work stuck" view (the Mission-Control strength, kept cheaply) | LOW | Derived from existing stage counts/states |
| Nested **lane mini-rows under Analyze** (local 8/8 · A1 2/4 · k8s 12 pend) inline in the rail | Surfaces compute pressure without leaving the spine | MEDIUM | v6.0 lane/quota state already computed for the dashboard |
| Right pane updates in lockstep with rail selection (contextual, not just file-centric) | Three-pane cohesion; non-file stages get a context blurb | LOW | Prototype `paneHTML()` behavior |

#### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Drag-to-reorder / collapsible-customizable rail | "Power users like to rearrange" | Single operator, fixed pipeline; persistence + state is pure overhead | Fixed rail order = the pipeline order (it *is* the DAG) |
| Full client-side router / SPA history management | "Make swaps feel instant + deep-linkable" | Violates the no-SPA-build constraint; HTMX + `hx-push-url` covers deep-linking | HTMX swap with `hx-push-url` for the stage param only |
| Free-floating / dockable / multi-tab panes (IDE-grade) | "Looks pro" | Massive state surface for one user on one screen | Fixed three columns; right pane is the only on-demand surface |
| Animated DAG graph canvas as the home (edges, auto-layout) | The literal "DAG" word | Alt-A was explicitly rejected; a graph canvas buries per-file review | Linear **rail** is the chosen DAG representation |

---

### WORK — Stage workspaces + execution-lane capacity cards (Phase 58)

Two patterns: (1) the **work-queue table with a stage action** (a worklist: rows + a trigger button + live count), and (2) **capacity/utilization cards** — the established "runner/worker pool" widget from CI consoles and Kueue/k8s dashboards (used/total slots, a utilization bar, and a queue/quota sub-state). The k8s lane's **quota-wait vs. Inadmissible** distinction is a real Kueue concept already surfaced by v6.0.

#### Table Stakes

| Feature | Why Expected | Complexity | Maps to existing capability |
|---------|--------------|------------|------------------------------|
| Discover workspace: recent-scans table (path/found/new/status/when) + not-yet-enriched backlog count + Scan trigger | WORK-01 | LOW | `recent_scans_table.html`, `scan_progress_card.html`; `POST /pipeline/scan`, `GET /pipeline/scan/status`, `/pipeline/recover` |
| Metadata + Fingerprint workspaces: stage queue + **manual** trigger | WORK-02 (metadata stays manual — Phase 35 decision) | LOW | `POST /pipeline/extract-metadata`, `POST /pipeline/fingerprint`, `/api/v1/fingerprint/progress` |
| Three Analyze **lane cards** (local / A1 / k8s) with used/total + utilization bar | WORK-03; the operator's mental model of compute pressure | MEDIUM | v6.0 dashboard partials: `analyzing_cloud_card`, `awaiting_cloud_card`, `localqueue_card`, `staged_pushing_card` |
| k8s lane surfaces **Kueue quota-wait vs. Inadmissible** | WORK-03; the two states mean different operator actions (wait vs. fix quota) | MEDIUM | v6.0 `inadmissible_card.html`, `admission_state_card.html` already render this |
| Per-file **lane badge** + windowed progress (window N/M) on in-flight rows | WORK-04 | MEDIUM | v6.0 windowed analysis (Phase 31) + cloud_job lane state |
| Workspaces auto-refresh via the existing stats poll (no manual reload) | WORK-05 | LOW | Existing 5s `hx-trigger="every 5s"` against `/pipeline/stats` |

#### Differentiators

| Feature | Value Proposition | Complexity | Maps to existing capability |
|---------|-------------------|------------|------------------------------|
| Lane card sub-caption stating the **routing rule** ("short < 90 min · saturated", "long ≥ 90 min · headroom") | Makes duration-routing legible without docs | LOW | v6.0 duration-routing thresholds (display only) |
| Inline **Inadmissible operator alert** on the Analyze stage (amber) | Carries the Phase 54 alert into the new IA where the operator is already looking | LOW | v6.0 already emits this card on the dashboard |
| Stage **Pause / Route-rules** affordance in the workspace header | Per-stage gating already exists; surfacing it here is a cheap win | LOW | `POST /pipeline/stages/{stage}/pause|resume|priority` |

#### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Editable routing-rule / threshold UI in the Analyze lane | "Let me tune the 90-min cutoff" | That's a backend config change; deferred as WORK-06/REVIEW-06; v7.0 is read-only visualization | Show the rule as a caption; config stays env/file |
| Auto-refresh faster than ~5s / websockets / SSE firehose | "Real-time feels better" | New transport for one operator; the 5s poll already exists and is proven | Reuse the existing poll cadence |
| Per-lane historical charts / throughput graphs | "Observability" | This is an admin tool, not Grafana; adds query + chart infra | A single sparkline lives only in the file record (RECORD-01) |
| Auto-triggering Metadata extraction | "Why click?" | Explicitly a manual stage (Phase 35); auto would regress a deliberate decision | Keep the manual Extract selected/all buttons |

---

### IDENT — Identity workspaces (Phase 59)

Pattern: **match-state worklist** — each row shows a candidate match, a confidence score, and a state (matched / no-match / pending), plus a visible **multi-step sub-chain** rendered as labeled chips (Search ✓ → Scrape ✓ → Match ⏳) so a hidden async pipeline becomes legible. Tracklist matching maps cleanly to existing endpoints. **Track-ID does not** — see caveat.

#### Table Stakes

| Feature | Why Expected | Complexity | Maps to existing capability |
|---------|--------------|------------|------------------------------|
| Tracklist workspace: **Search → Scrape → Match** shown inline as a 3-step with per-set match progress, triggerable from one surface | IDENT-02 | MEDIUM | `POST /pipeline/search-tracklists`, `/scrape-tracklists`, `/match-tracklists`; `tracklists` confidence/status partials exist |
| Per-set match confidence + "N/M tracks matched" | IDENT-02 | LOW | rapidfuzz scoring + `confidence_badge.html`, `status_badge.html` |
| Track-ID workspace: per-file **identity match state + confidence** | IDENT-01 | MEDIUM | **See caveat** — surface fingerprint match + tracklist match, NOT AcoustID/MusicBrainz |

#### Differentiators

| Feature | Value Proposition | Complexity | Maps to existing capability |
|---------|-------------------|------------|------------------------------|
| Live sub-chain chips that advance as the async work completes (poll-driven) | Turns an opaque 3-call chain into a visible progress story | LOW | Existing `scan_progress.html` poll pattern in `tracklists/` |
| "live set — n/a" honest state for full-set recordings | Most of this corpus is concert sets where single-recording ID is meaningless | LOW | Existing live-set classification |

#### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| **Build AcoustID → MusicBrainz lookup** to fulfill the prototype's Track-ID label | The mockup literally says it | **Net-new backend integration** (new API client, network dep, rate limits) — violates the "no backend change" milestone boundary; capability is absent today | Re-label/repurpose Track-ID to surface the **existing** identity signals: audfprint+panako fingerprint match/score + rapidfuzz tracklist match. Flag AcoustID/MusicBrainz as a **future milestone**, not v7.0 |
| Manual match override / candidate-picker UI in Track-ID | "Let me correct a bad match" | New write path + endpoint; tracklist already has the review surface | Corrections happen in the existing Tracklist review (inline edit fields exist) |
| Cross-file-server fingerprint identity ("is this the same audio as that?") | "Dedupe across hosts" | XAGENT-01, explicitly deferred; per-agent FP DBs don't cross-match | Keep the existing per-session cross-FS notice banner |

---

### REVIEW — Unified before→after diff/approve gate (Phase 60)

Pattern: the **review/approval queue** (GitHub PR file diffs, Dependabot/Renovate batched approvals, email-triage approve/skip). Established UX: a per-item **before→after diff** (struck-through old vs. highlighted new), per-item **Approve / Edit / Skip**, a header **bulk "approve all high-confidence"** gated by a threshold, and — critically — **reversibility** (undo / audit). For dedupe, the standard is **keeper-selection** (radio: keep one, archive the rest) with an "auto-keep best quality" bulk. This collapses 5 legacy tabs (Proposals/Preview/Tags/Cue/Duplicates) into one interaction; every sub-surface already has a backend.

#### Table Stakes

| Feature | Why Expected | Complexity | Maps to existing capability |
|---------|--------------|------------|------------------------------|
| Rename/Path, Tag-write, Move each as **before→after diff** with per-row Approve/Edit/Skip | REVIEW-01; one consistent interaction across all three | MEDIUM | `proposals` (approve/reject/undo, `proposal_row`, `row_detail`); `tags` (`tag_comparison`, `inline_edit`); `execution` (`collision_block`, preview tree) |
| Bulk **"approve all high-confidence"** gated by a confidence threshold | REVIEW-02; batch approval is the whole point at 1.3k pending | MEDIUM | `PATCH /proposals/bulk`, `bulk_actions.html`; tracklists `reject-low` shows threshold-gating exists |
| Dedupe: duplicate groups with **radio keeper-selection** (others archived) + bulk **auto-keep highest quality** | REVIEW-03 | MEDIUM | `duplicates`: `POST /{group}/resolve`, `/resolve-all`, `comparison_table`, `group_card` |
| Cue: generated `.cue` **preview + Approve**, gated on a matched tracklist | REVIEW-04 | LOW | `cue`: `POST /{tracklist_id}/generate`, `cue_row`, `cue_status` |
| Every applied change **audited + reversible** | REVIEW-05; irreplaceable collection, copy-verify-delete + undo | LOW | `execution/audit_log`, `audit_row`; existing undo endpoints (`/undo`, `/{group}/undo`, `/undo-all`) |

#### Differentiators

| Feature | Value Proposition | Complexity | Maps to existing capability |
|---------|-------------------|------------|------------------------------|
| **One diff component** reused across rename/tag/move (struck `del` + highlighted `add`) | Visual + cognitive consistency; the prototype's `diffRow()` is one function for all three | LOW | Single Jinja partial over three existing data sources |
| Confidence badge per row (e.g. 0.91 green / 0.42 amber) driving the bulk gate | Makes "high-confidence" concrete and trustworthy before a bulk action | LOW | Proposal/match confidence scores already stored |
| Inline **Edit** that opens the existing field editor in place | Avoids a context switch to a separate edit screen | LOW | `tags/inline_edit`, proposals edit, tracklist `inline_edit_field` all exist |

#### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Auto-apply high-confidence with no human click | "Trust the model, save time" | Breaks the core human-in-the-loop value for an irreplaceable archive; nothing moves without review | Bulk-approve still requires the operator to press the button |
| Per-stage configurable threshold + override UI | "Different stages need different cutoffs" | Explicitly deferred (REVIEW-06); config surface + persistence for one user | Ship one sensible fixed threshold in v7.0 |
| Rich 3-way / character-level diff viewer (Monaco-grade) | "Better diffs" | Filenames/tags are short strings; struck-vs-highlighted is sufficient and already in the prototype | Simple before→after string diff |
| Approve-with-no-undo "fast path" | "Power user speed" | Removes the reversibility safety net on physical file ops | Undo/audit is mandatory on every applied change |

---

### RECORD — Full per-file record + ⌘K palette + Agents + empty state (Phase 61)

Four sub-patterns, all well-established:
- **Detail drawer / slide-over** (full record over the shell).
- **⌘K command palette** (cmdk/Linear/Vercel convention): fuzzy search across entity buckets + quick commands, full keyboard nav. *Web-verified table stakes:* Cmd/Ctrl+K to open, Esc to close, ↑/↓ to move, Enter to select; bucket results (Files / Tracklists / Commands); fuzzy/typo-tolerant; promote recents.
- **Ephemeral/Job-based identity** in the Agents view (the k8s burst lane is a transient Kueue Job, not a heartbeating daemon — must classify as "ephemeral/never-heartbeats," never "perpetually-DEAD").
- **First-run empty state** for a scan-driven tool (point-at-a-directory CTA + live scan progress).

#### Table Stakes

| Feature | Why Expected | Complexity | Maps to existing capability |
|---------|--------------|------------|------------------------------|
| Full per-file record as a slide-over: identity, metadata diff, windowed multi-lane timeline, this file's pending approvals (inline-approvable), history | RECORD-01 | MEDIUM | `proposals/analysis_timeline.html` (Phase 31 windowed), `row_detail`, tag comparison, audit history — all exist |
| ⌘K opens, Esc closes, ↑/↓ navigate, Enter selects | RECORD-02; non-negotiable palette keys (web-verified) | MEDIUM | New palette UI over `POST /search` |
| ⌘K searches **files / tracklists / artists** in labeled buckets | RECORD-02 | MEDIUM | Existing unified search is a 3-entity UNION (file/tracklist/discogs) — already bucketed |
| ⌘K **quick commands**: scan, jump-to-stage, jump-to-review-queue, open Agents | RECORD-02 | LOW | Commands just call `go(stage)` / existing triggers |
| Agents page: local + A1 as **heartbeating** agents; k8s burst as **ephemeral Job-based identity** (liveness from in-flight Kueue workloads), never DEAD | RECORD-03; carries v6.0 KDEPLOY-04 intent | MEDIUM | `admin/agents.html`, `agents_table`; v6.0 ephemeral-identity classification already specced |
| First-run **empty state**: "point phaze at your music" + directory + agent selector + live scan progress | RECORD-04 | LOW | `scan_path_picker`, `scan_progress_card`; `agent-roots` endpoint; scan trigger |

#### Differentiators

| Feature | Value Proposition | Complexity | Maps to existing capability |
|---------|-------------------|------------|------------------------------|
| File record's **windowed multi-lane analysis timeline** (BPM/key/energy sparklines over set windows) | The concrete payoff of the "per-file full analysis" goal; unique to this corpus of long sets | MEDIUM | Phase 31 windowed analysis data + `analysis_timeline.html` |
| "This file's pending approvals" inline-approvable inside the record | Lets the operator resolve a file end-to-end without hopping to Review queues | LOW | Reuse the same approve endpoints from one place |
| ⌘K **recents** bucket (recently opened files/stages) | Web-verified best practice — promote recents before long-tail matches | LOW | Client-side recents (Alpine/localStorage); no backend |
| Agents amber **"ephemeral · N active workloads"** card with an explicit "no heartbeat — Job-based identity" note | Prevents the operator from misreading a healthy burst lane as broken | LOW | v6.0 Kueue workload counts |

#### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| ⌘K as a full natural-language / AI query interface | "Type anything" | Explicitly deferred (NLQ out of scope); huge surface; the existing search is keyword/FTS | Fuzzy keyword search over the existing 3-entity UNION + a fixed command list |
| ⌘K nested/breadcrumb command trees, command aliases, scripting | "Raycast parity" | Over-engineering for one user with ~6 commands | Flat bucketed list: Files · Tracklists · Commands |
| Showing the k8s burst lane as a DEAD agent when no Job is running | Reusing the heartbeat liveness model uniformly | The whole point of RECORD-03 — a transient Job has nothing to heartbeat; DEAD is a false alarm | Classify as "ephemeral/never"; liveness = in-flight Kueue workloads |
| Multi-step onboarding wizard / sample-data seeding for first run | "Polished onboarding" | One operator, one real corpus; a single scan CTA is the whole job | Single point-at-a-directory CTA + live scan progress |
| Editable per-file record (rename/move directly from the record) bypassing the gate | "Faster" | Routes around the approval/audit gate | Record's approvals go through the same Approve endpoints |

---

### CUT — Polish & cutover (Phase 62)

Pattern: **accessible keyboard-first app shell** + **responsive collapse** + **dead-code retirement** + **docs**. For a keyboard-driven console, a11y is table stakes, not polish: focus management, ARIA on the rail, skip link. Narrow-width handling is **rail-collapses-to-icons** (desktop tool — no mobile reflow).

#### Table Stakes

| Feature | Why Expected | Complexity | Maps to existing capability |
|---------|--------------|------------|------------------------------|
| Keyboard nav for the rail + ⌘K, visible focus states, skip link, ARIA on the DAG | CUT-01; at parity-or-better than today | MEDIUM | New shell markup; ARIA `role`/`aria-current` on rail nodes |
| Remove dead templates/routers/partials from the old tabbed UI once superseded | CUT-02; no orphaned dead code | MEDIUM | Retire `search/page.html`, standalone `*/list.html` shells, old `base.html` nav row once shell is live |
| Update user docs + per-service README for the new IA | CUT-03 | LOW | Docs follow the new rail model |
| Rail **collapses to icons** at narrow widths | CUT-04 | LOW | CSS/Alpine breakpoint on the rail width |

#### Differentiators

| Feature | Value Proposition | Complexity | Maps to existing capability |
|---------|-------------------|------------|------------------------------|
| `aria-current="page"` + roving-tabindex on rail nodes so the spine is fully keyboard-operable | Makes the nav-spine usable without a mouse; matches the keyboard-first ⌘K ethos | LOW | Shell markup only |

#### Anti-Features

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Full mobile/touch responsive layout | "Works on my phone" | SHELL-06 deferred; single-user desktop tool | Narrow-desktop rail-collapse only |
| Full first-class **light** theme C3 treatment | "Theme parity" | RECORD-05 deferred; dark is primary for v7.0 | Keep the existing toggle working; dark is the designed surface |
| WCAG AAA / screen-reader certification effort | "Accessibility" | Disproportionate for a single known operator | "Baseline + parity-or-better" (CUT-01 wording) |
| Big-bang delete of legacy routers before shell ships | "Clean cutover" | Risk of breaking redirects (SHELL-05) | Retire **after** superseded + redirects verified |

---

## Feature Dependencies

```
SHELL (57) app shell + rail-as-nav + /pipeline/stats poll
   └──required by──> WORK (58) stage workspaces render in the center pane
                        └──required by──> IDENT (59) identity workspaces (same workspace pattern)
                        └──required by──> REVIEW (60) Review&Apply stages live in the rail
   └──required by──> RECORD (61) full record opens *over* the shell; ⌘K lives in the header

WORK lane cards ──require──> v6.0 local/A1/k8s routing + Kueue admission state (already shipped)
RECORD windowed timeline ──requires──> Phase 31 windowed analysis (already shipped)
REVIEW diff/bulk ──requires──> existing proposals/tags/execution/duplicates/cue endpoints (already shipped)
⌘K (RECORD-02) ──requires──> existing POST /search 3-entity UNION (already shipped)

CUT (62) ──depends on──> all of SHELL/WORK/IDENT/REVIEW/RECORD existing (it retires their predecessors)

IDENT Track-ID (59) ──does NOT depend on──> any AcoustID/MusicBrainz capability (ABSENT — do not build)
```

### Dependency Notes

- **SHELL is the keystone.** Every other phase renders into the shell; 57 must land first and be stable (rail nav + center swap + stats poll).
- **WORK/IDENT/REVIEW reuse one workspace pattern** (Jura header + count + action + table/cards). Build it once in 58, reuse in 59–60.
- **REVIEW reuses one diff component** across rename/tag/move — three existing data sources, one partial.
- **CUT must come last** — it deletes the old templates/routers that the redirects (SHELL-05) and any not-yet-migrated surface still depend on.
- **IDENT-01 has no backend dependency to build** — and must not grow one. Surface existing fingerprint + tracklist signals.

## MVP Definition

(Here "MVP" = the must-land core of the v7.0 redesign; everything is already backend-complete.)

### Launch With (the 25 locked requirements)

- [ ] **SHELL-01..05** — the shell, rail-as-nav, `/` default, brand/theme preserved, legacy redirects. *Without this nothing else has a home.*
- [ ] **WORK-01..05** — Discover/Metadata/Fingerprint/Analyze workspaces + 3 lane cards + live poll. *The pipeline's center of gravity.*
- [ ] **IDENT-01..02** — Track-ID (existing signals) + Tracklist 3-step. *Identity legibility.*
- [ ] **REVIEW-01..05** — unified diff/approve gate + dedupe keeper + cue preview + reversibility. *The core human-in-the-loop value.*
- [ ] **RECORD-01..04** — full record + ⌘K + Agents (ephemeral k8s) + empty state. *Per-file payoff + global surfaces.*
- [ ] **CUT-01..04** — a11y, dead-code removal, docs, narrow collapse. *Productionizes the rewrite.*

### Add After Validation (v7.x — already enumerated as deferred)

- [ ] **REVIEW-06** — per-stage configurable confidence thresholds + override UI.
- [ ] **WORK-06** — `cloud_phase`-driven admission-state cards as Analyze sub-states (v6.0 KROUTE-06).
- [ ] **RECORD-05** — full first-class C3 light theme.

### Future Consideration (v8+)

- [ ] **AcoustID → MusicBrainz Track-ID** — a *real new backend capability*; the only honest path to the prototype's literal Track-ID label. Net-new, out of v7.0 scope.
- [ ] **SHELL-06** — mobile/touch layout.
- [ ] Cross-file-server fingerprint identity (XAGENT-01).

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Rail-as-nav shell + center swap (SHELL-01/02) | HIGH | MEDIUM | P1 |
| Legacy route redirects (SHELL-05) | HIGH | LOW | P1 |
| Unified before→after diff gate (REVIEW-01/02) | HIGH | MEDIUM | P1 |
| Reversibility/audit on every apply (REVIEW-05) | HIGH | LOW | P1 |
| Analyze lane cards + Inadmissible (WORK-03) | HIGH | MEDIUM | P1 |
| ⌘K palette (RECORD-02) | HIGH | MEDIUM | P1 |
| Ephemeral k8s Agents identity (RECORD-03) | MEDIUM | MEDIUM | P1 |
| Full per-file record w/ windowed timeline (RECORD-01) | HIGH | MEDIUM | P1 |
| First-run empty state (RECORD-04) | MEDIUM | LOW | P2 |
| a11y + narrow collapse (CUT-01/04) | MEDIUM | MEDIUM | P2 |
| Dead-code removal + docs (CUT-02/03) | MEDIUM | MEDIUM | P2 |
| Track-ID via existing signals (IDENT-01) | MEDIUM | MEDIUM | P2 |
| Per-stage threshold override (REVIEW-06) | LOW | MEDIUM | P3 (deferred) |
| AcoustID/MusicBrainz lookup | LOW (this milestone) | HIGH | P3 (out of scope) |

## Competitor / Prior-Art Feature Analysis

| Pattern | Prior art A | Prior art B | phaze v7.0 approach |
|---------|-------------|-------------|---------------------|
| Pipeline stage drill-down | GitLab CI stage view | Datadog CI Visibility | Left **rail-as-nav**; click swaps center via HTMX |
| Command palette | Linear / Vercel (cmdk) | Raycast | Flat bucketed ⌘K (Files/Tracklists/Commands) over existing FTS; Esc/↑↓/Enter |
| Review/approve queue | GitHub PR diffs | Dependabot batched approvals | One before→after diff + per-item Approve/Edit/Skip + bulk high-conf |
| Worker/capacity cards | k8s/Kueue dashboards | CI runner pools | Three lane cards (local/A1/k8s) with quota-wait vs. Inadmissible |
| Dedupe keeper-select | Photo-library dedupers | Email "keep one" | Radio keeper + auto-keep-best-quality bulk |
| Ephemeral job identity | Kueue Workload status | k8s Job (not Pod) lifecycle | "Ephemeral · N active workloads", never DEAD |

## Sources

- Locked design spec: `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` (HIGH)
- Interactive prototype: `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` (HIGH)
- v7.0 requirements: `.planning/REQUIREMENTS.md` (HIGH)
- Existing endpoint inventory: grep of `src/phaze/routers/*.py` and `src/phaze/templates/**` (HIGH — every feature traced to a present route/partial)
- Absence check: `grep -ri 'acoustid|musicbrainz' src/phaze` returns empty (HIGH — Track-ID/MusicBrainz is net-new, not existing)
- [Command Palette Pattern — UX Patterns for Developers](https://uxpatterns.dev/patterns/advanced/command-palette) (MEDIUM)
- [Command palettes for the web — Rob Dodson](https://robdodson.me/posts/command-palettes/) (MEDIUM)
- [Build a Command Palette: Cmd+K Like Linear and Vercel](https://www.techinterview.org/post/3233475212/build-command-palette-cmd-k/) (MEDIUM)
- [Monitor CI/CD on AWS CodePipeline with Datadog CI Visibility](https://www.datadoghq.com/blog/aws-codepipeline-ci-visibility/) (MEDIUM)
- [GitLab CI/CD pipelines](https://docs.gitlab.com/ci/pipelines/) (MEDIUM)

---
*Feature research for: phaze v7.0 DAG-centric hybrid console (UI/IA rewrite over existing backend)*
*Researched: 2026-06-29*
