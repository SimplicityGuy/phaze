# Phase 72: Per-Entry Compute Binding & Fail-Fast Retirement - Pattern Map

**Mapped:** 2026-07-05
**Files analyzed:** 4 modified (0 created) + 1 boundary-only (must NOT change)
**Analogs found:** 4 / 4 (all in-file / same-module precedents — this is a behavior-preserving refactor with a direct Phase-70 twin)

## Orientation

This phase is a **compute-side mirror of Phase 70 (MKUE-01)**. Every pattern it needs already exists
in the same three files it modifies — the work is *wiring an existing field* (`ComputeBackend.agent_ref`)
into resolution and *retiring two `>1` raises*, not introducing new abstractions. There is **no new
file**. All analogs are in-module (`services/backends.py`, `config.py`, `config_backends.py`), which
is exactly what a groundwork phase should look like: copy the sibling Kueue accessor, copy the sibling
container validator branch.

## File Classification

| Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---------------|------|-----------|----------------|---------------|
| `src/phaze/services/backends.py` (`ComputeAgentBackend`, `resolved_non_local_kind`, `resolve_backends`) | service (backend impl + registry factory) | request-response (per-tick dispatch) | `KueueBackend._kube()` / `KueueBackend.dispatch` — **same file, sibling class** | exact (in-file twin) |
| `src/phaze/config.py` (`active_compute_scratch_dir`, `_validate_registry`) | config (settings accessor + container validator) | transform (registry reduction) | `_validate_registry` bucket-dup / cluster-specific branches — **same method** | exact (in-file twin) |
| `src/phaze/config_backends.py` (`ComputeBackend`) | model (pydantic submodel) | transform (config validation) | `ComputeBackend._require_dispatch_fields` / `KueueBackend._require_kube` — **same file** | exact (in-file idiom) |
| `src/phaze/models/agent.py` (read-only reference) | model | — | N/A — read to confirm `Agent.id` PK slug is the binding key (D-01) | reference only |

## Pattern Assignments

### `src/phaze/services/backends.py` → per-entry compute binding accessor (D-02, discretion)

**Analog:** `KueueBackend._kube()` — **the exact template** (CONTEXT `<code_context>` calls it out by name).

**Fail-loud per-entry binding accessor** (`backends.py:313-324`):
```python
def _kube(self) -> KubeConfig:
    kube = getattr(self.config, "kube", None)
    if kube is None:
        raise kube_staging.KubeStagingError(f"kueue backend {self.id!r} has no [kube] config bound")
    return cast("KubeConfig", kube)
```
The compute twin should read `self.config.agent_ref` the same way — bound once in `resolve_backends`,
read per call, defense-in-depth fail-loud (the `_require_dispatch_fields` validator already guarantees
`agent_ref` is non-empty at construction, so this raise is belt-and-suspenders). Per D-05, the *resolution*
of that ref to a live Agent must **degrade to hold**, not raise (see next excerpt).

**Where the binding is recorded** — `resolve_backends` already constructs one `ComputeAgentBackend`
per entry and threads `config=entry` (`backends.py:461-462`):
```python
elif entry.kind == "compute":
    resolved.append(ComputeAgentBackend(id=entry.id, rank=entry.rank, cap=entry.cap, config=entry))
```
`self.config` is already the bound `ComputeBackend` submodel carrying `agent_ref` — **no constructor
change needed**, mirroring how `KueueBackend` reads `self.config.kube`. This is the "record-don't-rederive"
(MKUE-01) established pattern.

**The `select_active_agent(kind="compute")` seam to REPLACE** (`ComputeAgentBackend.is_available`,
`backends.py:245-255`):
```python
async def is_available(self, session: AsyncSession) -> bool:
    try:
        await select_active_agent(session, kind="compute")   # <-- single-active pick; replace with per-entry Agent.id lookup
    except NoActiveAgentError:
        return False
    return True
```
Per D-02 this "the single active compute agent" assumption is replaced by resolving **this backend's
bound `self.config.agent_ref` against `Agent.id`**. Per D-05 an absent/unregistered agent still returns
`False` (never raises) — the degrade-safe hold pattern below is preserved verbatim.

**Degrade-safe absent-agent → hold** (T-68-05, the invariant D-05 must preserve) — this catch structure
is the template for the new per-entry lookup's miss path:
```python
except NoActiveAgentError:
    logger.info("LocalBackend.dispatch hold: no fileserver agent online", file_id=str(file.id))
    return False
```
(`backends.py:217-219`; the compute equivalent returns `False` from `is_available`, `backends.py:253-254`.)

**Fail-fast retirement in `resolved_non_local_kind`** (D-03) — remove the compute-only `>1` raise
(`backends.py:492-498`):
```python
# REMOVE this raise (keep returning "compute" for the single-lane path; per D-03 generalize for N compute):
if len(non_local) > 1:
    raise ValueError(
        f"multiple compute backends {[backend.id for backend in non_local]} are configured, but "
        f"resolved_non_local_kind reduces a single compute lane (multi-compute lands in PROV-01)"
    )
return non_local[0].kind
```
Per discretion note in D-decisions: the compute-only branch **should still return `"compute"`** for N
compute — the generalization just drops the raise (`non_local[0].kind` already yields `"compute"` for a
compute-only registry). Confirm during planning that the kueue-any branch above (`backends.py:490-491`)
is untouched.

**ComputeAgentBackend.dispatch** (`backends.py:257-293`) also calls `select_active_agent(session, kind="fileserver")`
(the *push initiator*, `backends.py:269`) — that is a **fileserver** lookup, NOT the compute binding;
leave it as-is. Only the `kind="compute"` seam moves to per-entry binding.

---

### `src/phaze/config.py` → retire the compute `>1` raise + add duplicate-`agent_ref` boot guard (D-03, D-04)

**Analog (retire):** `active_compute_scratch_dir` `>1` raise (`config.py:483-487`) — the direct sibling
of the `resolved_non_local_kind` raise, same PROV-01 message shape:
```python
compute = [backend for backend in self.backends if backend.kind == "compute"]
if not compute:
    return None
if len(compute) > 1:                                      # <-- REMOVE per D-03
    raise ValueError(
        f"multiple compute backends {[backend.id for backend in compute]} are configured, but "
        f"active_compute_scratch_dir reduces a single compute backend (multi-compute lands in PROV-01)"
    )
backend = compute[0]
return backend.scratch_dir if isinstance(backend, ComputeBackend) else None
```
Per D-07 keep the ≤1-compute resolution of `scratch_dir` **byte-identical** (still returns the single
compute backend's `scratch_dir` for the ≤1 case). Generalizing the >1 case is Phase-73 territory for the
per-agent scratch widening — for Phase 72 the accessor just stops raising; confirm the ≤1 return value is
unchanged.

**Analog (add boot guard):** `_validate_registry` duplicate-bucket-id branch (`config.py:434-436`) — the
**exact idiom** D-04 asks for (id-tagged, `Counter`-based, container-level `@model_validator(mode="after")`):
```python
dupes = sorted(bid for bid, count in Counter(b.id for b in self.buckets).items() if count > 1)
if dupes:
    raise ValueError(f"duplicate bucket ids in registry: {dupes} — each [[buckets]] id must be unique (REG-05)")
```
The duplicate-`agent_ref` guard should copy this shape verbatim over the compute backends' `agent_ref`
values, naming the offending ref(s). `Counter` is **already imported** (`config.py:10`), so no new import.
Note the sibling cluster-specific `refs`/`len(refs) > 1` branch (`config.py:451-455`) shows the
alternate "collect-then-check-cardinality" style if the planner prefers naming the *entry ids* that
collide rather than the ref value:
```python
for bid, refs in cluster_specific_refs.items():
    if len(refs) > 1:
        raise ValueError(
            f"bucket {bid!r} is scope=cluster-specific but referenced by {len(refs)} kueue backends {refs} — at most one allowed (D-09)"
        )
```
Per discretion, placement is **container-level `_validate_registry`** (a cross-entry invariant the
per-variant submodel can't see — exactly why bucket-dup lives here, not on `BucketConfig`). This is the
established home for "whole-registry invariants the per-variant submodels can't see" (`config.py:416`).

Per D-05, this validator must **NOT** check DB existence of the agent — it is a *static, deterministic*
config check (duplicate detection only). An `agent_ref` naming an unregistered agent is legal at boot.

---

### `src/phaze/config_backends.py` → `ComputeBackend` submodel (D-01, existing field)

**Analog:** `ComputeBackend._require_dispatch_fields` / `KueueBackend._require_kube` (id-tagged fail-fast idiom).

**`agent_ref` field already exists** (`config_backends.py:88`) — Phase 72 does **NOT** add it:
```python
agent_ref: str | None = None
scratch_dir: str | None = None  # was ControlSettings.compute_scratch_dir (D-13)
```

**id-tagged per-variant validator** (the message style D-04's guard must mirror) — `config_backends.py:91-104`:
```python
@model_validator(mode="after")
def _require_dispatch_fields(self) -> "ComputeBackend":
    if not self.agent_ref:
        raise ValueError(f"backend {self.id!r} (kind=compute) requires an agent_ref")
    if not self.scratch_dir:
        raise ValueError(f"backend {self.id!r} (kind=compute) requires a scratch_dir")
    return self
```
And `KueueBackend._require_kube` (`config_backends.py:119-125`) shows the terse `msg = f"..."` variant.
Both fail loud with the offending entry `id` — the new duplicate-`agent_ref` guard follows this
"name the offending entry id in the message" convention.

**D-01 binding key confirmation** — `Agent.id` is the PK slug + FK target (`models/agent.py:25`,
`__table_args__` CheckConstraint `id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'` at `models/agent.py:35`), NOT
`Agent.name` (free-form `String(128)`, `models/agent.py:26`). `agent_ref` resolves against `Agent.id`.

---

## Shared Patterns

### Record-don't-rederive (MKUE-01)
**Source:** `services/backends.py:461-464` (`resolve_backends` threads `config=entry`), read per-call via `self.config` (`backends.py:321`, `backends.py:366`, `backends.py:378`).
**Apply to:** the compute binding accessor — bind `agent_ref` once at construction (already threaded), read `self.config.agent_ref` per call. Never re-derive via a module-global selector.

### Degrade-safe absent-agent → hold (T-68-05, load-bearing for D-05)
**Source:** `services/backends.py:251-254` (`is_available` catches `NoActiveAgentError` → `False`), `backends.py:217-219` (dispatch hold logs + returns `False`).
**Apply to:** the new per-entry `agent_ref → Agent.id` resolution's miss path. An unregistered / not-checked-in agent yields `is_available` False; **never raises**. Preserves the cron no-op discipline.

### id-tagged fail-fast (D-04 boot guard style)
**Source:** `config_backends.py:101,103,123` (per-variant); `config.py:436,454` (container-level, `Counter`-based).
**Apply to:** the duplicate-`agent_ref` boot validator — name the offending ref/entry-id in the message, fail at `_validate_registry` (static, no DB).

### Generalize-not-descope for fail-fasts (WR-01, Phase 70)
**Source:** `resolved_non_local_kind` kueue-any branch (`backends.py:490-491`) already tolerates N Kueue.
**Apply to:** both `>1` compute raises (`backends.py:494`, `config.py:483`) — drop the raise, keep the ≤1 return value byte-identical.

## Boundary — Files That Must NOT Change (Phase 73 territory, D-07)

| File | Why untouched |
|------|---------------|
| `src/phaze/routers/agent_push.py` (`~L133`, `report_pushed`) | Reads `settings.active_compute_scratch_dir` to build `f"{...}/{file_id}.{file.file_type}"` (`agent_push.py:133`). Per D-07 the ≤1-compute `/pushed` path stays **byte-identical**; per-agent scratch/push-destination widening (MCOMP-03) lands in Phase 73. The `select_active_agent(session, kind="compute")` call at `agent_push.py:93` is Phase-73 dispatch-attribution territory — leave it. |

## Behavior-Preserving Proof Precedent (D-06)

**Source pattern:** Phase 68 D-01 golden byte-identical characterization (referenced in `backends.py`
module docstring, `backends.py:19-24`). The acceptance bar is: (1) golden characterization of the
≤1-compute dispatch/resolution path before-vs-after, PLUS (2) an explicit zero-compute (all-local)
regression proving no cloud activity. The `_default_local_registry` factory (`config_backends.py:223-229`,
`id="local", rank=99, cap=1`) is the all-local baseline the zero-compute regression exercises;
`cloud_enabled` (`config.py:459-466`) returning `False` for the implicit-local registry is the
"no cloud activity" assertion surface.

## No Analog Found

None. Every pattern this phase needs is an in-module sibling (the deliberate Phase-70 compute twin).
This is expected for behavior-preserving groundwork.

## Metadata

**Analog search scope:** `src/phaze/services/backends.py`, `src/phaze/config.py`, `src/phaze/config_backends.py`, `src/phaze/services/enqueue_router.py`, `src/phaze/models/agent.py`, `src/phaze/routers/agent_push.py`
**Files scanned:** 6
**Pattern extraction date:** 2026-07-05
