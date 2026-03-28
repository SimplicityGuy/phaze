# Phase 4: Task Queue & Worker Infrastructure - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-28
**Phase:** 04-task-queue-worker-infrastructure
**Areas discussed:** Worker concurrency, Task granularity, Retry strategy

---

## Worker Concurrency Model

| Option | Description | Selected |
|--------|-------------|----------|
| Single worker, configurable concurrency | One arq worker process with configurable max_jobs. Scale by adjusting max_jobs or adding replicas. | |
| Multiple worker containers | Run N worker containers in docker-compose with lower concurrency each. | |
| You decide | Let Claude pick the best approach. | ✓ |

**User's choice:** You decide
**Notes:** User deferred to Claude. Recommended approach (single worker, configurable concurrency) will be used.

---

## Task Granularity

| Option | Description | Selected |
|--------|-------------|----------|
| One task per file | Each file gets its own arq job. Maximum parallelism, granular retries. | ✓ |
| Batched (100 files per task) | Fewer jobs but one failure affects whole batch. | |
| You decide | Let Claude pick based on scale requirements. | |

**User's choice:** One task per file (Recommended)
**Notes:** None

---

## Retry & Failure Handling

| Option | Description | Selected |
|--------|-------------|----------|
| 3 retries with exponential backoff | arq built-in retry. After 3 failures, mark permanently failed. | ✓ |
| Unlimited retries with cap | Keep retrying with increasing delays up to max interval. | |
| You decide | Let Claude pick the retry strategy. | |

**User's choice:** 3 retries with exponential backoff (Recommended)
**Notes:** None

---

## Claude's Discretion

- Worker concurrency model (topology, container count)
- Process pool strategy for CPU-bound work
- Job observability approach
- arq WorkerSettings configuration details
- Redis connection pooling
