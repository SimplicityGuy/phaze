---
phase: 29-deployment-hardening-agents-admin
reviewed: 2026-05-16T00:00:00Z
depth: standard
files_reviewed: 22
files_reviewed_list:
  - src/phaze/cert_bootstrap.py
  - src/phaze/entrypoint.py
  - src/phaze/config.py
  - src/phaze/services/agent_client.py
  - src/phaze/tasks/_shared/agent_bootstrap.py
  - src/phaze/scripts/__init__.py
  - src/phaze/scripts/download_models.py
  - src/phaze/tasks/_shared/model_bootstrap.py
  - src/phaze/tasks/agent_worker.py
  - src/phaze/tasks/heartbeat.py
  - src/phaze/agent_watcher/__main__.py
  - src/phaze/constants.py
  - src/phaze/services/agent_liveness.py
  - src/phaze/utils/__init__.py
  - src/phaze/utils/humanize.py
  - src/phaze/routers/admin_agents.py
  - src/phaze/templates/admin/agents.html
  - src/phaze/templates/admin/partials/_status_pill.html
  - src/phaze/templates/admin/partials/agents_table.html
  - src/phaze/templates/base.html
  - src/phaze/main.py
  - docker-compose.yml
  - docker-compose.agent.yml
  - .env.example
  - .env.example.agent
  - scripts/download-models.sh
  - .github/workflows/docker-publish.yml
  - justfile
  - pyproject.toml
findings:
  critical: 3
  warning: 4
  info: 3
  total: 10
status: issues_found
---

# Phase 29: Code Review Report

**Reviewed:** 2026-05-16
**Depth:** standard
**Files Reviewed:** 22 source modules + 7 infrastructure files
**Status:** issues_found

## Summary

Phase 29 delivers TLS bootstrapping, agent liveness classification, the `/admin/agents` UI page, model auto-download, heartbeat cron, and Docker Compose hardening. The cryptographic implementation in `cert_bootstrap.py` is well-structured (correct extensions, authority key identifiers, ECDSA P-256, proper file modes documented). The TLS verification threading in `agent_bootstrap.py` correctly passes the CA file path through to `httpx.AsyncClient`. The Jinja2 templates use autoescape correctly and the HTMX polling pattern is sound.

Three blockers were found: a documented-but-absent HTTPS enforcement validator, a Redis URL env-var naming mismatch that causes production agents to fail to start or silently connect to the wrong broker, and an incomplete model-presence check that allows a partially-downloaded model set to masquerade as complete. Four warnings address a private-key permission window during cert generation, an unguarded Postgres port exposure, the `.part` file not being cleaned up on failed downloads, and the `watcher` service unnecessarily mounting the models directory as writable.

---

## Critical Issues

### CR-01: Documented `_enforce_https_in_production` validator is absent — agent can contact app server over plain HTTP in production

**File:** `src/phaze/config.py` (entire `AgentSettings` class) and `.env.example.agent:21-23`

**Issue:** `.env.example.agent` line 21 states:

```
# MUST be HTTPS -- the agent client refuses http:// URLs in production
# (AgentSettings._enforce_https_in_production guard, Phase 29 Plan 02).
```

No such validator exists anywhere in `config.py`. `AgentSettings` only has `_enforce_required_agent_fields` and `_enforce_redis_password_in_production`. An operator who sets `PHAZE_AGENT_ENV=production` and `PHAZE_AGENT_API_URL=http://app-server:8000` gets no rejection at startup — the agent silently connects over plaintext, bypassing the entire mTLS layer that the cert bootstrap exists to provide. Because `construct_agent_client` uses `verify=cfg.agent_ca_file`, the httpx client would attempt TLS verification against a plain-HTTP server and fail with a protocol error rather than a clear configuration error.

**Fix:** Add the validator to `AgentSettings`:

```python
@model_validator(mode="after")
def _enforce_https_in_production(self) -> "AgentSettings":
    """Phase 29 Plan 02: production agents MUST use HTTPS for agent_api_url."""
    if self.agent_env == "production":
        parsed = urlparse(self.agent_api_url)
        if parsed.scheme != "https":
            raise ValueError(
                f"agent_env=production requires agent_api_url to use https:// "
                f"(got scheme={parsed.scheme!r}; Phase 29 Plan 02)"
            )
    return self
```

---

### CR-02: `PHAZE_REDIS_URL` env var is silently ignored — production agent connects to wrong Redis or fails to start

**File:** `src/phaze/config.py:36` (`redis_url` field) and `.env.example.agent:33`, `src/phaze/tasks/agent_worker.py:38`

**Issue:** `BaseSettings.redis_url` has no `validation_alias`:

```python
redis_url: str = "redis://redis:6379/0"
```

With `env_prefix` absent and no `validation_alias`, pydantic-settings maps this to the env var `REDIS_URL` (uppercased field name). Both `.env.example.agent` (line 33) and the `agent_worker.py` docstring (line 38) instruct operators to set `PHAZE_REDIS_URL`. That variable is silently ignored by pydantic-settings.

In a production deployment where the operator follows the `.env.example.agent` template:
- `PHAZE_AGENT_ENV=production` is recognized (it has an alias).
- `PHAZE_REDIS_URL=redis://default:secret@app-server:6379/0` is **ignored**.
- `redis_url` falls through to its default `"redis://redis:6379/0"` (no password).
- `_enforce_redis_password_in_production` fires, finds no password, raises `ValueError` → **agent container fails to start**.

Even if the operator works around the startup failure (e.g., sets `PHAZE_AGENT_ENV=dev`), the agent's SAQ queue connects to `redis://redis:6379/0`, which is the docker-compose service-name DNS that does not exist on the file server host. Tasks are enqueued to an unreachable Redis.

The same mismatch appears in `tests/test_task_split.py` (lines 42, 53, 89, 132, 217, 262) which sets `PHAZE_REDIS_URL` expecting it to affect `get_settings().redis_url` — those tests are not actually controlling the field they think they are.

**Fix (two-part):**

1. Add `validation_alias` to `redis_url` in `BaseSettings`:

```python
redis_url: str = Field(
    default="redis://redis:6379/0",
    validation_alias=AliasChoices("PHAZE_REDIS_URL", "REDIS_URL", "redis_url"),
    description="Redis connection URL. Agent env var: PHAZE_REDIS_URL.",
)
```

2. Update `.env.example` accordingly to use `PHAZE_REDIS_URL` (or keep both aliases and update the base `.env.example` to document it).

---

### CR-03: `ensure_models_present` short-circuits on ANY `.pb` file — a partial download leaves the agent silently running with incomplete models

**File:** `src/phaze/tasks/_shared/model_bootstrap.py:45-47`

**Issue:**

```python
pb_files = list(models_dir.glob("*.pb"))
if pb_files:
    logger.info("Models present (%d weight files at %s)", len(pb_files), models_dir)
    return
```

The download suite installs 34 `.pb` files (33 classifiers + 1 genre model per `download_models.py`). If the first download run is interrupted mid-stream (network drop, container OOM, Docker stop) after at least one file has been atomically renamed from `.part`, subsequent container starts find that file and skip the download entirely. The 2–33 missing models are never fetched. Essentia will error at runtime when it tries to load absent weight files — but the error surfaces only during audio analysis, not at startup, making the root cause invisible.

This is distinct from the documented `.part` idempotency (`.part` files correctly don't match `*.pb`). The failure mode is a completed partial rename.

**Fix:** Compare the actual count against the expected count:

```python
EXPECTED_PB_COUNT = len(CLASSIFIER_MODELS) + len(GENRE_MODELS)  # 34

def ensure_models_present(models_dir: Path) -> None:
    pb_files = list(models_dir.glob("*.pb"))
    if len(pb_files) >= EXPECTED_PB_COUNT:
        logger.info("Models present (%d weight files at %s)", len(pb_files), models_dir)
        return
    if pb_files:
        logger.warning(
            "Incomplete model set (%d/%d .pb files); re-running download to fill gaps",
            len(pb_files), EXPECTED_PB_COUNT,
        )
    # ... download_to() call unchanged
```

Import `CLASSIFIER_MODELS` and `GENRE_MODELS` from `phaze.scripts.download_models` for the count. The `download_to()` function's per-file idempotency means re-running is safe and only fetches missing files.

---

## Warnings

### WR-01: Private key files created world-readable for a brief window before `chmod(0o600)`

**File:** `src/phaze/cert_bootstrap.py:215-222` and `227-234`

**Issue:** `Path.write_bytes()` creates the file using `open(path, "wb")` which applies the process umask. In Docker containers the default umask is typically `0022`, making the file mode `0644` at creation time. The `chmod(0o600)` call follows immediately after, but there is a brief window — however small — where the private key (CA and leaf) is world-readable at the filesystem level.

```python
ca_key_path.write_bytes(...)   # mode = 0o644 (umask 022 applied)
ca_key_path.chmod(0o600)       # then restricted
```

For a single-process pre-uvicorn entrypoint on a bind mount this is a low-probability race, but it violates the security contract that private keys must never be world-readable, even transiently.

**Fix:** Write to a temp file with restricted permissions, then rename:

```python
import os, tempfile

def _write_private_key(path: Path, pem: bytes) -> None:
    """Write private key atomically with 0o600 permissions."""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".key.tmp")
    try:
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        os.rename(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
```

This ensures the key is never readable at any permission other than `0o600`.

---

### WR-02: PostgreSQL port `5432:5432` binds to `0.0.0.0` — database exposed on all network interfaces

**File:** `docker-compose.yml:62-63`

**Issue:**

```yaml
ports:
  - "5432:5432"
```

Without an explicit bind-IP prefix (like `127.0.0.1:5432:5432`), Docker binds to `0.0.0.0`. On a multi-homed home server the Postgres instance is reachable from the LAN. The Redis port correctly uses `${REDIS_BIND_IP:-127.0.0.1}:6379:6379`, but Postgres has no equivalent guard.

Unlike Redis, Postgres does have its own authentication (`POSTGRES_PASSWORD`), but: (a) the example password is `phaze`/`phaze`, (b) there is no connection allowlist in `pg_hba.conf` override, and (c) the phase's own security posture applies LAN-binding to Redis, making the inconsistency with Postgres notable.

**Fix:**

```yaml
ports:
  - "${POSTGRES_BIND_IP:-127.0.0.1}:5432:5432"
```

Add `POSTGRES_BIND_IP=127.0.0.1` to `.env.example` with a comment matching the Redis entry (production sets it to the app-server's LAN IP or leaves it as loopback if agents only reach Postgres via the API).

---

### WR-03: `.part` temp file not removed on download error — stale partial files accumulate

**File:** `src/phaze/scripts/download_models.py:84-90`

**Issue:**

```python
tmp = dest.with_suffix(dest.suffix + ".part")
with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
    response.raise_for_status()
    with tmp.open("wb") as fh:
        for chunk in response.iter_bytes(chunk_size=64 * 1024):
            fh.write(chunk)
tmp.rename(dest)  # POSIX-atomic per file
```

If the write loop raises (disk full, I/O error, `KeyboardInterrupt`), `tmp` is left on disk and never cleaned up. On the next run `_download_one` checks `dest.exists()` (the final path), finds it absent, and creates a second `.part` file — potentially in a half-written state alongside the old one. On a disk-full scenario the accumulation of `.part` files makes the situation worse.

**Fix:**

```python
tmp = dest.with_suffix(dest.suffix + ".part")
try:
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                fh.write(chunk)
    tmp.rename(dest)
except Exception:
    tmp.unlink(missing_ok=True)
    raise
```

---

### WR-04: `watcher` service mounts `MODELS_PATH` as `:rw` but never uses it

**File:** `docker-compose.agent.yml:50`

**Issue:**

```yaml
watcher:
  volumes:
    - "${MODELS_PATH:-./models}:/models:rw"
```

`phaze.agent_watcher.__main__` explicitly documents (line 186–191) that the watcher **does not** call `ensure_models_present`. The watcher performs file discovery only and has no code path that reads or writes `/models`. Mounting it read-write grants the watcher container unnecessary write access to the models volume — a violation of the principle of least privilege. If the watcher were ever compromised, an attacker could overwrite model weights.

**Fix:**

```yaml
watcher:
  volumes:
    - "${MODELS_PATH:-./models}:/models:ro"
```

The comment on line 20 of `docker-compose.agent.yml` should also be corrected: "MODELS_PATH bind mount is rw on **worker** (D-21 auto-download); ro on watcher."

---

## Info

### IN-01: No SHA-256 integrity check on downloaded model files

**File:** `src/phaze/scripts/download_models.py:6-8`, `_download_one` function

**Issue:** The module docstring acknowledges "verifies SHA-256 if provided (deferred to a future plan)." The 34 binary TensorFlow protobuf files (~150MB total) are downloaded over HTTPS from `essentia.upf.edu` with no post-download integrity check. A compromised CDN, MITM on the TLS session (unlikely given pinned CA in the agent-to-api path, but not in play for the outbound download), or a supply chain compromise on the model host would silently substitute model weights. The models run in the Essentia inference engine which executes native code.

**Recommendation:** Maintain a `MODELS_SHA256` dict in `download_models.py` and verify each `.pb` + `.json` file before the atomic rename. This can be implemented without blocking the phase.

---

### IN-02: `refreshed_at_iso` injected directly into Alpine.js `x-data` string — pattern is fragile

**File:** `src/phaze/templates/admin/partials/agents_table.html:73`

**Issue:**

```html
x-data="{ refreshedAt: new Date('{{ refreshed_at_iso }}'), ... }"
```

`refreshed_at_iso` is `datetime.now(UTC).isoformat()`, which produces only ISO 8601 characters (`0-9`, `:`, `.`, `+`, `-`, `T`, `Z`) — safe in this case. Jinja2's autoescape HTML-encodes the value but does NOT JS-escape it. The pattern is fragile: if the source of `refreshed_at_iso` ever changes to accept user input or a non-UTC timezone string containing quotes, it becomes an XSS vector. The `data-refreshed-at` attribute on the `<section>` element (line 18) is a safer pattern that Alpine can read via `$el.dataset.refreshedAt`.

**Recommendation:** Read the timestamp from the data attribute rather than interpolating into the `x-data` expression:

```html
<section id="agents-table-section" data-refreshed-at="{{ refreshed_at_iso }}" ...>
...
<p x-data="{ get refreshedAt() { return new Date(document.getElementById('agents-table-section').dataset.refreshedAt); }, ... }">
```

This separates the server-rendered value from the JS expression and survives any future changes to `refreshed_at_iso`.

---

### IN-03: `_build_default_settings` is dead code — both branches return `ControlSettings()`

**File:** `src/phaze/config.py:297-312`

**Issue:**

```python
def _build_default_settings() -> ControlSettings:
    role = os.environ.get("PHAZE_ROLE", "control")
    if role == Role.AGENT.value:
        # Agent worker entry points should call get_settings() / AgentSettings()
        # directly.
        return ControlSettings()
    return ControlSettings()
```

Both branches return `ControlSettings()`. The `if role == Role.AGENT.value:` branch has a comment explaining why it returns `ControlSettings`, but the dead branch obscures intent and could confuse future maintainers into thinking the agent path has different behavior. The comment is correct, but the conditional is not needed.

**Recommendation:** Simplify to a single statement:

```python
def _build_default_settings() -> ControlSettings:
    """Module-level singleton always uses ControlSettings regardless of role.

    Agent-role processes must call get_settings() directly. See module docstring.
    """
    return ControlSettings()
```

---

_Reviewed: 2026-05-16_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
