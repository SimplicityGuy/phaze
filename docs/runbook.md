<!-- generated-by: gsd-doc-writer -->
# Operator Runbook — backend lanes, force-local revert & secrets

This is the day-to-day operator runbook for the multi-cloud backend registry (2026.7.1). It
covers the incident controls and read-outs an operator uses in the v7.0 console shell (`/` — the
legacy `GET /pipeline/` 302-redirects there):

- **[Force-local incident revert](#force-local-incident-revert)** — the master toggle that pins
  all analysis to local, live, with no redeploy.
- **[Reading the N lanes](#reading-the-n-lanes)** — how to read the registry-derived lane grid on
  the Analyze workspace (rank order, in-flight/cap, offline, Kueue admission).
- **[Spillover behavior](#spillover-behavior)** — how the tiered scheduler drains long files across
  backends by rank and cap.
- **[Per-backend `_FILE` secrets](#per-backend-_file-secrets)** — where backend credentials live and
  the one rule: never print a secret value.
- **[Stranded `active` SAQ jobs (phaze-o0n6)](#stranded-active-saq-jobs-phaze-o0n6)** — the
  `phaze queue status` guard, what its non-zero exit means, and how to clear a pre-existing backlog.
- **[Removing fingerprint-era data (phaze-0jpe)](#removing-fingerprint-era-data-phaze-0jpe)** — a
  one-time, manual, post-deployment cleanup of the retired `audfprint`/Panako sidecars' on-host
  volumes and published images. Not part of routine operation.

For the **config model** behind all of this — the `backends.toml` registry, the `[[backends]]` /
`[[buckets]]` schema, and the trivial `cloud_target`→`backends` mapping — see
[configuration.md → Backend registry](configuration.md#backend-registry-backendstoml) and
[configuration.md → Cloud target](configuration.md#cloud-target-removed-in-phase-67). For standing
up a cloud target, see [cloud-burst.md](cloud-burst.md) (OCI A1 compute agent) and
[k8s-burst.md](k8s-burst.md) (Kueue cluster). For adding a **2nd+ compute agent**, cost-tiered
across mixed arm64/x86 hosts, see [multi-compute.md](multi-compute.md).

## Force-local incident revert

When a cloud backend misbehaves (a Kueue cluster is wedged, a compute agent is unreachable, or a
staging bucket is throwing errors) and you want **all** analysis to run on the local file server
**right now**, use the **force-local master toggle** in the shell header. It is the incident
"pull everything back to local" switch — reversible, one click, and **no redeploy**.

**Where it is.** The shell header carries a single master pill, seeded on **every** page (not just
Analyze), so the global incident control shows correct state everywhere:

| Pill state | Meaning |
|------------|---------|
| `CLOUD ROUTING` (neutral pill) | Normal operation — backends dispatch by rank across the registry (multi-backend routing active). |
| `FORCED LOCAL` (amber incident pill) | Engaged — all routing is pinned to local; no new cloud/Kueue dispatch happens. |

**Engaging it.** Click the pill to toggle `CLOUD ROUTING` → `FORCED LOCAL`. This writes a durable
`route_control` row (it survives a restart — the switch is state, not an env var) and takes effect
**live**, gating routing at two places at once:

- **The drain** — `stage_cloud_window` no-ops while forced, so no file is staged to S3 or pushed to
  a compute agent.
- **The duration router** — new long files (duration ≥ the route threshold) route to the **local**
  queue instead of being held for cloud.

```mermaid
stateDiagram-v2
    [*] --> CLOUD_ROUTING
    CLOUD_ROUTING: CLOUD ROUTING
    CLOUD_ROUTING: backends dispatch by rank (multi-backend)
    FORCED_LOCAL: FORCED LOCAL
    FORCED_LOCAL: durable route_control row · reversible · no redeploy
    CLOUD_ROUTING --> FORCED_LOCAL: click pill (engage)
    FORCED_LOCAL --> CLOUD_ROUTING: click pill (revert)

    state FORCED_LOCAL {
        [*] --> Gates
        Gates: Two gates fire at once
        Gates --> Drain: stage_cloud_window no-ops (no stage/push)
        Gates --> Router: duration router → local queue
        --
        Held: Files already held in AWAITING_CLOUD
        Held: stay held (drain no-ops — neither dispatched nor spilled)
    }
```

**Reverting it.** Click the pill again to toggle `FORCED LOCAL` → `CLOUD ROUTING`. You will see the
confirmation "Cloud routing restored — backends dispatch by rank." Normal rank-tiered dispatch
resumes on the next drain tick. Reverting is the **safe** direction — there is nothing destructive
about this toggle, so it is fine to flip it during an incident and flip it back once the backend
recovers.

> **Held-file note (read this before you engage it).** Engaging force-local does **not** yank work
> that is already in flight. Files **already held** in `AWAITING_CLOUD` when you engage force-local
> **stay held** — the drain no-ops, so it neither dispatches them nor spills them back to local. It
> is only **new** long files that route local while forced. Held files release and resume normal
> rank-tiered dispatch once you revert to `CLOUD ROUTING` (or, for a single file, once its backend
> comes back and the drain runs). If you need those held files analyzed **locally** during a long
> outage, that is a manual re-drive, not an automatic effect of the toggle.

This toggle replaces the old "set `PHAZE_CLOUD_TARGET=local` and restart the control plane" dance —
that flat selector was removed in Phase 67 (see
[configuration.md → Cloud target](configuration.md#cloud-target-removed-in-phase-67)).

## Reading the N lanes

The **Analyze workspace** renders one **lane card per registry backend** — a `local` lane plus one
card for each `compute` and `kueue` backend you declared in `backends.toml`. The grid is the primary
signal for "where is analysis running and is any backend in trouble."

**Rank order = dispatch preference.** Cards render **rank ascending, left-to-right / top-to-bottom**
— the **lowest rank is the most-preferred (cheapest) backend and is used first**, so the **top-left
lane is what gets used first**. The implicit `local` backend sorts last (rank 99). Reading the grid
left-to-right is therefore reading the scheduler's dispatch order.

**Each lane card shows:**

| Element | What it tells you |
|---------|-------------------|
| Title `{glyph} {KIND · ID}` + `RANK {n}` caption | Which backend this is and its cost-tier rank (dispatch preference). |
| Capacity numeral `{in_flight}/{cap}` | How many analyses this backend is running vs its concurrency cap. The capacity bar fills to `in_flight / cap`. |
| `available` sub-label | The lane is up and taking work (e.g. `short sets < 90 min` for local, `long sets ≥ 90 min` for a compute lane). |
| `offline` word (amber) + greyed glyph | The lane's availability probe failed **for this poll only** — it is isolated and never stalls the rest of the grid. |
| Kueue admission caption `{quota_wait} waiting · {inadmissible} inadmissible` | For `kueue` lanes only: how many workloads are waiting on quota vs how many are **Inadmissible**. |

**Quota-wait vs Inadmissible (the Kueue distinction that matters).** On a Kueue lane the caption
separates two very different conditions:

- **`{n} waiting`** — workloads are queued behind cluster **quota** and will admit when capacity
  frees up. This is **healthy back-pressure**; do nothing.
- **`{n} inadmissible`** — one or more workloads are **Inadmissible**: Kueue is refusing to admit
  them because of an **operator/cluster misconfiguration** (a missing or mis-sized LocalQueue /
  ClusterQueue). This segment turns **amber** and is word-labelled as an alert when it is > 0. An
  Inadmissible workload **waits indefinitely without consuming the re-drive budget** — so it will
  not fail on its own; you have to fix the cluster. See
  [k8s-burst.md → Cluster-admin runbook](k8s-burst.md#cluster-admin-runbook) for the LocalQueue /
  ClusterQueue objects to check, and the `localqueues: get` RBAC verb the startup probe needs.

Every lane state is shown with a **word and a glyph**, never color alone — an `offline` lane says
"offline", an Inadmissible count is labelled "inadmissible" — so the grid is readable without relying
on hue.

**If the whole grid is unavailable** you will see `Lane status unavailable` with the note that lane
status could not be read this cycle; it refreshes on the next update. A single failing backend
degrades to that **one** lane rendering `offline`, never a page error.

## Spillover behavior

The tiered scheduler drains long files across the registry **by rank, then spills on cap**:

1. For each long file eligible for cloud analysis, the scheduler considers backends in **ascending
   rank** — cheapest/most-preferred first.
2. A backend is **eligible** only if it is **available** (its probe passed) and its **in-flight count
   is below its `cap`**.
3. If the top-ranked backend is **at `cap`** (its lane shows `{cap}/{cap}`), the file **spills** to
   the next eligible backend down the rank order. If an entire tier is full or offline, work
   continues spilling to the next tier — and ultimately the `local` backend (rank 99) can be the
   final catch.
4. A backend that goes **offline** is simply skipped for that drain tick; its would-be work spills to
   the next eligible lane, and it re-enters the rotation automatically when its probe recovers.

**The full→local spill is staleness-gated; the offline→local spill is not.** The final catch to
`local` is **not** unconditional — the two ways a tier can be unusable are treated differently
(`services/backend_selection.py`):

- **Every non-local backend is `offline`** (probe failed) → `local` becomes eligible **immediately**;
  the file spills to local on that same drain tick.
- **Higher-rank backends are online but `FULL`** (`in_flight` at `cap`) → `local` is eligible **only
  after** the file has waited in `AWAITING_CLOUD` past `cloud_spill_to_local_after_seconds`
  (`PHAZE_CLOUD_SPILL_TO_LOCAL_AFTER_SECONDS`, default **900 s / 15 min**). Until that threshold the
  file **stays held** rather than spilling to slow local — this absorbs a transient full window so
  short cap spikes do not dump long sets onto the local file server. Once the wait elapses (or the
  file exhausts its cloud attempt budget), local becomes eligible and it spills.

Reading this off the grid: when you see the top-left lane sitting at `{cap}/{cap}` and the next lane
picking up new in-flight work, that is spillover working as designed — not a fault. If **every** cloud
tier is at cap and files are **not** yet spilling to `local`, that is the staleness gate holding them
for the 15-minute window, not a stall. Persistent spillover all the way to `local` for **long** files
usually means every cloud tier is either offline or has been at cap past the threshold; check the
offline lanes and any Inadmissible Kueue caption.

```mermaid
flowchart TD
    start([Long file eligible for cloud]) --> rank[Walk backends in ascending rank]
    rank --> avail{Backend available?<br/>probe passed}
    avail -- no --> nextoff[Skip this tick; try next rank]
    avail -- yes --> slot{in_flight &lt; cap?}
    slot -- yes --> disp[["Dispatch here (rank-first winner)"]]
    slot -- no --> nextfull[Spill to next rank]
    nextoff --> more{More backends?}
    nextfull --> more
    more -- yes --> rank
    more -- no --> localcatch{Why is every<br/>non-local tier unusable?}
    localcatch -- "all OFFLINE" --> localnow[["local eligible NOW → spill to local"]]
    localcatch -- "online but FULL" --> gate{"waited &ge; cloud_spill_to_local_after_seconds?<br/>(default 900s)"}
    gate -- no --> hold[["Stay held in AWAITING_CLOUD this tick"]]
    gate -- yes --> localgated[["local eligible → spill to local"]]
```

While **force-local** is engaged, none of this runs — the drain no-ops and new long files route
straight to local (see [Force-local incident revert](#force-local-incident-revert)).

## "The cloud lane is not draining" warning

A held file is deliberately silent: the drain must not touch the parked row, because its `updated_at`
**is** the staleness clock the spill gate above reads. That silence once hid a full day of starvation —
a lane logging `staged: 0, skipped: 3` every 5 min while a cloud backend sat idle with free capacity.

So the drain now escalates. When **three consecutive ticks** (~15 min) hold **100%** of every candidate
they examined, it logs at WARNING:

```
stage_cloud_window: every candidate held on consecutive ticks -- the cloud lane is not draining
    consecutive_all_held_ticks=3 hold_reasons={'cloud_attempts_exhausted': 14}
    candidates_scanned=14 free_slots=3
```

`hold_reasons` is the actionable part — each names which selection filter rejected the candidates:

| Reason | Means | What to do |
|---|---|---|
| `cloud_attempts_exhausted` | These files spent their cloud budget (`cloud_submit_max_attempts`), so they can only route **local** — and local has no free slot. | **Act.** An attempt counter only grows, so this never clears on its own. Give the local backend headroom (raise its `cap`, or let its in-flight work finish), or investigate why those files kept failing their cloud submits. |
| `local_spill_not_reached` | Cloud is online but full, and the files have not yet waited out `cloud_spill_to_local_after_seconds`. | **Wait.** This is the staleness gate working; it clears itself when the clock elapses. |
| `no_free_slots` | Every backend is offline or at cap. | Check the lane grid and the offline probes — this is fleet-wide saturation, not a per-file problem. |

The routine per-tick INFO line carries the same `hold_reasons` plus `candidates_scanned`, so you can read
the same breakdown without waiting for the warning.

**Note that holds no longer block the queue.** The drain walks *past* unroutable candidates (paging up to
500 rows per tick) and stages routable work from behind them in the same tick, so a run of permanently
held files slows the lane but does not stop it. A lane that is genuinely stuck — the warning firing tick
after tick — means the unroutable run is longer than the per-tick scan budget, and the table above is how
you clear it.

## Per-backend `_FILE` secrets

Backend credentials follow the same **`_FILE` secret convention** used elsewhere in Phaze — the
secret value lives in a file (a Docker/Swarm secret, a Kubernetes secret mount, or a SOPS-decrypted
file), and the config points at the **path**, never the value.

- **Per-backend secrets live inline in `backends.toml`.** Each `kueue` backend's kubeconfig / SA
  token and each staging bucket's S3 access-key / secret-key are inline **`*_file` pointers** inside
  the registry (e.g. a bucket's `access_key_id_file` / `secret_access_key_file`, a Kueue backend's
  `kubeconfig_file` / `sa_token_file`). They are resolved by the shared secret-file helper and are
  **control-plane only** — they are never sent to the file-server agent or the Kueue pod.
- **Control-plane env secrets** (the LLM API keys and the `database_url` / `redis_url` / `queue_url`
  DSNs) still use the env `<VAR>_FILE` form (e.g. `ANTHROPIC_API_KEY_FILE`,
  `PHAZE_QUEUE_URL_FILE`).

For the exact field list, the `_FILE` resolution semantics (precedence, newline-stripping, fail-fast
on a missing file), and which fields are secret-bearing, see
[configuration.md → Secrets via files](configuration.md#secrets-via-files-_file-convention) and
[configuration.md → Backend registry](configuration.md#backend-registry-backendstoml). This runbook
does **not** restate the field table.

> **The one rule: never print a secret value.** When capturing logs, filing an incident note, or
> pasting a config into a ticket, reference a credential **by its field/pointer name only** (e.g.
> "`secret_access_key_file` for bucket `staging-a`") — never the token, key, or DSN value itself.
> Phaze masks `SecretStr` fields in logs and reprs and logs the resolved registry as a secret-free
> `{id, kind, rank, cap}` projection at boot; keep that discipline in everything you write down.

## Stranded `active` SAQ jobs (phaze-o0n6)

### What the alarm means

SAQ's `_enqueue` upsert only overwrites a conflicting key whose status is in
`('aborted','complete','failed')`. `'active'` is not in that list, so **any** `saq_jobs` row left in
`status='active'` holds its deterministic key `process_file:<file_id>` permanently, and every
re-enqueue of that file — including via the Recover button and the recovery CLI — silently returns
`None`. Rows get left there routinely: `PostgresQueue._dequeue` marks rows `active` in bulk and
buffers them in an in-process `asyncio.Queue`, so a restart, deploy, OOM or kill abandons every
buffered row with nothing alive to finalize it. SAQ's sweeper is the nominal remedy and does not keep
up (it waits on each abort serially); on 2026-07-31 this had reached 2,413 rows on one analyze queue,
keying 2,411 files that had never been analyzed.

Read the queue:

```bash
phaze queue status --queue phaze-agent-<agent>-analyze
```

`stranded` is the count of rows past their own job timeout plus `PHAZE_ACTIVE_REAP_SLACK_SECONDS`
— exactly what `reap_stranded_active_jobs` will delete on its next minute tick. The command **exits
1** when that count exceeds the lane's concurrency, which cannot happen in healthy operation: the lane
runs at most `concurrency` jobs at once, so anything above that is abandoned claims. Run it from a
monitor. Exit 1 is the alarm; a degraded read (unreadable `saq_jobs`) still exits 0, because a missing
measurement is not a detected incident.

### Steady state: nothing to do

The controller runs `reap_stranded_active_jobs` every minute. It DELETEs each stranded row — releasing
the key — and deliberately leaves the file's `scheduling_ledger` row alone. That row is the **recovery
source**, not a second block: `recover_orphaned_work` re-drives `ledger MINUS live-saq_jobs-keys MINUS
domain-completed`, so freeing the key is precisely what turns the ledger row from invisible into an
orphan the next recovery pass replays (with its stored 7200s timeout, onto the file's owning
fileserver). **Never delete the `scheduling_ledger` rows** as part of a manual cleanup: doing so
destroys the only durable record that the file was ever scheduled, and no path will ever pick it up
again.

### Clearing a pre-existing backlog

A backlog that accumulated before this cron existed drains on its own, but the re-drive is gated, so
it needs one operator action. After deploying:

1. Confirm the reaper is running and the count is falling:

   ```bash
   phaze queue status --queue phaze-agent-<agent>-analyze   # watch `stranded` drop toward 0
   docker compose logs controller | grep "stranded 'active' jobs reaped"
   ```

   The log line names every released key, so it is also the record of which files were unblocked.

2. Once `stranded` reaches 0, re-drive the freed files. `recover_orphaned_work`'s automatic pass is
   gated on the queue being empty (a genuine queue-loss), which a busy deployment is not — so use the
   **Recover** button in the Analyze workspace, which calls the same function with `force=True`. That
   bypasses only the no-op detect gate; the per-item deterministic-key dedup still applies, so a
   forced reconcile over a live queue cannot double the queue.

3. Verify the files re-entered the pending set — the Analyze stage card's pending/orphan counts should
   move by the number of keys the reaper logged, and `phaze queue status` should show the lane running
   again.

If `stranded` does **not** fall, the rows are inside their own timeout window: `process_file` carries a
7200s timeout, so a row is not eligible until 7200s + `PHAZE_ACTIVE_REAP_SLACK_SECONDS` after its
`started`. That bound is per-row and deliberate — it is what keeps the reaper from deleting the broker
row of a job a worker is still executing. Wait it out rather than lowering the slack.

## Removing fingerprint-era data (phaze-0jpe)

> **Manual, operator-approved, post-deployment only.** Nothing in this section is automated and
> no agent runs it. It is a one-time cleanup to perform **after** the fingerprint-removal epic
> (`phaze-0jpe`) has been deployed to the file server and application server and confirmed
> healthy — not before, and not as part of that deployment. Until you have verified the new
> images are running cleanly, leave everything below in place.

**Why this waits for an explicit go.** The on-host `audfprint_data`/`panako_data` Docker volumes
and the historical `fingerprint_results` investigation records are the **only surviving
evidence** for two things this repo needed to make its removal decision: the two production
outages (`phaze-p3hj`'s zero-byte audfprint database, `phaze-iq65`'s Panako silently storing 19
of 11,411 files), and the capacity measurements in
[docs/design/0002-fingerprint-removal.md](design/0002-fingerprint-removal.md) that the deferred
spike molecule `phaze-oof3` depends on if fingerprinting is ever reconsidered. Deleting this data
early destroys evidence that cannot be reconstructed. Do not delete it as a matter of tidiness —
delete it only once you, the operator, have decided it is no longer needed.

### 1. Remove the on-host `audfprint_data` / `panako_data` volumes

On the file-server host, **after** confirming the redeployed agent stack (no `audfprint`/`panako`
sidecar services left in `docker-compose.agent.yml`) has been running healthily for a while:

```bash
docker volume ls | grep -E 'audfprint_data|panako_data'   # confirm exactly what's there first
docker volume rm audfprint_data panako_data                # or the compose-project-prefixed names
                                                            # docker volume ls actually reports
```

This is destructive and irreversible. If you have any doubt about whether the outage
investigation or a future `phaze-oof3` capacity re-check might still need this data, **do not run
it** — keep the volumes until you are certain.

### 2. Prune the published sidecar images

The CI matrix that built and published `ghcr.io/simplicityguy/phaze/audfprint` and
`ghcr.io/simplicityguy/phaze/panako` was removed along with the sidecars themselves (`phaze-0jpe`,
infra removal). Existing tags already pushed to GHCR are not deleted automatically. Once you no
longer need them for rollback or forensics:

1. In the repository's GitHub Packages UI (or via `gh api`/the GHCR API), list the tags under
   each of `ghcr.io/simplicityguy/phaze/audfprint` and `ghcr.io/simplicityguy/phaze/panako`.
2. Delete the tags/versions, then delete the now-empty packages if GHCR does not do so
   automatically. There is no `just` recipe for this — it is a registry-console action, not a
   repo operation.
3. Keep at least the last known-good tag of each until you are confident you will not need to
   stand up the sidecars again for a comparison or a rollback.

### 3. Historical `tracklists.source = 'fingerprint'` rows — purged by the migration

Migration `046_drop_fingerprint_schema.py` **purges** `Tracklist` rows with `source = 'fingerprint'`
(the historical output of the retired audio-fingerprint scan path, `scan_live_set`), together with
their `tracklist_versions`, `tracklist_tracks`, and any `discogs_links` attached to those tracks.
**There is nothing for an operator to do here** — it happens automatically when `046` runs.

Two things worth knowing if you are reviewing that migration or debugging a restore:

- **The delete runs child-first, and has to.** No foreign key in the chain
  (`discogs_links` → `tracklist_tracks` → `tracklist_versions` → `tracklists`) declares
  `ON DELETE CASCADE`; they are all `NO ACTION`. A bare `DELETE FROM tracklists WHERE
  source = 'fingerprint'` raises a foreign-key violation in exactly the environments that still
  hold rows. The migration issues four statements leaf-to-root for that reason, each re-deriving
  its own scope from `tracklists.source` so the sequence is idempotent.
- **In production this is a no-op.** Measured 2026-07-29: `tracklists` held **0 rows of any
  source**, and `tracklist_versions` / `tracklist_tracks` were empty too — consistent with both
  engines having been dead for weeks before removal. The purge is written defensively for a
  restored backup or a developer database predating the outage, where the rows *can* exist and
  the ordering *does* matter.

Note this reverses the migration's original recorded decision to leave the rows in place; that
earlier reasoning (they are inert — the `phaze-p1vy` source allowlist permanently excludes them
from any re-read, and phaze-2akf carried that allowlist into the on-demand `refresh_tracklists`)
still holds on the facts. The operator's call was that a
tracklist attributed to a retired engine is not worth keeping as a record.

## See also

- [configuration.md → Backend registry](configuration.md#backend-registry-backendstoml) — the
  `backends.toml` schema (`[[backends]]` / `[[buckets]]`, ranks, caps, inline `*_file` secrets).
- [configuration.md → Cloud target](configuration.md#cloud-target-removed-in-phase-67) — the removed
  `cloud_target` selector and the 1:1 `cloud_target`→`backends` equivalence.
- [cloud-burst.md](cloud-burst.md) — provisioning an OCI A1 `compute` backend.
- [k8s-burst.md](k8s-burst.md) — provisioning a `kueue` backend + its cluster-admin objects.
- [design/0002-fingerprint-removal.md](design/0002-fingerprint-removal.md) — the ADR for why
  audio fingerprinting was removed in full, including the evidence the volume-removal step above
  protects.

The removal epic's file-by-file inventory was deleted on 2026-07-29 once the removal was
complete: ADR-0002 and the git history of epic phaze-0jpe are the record of what changed.
