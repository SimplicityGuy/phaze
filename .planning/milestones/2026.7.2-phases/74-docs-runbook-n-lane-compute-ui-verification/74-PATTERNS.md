# Phase 74: Docs, Runbook & N-Lane Compute UI Verification - Pattern Map

**Mapped:** 2026-07-06
**Files analyzed:** 6 (1 new doc, 1 new test, 3 modified, 1 conditional-fix source)
**Analogs found:** 6 / 6

> This is a docs-over-shipped-machinery phase. The only genuine *code* deltas are the compose+guard-test
> parametrization (D-05, mandatory) and — **conditionally** — a `_probe_availability` serialization fix if the
> new regression test exposes the N≥2-compute session race (D-04 "fix if a gap surfaces"). Everything else is
> prose + a mermaid diagram + a TOML/table.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `docs/multi-compute.md` (NEW) | doc | prose/transform | `docs/cloud-burst.md` + `docs/runbook.md` | exact (sibling doc) |
| `docker-compose.cloud-agent.yml` (MOD) | config | deployment artifact | itself (parametrize in place) | exact |
| `tests/agents/deployment/test_cloud_agent_compose.py` (MOD) | test | structural (raw-YAML) | itself (2 assertions to relax) | exact |
| `tests/shared/services/test_lane_snapshot.py` (NEW tests) | test | request-response (service) | `test_snapshot_shape_and_rank_order` (same file, :268) | exact |
| `src/phaze/services/backends.py` `_probe_availability` (CONDITIONAL fix) | service | event-driven (async fan-out) | `_probe_one`/`get_backend_lane_snapshot` (same file) | exact |
| `.planning/REQUIREMENTS.md` + `.planning/ROADMAP.md` (MOD, closeout) | doc/bookkeeping | transform | Phase 66 traceability flip (`test_requirements_traceability.py`) | exact |

## Pattern Assignments

### `docs/multi-compute.md` (NEW doc)

**Analogs:** `docs/cloud-burst.md` (single-A1 provisioning walkthrough), `docs/runbook.md` (N-lane read-out vocabulary).

**Line-1 marker + title pattern** (`docs/cloud-burst.md:1-2`, identical in `runbook.md:1-2`):
```markdown
<!-- generated-by: gsd-doc-writer -->
# Cloud Burst — OCI A1 compute agent (v5.0)
```
Every doc opens with the `<!-- generated-by: gsd-doc-writer -->` HTML comment on **line 1**, then an `# H1`.
Use `# Multi-Compute Agents — mixed arm64/x86 cost-tiered lanes` (or similar).

**Cross-link vocabulary** (`docs/runbook.md:16-21`) — link OUT to the canonical field table, never restate it (D-03):
```markdown
For the **config model** behind all of this — the `backends.toml` registry, the `[[backends]]` /
`[[buckets]]` schema ... — see
[configuration.md → Backend registry](configuration.md#backend-registry-backendstoml) ...
For standing up a cloud target, see [cloud-burst.md](cloud-burst.md) (OCI A1 compute agent) and
[k8s-burst.md](k8s-burst.md) (Kueue cluster).
```
The new doc must cross-link from `cloud-burst.md`, `runbook.md`, `configuration.md § Backend registry`, and
the `docs/README.md` index (D-01). The README index row style (`docs/README.md:29-31`):
```markdown
| **[Operator Runbook](runbook.md)** | 🛠️ Force-local incident revert, reading the N backend lanes ... |
| **[Cloud Burst](cloud-burst.md)** | ☁️ OCI A1 compute-agent deploy, Tailscale ACL, broker role ... |
```

**Per-agent env-var mnemonic — follow the established `PHAZE_AGENT_ID` convention** (`docs/cloud-burst.md:263`,
`docs/deployment.md:322`, `docs/configuration.md:346`). There is **no bare `AGENT_ID` compose variable** any code
reads (`grep -rn PHAZE_AGENT_ID src/` returns nothing). The convention is: `PHAZE_AGENT_ID` is a documentation
mnemonic/placeholder that feeds `PHAZE_AGENT_QUEUE=phaze-agent-<id>` (the SAQ queue the worker consumes) and the
`agent_ref = <id>` backend field. Do NOT introduce a standalone `AGENT_ID` env var in the new doc's per-agent
compose recipe — use `PHAZE_AGENT_ID` as the placeholder feeding `PHAZE_AGENT_QUEUE` / `agent_ref`, exactly like
`cloud-burst.md` and `deployment.md` Step 4.

**Mermaid architecture-at-a-glance pattern** (`docs/cloud-burst.md:38-67`) — mermaid over ASCII, subgraphs
per host, rank annotations inline. Copy this shape; show the rank-tiered drain (arm64 rank 10 → x86 rank 20 →
local rank 99). RESEARCH provides a ready mermaid render-path diagram (74-RESEARCH.md:83-96).

**Rank/cap read-out vocabulary to reuse** (`docs/runbook.md` "Reading the N lanes" §, and the enable-note at
`docs/cloud-burst.md:63-67`): "RANK {n}", "{in_flight}/{cap}", offline word+glyph, "spill to the next rank when
a lane is at `cap`". Keep the new doc and the `_lane_card.html` UI read-out speaking the same language (specifics §89).

**Worked `backends.toml` (D-02/D-03)** — schema-verified shape is in 74-RESEARCH.md:217-249 (fields validated
against `src/phaze/config_backends.py:79-115`: required `agent_ref`/`push_host`/`scratch_dir`, optional `ssh_user`,
`rank ge=0 lt=1000`, `cap gt=0 lt=1000`; duplicate `agent_ref` fails at boot per `config.py:437-450`).

**Secret hygiene (V7/V9, from `docker-compose.cloud-agent.yml:39-41`)** — show the `*_FILE` secret-pointer
pattern only; never inline a token/SSH key/`DATABASE_URL` in the worked compose.

---

### `docker-compose.cloud-agent.yml` (MODIFIED — parametrize image + command, D-05/R-1)

**Analog:** the file itself. Two arm64-hardcoded lines become `${VAR:-default}` with arm64 defaults preserved.

**Current image line** (`docker-compose.cloud-agent.yml:45`):
```yaml
    image: ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64
```
**Current command line** (`:51`):
```yaml
    command: python3 -m saq phaze.tasks.agent_worker.settings
```
**Target parametrization** (RESEARCH Pattern 2, 74-RESEARCH.md:129-137) — arm64 default, x86 override:
```yaml
    # arm64 default; x86 operator overrides to the standard tag (NO -arm64 suffix)
    image: ${PHAZE_CLOUD_AGENT_IMAGE:-ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}-arm64}
    # arm64 default (py3.13/--system → python3); x86 operator sets uv run saq …
    command: ${PHAZE_CLOUD_AGENT_CMD:-python3 -m saq phaze.tasks.agent_worker.settings}
```
> The exact substitution spelling is planner/impl discretion — the load-bearing invariants are (a) the **raw**
> default still ends in `-arm64` / starts `python3 -m saq` (guard test, below) and (b) `docker compose config`
> validates (Assumption A2 — verify locally if colima available). The block header comment (`:34-41`) documenting
> the arm64/x86 wrinkle should be extended to note the two new override vars.

---

### `tests/agents/deployment/test_cloud_agent_compose.py` (MODIFIED — relax guard assertions, D-05/Pitfall 2)

**Analog:** the failing assertions in the same file. `yaml.safe_load` does NOT interpolate, so after
parametrization the raw string is wrapped in `${PHAZE_CLOUD_AGENT_IMAGE:-…}` and the command raw first token
becomes `${PHAZE_CLOUD_AGENT_CMD:-python3`.

**Assertion to relax #1a** (`:116`, in `test_worker_image_is_arm64_ghcr_pinned`):
```python
    assert image.startswith("ghcr.io/simplicityguy/phaze:"), f"worker image must be ghcr.io/simplicityguy/phaze:<tag>; got {image!r}"
```
→ after parametrization the raw image STARTS WITH `${PHAZE_CLOUD_AGENT_IMAGE:-`, so `startswith("ghcr.io/simplicityguy/phaze:")`
evaluates **False**. Relax it to a **substring** check — `assert "ghcr.io/simplicityguy/phaze:" in image` —
consistent with how `PHAZE_IMAGE_TAG in image` (`:117`) is already phrased. Keep the `PHAZE_IMAGE_TAG in image`
pin check as-is.

**Assertion to relax #1b** (`:118`, in `test_worker_image_is_arm64_ghcr_pinned`):
```python
    assert image.endswith("-arm64"), f"worker image MUST end with -arm64 ...; got {image!r}"
```
→ must accept the `${VAR:-…-arm64}` form and assert the **default** still renders arm64 (e.g. `"-arm64}" in image`
or `"latest-arm64" in image`). The relaxed pair (#1a substring prefix + #1b arm64-default marker) must still prove
the arm64 DEFAULT renders — this is a relaxation to tolerate the `${VAR:-default}` wrapper, NOT a blanket removal.

**Assertion to relax #2** (`:144`, in `test_worker_command_invokes_system_python_not_uv`):
```python
    tokens = command.split() if isinstance(command, str) else [str(t) for t in command]
    assert "uv" not in tokens, ...
    assert tokens[:3] == ["python3", "-m", "saq"], ...
```
→ the raw first token becomes `${PHAZE_CLOUD_AGENT_CMD:-python3`; update to recognize the `${VAR:-default}` form
and assert the **default** is `python3 -m saq …` (the arm64 default preserved, `uv` still forbidden as the default).
The existing `command is None` early-return branch (`:136-138`) stays valid.

---

### `tests/shared/services/test_lane_snapshot.py` (NEW regression tests, D-04/R-2)

**Analog:** `test_snapshot_shape_and_rank_order` (same file, `:268-321`) — the canonical `get_backend_lane_snapshot`
test. It builds real backend impls, monkeypatches `resolve_backends`, seeds `CloudJob` rows, asserts composed lane dicts.

**Core snapshot-test pattern to copy** (`test_lane_snapshot.py:270-278`):
```python
    local = LocalBackend(id="local", rank=99, cap=1)
    compute = ComputeAgentBackend(id="a1", rank=10, cap=2)
    kueue = KueueBackend(id="k8s", rank=20, cap=3)
    monkeypatch.setattr(backends_mod, "resolve_backends", lambda _settings: [local, compute, kueue])

    async def _fake_probe(_session: Any, _backends: Any) -> dict[str, bool]:
        return {"local": True, "a1": True, "k8s": False}
    monkeypatch.setattr(backends_mod, "_probe_availability", _fake_probe)
```
Backend ctors are keyword: `ComputeAgentBackend(id=..., rank=..., cap=...)` (see `_kind_of` test at `:255`).

**Variant A — deterministic "one lane per compute backend"** (monkeypatched probe, mirrors existing tests).
RESEARCH sketch (74-RESEARCH.md:107-121): two `ComputeAgentBackend` + one `LocalBackend`; assert
`[ln["id"] for ln in compute_lanes] == ["a1-arm64", "x86-spill"]` and `len(compute_lanes) == 2` (no kind dedup).

**Variant B — REAL probe fan-out (the R-2 race arbiter)** — do **NOT** monkeypatch `_probe_availability`
(Pitfall 1). Register two ONLINE compute agents in the DB so `ComputeAgentBackend.is_available` →
`select_agent_by_id` (backends.py:295-308) returns online, then assert **both** compute lanes come back
`available=True`.

**Agent-seeding fixture to reuse — do NOT hand-roll** (`tests/_queue_fakes.py:331`, already imported by
`tests/shared/services/test_enqueue_router.py:39`):
```python
async def seed_active_agent(session, agent_id="nox", *, kind="fileserver") -> Agent:
    # inserts a non-revoked, recent-last_seen_at agent; COMMITS + refreshes (WR-03 canonical fixture)
```
Usage (from `test_enqueue_router.py:150-151`): `await seed_active_agent(session, "compute-01", kind="compute")`.
For the real-fan-out variant the two `agent_id`s MUST match the two backends' `agent_ref` values (the compute
backend's bound config `agent_ref` is what `select_agent_by_id` looks up). NOTE: the plain `ComputeAgentBackend(id=…)`
ctor has no bound `config`; the real-fan-out variant needs a construction whose `_agent_ref()` returns the seeded
id — check how existing tests bind `agent_ref` (grep `agent_ref` under `tests/analyze/services/test_backends.py`
and `tests/shared/config/test_backend_registry.py`) rather than hand-rolling config.

**degrade-path stand-ins available for reuse** (same file `:79-151`): `_ExplodingSession`, `_SlowBackend`,
`_FastBackend`, `_RaisingBackend`, `_PoisonRecoverSession`, `_DbPoisoningLane` — the `_DbPoisoningLane`/
`_PoisonRecoverSession` pair (`:340-413`) is the closest existing model of a compute probe poisoning the shared
session; study it before writing the real-race assertion.

---

### `src/phaze/services/backends.py` `_probe_availability` (CONDITIONAL fix, D-04/Pitfall 1)

**Only touch if Variant B fails.** Current fan-out (`backends.py:651-661`) gathers ALL probes over ONE shared session:
```python
async def _probe_availability(session: AsyncSession, backends: list[Backend]) -> dict[str, bool]:
    results = await asyncio.gather(*(_probe_one(session, backend) for backend in backends))
    return dict(results)
```
The docstring (`:655-658`) still asserts the **retired** "D-05 invariant caps compute at ≤1" claim — STALE since
Phase 72 (MCOMP-01). Update the docstring when touching the file regardless.

**Fix pattern (if the race manifests):** local probes short-circuit (no I/O, `_probe_one:641-642`) and Kueue probes
use kr8s (no session), so keep those concurrent but **serialize the compute (`select_agent_by_id`) probes** — the
only ones touching the shared `AsyncSession`. The existing `_kind_of` isinstance dispatch (`:664-672`) is the
seam for partitioning `compute` vs non-compute backends. The post-fan-out `await session.rollback()` guard in
`get_backend_lane_snapshot:697` already handles single-probe poisoning but NOT two probes racing before it
(Pitfall 1 warning). If edited, `services/backends.py` must stay ≥90% per-module coverage (CLAUDE.md floor).

**Confidence: MEDIUM** — whether the race deterministically trips depends on asyncpg/SQLAlchemy await interleaving
(Assumption A1). Variant B is the arbiter; report to planner as *verify-then-fix*, not a certain bug.

---

### `.planning/REQUIREMENTS.md` + `.planning/ROADMAP.md` (MODIFIED at CLOSEOUT, Pitfall 4)

**Analog:** the Phase 66 traceability flip, guarded by `tests/shared/core/test_requirements_traceability.py`.
NOT part of the implementation plans — done at phase closeout after `74-VERIFICATION.md` reports `status: passed`.
Flip MCOMP-07 checkbox `- [ ]`→`- [x]` AND its Traceability row `Pending`→`Complete` (both must agree), and
ROADMAP Phase 74 `- [ ]`→`- [x]`. Run `just docs-drift` before merge. In-flight `[ ]`+Pending is tolerated during
execution (only bites at closeout).

## Shared Patterns

### Doc line-1 marker + cross-link discipline
**Source:** `docs/cloud-burst.md:1`, `docs/runbook.md:1-21`, `docs/README.md:29-31`
**Apply to:** `docs/multi-compute.md` (new) + the four cross-linkers (cloud-burst, runbook, configuration, README index)
- `<!-- generated-by: gsd-doc-writer -->` on line 1, `# H1` on line 2.
- Link OUT to `configuration.md#backend-registry-backendstoml` for field schema; show scenario only (D-03).
- No `test_docs_ia_current.py` assertion enforces README-index membership of the new doc (it checks specific
  DAG/shell content, `:91-116`), so the index entry is a convention to honor, not a gated requirement.

### Secret hygiene (V7/V9)
**Source:** `docker-compose.cloud-agent.yml:39-41` (`*_FILE` machinery), `backends.py` lane dicts (secret-free keys)
**Apply to:** the new doc's worked compose + `backends.toml` examples
- Lane snapshot carries ONLY `{id, kind, rank, cap, in_flight, available, quota_wait, inadmissible}` — no SecretStr.
- Docs must reference `*_FILE` env pointers, never inline creds/tokens/SSH keys.

### Real-DB agent seeding (test)
**Source:** `tests/_queue_fakes.py:331` `seed_active_agent(session, id, *, kind=…)`
**Apply to:** the real-fan-out lane regression variant (Variant B)
- Commits a non-revoked, fresh-`last_seen_at` `Agent`; `kind="compute"` makes `select_agent_by_id(..., kind="compute")` resolve online.

## No Analog Found

None. Every deliverable maps to an in-repo sibling (a sibling doc, the file itself, an adjacent test, or an
in-file helper). This is a docs+verify phase over Phase 71/72/73 machinery.

## Metadata

**Analog search scope:** `docs/`, `tests/shared/services/`, `tests/agents/deployment/`, `tests/`,
`src/phaze/services/backends.py`, `docker-compose.cloud-agent.yml`, `tests/_queue_fakes.py`, `tests/conftest.py`
**Files scanned:** ~12
**Pattern extraction date:** 2026-07-06
