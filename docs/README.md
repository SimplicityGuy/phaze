<!-- generated-by: gsd-doc-writer -->
# 📖 Phaze Documentation

**Phaze is a music collection organizer that fingerprints, analyzes, and AI-renames music and concert files behind a human-in-the-loop approval UI.**

This is the documentation index for the `docs/` directory. For the project overview, quick start, and technology stack, see the [main README](../README.md).

## 🏁 Getting Started

| Document | Purpose |
| -------- | ------- |
| **[Quick Start Guide](quick-start.md)** | 🚀 Get Phaze running in minutes |
| **[Configuration](configuration.md)** | ⚙️ Environment variables and settings reference |

## 📐 Reference

| Document | Purpose |
| -------- | ------- |
| **[Architecture Overview](architecture.md)** | 🏛️ System design, data flow, distributed agents, and Mermaid diagrams |
| **[API Reference](api.md)** | 🔌 REST and HTMX UI endpoints, plus the distributed Agent API |
| **[Database Schema & Migrations](database.md)** | 🗄️ PostgreSQL schema and Alembic migrations |
| **[Project Structure](project-structure.md)** | 📁 Codebase layout and module organization |

## 🚀 Operations

| Document | Purpose |
| -------- | ------- |
| **[Deployment Guide](deployment.md)** | 🐳 Docker Compose deploy, image pipeline, and remote agents |
| **[Operator Runbook](runbook.md)** | 🛠️ Force-local incident revert, reading the N backend lanes, spillover, and per-backend `_FILE` secrets |
| **[Cloud Burst](cloud-burst.md)** | ☁️ OCI A1 compute-agent deploy, Tailscale ACL, broker role, enabled via a `kind="compute"` entry in `backends.toml` |
| **[Multi-Compute Agents](multi-compute.md)** | ⚙️ Add a 2nd+ compute agent, mixed arm64/x86 rank/cap cost-tiering, per-agent compose, N-lane read-out |
| **[Agent Queue Lanes](agent-queue-lanes.md)** | 🛤️ Per-lane file-server workers (analyze/fingerprint/meta/io), core budget + thread pinning, single-lane heartbeat, legacy-queue drain runbook |
| **[Kubernetes Burst](k8s-burst.md)** | ☸️ Kueue Job-runner runbook: ResourceFlavor/ClusterQueue/LocalQueue, namespaced RBAC, `_FILE` Secret, S3 staging, enabled via a `kind="kueue"` entry in `backends.toml` |

______________________________________________________________________

<div align="center">
↩️ Back to the <a href="../README.md">main README</a>
</div>
