# Phase 51: Deployment, config & docs - Pattern Map

**Mapped:** 2026-06-26
**Files analyzed:** 10 (3 code, 1 compose, 1 test, 4 docs, 1 cross-repo deliverable)
**Analogs found:** 10 / 10 (every file has a strong in-repo analog)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/config.py` (MODIFY: add `cloud_burst_enabled`) | config | request-response | `enable_saq_ui` Field at `config.py:292` | exact |
| `src/phaze/routers/pipeline.py` (MODIFY: gate routing + backfill) | router | request-response | self — `_route_discovered_by_duration:308`, `trigger_backfill_cloud:639` | exact (in-place edit) |
| `src/phaze/tasks/release_awaiting_cloud.py` (MODIFY: gate `stage_cloud_window`) | task (cron) | event-driven | self — existing GATE 1/2 no-op pattern at `:130-154` | exact (in-place edit) |
| `docker-compose.cloud-agent.yml` (NEW) | config (compose) | — | `docker-compose.agent.yml` | role-match (strip media services) |
| `tests/test_deployment/test_cloud_agent_compose.py` (NEW) | test | — | `tests/test_deployment/test_agent_compose.py` | exact |
| `docs/cloud-burst.md` (NEW) | docs | — | `docs/deployment.md` (step-walkthrough) + `docs/arm64-agent-image.md` | role-match |
| `docs/configuration.md` (MODIFY: add cloud-burst table) | docs | — | self — "Worker / task queue settings" table at `:66-76` | exact |
| `docs/deployment.md` (MODIFY: add pointer) | docs | — | self — "See also" / Deployment Targets table | exact |
| `docs/README.md` (MODIFY: index entry) | docs | — | self — "🚀 Operations" table at `:24-28` | exact |
| `.planning/phases/51-.../51-HOMELAB-CHANGE-PROMPT.md` (NEW) | cross-repo deliverable | — | `.planning/milestones/v4.0-phases/36-.../36-HOMELAB-CHANGE-PROMPT.md` | exact |

---

## Pattern Assignments

### `src/phaze/config.py` (config — add `cloud_burst_enabled`)

**Analog:** `enable_saq_ui` bool kill-switch Field at `config.py:292` (lives on `BaseSettings`; the new field goes on `ControlSettings` next to the other cloud knobs at `:376-416`).

**Bool-toggle Field pattern** (`config.py:288-296`) — copy verbatim, swap names/default:
```python
# Phase 33: mount the SAQ monitoring dashboard at /saq ...
enable_saq_ui: bool = Field(
    default=True,
    validation_alias=AliasChoices("PHAZE_ENABLE_SAQ_UI", "enable_saq_ui"),
    description="Mount the SAQ monitoring dashboard at /saq in the API (Phase 33).",
)
```

**New field to add** (place inside `ControlSettings`, alongside `cloud_route_threshold_sec` at `:376`, since the control plane owns routing — RESEARCH "Master Toggle Wiring"):
```python
cloud_burst_enabled: bool = Field(
    default=False,
    validation_alias=AliasChoices("PHAZE_CLOUD_BURST_ENABLED", "cloud_burst_enabled"),
    description="Master switch for the cloud-burst feature. False (default) reverts to all-local analysis (Phase 51, CLOUDDEPLOY-04).",
)
```

**Why `ControlSettings` not `BaseSettings`:** the two reader sites (`routers/pipeline.py` module-level `settings`, and `stage_cloud_window`'s `get_settings()`) both resolve `ControlSettings` under `PHAZE_ROLE=control`. The existing `cloud_route_threshold_sec`/`cloud_max_in_flight`/`push_max_attempts`/`compute_scratch_dir` block (`:376-416`) is the established home and the comment style to mirror (each carries a `Phase NN D-NN:` provenance comment + bounded `Field`).

**Note:** plain `bool`, no `gt/lt` bounds (unlike the int knobs). No `SECRET_FILE_FIELDS` change (not secret-bearing). The `_FILE` machinery (`SECRET_FILE_FIELDS:438`, `SECRET_FILE_PRESERVE_WHITESPACE:444`) already covers all cloud secrets — do not touch it for this toggle.

---

### `src/phaze/routers/pipeline.py` (router — gate routing seam + backfill)

**Analog:** the file's own existing structure. Two edit sites, both `[VERIFIED]` current.

**Site 1 — routing seam, `_route_discovered_by_duration` (`pipeline.py:308`)** — one-condition change:
```python
# current (line 308):
is_long = duration is not None and duration >= threshold_sec
# Phase 51 (D-02): OFF -> nothing is "long" -> every file falls to the local branch
is_long = settings.cloud_burst_enabled and duration is not None and duration >= threshold_sec
```
`settings` is the module-level `from phaze.config import settings` singleton already used at `:368` (`settings.cloud_route_threshold_sec`) and `:369` (`settings.models_path`). This single edit covers BOTH `trigger_analysis` (`:344`) and the UI trigger, since both call this function. With OFF, no row is set to `FileState.AWAITING_CLOUD` (`:312`); short+long both append to `local_files` (`:315`). **Discretion (RESEARCH):** optionally thread a `cloud_enabled: bool` parameter (parity with how `threshold_sec` is already passed at `:364-370`) for unit-testability — the test map calls for a `-k cloud_burst_disabled` unit test.

**Site 2 — backfill early-return, `trigger_backfill_cloud` (`pipeline.py:639-710`)** — add an explicit guard BEFORE the candidate query at `:660`, mirroring the existing `count == 0` early-return shape at `:662-667`:
```python
# existing zero-candidate early-return (the structural template to copy):
count = await count_backfill_candidates(session, threshold)
if count == 0:
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/backfill_response.html",
        context={"request": request, "count": 0},
    )
```
Add immediately ABOVE the `count = ...` line (Pitfall 2 — gating site 1 alone would silently reset 144 `ANALYSIS_FAILED` files to `DISCOVERED` and re-route them local to re-time-out):
```python
if not settings.cloud_burst_enabled:
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/backfill_response.html",
        context={"request": request, "count": 0, "disabled": True},
    )
```
Reuse the SAME `backfill_response.html` partial (no new template); add an optional `disabled` flag the template can surface. Do NOT mutate any `file.state` on this path.

---

### `src/phaze/tasks/release_awaiting_cloud.py` (cron — gate `stage_cloud_window`)

**Analog:** the function's own existing GATE 1 / GATE 2 clean-no-op pattern (`:130-154`).

**Existing no-op contract to mirror** (`:130-135`, GATE 1):
```python
# GATE 1: a compute agent (the analysis consumer) must be online. Absent -> clean hold no-op.
try:
    await select_active_agent(session, kind="compute")
except NoActiveAgentError:
    logger.info("stage_cloud_window no-op: no compute agent online")
    return {"staged": 0, "skipped": 0}
```

**New gate to add** at the very top of `stage_cloud_window`, BEFORE the existing `max_in_flight = get_settings().cloud_max_in_flight` read at `:121` (so it short-circuits before opening a session / taking the advisory lock at `:128`):
```python
cfg = get_settings()
if not cfg.cloud_burst_enabled:  # type: ignore[attr-defined]
    return {"staged": 0, "skipped": 0}
max_in_flight = cfg.cloud_max_in_flight  # type: ignore[attr-defined]
```
**Key conventions:** (a) return the normal `{"staged": 0, "skipped": 0}` dict — NEVER raise (matches the existing T-50-cron-raise discipline already enforced by GATE 1/2); (b) keep the `# type: ignore[attr-defined]` comment exactly as the existing `:121` access does (`get_settings()` returns the union type, so `cloud_max_in_flight`/`cloud_burst_enabled` are flagged); (c) clause (c) of D-03 (Phase 49 release cron) is satisfied here — there is NO separate `release_awaiting_cloud` cron; Phase 50 replaced it with `stage_cloud_window` in this same file.

---

### `docker-compose.cloud-agent.yml` (NEW compose — worker-only, arm64, named scratch)

**Analog:** `docker-compose.agent.yml` — strip `watcher`/`audfprint`/`panako`, drop the media (`SCAN_PATH`) mounts, add `-arm64` suffix + `network_mode: host` + a named scratch volume.

**`worker` service to copy + adapt** (from `docker-compose.agent.yml:32-42`):
```yaml
services:
  worker:
    image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}   # agent.yml form
    command: uv run saq phaze.tasks.agent_worker.settings
    env_file: .env
    environment:
      - PHAZE_ROLE=agent
    volumes:
      - "${SCAN_PATH:?SCAN_PATH required}:/data/music:ro"     # REMOVE (no media on compute)
      - "${MODELS_PATH:-./models}:/models:rw"                 # KEEP (rw, D-07 auto-download)
      - "${CA_PATH:-./certs}:/certs:ro"                       # KEEP (ro, D-07)
    restart: unless-stopped
```

**Target shape** (RESEARCH "Compose Pattern", D-05..D-08):
```yaml
services:
  worker:
    image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64   # D-08: -arm64 MANDATORY
    command: uv run saq phaze.tasks.agent_worker.settings
    network_mode: host                       # D-05: reach lux via host tailscaled + MagicDNS
    env_file: .env
    environment:
      - PHAZE_ROLE=agent
      - PHAZE_AGENT_KIND=compute             # relaxes empty-scan-roots gate (config.py:470)
    volumes:
      - cloud_scratch:${PHAZE_CLOUD_SCRATCH_DIR:?PHAZE_CLOUD_SCRATCH_DIR required}:rw   # D-07 named vol
      - "${MODELS_PATH:-./models}:/models:rw"
      - "${CA_PATH:-./certs}:/certs:ro"
    restart: unless-stopped
volumes:
  cloud_scratch:
```
**Invariants vs the analog:** single `worker` service (no media-bound sidecars); NO `DATABASE_URL`/`POSTGRES_*` (DIST-04); NO `SCAN_PATH`/media bind; image ends `-arm64`; named volume (`volumes:` block at file foot, like `audfprint_data`/`panako_data` at `agent.yml:78-80`); MODELS `rw`, CA `ro`. Header-comment style: copy the `# docker-compose.agent.yml — ...` banner + "Invariants (asserted by tests/...)" block from `agent.yml:1-29`.

---

### `tests/test_deployment/test_cloud_agent_compose.py` (NEW test — YAML-parse invariants)

**Analog:** `tests/test_deployment/test_agent_compose.py` — pure `yaml.safe_load`, no docker daemon; asserts raw `${VAR}` tokens (no interpolation).

**Module scaffold to copy** (`test_agent_compose.py:32-58`):
```python
COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.agent.yml"   # -> cloud-agent.yml

def _load_agent_compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text())

def _env_to_strs(env: Any) -> list[str]:
    if isinstance(env, list):
        return [str(e) for e in env]
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    return []
```

**Service-set assertion** (adapt `test_agent_compose_service_list:61-66`):
```python
def test_cloud_agent_compose_service_list() -> None:
    data = _load_agent_compose()
    assert set(data["services"].keys()) == {"worker"}   # worker-only (no watcher/audfprint/panako)
```

**No-Postgres / DIST-04 assertion** (copy `test_agent_compose_has_no_postgres_env:69-84` verbatim — asserts no `DATABASE_URL`, no `POSTGRES_*`, no `depends_on: postgres`).

**Role + kind assertion** (extend `test_worker_service_has_phaze_role_agent:87-91`):
```python
worker_env = _env_to_strs(data["services"]["worker"].get("environment", []))
assert any("PHAZE_ROLE=agent" in e for e in worker_env)
assert any("PHAZE_AGENT_KIND=compute" in e for e in worker_env)
```

**New `-arm64` image assertion** (the net-new assertion vs the agent test — adapt `test_all_agent_services_pull_from_ghcr:113-137`):
```python
image = data["services"]["worker"]["image"]
assert image.startswith("ghcr.io/simplicityguy/phaze:")
assert "PHAZE_IMAGE_TAG" in image
assert image.endswith("-arm64")   # D-08 — mandatory suffix (Pitfall 3)
```

**Named-scratch + no-media assertions** (new): assert the scratch volume entry is a named-volume ref (left side has no leading `/` or `.` — it is `cloud_scratch`) declared under top-level `volumes:`, and that NO volume string contains `SCAN_PATH` or `/data/music`. Note: the existing `test_all_scan_path_mounts_use_failfast_syntax:94-110` is the inverse of what you want here — the cloud test asserts SCAN_PATH is ABSENT entirely.

---

### `docs/configuration.md` (MODIFY — add cloud-burst table)

**Analog:** the existing "Worker / task queue settings" table (`configuration.md:66-76`) and the `_FILE` table (`:27-34`).

**Table heading + column convention to copy** (`:66-76`):
```markdown
## Worker / task queue settings (all roles)

| Variable                       | Required | Default | Description                                          |
|--------------------------------|----------|---------|------------------------------------------------------|
| `WORKER_MAX_JOBS`              | No       | `8`     | Concurrent SAQ jobs per worker.                      |
```
Add a new `## Cloud-burst settings` section using the same 4-column shape, but per D-12 the columns are **knob / env var / default / `_FILE`-secret?** plus a Description. Source every Description from the `Field(...)` text already in `config.py` (RESEARCH "Config Knobs Audit" enumerates all 14 rows + the two criterion-named knobs `WORKER_MAX_JOBS` and `PHAZE_AGENT_QUEUE`). The `_FILE`-bearing rows (`push_ssh_key`, `push_known_hosts`, `agent_token`, `queue_url`, `redis_url`) must ALSO appear in the `_FILE` table at `:27-34` (extend it). Document `PHAZE_CLOUD_BURST_ENABLED` default `False` + the OFF=all-local semantics. The `PHAZE_*` vs bare-name dual-form convention is described at `:19` — follow it (e.g. `PHAZE_CLOUD_BURST_ENABLED (or cloud_burst_enabled)`).

**File marker:** keep the `<!-- generated-by: gsd-doc-writer -->` line 1 marker (every doc carries it).

---

### `docs/deployment.md` (MODIFY — pointer only)

**Analog:** self — the "Deployment Targets" table (`:13-19`) and the `## See also` section (`:461`).

Add `docker-compose.cloud-agent.yml` as a fourth row in the Deployment Targets table (`:13-19`), and a short `## Cloud-burst compute agent` pointer section (or a "See also" bullet) linking to `docs/cloud-burst.md` — do NOT inline the vendor runbook here (D-13 keeps deployment.md from bloating past its 469 lines). Mirror the table's existing `| File | Host | Services | Notes |` columns: row = `| docker-compose.cloud-agent.yml | OCI A1 (cloud) | worker (agent role, kind=compute) | arm64 image, no media, named scratch. See cloud-burst.md. |`.

---

### `docs/README.md` (MODIFY — index entry)

**Analog:** self — the "🚀 Operations" table (`:24-28`).

Add a row to the Operations table mirroring the existing Deployment Guide row exactly:
```markdown
## 🚀 Operations

| Document | Purpose |
| -------- | ------- |
| **[Deployment Guide](deployment.md)** | 🐳 Docker Compose deploy, image pipeline, and remote agents |
| **[Cloud Burst](cloud-burst.md)** | ☁️ OCI A1 compute-agent deploy, Tailscale ACL, broker role, master toggle |
```
Keep the emoji-prefixed-purpose convention and the `<!-- generated-by: gsd-doc-writer -->` line-1 marker.

---

### `docs/cloud-burst.md` (NEW — feature home: compose/deploy walkthrough + runbook + smoke test)

**Analog:** `docs/deployment.md`'s numbered `## Step N — ...` walkthrough structure (`:81-270`) for the compose/deploy + smoke-test sections; `docs/arm64-agent-image.md:189-194` ("Tag naming") for the `-arm64` pin rationale.

**Heading style to copy** (`deployment.md:81,117,131,190,218,245`): numbered operator steps — `## Step 1 — Bring up the application server`, etc. Apply the same to the cloud-burst deploy ordering (RESEARCH "Deploy ordering" has the 7-step sequence: OpenTofu apply → ACL → broker role SQL → release → A1 `.env` + compose up → flip toggle + restart → smoke test).

**Content blocks to embed (copies, per D-10/D-13 — phaze is source-of-truth spec):**
- The Tailscale grants ACL JSON (RESEARCH "Tailscale Grants ACL" — `jsonc` fenced block).
- The least-privilege `phaze_broker` PG role SQL (RESEARCH "Least-Privilege Postgres Role" — `sql` fenced block; the empirically-verified `GRANT USAGE, CREATE ON SCHEMA public` finding is load-bearing).
- The OCI A1 OpenTofu spec (RESEARCH "OCI Always-Free A1 OpenTofu Spec" — `hcl` fenced block; spec at 2 OCPU / 12 GB).
- A runtime-state doc note: off-by-default ships the cloud feature dormant; pre-existing `AWAITING_CLOUD` rows release once enabled; flipping the toggle requires a control-plane restart (Pitfall 6).

**File marker:** start with `<!-- generated-by: gsd-doc-writer -->`.

---

### `.planning/phases/51-.../51-HOMELAB-CHANGE-PROMPT.md` (NEW — cross-repo deliverable)

**Analog:** `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-HOMELAB-CHANGE-PROMPT.md` — the Phase 36 "Step D" precedent (ROADMAP §"Phase 36" line 276).

**Structure to copy** (`36-HOMELAB-CHANGE-PROMPT.md:1-70`):
- Line 1 marker `<!-- generated-by: gsd-executor -->` + `# Homelab Change Prompt — <title>`.
- A blockquote `> **Paste the section below into the homelab repo agent.** ...` intro (`:4-8`).
- `## Context for the homelab agent` summary of what changed (`:12-32`).
- Numbered `## N. <change>` sections, each with a fenced config/SQL/HCL block and placeholders-only secrets (`:36-68`), ending with deploy ordering via `datum@nox` / `datum@lux`.

**Content (D-09/D-10):** OpenTofu OCI A1 module spec (2 OCPU/12 GB, Ubuntu 24.04 arm64, boot volume, SSH key, VCN/subnet/IGW/route-table/security-list), the Tailscale grants ACL JSON (A1→`lux:{5432,6379,8000}` + `nox→A1:22`), the `phaze_broker` PG role SQL, and the 7-step deploy ordering. Carry the SAME placeholders-only-never-real-secrets discipline (`36-...:66-68`).

---

## Shared Patterns

### `Field(...)` with `validation_alias=AliasChoices(...)`
**Source:** `config.py:292` (`enable_saq_ui`), `:376` (`cloud_route_threshold_sec`).
**Apply to:** the new `cloud_burst_enabled` field. Every operator-facing knob uses `AliasChoices("PHAZE_<NAME>", "<name>")` (dual form) + a `description=` sourced into `docs/configuration.md`. Bools get no bounds; ints get `gt=/lt=` fail-fast bounds.
```python
cloud_burst_enabled: bool = Field(
    default=False,
    validation_alias=AliasChoices("PHAZE_CLOUD_BURST_ENABLED", "cloud_burst_enabled"),
    description="Master switch for the cloud-burst feature ... (Phase 51, CLOUDDEPLOY-04).",
)
```

### Clean cron no-op (never raise)
**Source:** `release_awaiting_cloud.py:130-135` (GATE 1).
**Apply to:** the `stage_cloud_window` toggle gate. Disabled/short-circuit paths return the normal result dict (`{"staged": 0, "skipped": 0}`); they NEVER raise (T-50-cron-raise).

### YAML-parse compose invariant test (no docker daemon)
**Source:** `tests/test_deployment/test_agent_compose.py:32-137`.
**Apply to:** `test_cloud_agent_compose.py`. Use `yaml.safe_load` (asserts raw `${VAR}` tokens, NOT interpolated runtime values); `Path(__file__).resolve().parents[2] / "<compose>"`; `_env_to_strs` helper for list-or-dict `environment`.

### `_FILE`-secret machinery (already complete — do not extend for the toggle)
**Source:** `config.py:438` (`SECRET_FILE_FIELDS`), `:444` (`SECRET_FILE_PRESERVE_WHITESPACE`), `_resolve_secret_files` validator; documented at `configuration.md:21-48`.
**Apply to:** the cloud-agent compose mounts the existing `*_FILE` paths (`PHAZE_QUEUE_URL_FILE`, `PHAZE_AGENT_TOKEN_FILE`, `PHAZE_PUSH_SSH_KEY_FILE`, `PHAZE_PUSH_KNOWN_HOSTS_FILE`); the config table documents which fields are `_FILE`-bearing. NO new resolution code.

### Doc file conventions
**Source:** every file in `docs/` starts with `<!-- generated-by: gsd-doc-writer -->`; tables use `| col | col |` with emoji-prefixed purposes in index tables; operator runbooks use numbered `## Step N — ...` headings (`deployment.md`).
**Apply to:** all four doc files + the homelab prompt (`<!-- generated-by: gsd-executor -->` marker for the planning-dir deliverable, matching the Phase 36 analog).

---

## No Analog Found

None. Every file in this phase maps to a strong in-repo analog (the phase is deliberately a deployment/config/docs phase mirroring shipped Phase 49/50 machinery). The genuinely external surface — Tailscale grants JSON, OCI A1 OpenTofu, the `phaze_broker` PG role SQL — is fully specified in RESEARCH (with empirical PG verification) and is authored as a documentation/spec deliverable, not as new phaze code; the homelab change-prompt itself has an exact precedent (Phase 36).

## Metadata

**Analog search scope:** `src/phaze/config.py`, `src/phaze/routers/pipeline.py`, `src/phaze/tasks/release_awaiting_cloud.py`, `docker-compose.agent.yml`, `tests/test_deployment/`, `docs/`, `.planning/milestones/v4.0-phases/36-*/`.
**Files scanned:** 11 (2 fully read analogs: agent compose + its test; targeted reads of config/pipeline/cron/docs).
**Pattern extraction date:** 2026-06-26
