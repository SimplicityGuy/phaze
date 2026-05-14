---
status: testing
phase: 27-watcher-service-user-initiated-scan
source: [27-VERIFICATION.md]
started: 2026-05-13T23:27:39Z
updated: 2026-05-14T18:15:00Z
---

## Current Test

number: 3
name: Visual layout verification of admin UI
expected: |
  /pipeline/ dashboard renders Trigger Scan card above stats panel with agent dropdown, scan_root select, and subpath input. All UI-SPEC components (trigger_scan_card, scan_path_picker, recent_scans_table, scan_status_pill, scan_submit_error) render correctly per the UI-SPEC markup. Status pill colors match design tokens.
awaiting: user response

## Tests

### 1. End-to-end file drop → FileRecord under LIVE batch
expected: Start docker compose with the watcher service and drop a new music file (.mp3) into the watched root. After the settle period (10s), a new FileRecord appears in Postgres under the agent's LIVE ScanBatch with (agent_id, original_path) as the natural key. Re-dropping the same file produces no duplicate rows.
result: pass
note: |
  PASSED 2026-05-13 after closing 9 UAT gaps surfaced during live bringup. The
  fixes landed as 11 atomic commits on the phase-27 branch — see
  `27-UAT-GAPS-SUMMARY.md` for the full list. Verified on rancher-desktop with:
    - Fresh docker compose stack (no pre-existing volume), api ran 14 migrations
    - `ensure_dev_agent` seeded a usable dev-agent + LIVE-sentinel ScanBatch
    - Watcher booted with PollingObserver, authed via /whoami (HTTP 200)
    - File drop → POST /api/internal/agent/files (HTTP 200) → FileRecord
      in Postgres bound to the LIVE batch
    - Re-touch of the same file produced 0 duplicate rows (composite UQ holds)
gaps_closed_during_uat:
  - "gap-1: SAQ Worker.__init__ rejected timeout/retries/keep_result kwargs (Phase 26 bug surfaced by UAT)"
  - "gap-2: alembic upgrade head did not run on api startup (added to lifespan + PHAZE_AUTO_MIGRATE knob)"
  - "gap-3: no developer-quickstart for seeding an initial agent on fresh DB (added ensure_dev_agent)"
  - "gap-4: .env.example missing required agent-mode vars + host-vs-container guidance"
  - "gap-5: pydantic ValidationError hid the operator-actionable error on missing env"
  - "gap-6: agent_watcher README missing fresh-install quickstart"
  - "gap-7: watcher had no stdout logger — healthy and hung watchers were indistinguishable"
  - "gap-8: macOS docker bind mounts don't propagate inotify events; added PollingObserver mode"
  - "gap-9: ensure_dev_agent created the agent but not the LIVE-sentinel ScanBatch; controller's POST /files batch_id resolution crashed with NoResultFound"

### 2. Admin UI scan trigger → progress polling → terminal halt
expected: Navigate to /pipeline/ admin UI. Select an agent and a path under its scan_roots. Trigger a scan. The card returns the scan_progress_card partial with RUNNING state and hx-trigger='every 2s'; the card auto-updates every 2s; when scan completes the card transitions to COMPLETED state and polling halts (no hx-trigger AND no hx-get in completed markup).
result: pass
note: |
  PASSED 2026-05-14 after closing gap-13 (docker-compose missing agent-worker).
  Verified end-to-end on rancher-desktop / linux-arm64:
    - POST /pipeline/scans (200 OK) → INSERT scan_batches → SAQ enqueue
      of scan_directory(batch_id=2b7e319c-...) on phaze-agent-dev-agent queue
    - agent-worker (new compose service) consumed both queued jobs
      (scan_directory + extract_file_metadata from Test 1's stuck file)
      with status="complete"
    - GET /pipeline/scans/{id} now returns the COMPLETED partial:
        * green COMPLETED pill, "1 / 1 files"
        * no hx-trigger attribute (polling halts)
        * no hx-get attribute (polling halts)
        * aria-live="polite" preserved
gaps_closed_during_uat:
  - "gap-13: docker-compose.yml had `worker` (controller queue only) and `watcher` (filesystem observer only) but no SAQ consumer for `phaze-agent-<agent_id>`. Added an `agent-worker` service running `saq phaze.tasks.agent_worker.settings` with PHAZE_ROLE=agent + PHAZE_AGENT_QUEUE=phaze-agent-dev-agent. Also made `phaze.services.analysis` import lazy in `phaze.tasks.functions` so the agent worker module is importable on linux-arm64 (where essentia-tensorflow is gated out by pyproject.toml platform markers)."

### 3. Visual layout verification of admin UI
expected: /pipeline/ dashboard renders Trigger Scan card above stats panel with agent dropdown, scan_root select, and subpath input. All UI-SPEC components (trigger_scan_card, scan_path_picker, recent_scans_table, scan_status_pill, scan_submit_error) render correctly per the UI-SPEC markup. Status pill colors match design tokens.
result: [pending]

## Summary

total: 3
passed: 2
issues: 0
pending: 1
skipped: 0
blocked: 0

## Gaps
