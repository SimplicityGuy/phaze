# Phase 51: Deployment, config & docs - Context

**Gathered:** 2026-06-26
**Status:** Ready for planning

<domain>
## Phase Boundary

Make the compute (cloud) agent **deployable and fully operator-controlled**. Deliverables:

1. **Cloud-agent compose file** (net-new) — a Tailscale-connected, compute-only stack for the OCI A1: arm64 image, no media mount, scratch volume, no watcher/fingerprint sidecars.
2. **Master enable toggle** (net-new code) — a single `cloud_burst_enabled` setting that, when OFF, reverts to all-local analysis with no other change (CLOUDDEPLOY-04). This is the only net-new code path in the phase; it gates the Phase 49 routing seam + the cloud crons.
3. **Config knobs documented** — every cloud-burst parameter (most already shipped in Phases 49/50) surfaced in `docs/configuration.md` with `_FILE`-secret support called out (CLOUDDEPLOY-02).
4. **OCI A1 + Tailscale-ACL provisioning** — authored as **OpenTofu IaC in the homelab repo**; Phase 51 (phaze repo) delivers a ready-to-paste homelab change prompt that specifies the OpenTofu module + Tailscale ACL JSON + least-privilege Postgres queue-broker role; `docs/cloud-burst.md` references it (CLOUDDEPLOY-03).

Requirements: CLOUDDEPLOY-01..04. **Depends on Phase 50** (deploys the full working push pipeline).

**Out of scope:** the push pipeline itself (Phase 50); duration routing/backfill (Phase 49); the arm64 image build (Phase 47); the compute-agent type (Phase 48); cost/throughput-aware routing (CLOUDROUTE-05, deferred); authoring OpenTofu/OCI infra inside the phaze repo (belongs in homelab — workspace boundary).

</domain>

<decisions>
## Implementation Decisions

### Master enable toggle (CLOUDDEPLOY-04)
- **D-01:** New setting **`cloud_burst_enabled: bool`, default `False`**, alias `PHAZE_CLOUD_BURST_ENABLED` (mirrors the `enable_saq_ui` naming precedent at `config.py:292`). Off-by-default is the safe state: a fresh deploy behaves all-local until the operator provisions the A1 + push config and explicitly turns cloud on. **Note:** this means the Phase 49/50 cloud machinery, currently unconditional, becomes OFF on the next deploy until the operator flips the toggle — intended behavior.
- **D-02:** **OFF = pure pre-Phase-49 behavior.** When `cloud_burst_enabled` is False, ALL files (including long ≥ `cloud_route_threshold_sec` files) route to the fileserver/local queue. This is the honest "all-local analysis with no other change" of CLOUDDEPLOY-04. Long files MAY time out locally — that is the operator's accepted trade-off when cloud is off; the bounded ~4h timeout + `retries=1` (Phase 31) means a timeout fails cleanly (`ANALYSIS_FAILED`), not a crash. This deliberately suspends Phase 49's "never analyze a long file locally" invariant **only while the toggle is OFF** (the invariant holds whenever cloud burst is ON).
- **D-03:** **The toggle gates EVERY cloud entry point**, not just routing. When OFF, the following all check the flag and no-op early: (a) the duration-routing decision in `_route_discovered_by_duration` / `trigger_analysis` (`routers/pipeline.py`) — long files go local; (b) the Phase 50 staging/top-up cron (no new `push_file` enqueues); (c) the Phase 49 `release_awaiting_cloud` cron. One toggle ⇒ zero cloud activity anywhere.
- **D-04:** **In-flight cloud work drains; OFF only stops NEW cloud work.** Files already `PUSHING`/`PUSHED` finish their current transfer + analysis on the compute agent (no mid-transfer/mid-analysis abort, no scratch reclaim). Simplest + safest — avoids wasted transfers and partial-state cleanup. No new long files enter the cloud path once OFF.

### Cloud-agent compose & Tailscale (CLOUDDEPLOY-01)
- **D-05:** **Host-installed `tailscaled`** on the OCI A1 (apt install + `tailscale up`, owned by the homelab OpenTofu / runbook), NOT a Tailscale sidecar container. The compose stack uses the host's Tailscale connectivity to reach lux's services. Simplest on a dedicated single-purpose VM we fully control; keeps the compose file clean (no `TS_AUTHKEY` secret, no `network_mode` sidecar wiring).
- **D-06:** **Worker-only compose.** Only the agent SAQ worker (`PHAZE_ROLE=agent`, `kind=compute`). **No watcher** (a compute agent owns no scan roots — `kind=compute` relaxes the empty-scan-roots gate, `config.py:470`), **no fingerprint sidecars**, **NO media mount** (DIST-04 — agents reach Postgres only via the queue + the app HTTP API, never a media bind). Mirrors `docker-compose.agent.yml`'s invariants minus the media-bound services.
- **D-07:** **Scratch = a named docker volume** mounted at `cloud_scratch_dir` (not a host bind mount — no operator dir-create/chown step). `MODELS_PATH` mounted `rw` (auto-download); CA cert mounted `ro`. Matches the `docker-compose.agent.yml` volume conventions.
- **D-08:** **Image line: `image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64`.** The Phase 47 image is **NOT multi-arch** — it is published as a separate `-arm64`-suffixed tag (`latest-arm64` on default branch, `<version>-arm64` on release tags; see `docs/arm64-agent-image.md:189-194`). The `${PHAZE_IMAGE_TAG:-latest}` convention from `docker-compose.agent.yml` is preserved, but the **`-arm64` suffix is mandatory**. Production pins `PHAZE_IMAGE_TAG=v5.0.0` → resolves to `v5.0.0-arm64`.

### OCI A1 + Tailscale-ACL + PG role (CLOUDDEPLOY-03)
- **D-09:** **OCI A1 infra is OpenTofu IaC in the homelab repo**, NOT a phaze-docs console click-path and NOT authored in the phaze repo (workspace boundary — no cross-project deps). Phase 51 delivers a **ready-to-paste change prompt for the homelab repo agent** (the Phase 36 "Step D" precedent) specifying the OpenTofu OCI A1 module: Always-Free A1 Ampere, arm64 Ubuntu 24.04 image, boot volume, SSH key, networking/security-list.
- **D-10:** **Tailscale ACL + least-privilege Postgres queue-broker role are applied in the homelab repo, spec'd by phaze.** The change prompt carries the **exact ACL JSON** (scoping A1 → `lux:{5432,6379,8000}` + `nox → A1:22`) and the **exact PG role SQL** as the authoritative spec; the homelab agent applies them (Tailscale provider for the ACL if available, else ACL JSON in homelab; PG role via homelab's Postgres provisioning). phaze's `docs/cloud-burst.md` keeps copies for reference. phaze stays the source-of-truth spec; all live infra lives in homelab.
- **D-11:** **Least-privilege PG role is full SQL in the spec/runbook.** Since Phase 36 migrated SAQ to the **Postgres queue backend**, the compute agent connects via `PHAZE_QUEUE_URL` for the `saq_jobs` table ONLY (NOT the app ORM — DIST-04). Document `CREATE ROLE` + minimal `GRANT`s scoped to `saq_jobs` + its sequence. **Grant-timing note:** SAQ auto-creates `saq_jobs` on first boot, so document either (a) the role needs `CREATE` on first boot, or (b) pre-create the table and grant table-scoped privileges only — researcher/planner picks the safer option.

### Config documentation (CLOUDDEPLOY-02)
- **D-12:** **Cloud-burst config knobs get a full table in `docs/configuration.md`** — columns: knob, env var, default, `_FILE`-secret? — covering `cloud_burst_enabled`, `cloud_route_threshold_sec`, `cloud_max_in_flight`, `push_max_attempts`, `compute_scratch_dir`, `cloud_scratch_dir`, `push_ssh_host`, `push_ssh_user`, `push_timeout_sec`, `push_connect_timeout_sec`, and the `_FILE`-secret fields `push_ssh_key` + `push_known_hosts` (+ `agent_token`). Plus the master-toggle semantics (OFF = all-local). Source the descriptions from the `Field(...)` descriptions already in `config.py`.

### Doc layout
- **D-13:** **One new `docs/cloud-burst.md`** holds the whole feature in one place: the cloud-agent compose/deploy walkthrough + the runbook (referencing the homelab OpenTofu for OCI infra, with the ACL JSON + PG role SQL copies) + deploy ordering + a smoke test. The cloud-burst **config** subsection lives in `docs/configuration.md` (canonical config home, D-12). `docs/deployment.md` gets a short pointer to `cloud-burst.md` (avoid bloating its existing 469 lines with the vendor-specific runbook).

### Claude's Discretion
- PG-role grant-timing approach (CREATE-on-first-boot vs pre-create table) — pick the safer of D-11's two options.
- Exact compose env var list / `.env` layout for the cloud-agent file, within the worker-only + no-media + named-scratch + `-arm64`-image constraints.
- Whether the master toggle is read once at startup vs per-request/per-cron-tick (prefer per-tick so flipping it doesn't require a restart, unless that complicates the routing seam).
- Exact wording/structure of the homelab change prompt, within the D-09/D-10 spec content.
- Whether a deployed-compute-agent smoke-test step is a doc checklist vs a scripted check.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase scope & requirements
- `.planning/ROADMAP.md` §"Phase 51: Deployment, config & docs" — goal, 4 success criteria, dependency on Phase 50.
- `.planning/REQUIREMENTS.md` — CLOUDDEPLOY-01..04 (in scope); CLOUDROUTE-05 (deferred).
- `.planning/phases/50-push-pipeline/50-CONTEXT.md` — the push-pipeline decisions this phase deploys (FileState `PUSHING`/`PUSHED`, staging cron, `push_*` config, static push target, strict known_hosts).
- `.planning/phases/49-duration-routing-backfill/49-CONTEXT.md` — routing seam + the "never analyze a long file locally" invariant that D-02 conditionally suspends.

### Master toggle — routing seam & crons (primary net-new change surface)
- `src/phaze/routers/pipeline.py` — `trigger_analysis` (`:344`), `_route_discovered_by_duration` (`:368` call site), and the backfill paths (`:600`, `:646`) — gate long→local routing on `cloud_burst_enabled` here.
- `src/phaze/tasks/release_awaiting_cloud.py` — Phase 49 release cron — must no-op when the toggle is OFF.
- The Phase 50 staging/top-up cron (controller) — must no-op when the toggle is OFF.
- `src/phaze/config.py` — `ControlSettings` cloud-burst knobs (`cloud_route_threshold_sec` `:376`, `cloud_max_in_flight` `:389`, `push_max_attempts` `:399`, `compute_scratch_dir` `:412`); add `cloud_burst_enabled` here following the `enable_saq_ui` Field pattern (`:292`). `AgentSettings` push/scratch knobs (`push_ssh_host` `:579`, `push_ssh_user` `:584`, `cloud_scratch_dir` `:589`, `push_timeout_sec` `:594`, `push_connect_timeout_sec` `:601`, `push_ssh_key` `:609`, `push_known_hosts` `:614`); `kind` Literal (`:470`). `_FILE`-secret wiring: `SECRET_FILE_FIELDS` (`:438`), `SECRET_FILE_PRESERVE_WHITESPACE` (`:444`).

### Compose & deployment
- `docker-compose.agent.yml` — the fileserver-agent compose; the structural template for the new cloud-agent compose (image-tag convention, `_FILE` secrets, volume/mount conventions, the `tests/test_deployment/test_agent_compose.py` invariants to mirror: no `DATABASE_URL`, no postgres/redis service, MODELS rw, CA ro).
- `docker-compose.yml`, `docker-compose.override.yml` — app-server stack + local-dev overrides (reference for env conventions).
- `docs/arm64-agent-image.md` §"Tag naming (consumed by Phase 51)" (`:189-194`) — the `-arm64` suffix tag scheme; the compose MUST pin `<version>-arm64` (D-08).

### Docs to write/update
- `docs/configuration.md` (254L) — add the cloud-burst config table (D-12).
- `docs/deployment.md` (469L) — add a pointer to `docs/cloud-burst.md` (D-13).
- `docs/cloud-burst.md` — NEW: compose/deploy walkthrough + runbook + homelab-OpenTofu reference + smoke test (D-13).
- `docs/README.md` — docs index; add the new `cloud-burst.md` entry.

### Cross-repo (homelab) deliverable
- Phase 36's "Deliverable (Step D — homelab)" pattern in `.planning/ROADMAP.md` §"Phase 36" — the precedent format for the ready-to-paste homelab change prompt (D-09/D-10): env/service changes, deploy ordering via `datum@nox` / `datum@lux`.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `enable_saq_ui` (`config.py:292`): the exact bool-toggle Field pattern for `cloud_burst_enabled` (alias + default + description).
- `docker-compose.agent.yml`: the compose template — strip watcher/fingerprint/media, swap to the `-arm64` image and a named scratch volume.
- `_FILE`-secret machinery (`SECRET_FILE_FIELDS`, `SECRET_FILE_PRESERVE_WHITESPACE`, `_resolve_secret_files`): already supports `push_ssh_key` + `push_known_hosts`; the cloud-agent compose just mounts the `*_FILE` paths.
- `tests/test_deployment/test_agent_compose.py`: the test-pattern precedent for asserting cloud-agent compose invariants (no media, no DB, named scratch, `-arm64` image).
- All cloud-burst config knobs already exist (Phases 49/50) with `Field(...)` descriptions — `docs/configuration.md` can be sourced directly from them.

### Established Patterns
- Control-side cloud routing flows through `_route_discovered_by_duration` (`routers/pipeline.py`) — the single seam where the master toggle short-circuits to local.
- `FileState` is a code-only StrEnum → no migration concerns for this phase (no new states added here).
- Crons are gated/recovery-scoped (Phase 42 principle) — the toggle adds a flag check at the top of each cloud cron.
- phaze emits a homelab change prompt for infra-side work (Phase 36 Step D) rather than reaching into the homelab repo.

### Integration Points
- `config.py` — new `cloud_burst_enabled` knob; documents existing push/scratch/threshold knobs.
- `routers/pipeline.py` + the two cloud crons — flag-gated short-circuit to all-local.
- New `docker-compose.cloud.yml` (or similar) — compute-only, Tailscale-host, `-arm64`, named scratch.
- `docs/cloud-burst.md` + `docs/configuration.md` + `docs/deployment.md` + `docs/README.md` — doc surface.
- Homelab change prompt — OpenTofu OCI A1 module + Tailscale ACL JSON + PG role SQL.

</code_context>

<specifics>
## Specific Ideas

- The Tailscale ACL must scope the A1 to **exactly** `lux:{5432,6379,8000}` (postgres queue + redis cache + app API) + `nox → A1:22` (push SSH target) — no broader access.
- `5432` is required because Phase 36 made the SAQ queue Postgres-backed; the compute agent's `PHAZE_QUEUE_URL` connects there for `saq_jobs` only (still no app ORM / `DATABASE_URL` — DIST-04 holds).
- Off-by-default master toggle is deliberate: the just-built Phase 49/50 cloud feature ships OFF and the operator opts in after provisioning. Capture this clearly so the v5.0 deploy/redeploy doesn't surprise.
- v5.0 chose rsync-over-Tailscale push, NO object storage — the runbook/compose must not reintroduce any object-storage assumption.
- OpenTofu (not Terraform) is the IaC tool for the homelab OCI infra (user directive, 2026-06-26).

</specifics>

<deferred>
## Deferred Ideas

- Cost/throughput-aware routing beyond the fixed duration threshold — CLOUDROUTE-05, out of scope this milestone.
- Dynamic multi-compute-agent target discovery via heartbeat — static single-A1 config is sufficient (Phase 50 D-05).
- Hard-stop + reclaim of in-flight cloud work on toggle-OFF — drain-only chosen (D-04); revisit only if a fast kill-switch is ever needed.
- Tailscale sidecar container packaging — host-installed `tailscaled` chosen (D-05); revisit if the A1 host is ever not fully operator-controlled.
- Terraform/OCI-CLI alternatives for provisioning — superseded by the homelab OpenTofu directive (D-09).

None of the above are blockers; discussion stayed within phase scope.

</deferred>

---

*Phase: 51-deployment-config-docs*
*Context gathered: 2026-06-26*
