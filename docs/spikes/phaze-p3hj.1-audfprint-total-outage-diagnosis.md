# D1 — audfprint fails on every file: per-file failure, or total engine outage?

> **Historical (2026-07-29).** This outage was resolved by *removal, not repair*: epic
> `phaze-0jpe` (2026-07-28) deleted audio fingerprinting from the product entirely
> (see [ADR-0002](../design/0002-fingerprint-removal.md)). The follow-up fix bead
> `phaze-p3hj.2` was never actioned, and the `phaze_audfprint_data` volume preserved below is
> now cleanup material — see
> [runbook.md → Removing fingerprint-era data](../runbook.md#removing-fingerprint-era-data-phaze-0jpe).
> The diagnosis is kept as-is for the record.

- **Bead:** `phaze-p3hj.1` (epic `phaze-p3hj` — audfprint fingerprinting fails on EVERY file
  while panako succeeds, latest release)
- **Date:** 2026-07-28
- **Tree:** branch `wt/bead/issue/phaze-p3hj.1`
- **Scope:** diagnosis only, per the bead's acceptance criteria. **No fix applied in this bead.**
  No error handling was widened or softened.
- **What was touched on the live host, exactly.** Every database statement was a `SELECT`. No
  container was restarted, no deployment config edited, nothing chowned, no requeue or ingest
  queued. The live `fprint.pklz` and the `phaze_audfprint_data` volume were **not** written,
  deleted or repaired — they are left exactly as found so `phaze-p3hj.2` can verify against
  them. The archive mounts (`/data/downloads`, `/data/staging`) are read-only and were only
  read.
  **One disclosure:** the §6 positive control ran `audfprint new` with `--dbase` pointing at
  `/tmp/rp*/t.pklz` — the container's own ephemeral filesystem, not the volume and not the
  archive — and removed those scratch files afterwards. That is a write to the container layer,
  which the read-only rule of engagement did not explicitly cover; flagging it rather than
  leaving it implicit.

---

## Verdict

**Total engine outage, rendered per-row as 11,180 per-file `failed` verdicts.** It is not a
per-file failure, and it is not a genuine no-match.

`/data/fprint/fprint.pklz` on the `phaze_audfprint_data` volume is a **zero-byte file**. Every
`audfprint add` and every `audfprint match` begins by loading that file, so every invocation of
either dies with the same `EOFError` before it ever looks at the audio. The engine has been in
this state continuously since at least 2026-07-18 and **is still in it now, on the deployed
2026.7.9 release**.

The engine cannot self-heal, because both the bootstrap decision and the health check are
keyed on `Path(FPRINT_DB).exists()`, and a zero-byte file exists.

---

## 1 — The verbatim error, and where it came from

### 1a. From the database column (`fingerprint_results.error_message`)

```
ssh -4 datum@host-prod 'docker exec postgres psql -U phaze -d phaze -c "..."'
```

```
select engine, coalesce(error_message,'<null>'), count(*)
  from fingerprint_results where status='failed' group by 1,2 order by 3 desc;
```

All 11,180 failed audfprint rows carry **one single distinct value** (`count(distinct
error_message) = 1`):

```
HTTP 500: {"detail":"/app/.venv/lib/python3.14/site-packages/docopt.py:165: SyntaxWarning: "\S" is an invalid escape sequence. ...
/app/.venv/lib/python3.14/site-packages/docopt.py:456: SyntaxWarning: "\S" is an invalid escape sequence. ...
Traceback (most recent call last):
  File "/app/audfprint/audfprint.py", line 504, in <module>
    main(sys.argv)
    ~~~~^^^^^^^^^^
  File "/app/audfprint/audfprint.py", line 450, in main
    hash_tab = hash_table.HashTable(dbasename)
  File "/app/audfprint/hash_table.py", line 62, in __init__
    self.load(filename)
    ~~~~~~~~~^^^^^^^^^^
  File "/app/audfprint/hash_table.py", line 205, in load
    self.load_pkl(name)
    ~~~~~~~~~~~~~^^^^^^
  File "/app/audfprint/hash_table.py", line 219, in load_pkl
    temp = pickle.load(f, **pickle_options)
EOFError: Ran out of input
"}
```

The `docopt` `SyntaxWarning` block is upstream noise on Python 3.14 and is **not** the failure;
the process exits nonzero on the `EOFError` below it.

The `HTTP 500: {...}` envelope pins the *source path*: that string is built at
`src/phaze/services/fingerprint.py:154`, `f"HTTP {resp.status_code}: {resp.text}"`, inside
`_post_ingest`. `_post_query` never reads the response body (it raises
`EngineQueryError(engine, f"query engine failure: HTTP {resp.status_code}")` at line 177), so
these rows are unambiguously the **ingest** path.

### 1b. From the sidecar's own container log

```
ssh -4 datum@host-store 'docker logs phaze-audfprint'
```

```
audfprint ingest failed for <set-01>: ... EOFError: Ran out of input
INFO:     172.19.0.2:41860 - "POST /ingest HTTP/1.1" 500 Internal Server Error
```

Same traceback, logged server-side by `services/audfprint/app.py:231` before the 500 is raised.

### 1c. Reproduced by direct invocation, in the running container

`audfprint list` loads the database and writes nothing, so it is a safe read-only probe of the
exact failing code path:

```
$ docker exec phaze-audfprint uv run python /app/audfprint/audfprint.py list \
      --dbase /data/fprint/fprint.pklz
Tue Jul 28 15:49:52 2026 Reading hash table /data/fprint/fprint.pklz
Traceback (most recent call last):
  File "/app/audfprint/audfprint.py", line 504, in <module>
  File "/app/audfprint/audfprint.py", line 450, in main
    hash_tab = hash_table.HashTable(dbasename)
  File "/app/audfprint/hash_table.py", line 62, in __init__
  File "/app/audfprint/hash_table.py", line 205, in load
  File "/app/audfprint/hash_table.py", line 219, in load_pkl
    temp = pickle.load(f, **pickle_options)
EOFError: Ran out of input
```

---

## 2 — The physical evidence: the database file is zero bytes

The fingerprint sidecars do **not** run on `host-prod` (which runs `phaze-api` /
`phaze-worker`); they run on the registered agent, `host-store` (the sole `kind=fileserver` row
in `agents`, `agent_version 2026.7.9`, heartbeating now).

```
$ docker exec phaze-audfprint stat -c '%n size=%s uid=%u gid=%g mode=%a mtime=%y' \
      /data/fprint/fprint.pklz
/data/fprint/fprint.pklz size=0 uid=1000 gid=1000 mode=644 mtime=2026-07-19 00:22:49.207012661 +0000

$ docker exec phaze-audfprint id
uid=1000(audfprint) gid=1000(audfprint) groups=1000(audfprint)

$ docker exec phaze-audfprint du -sh /data/fprint
36K     /data/fprint
```

Container and mount context, verified rather than assumed:

```
$ docker inspect phaze-audfprint --format '{{range .Mounts}}...{{end}}'
bind   /<archive-mount>/downloads -> /data/downloads  ro=true
bind   /<archive-mount>/staging   -> /data/staging    ro=true
volume phaze_audfprint_data       -> /data/fprint     ro=false

$ docker exec phaze-agent-worker-fingerprint printenv | grep SCAN_ROOTS
PHAZE_AGENT_SCAN_ROOTS=/data/downloads,/data/staging
```

Note that the deployed sidecar mounts the media at `/data/downloads` and `/data/staging`,
whereas `docker-compose.agent.yml` in the repo declares `${SCAN_PATH}:/data/music:ro`. The
deployed compose has diverged from the tracked one. That is not causal here — the media is
reachable under both names, and the failure happens before any audio file is opened — but it is
worth knowing before writing a reproduction that hardcodes `/data/music`.

Three facts follow directly:

- **`size=0`.** The file exists and is empty.
- **`uid=1000 gid=1000 mode=644`, owned by the container's own account.** The file is readable
  and writable by the process that opens it.
- **36K total for the whole volume.** audfprint has never persisted a single fingerprint. Even
  the 248 rows recorded as `success` left nothing durable behind.

### Why "zero bytes" and not "torn" is the load-bearing detail

`gzip` + `pickle` distinguish the two cases with *different* exception messages. Verified
empirically:

```python
zero-byte:  EOFError: Ran out of input
valid:      OK
truncated:  EOFError: Compressed file ended before the end-of-stream marker was reached
```

The observed message is `Ran out of input`, so the file is **exactly zero bytes** — not a
half-written gzip stream. That narrows the corrupting event to the window between
`gzip.open(name, 'wb')` (which truncates the file to zero immediately) and the first flush of
compressed output to disk, inside upstream `HashTable.save`:

```python
# /app/audfprint/hash_table.py:178-197 (pinned SHA cb03ba9)
def save(self, name, params=None, file_object=None):
    ...
    f = gzip.open(name, 'wb')                       # <- truncates to 0 bytes RIGHT HERE
    pickle.dump(self, f, pickle.HIGHEST_PROTOCOL)   # <- no temp file, no os.replace, no close()
```

There is no temp-file-plus-rename and no atomic replace anywhere in this path. Any abnormal
termination in that window leaves precisely the artifact observed.

---

## 3 — Why the outage is permanent

Two separate predicates in `services/audfprint/app.py` both accept a zero-byte file as a
working database, and between them they close every escape route:

```python
# app.py:102 — bootstrap decision
command = "add" if Path(FPRINT_DB).exists() else "new"

# app.py:79-81 — health check
db_path = Path(FPRINT_DB)
if db_path.exists():
    return True, "database present"
```

- `_run_ingest` sees `exists() == True`, so it always chooses `add`. `add` takes the
  `else` branch of `audfprint.py:449-450` (`hash_tab = hash_table.HashTable(dbasename)`) and
  loads. The `new` branch — the only code path that could rebuild the database — is
  unreachable forever.
- `query()` short-circuits to `matches=[]` only when the file is **absent** (`app.py:239`).
  Present-but-empty falls through to `match`, which loads the same file and dies the same way.
- `_database_bootstrap_status` returns healthy on mere existence, so `/health` is green.
  Confirmed live, against the zero-byte file:

```
$ docker exec phaze-audfprint python -c "...urlopen('http://127.0.0.1:8001/health')..."
{"status":"healthy","engine":"audfprint","detail":"database present"}
```

This is precisely the shape the module's own comment says the health endpoint exists to
prevent.

### Correction to the epic's premise: the health signal is not green, it is *absent*

The epic (and both lead beads) assume "`/health` stays green" — a health check that reports
healthy and is therefore misleading. The truth is worse, and it changes what a fix has to do.
The endpoint does return 200 on the empty database, as shown above, but **nothing anywhere
reads it**:

```
$ docker inspect phaze-audfprint --format '...'
phaze-audfprint: Healthcheck=NONE State.Health=none User=1000:1000 Restarts=0
phaze-panako:    Healthcheck=NONE State.Health=none User=1000:1000 Restarts=0
```

- **No Docker healthcheck on either sidecar.** `State.Health=none`, so `docker ps` shows no
  health column for them, nothing restarts them, and no orchestrator signal exists.
- **No application caller either.** `FingerprintOrchestrator.health_all`
  (`services/fingerprint.py:348`) and both adapters' `health()` (:208, :245) are invoked only
  from `tests/fingerprint/services/test_fingerprint.py`. There is no production call site. The
  repo already knows this — `vulture_whitelist.py:213` carries the literal entry
  `_.health_all  # unused method (src/phaze/services/fingerprint.py:243)`.

**Consequence for `phaze-p3hj.2`:** making `_database_bootstrap_status` content-aware — the
obvious reading of the fix hints in `phaze-6xqg` and `phaze-25cc` — would change **nothing
observable**. A correct 503 from an endpoint with no consumers is still silence. A fix that
relies on the health check must also wire it up (a compose `healthcheck:` on the sidecar, an
application probe, or both), or it must not rely on it at all.

### What the UI is actually reading

Not `/health`. Two different reads, and they disagree:

- **The per-engine column in the screenshot** — `routers/pipeline.py:2036` selects
  `FingerprintResult.status` for the file and returns `{"engine_statuses": [...]}`. That is the
  raw per-row `failed` / `success`, which is why the outage is rendered per file.
- **The FINGERPRINT stage itself** — `services/stage_status.py:245` (DERIV-05, "any engine
  success wins"): the stage is *done* if **any** engine row is in
  `_DONE_FP = ("success", "completed")`. panako succeeded on 11,427 of 11,428 files, so the
  pipeline stage reads **done** for essentially the whole archive. `stage_status.py:295-298`
  (ELIG-04) marks the stage failed only when NO engine succeeded and at least one failed — which
  is true for almost none of these files.

So a total audfprint outage is invisible at the stage/pipeline level by design, and visible only
in the per-engine column an operator has to go looking for. That is the precise mechanism behind
the epic's "it surfaces as worse match quality, which is invisible without looking at this
column", and it is the same `any-engine-wins` rule as `tasks/fingerprint.py:120` in §4, applied
a second time on the read side.

---

## 4 — Per-file failure or total outage? Total outage, rendered per-row

The engine-level classification is working correctly all the way up to the last step:

- `services/audfprint/app.py:232` returns HTTP **500**.
- `services/fingerprint.py:151` sets `engine_error = resp.status_code >= 500` → `True`.
- `tasks/fingerprint.py:120` is where it is lost:

```python
if results and not any(r.status == "success" for r in results.values()) and all(r.engine_error for r in results.values()):
    raise FingerprintEnginesUnavailable(msg)
```

The `phaze-ds1z` guard only trips when **every** engine is dead. panako succeeds on the same
file, so `any(... == "success")` is `True`, the guard is skipped, and audfprint's
`engine_error=True` result is persisted as an ordinary per-file `failed` row with the engine
traceback in `error_message`. The in-code rationale is explicit and deliberate ("a single live
engine still finishes the stage; a dead sibling must not block it") — the consequence is that a
**single**-engine total outage is indistinguishable in the UI from 11,180 individually corrupt
files. That is the finding `phaze-p3hj.3` (fail loudly, don't silently degrade) needs.

Distinguishing evidence that this is an outage and not 11,180 real per-file failures:

| Evidence | Value |
|---|---|
| distinct `error_message` values across all failed audfprint rows | **1** |
| audfprint rows: `failed` / `success` | 11,180 / 248 |
| panako rows on the same files: `failed` / `success` | 1 / 11,427 |
| mean wall gap between consecutive rows, after a `success` | 2.672 s |
| mean wall gap between consecutive rows, after a `failed` | 2.719 s |
| bytes of fingerprint data audfprint has ever persisted | **0** |
| `tracklists` rows | 0 |

A genuine per-file failure population would show a spread of error texts and a markedly
different cost for a success (a real ingest decodes and landmarks the whole file) than for a
failure. Successes and failures cost the *same* 2.7 s, and the error text is byte-identical
across 11,180 rows. Nothing was ever fingerprinted.

The 248 `success` rows are not counter-evidence: audfprint's volume holds zero bytes of
fingerprint data, so whatever those runs returned 200 for did not survive.

### Not a no-match, either

`services/fingerprint.py` draws the `phaze-z7yw` distinction correctly and it is not the source
of confusion here. A genuine no-match is a 200 with `matches: []`; this is a 500, which
`_post_query` turns into `EngineQueryError` (outage) and `_post_ingest` into
`engine_error=True` (outage). Both classify correctly. The "failed" in the UI comes from the
ingest path, and it means outage.

---

## 5 — Which of the three 2026-07-27 leads this is

### `phaze-6xqg` — **MATCH** (with one correction)

The mechanism is exactly the one 6xqg describes: upstream's `save_pkl`/`save` rewrites
`fprint.pklz` in place with a plain `gzip.open(..., 'wb')` + `pickle.dump` and no
temp-plus-rename, so an abnormal termination during the save leaves the database unusable;
afterwards every `/ingest` takes the `add` branch, every `/ingest` and `/query` 500s forever,
`/health` stays green at 200 "database present", the hub classifies 5xx as `engine_error`, and
the only known recovery is deleting the file. Every one of those predictions is confirmed above
against the live host.

**Correction for the fix bead:** 6xqg describes the artifact as a "torn (truncated gzip-pickle)"
file. The artifact actually on disk is **zero bytes** (§2), which is a *narrower* window than
6xqg assumes — between the truncate and the first flush, not anywhere in the dump. This matters
for the fix in two ways: (a) a recovery probe that only rejects *malformed* pickles must also
reject empty ones; and (b) the atomic `temp + os.replace()` remedy 6xqg proposes fixes both
shapes at once and is the right one, whereas a "verify then restore .bak" strategy alone would
still allow the zero-byte window to reopen.

The specific precipitating kill is **not** recoverable from surviving evidence — see §7.

### `phaze-25cc` — **ELIMINATED**

25cc predicts EACCES from a pre-existing volume stranded at uid ~999 under the uid-1000 pin.
Refuted on three independent counts:

1. `stat` reports `uid=1000 gid=1000 mode=644` — the file is owned by the container's own
   account, not a stranded 999. The process can read *and* write it.
2. The failure is `EOFError`, not `PermissionError: [Errno 13]`. The traceback proves
   `gzip.open` **succeeded** and `pickle.load` read the stream to EOF; a permission failure
   could not have got that far.
3. The controlling counter-example is on the same host, same volume driver, same uid pin:
   `phaze-panako`'s `/data/fprint` holds **95 G** of live fingerprint data owned by
   `panako:panako`, written continuously. If the uid pin had stranded these volumes, panako
   would be dead too — and panako is the engine that works.

25cc may still be a real latent defect on a *differently upgraded* host. It is not what is
happening here, so it should **not** be closed as a duplicate of this epic — it should stay open
on its own merits.

### Disposition for the follow-up bead

- **`phaze-6xqg` is the root cause.** Fold its fix into `phaze-p3hj.2` and close 6xqg pointing
  at it, carrying the empty-vs-torn correction above.
- **`phaze-25cc` is not.** Leave it open; it is a different (still unproven) failure mode.
- **`phaze-cf0z` is a real defect and independently worth fixing**, but it is not a duplicate of
  this epic either — it is why half of this outage left no evidence.

Minimal-patch scope, consistent with the operator's constraint (atomic write-and-rename, bounded
lock hold; **not** a redesign of the single-pickle store or of `_db_lock`):

1. Atomic save — write to a temp file on the same volume and `os.replace()` it over
   `fprint.pklz`, so no window exists in which the live path is empty or partial.
2. A bootstrap/health predicate that tests *loadability*, not `exists()` — the three on-disk
   states are already distinguishable by exception (see §6).
3. Whatever makes (2) observable, per the health-signal correction in §3.

### `phaze-cf0z` — **CONFIRMED as a real blind spot, but not the cause**

cf0z is correct that `query()` (`app.py:248-249`) raises without logging stderr, and that
`_post_query` discards the body. It is not why *this* is undiagnosable — the **ingest** path
does log server-side and does return stderr in the body, which is the only reason the error text
in §1 exists at all.

But it has a direct consequence for this outage: because `match` loads the same zero-byte file,
the query half of the engine is certainly dead too, and **there is no evidence of it anywhere** —
not in `fingerprint_results`, not in the sidecar log, not in the hub log. Half of this outage is
invisible by construction. `tracklists = 0` is consistent with that, and the epic's own
observation that a dead engine surfaces only as degraded match quality is exactly what cf0z
guarantees.

---

## 6 — Minimal reproduction

Self-contained, inside the deployed `ghcr.io/simplicityguy/phaze/audfprint:2026.7.9` image, in a
scratch path (does not touch the real volume):

```bash
docker exec phaze-audfprint sh -c '
  mkdir -p /tmp/rp
  # 1. bootstrap a VALID database from one decodable file (the "new" branch)
  uv run python /app/audfprint/audfprint.py new --dbase /tmp/rp/t.pklz "<track-01>"
  ls -l /tmp/rp/t.pklz                 # -> non-zero size
  uv run python /app/audfprint/audfprint.py list --dbase /tmp/rp/t.pklz   # -> exit 0

  # 2. simulate a kill between gzip.open(name,"wb") and the first flush
  : > /tmp/rp/t.pklz
  ls -l /tmp/rp/t.pklz                 # -> 0

  # 3. every subsequent load dies -- this is the bug
  uv run python /app/audfprint/audfprint.py list  --dbase /tmp/rp/t.pklz  # -> EOFError: Ran out of input
  uv run python /app/audfprint/audfprint.py add   --dbase /tmp/rp/t.pklz "<track-02>"
  uv run python /app/audfprint/audfprint.py match --dbase /tmp/rp/t.pklz "<track-02>"
'
```

Service-level reproduction (what the operator sees):

```bash
# with a zero-byte /data/fprint/fprint.pklz on the volume
curl -s localhost:8001/health                                        # 200 {"status":"healthy","detail":"database present"}
curl -s -XPOST localhost:8001/ingest -d '{"file_path":"<track-01>"}'  # 500, EOFError: Ran out of input
curl -s -XPOST localhost:8001/query  -d '{"file_path":"<track-01>"}'  # 500, stderr dropped (phaze-cf0z)
```

A fix is verified when, starting from a zero-byte `fprint.pklz`, `/health` reports **unhealthy**
and/or `/ingest` recovers and returns 200 — and when a `SIGKILL` delivered during
`HashTable.save` leaves the previous database intact rather than truncated.

### Positive control — the engine itself is not broken

Run in the deployed container against six randomly sampled real archive files, each bootstrapped
into its own scratch database:

```
file#1 bytes=6069088 dbsize=472450 saved=1 err=
file#2 bytes=8046258 dbsize=474524 saved=1 err=
file#3 bytes=7698893 dbsize=455365 saved=1 err=
file#4 bytes=7143282 dbsize=481649 saved=1 err=
file#5 bytes=7054275 dbsize=479441 saved=1 err=
file#6 bytes=3524046 dbsize=437298 saved=1 err=
```

**6 of 6 succeeded**, each producing a valid 437–482 KB database that `audfprint list` then
loads cleanly. `ffmpeg` is present at `/usr/bin/ffmpeg`. So decode, landmarking, `save` and
`load` all work in the shipped `2026.7.9` image against real archive media — the invocation
contract still matches what the pinned `cb03ba9` upstream provides, and the CLI has **not**
drifted. The entire outage is the one zero-byte file. This eliminates "audfprint cannot process
this archive" as a competing explanation.

Two secondary observations, recorded but out of scope for this bead:

- A file that genuinely fails to decode raises `OSError: wavfile2peaks: Error reading ...` and
  exits nonzero, which `_post_ingest` classifies as **`engine_error=True`** because the sidecar
  returns 500. That is a per-*file* failure being reported as an engine outage — the mirror
  image of the misclassification in §4.
- The three on-disk states produce three distinguishable exceptions, which a content-aware
  health check can use directly: absent → `FileNotFoundError`, zero-byte → `EOFError: Ran out of
  input`, torn → `EOFError: Compressed file ended before the end-of-stream marker was reached`.

**A note for `phaze-p3hj.2`:** deleting `fprint.pklz` is enough to make the *next* ingest work,
and is not enough to close the bug. The file was already deleted and re-bootstrapped once
(directory mtime `2026-07-18 20:36 UTC`, minutes after `2026.7.8` shipped the `bf8c191`
bootstrap fix) and came back **zero bytes again** by `2026-07-19 00:22:49 UTC`. Recovery without
crash-safety regresses.

---

## 7 — What could not be determined, and the next diagnostic step

**Determined:** the root cause of the outage — a zero-byte `fprint.pklz` that every `add`/`match`
loads, produced by a non-atomic in-place `gzip.open(..., 'wb')` write, made permanent by two
`exists()`-only predicates and invisible by a health check that never reads the file.

**Not determined:** *which* abnormal termination truncated the file on
`2026-07-19 00:22:49 UTC`. The candidates are the `subprocess.run(timeout=...)` `SIGKILL`
(`SUBPROCESS_TIMEOUT` was a hardcoded **120 s** on the `2026.7.7` release that produced the
original 11,180 rows — hopeless for multi-hour concert sets, so kills were routine; it is 3600 s
and env-wired now, `phaze-mv1f`), an OOM kill of the child (a fresh table is
`np.zeros((2**20, 100), uint32)` ≈ 400 MB before pickling, and the host shows 28 G of 62 G in
use with a 95 G panako corpus alongside), or a `docker stop` `SIGKILL` at redeploy. The evidence
that would settle it is gone: `phaze-audfprint` has been recreated (`2026-07-24T04:39`) and
restarted (`2026-07-25T23:48`) since, and `docker logs` only reaches back to the current start.

**This does not block the fix**, because all three candidates are the same defect class and the
same remedy (`temp + os.replace()` plus a content-aware health check and bootstrap predicate)
closes all three.

**If the precipitating kill must be identified**, the specific next step is:
`journalctl -k --since 2026-07-18 --until 2026-07-19 | grep -i -E 'oom|killed process'` on
`host-store` and `docker events --since 2026-07-18T00:00 --until 2026-07-19T12:00 --filter
container=phaze-audfprint`. A kernel OOM record naming the `python .../audfprint.py` child
settles it; its absence points at the 120 s `SUBPROCESS_TIMEOUT` kill.

---

## 8 — Timeline

| When (UTC) | What |
|---|---|
| 2026-07-18 05:00:32 → 13:38:13 | The 11,428-file fingerprint run. audfprint: 248 success / 11,180 failed, one distinct error. panako: 11,427 success / 1 failed. Release `2026.7.7` (tagged 2026-07-17 21:17 PDT). |
| 2026-07-18 20:36 | `/data/fprint` directory mtime — `fprint.pklz` deleted and recreated. `2026.7.8`, which first contains the `bf8c191` bootstrap fix, was tagged 2026-07-18 13:10 PDT. |
| 2026-07-19 00:22:49 | `fprint.pklz` last written. **Zero bytes.** Unchanged ever since. |
| 2026-07-24 | 19 files re-fingerprinted on `2026.7.9`. panako: 19 success. audfprint: **19 failed, same `EOFError`.** The bug is live on the current release. |
| 2026-07-25 | Robert reports the epic against the latest release. |
| 2026-07-28 15:49 | Reproduced read-only against the live sidecar. `/health` still returns 200 "database present". |

---

## 9 — Files read (no files modified)

- `services/audfprint/app.py` — `_run_ingest` (:102), `_database_bootstrap_status` (:70-87),
  `ingest` (:218-233), `query` (:236-263)
- `src/phaze/services/fingerprint.py` — `IngestResult.engine_error` (:39-56),
  `EngineQueryError` (:59-71), `_post_ingest` (:132-154), `_post_query` (:157-177)
- `src/phaze/tasks/fingerprint.py` — the `phaze-ds1z` outage guard (:100-124)
- `services/audfprint/Dockerfile.audfprint` — pinned `AUDFPRINT_SHA=cb03ba9`, single uvicorn
  worker, uid/gid 1000
- upstream `dpwe/audfprint` @ `cb03ba9` — `audfprint.py` `main` (:434-499) and `do_cmd`
  (:173-185), `hash_table.py` `HashTable.__init__` (:59-62), `save` (:178-197), `load` (:199),
  `load_pkl` (:213-219)
