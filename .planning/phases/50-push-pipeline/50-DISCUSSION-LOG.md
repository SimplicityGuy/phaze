# Phase 50: Push pipeline - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-25
**Phase:** 50-push-pipeline
**Areas discussed:** Stay-one-ahead control loop, Push transport & target wiring, In-flight state & observability, Integrity verify & scratch cleanup, Routing seam

---

## Stay-one-ahead control loop

### Pipeline shape
| Option | Description | Selected |
|--------|-------------|----------|
| Two-stage: push_file → process_file | New fileserver-agent push_file rsyncs to scratch, then enqueues process_file on the compute queue; each a ledger-tracked, deterministic-keyed job | ✓ |
| Single compute job pulls | process_file on compute initiates the transfer (compute pulls) | |
| Push embedded in release path | Extend the Phase 49 release/routing path to rsync inline before enqueueing process_file | |

### Top-up driver
| Option | Description | Selected |
|--------|-------------|----------|
| Controller cron tops up | A controller cron counts staged+in-flight from ledger/state and enqueues push_file until the window is full; recovery-only-compatible | ✓ |
| Completion-chained + cron backstop | process_file completion triggers the next push immediately; cron is only a backstop | |
| Cron only, window=1 | Push exactly one file at a time, no overlap | |

### Agent offline mid-window
| Option | Description | Selected |
|--------|-------------|----------|
| Hold, resume on return | In-flight ledger rows stay; recovery re-drives when a compute agent reappears; never fall back to local | ✓ |
| Revert to AWAITING_CLOUD | Reset not-yet-analyzed files to AWAITING_CLOUD for the release cron to re-route | |

**Notes:** Two-stage + cron-driven window respects the Phase-42 "automation is recovery-only" principle. No-local-fallback preserves Phase 49's load-bearing safety invariant (long files time out locally).

---

## Push transport & target wiring

### Target & SSH identity source
| Option | Description | Selected |
|--------|-------------|----------|
| Static config on fileserver | push_ssh_host/user/key(_FILE)/scratch_dir as pydantic-settings on the fileserver | ✓ |
| Dynamic from agent heartbeat | Compute agent advertises host+scratch in last_status; fileserver reads it at push time | |
| Hybrid: host from agent, key/dir static | Resolve host from the Agent row, secrets/dir from static config | |

### rsync mechanics
| Option | Description | Selected |
|--------|-------------|----------|
| rsync -e ssh, partial-dir + atomic rename | Subprocess rsync over SSH, no half-files at final path; app-level sha256 verify still runs compute-side | ✓ |
| scp / sftp | Simpler but no resume/delta/partial semantics | |
| rsync, you decide flags | rsync over SSH, exact flags to Claude's discretion | |

### Host-key verification
| Option | Description | Selected |
|--------|-------------|----------|
| Pinned known_hosts (strict) | Operator-provisioned host key, StrictHostKeyChecking=yes | ✓ |
| accept-new (TOFU) | Trust on first use, pin thereafter | |

**Notes:** Static config is sufficient for the single-A1 milestone (CLOUDDEPLOY-02 "push SSH target" knob). Strict known_hosts setup belongs in the Phase 51 runbook (CLOUDDEPLOY-03). Tailscale authenticates the network path.

---

## In-flight state & observability

### State model
| Option | Description | Selected |
|--------|-------------|----------|
| Add PUSHING + PUSHED states | New StrEnum members, no migration (String(30)); explicit, queryable, honest dashboard | ✓ |
| Ledger-only, no new states | Track in-flight via ledger rows + scratch presence; file stays DISCOVERED/AWAITING_CLOUD | |
| Single CLOUD_IN_FLIGHT state | One coarse state covering push-through-analyze | |

### Dashboard surfacing
| Option | Description | Selected |
|--------|-------------|----------|
| Separate staged + in-flight cards | "Staged (pushing)" + "Analyzing (cloud)" count cards via the Phase 49 pattern | ✓ |
| Single 'Cloud in-flight' card | One combined count | |
| You decide card breakdown | Surface counts, exact split to Claude | |

**Notes:** Explicit states let the cron count the window directly from state and make the "one ahead" model visible. Click-through deferred (consistent with Phase 49).

---

## Integrity verify & scratch cleanup

### sha256 source
| Option | Description | Selected |
|--------|-------------|----------|
| In the ProcessFilePayload | Control plane includes expected_sha256 + scratch path in the payload; no extra API call | ✓ |
| Fetch via internal agent API | Compute agent GETs the expected sha256 at analysis time | |

### Mismatch handling
| Option | Description | Selected |
|--------|-------------|----------|
| Re-push, bounded attempts | Clean fail + delete bad scratch; re-drive push up to a configurable max, then ANALYSIS_FAILED | ✓ |
| Re-push, unbounded | Always re-push, no cap | |
| Fail terminal immediately | Mismatch → ANALYSIS_FAILED, manual re-drive | |

### Cleanup & orphan reconciliation
| Option | Description | Selected |
|--------|-------------|----------|
| process_file finally + startup janitor | Delete in finally (success/terminal); compute-agent startup sweep removes orphans from killed workers | ✓ |
| process_file finally only | Cleanup solely in finally; relies on SAQ retry | |
| Separate cleanup task | A distinct delete_scratch task after analysis | |

**Notes:** Payload-carried sha256 avoids an internal-API round-trip (compute agent has no direct ORM). Bounded re-push prevents an infinite loop on a persistently corrupt source. The startup janitor satisfies the "no orphaned scratch files" criterion for hard-killed workers.

---

## Routing seam (Phase 49 integration)

| Option | Description | Selected |
|--------|-------------|----------|
| Funnel all cloud files through staging | Cloud-routed long files always land cloud-pending; the staging cron is the single entry to the window | |
| Immediate push if window has room | Routing enqueues push_file right away when a compute agent is online and the window has space, else AWAITING_CLOUD | |
| You decide the seam | Keep the ≤N window as the invariant; Claude picks funnel vs fast-path | ✓ |

**Notes:** Hard constraint regardless of approach — the ≤N window is never exceeded; a direct-to-compute enqueue that bypasses the push step or the window bound is a bug.

## Claude's Discretion

- Routing-seam approach (funnel-all vs fast-path-within-window), bounded by the ≤N invariant.
- Re-push attempt-counter storage location.
- Eligibility ordering for which file the cron stages next (e.g. FIFO by discovery).
- Exact rsync flags beyond atomicity + integrity.
- Config knob names/defaults (convention match to `cloud_route_threshold_sec`).

## Deferred Ideas

- Dynamic compute-agent target discovery via heartbeat (multi/rotating agents).
- Cloud-agent compose, Tailscale ACL, least-privilege Postgres queue role, runbook docs — Phase 51 (CLOUDDEPLOY-*).
- Click-through drill-down lists for the new cloud count cards.
- Cost/throughput-aware routing (CLOUDROUTE-05).
- Round-robin / least-loaded dispatch among multiple compute agents.
