---
status: partial
phase: 29-deployment-hardening-agents-admin
source: [29-VERIFICATION.md]
started: 2026-05-17T00:09:50Z
updated: 2026-05-17T00:09:50Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. Real Two-Host Deployment Smoke
expected: Agent row appears on `/admin/agents` with green ALIVE pill within 60 seconds of `just up-agent` on the file-server host. Agent logs show cert banner, model download (first run), and heartbeat every 30s. `REDIS_BIND_IP` set to the app-server LAN IP prevents file-server direct Redis access from outside the app-server LAN. `docker compose exec api ls /data/music` on the application-server host returns `No such file` (filesystem isolation smoke, D-20).
result: [pending — file-server hardware not yet available; operator accepted `verified-docs-only` for Plan 08 Task 2]

## Summary

total: 1
passed: 0
issues: 0
pending: 1
skipped: 0
blocked: 0

## Gaps
