# 📖 Phaze Documentation

**Phaze is a music collection organizer that analyzes and AI-renames music and concert files behind a human-in-the-loop approval UI.**

This is the repository's documentation index. For the project overview, quick start, and technology stack, see the [main README](../README.md).

The documents listed below describe the current product and maintained developer contract. Dated
design specifications, spikes, and most of the `.planning/` tree are historical evidence: retain
their measurements and conclusions, but do not treat an old “current” label as the live backlog.
The [planning boundary](../.planning/README.md) and [specification boundary](superpowers/specs/README.md)
explain that distinction; the [documentation audit](documentation-audit-2026-08-19.md) records the
2026-08-19 historical snapshot, not the current inventory.
Unresolved repository-local targets in historical snapshots are intentionally classified
`historical_by_boundary`, not presented as current navigation.

## 🏁 Getting Started

| Document | Purpose |
| -------- | ------- |
| **[Quick Start Guide](quick-start.md)** | 🚀 Get Phaze running in minutes |
| **[Configuration](configuration.md)** | ⚙️ Environment variables and settings reference |
| **[Agentic Git Flow](AGF.md)** | 🐝 Beadhive roles, lifecycle, review, integration, and test isolation |

## 🧰 Development and Validation

| Document | Purpose |
| -------- | ------- |
| **[Gates and Isolation](gates-and-isolation.md)** | 🧪 Validation-boundary evidence, PostgreSQL/Redis seat isolation, and trustworthy gate output |
| **[Git Topology and Verification](git-topology-and-verification.md)** | 🌳 Worktree topology, verification fidelity, and operator-attribution rules |
| **[Public Just Recipe Contract](just-recipe-contract.md)** | 🔧 Complete executable inventory of public recipes and their consumers |
| **[Repository Maintenance Contract](repository-maintenance-contract.md)** | 🧹 Reproducible code-comment and documentation inventory/classification rules |
| **[Maintenance Audit — 2026-09-10](maintenance-audit-2026-09-10.md)** | ✅ Final inventory, dispositions, limitations, and validation evidence for the maintenance sprint |
| **[Structural Maintenance Rebaseline — 2026-09-11](structural-maintenance-rebaseline-2026-09-11.md)** | 🧭 Post-maintenance boundaries, compatibility inventories, invariants, and GO dispositions for the structural follow-on |
| **[Test Bucket Mapping](../tests/BUCKETS.md)** | 🪣 Historical reorganization baseline plus the live CI bucket and shard boundaries |
| **[Watcher Developer Guide](../src/phaze/agent_watcher/README.md)** | 👁️ Watcher flow, bootstrap, configuration, import boundary, and operational behavior |

## 📐 Reference

| Document | Purpose |
| -------- | ------- |
| **[Architecture Overview](architecture.md)** | 🏛️ System design, data flow, distributed agents, and Mermaid diagrams |
| **[API Reference](api.md)** | 🔌 REST and HTMX UI endpoints, plus the distributed Agent API |
| **[Database Schema & Migrations](database.md)** | 🗄️ PostgreSQL schema and Alembic migrations |
| **[Project Structure](project-structure.md)** | 📁 Codebase layout and module organization |
| **[Essentia Analysis & Replacement](essentia-analysis.md)** | 🔬 Where essentia is used, its true compute profile (DSP/decode-bound), the feature surface to preserve, and why no lighter drop-in replacement exists |
| **[1001Tracklists Scraping](tracklist-scraping.md)** | 🕸️ SSRF allow-list + redirect recheck, the shared whole-host rate limiter and its single-replica limit, the render engine's Turnstile handling, the poisoned-result skip, and the two TTL caches |
| **[Architecture Decisions](design/)** | 🧭 Accepted decisions and their status |
| **[Historical Evidence Audit](historical-evidence-audit-2026-09-11.md)** | 🧾 Reproducible identifier, numeric-equivalence, archive-boundary, link, Mermaid, and graph-reference reconciliation |
| **[UI Design Reference](ui-design-reference.md)** | 🎛️ Current production visual and interaction contract |
| **[UI Reference Fixtures](ui-reference-fixtures.html)** | 🖼️ Maintained visual fixture catalogue used by the UI reference |

## 🎨 Design System

| Document | Purpose |
| -------- | ------- |
| **[Design System](../design/DESIGN_SYSTEM.md)** | 🎨 Tokens, components, accessibility, and interaction patterns |
| **[Resonant Precision](../design/resonant-precision.md)** | 🎚️ Design language and implementation direction |

## 📈 Telemetry

| Document | Purpose |
| -------- | ------- |
| **[Alerting](telemetry/alerting.md)** | 🚨 Alert rules and operator response |
| **[Exporter](telemetry/exporter.md)** | 📤 Metrics export contract and configuration |
| **[Metric Catalogue](telemetry/metric-catalogue.md)** | 📊 Maintained metric names, labels, and meanings |
| **[Overhead](telemetry/overhead.md)** | ⏱️ Telemetry cost and measurement guidance |
| **[Traces](telemetry/traces.md)** | 🔎 Trace spans and correlation contract |

## 🚀 Operations

| Document | Purpose |
| -------- | ------- |
| **[Deployment Guide](deployment.md)** | 🐳 Docker Compose deploy, image pipeline, and remote agents |
| **[Operator Runbook](runbook.md)** | 🛠️ Force-local incident revert, reading the N backend lanes, spillover, and per-backend `_FILE` secrets |
| **[Cloud Burst](cloud-burst.md)** | ☁️ OCI A1 compute-agent deploy, Tailscale ACL, broker role, enabled via a `kind="compute"` entry in `backends.toml` |
| **[Multi-Compute Agents](multi-compute.md)** | ⚙️ Add a 2nd+ compute agent, mixed arm64/x86 rank/cap cost-tiering, per-agent compose, N-lane read-out |
| **[Agent Queue Lanes](agent-queue-lanes.md)** | 🛤️ Per-lane file-server workers (analyze/meta/io), core budget + thread pinning, per-lane heartbeats, legacy-queue drain runbook |
| **[Kubernetes Burst](k8s-burst.md)** | ☸️ Kueue Job-runner runbook: ResourceFlavor/ClusterQueue/LocalQueue, namespaced RBAC, `_FILE` Secret, S3 staging, enabled via a `kind="kueue"` entry in `backends.toml` |
| **[arm64 Agent Image](arm64-agent-image.md)** | 🦾 `Dockerfile.agent-arm64` build recipe for the Ampere A1 compute-agent image: Python 3.13 exception, essentia built from source, tag naming |

## 🗃️ Repository Boundaries and Fixtures

| Document | Purpose |
| -------- | ------- |
| **[Repository Conventions](../CONVENTIONS.md)** | 🔒 No-local-identifiers policy and safe placeholders |
| **[Planning Archive Boundary](../.planning/README.md)** | 🕰️ What remains historical evidence and what is maintained |
| **[Spike Archive Boundary](spikes/README.md)** | 🧪 How to read retained spike evidence and unresolved historical links |
| **[Specification Archive Boundary](superpowers/specs/README.md)** | 📜 Status of retained design specifications |
| **[Telemetry Measurement Boundary](telemetry/measurements/README.md)** | 📏 Status and interpretation of retained telemetry measurements |
| **[Rendered Tracklist Fixtures](../tests/identify/fixtures/tracklist_render/README.md)** | 🧩 Provenance and maintenance rules for rendered-page fixtures |
| **[Tracklist Search Fixtures](../tests/identify/fixtures/tracklist_search/README.md)** | 🔍 Provenance and maintenance rules for search-page fixtures |

______________________________________________________________________

<div align="center">
↩️ Back to the <a href="../README.md">main README</a>
</div>
