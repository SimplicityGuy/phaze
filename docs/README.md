<!-- generated-by: gsd-doc-writer -->
# 📖 Phaze Documentation

**Phaze is a music collection organizer that analyzes and AI-renames music and concert files behind a human-in-the-loop approval UI.**

This is the documentation index for the `docs/` directory. For the project overview, quick start, and technology stack, see the [main README](../README.md).

The documents listed below describe the current product. Dated design specifications, spikes, and
the `.planning/` tree are historical evidence: retain their measurements and conclusions, but do
not treat an old “current” label as the live backlog. The [documentation audit](documentation-audit-2026-08-19.md)
records the complete reviewed/changed/unchanged inventory for this refresh.

## 🏁 Getting Started

| Document | Purpose |
| -------- | ------- |
| **[Quick Start Guide](quick-start.md)** | 🚀 Get Phaze running in minutes |
| **[Configuration](configuration.md)** | ⚙️ Environment variables and settings reference |
| **[Agentic Git Flow](AGF.md)** | 🐝 Beadhive roles, lifecycle, review, integration, and test isolation |

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
| **[UI Design Reference](ui-design-reference.md)** | 🎛️ Current production visual and interaction contract |

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

______________________________________________________________________

<div align="center">
↩️ Back to the <a href="../README.md">main README</a>
</div>
