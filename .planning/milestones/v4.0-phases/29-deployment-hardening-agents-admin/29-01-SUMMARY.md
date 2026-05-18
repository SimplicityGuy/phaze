---
phase: 29-deployment-hardening-agents-admin
plan: 01
subsystem: auth
tags: [phase-29, auth, tls, cert-bootstrap, security, v4.0, cryptography, httpx-verify]

requires:
  - phase: 26-task-code-reorg-http-backed-agent-worker
    provides: PhazeAgentClient, AgentSettings, construct_agent_client, import-boundary invariant (D-25)
  - phase: 27-watcher-service-user-initiated-scan
    provides: phaze.tasks._shared.agent_bootstrap module (D-17)
provides:
  - phaze.cert_bootstrap.ensure_certs_present (idempotent CA + leaf x509 generation)
  - phaze.entrypoint pre-uvicorn shim (runs cert bootstrap then execvp uvicorn)
  - PhazeAgentClient.__init__ verify= kwarg (default True; preserves Pitfall 10)
  - AgentSettings.agent_ca_file (D-03; default /certs/phaze-ca.crt)
  - BaseSettings.api_tls_sans (D-02; default localhost,127.0.0.1,api)
  - construct_agent_client fail-fast on missing/empty CA file (D-03)
  - tests/test_services/test_agent_client_tls.py (D-04 wrong-CA → ConnectError)
  - tests/test_cert_bootstrap.py (D-22; 7 LOCKED cases incl. WARNING-8 banner-via-logger)
  - tests/test_task_split.py::test_cert_bootstrap_stays_postgres_free (D-22 extension of D-25)
affects:
  - Phase 29 Plan 03 (docker-compose api command switches to python -m phaze.entrypoint)
  - Phase 29 Plan 02 (Redis hardening reuses BaseSettings.api_tls_sans pattern)
  - All Phase 30+ agent code that constructs httpx clients to the app server

tech-stack:
  added:
    - cryptography>=46.0.0,<49 (resolved 48.0.0; PyCA-maintained; abi3 wheels)
    - cffi v2.0.0, pycparser v3.0 (transitive via cryptography)
  patterns:
    - "Pre-uvicorn entrypoint shim: bootstrap-then-execvp so signals + PID-1 propagate cleanly"
    - "Idempotent self-signed CA + leaf generation via cryptography.x509.CertificateBuilder (ECDSA P-256)"
    - "verify= kwarg pass-through with default=True to preserve respx-mocked transport tests (Pitfall 10)"
    - "Banner emission via BOTH print() AND logger.warning() (CONTEXT D-02 D-discretion 'Both')"
    - "Postgres-free import boundary extended to pre-uvicorn modules (cert_bootstrap inherits Phase 26 D-25)"

key-files:
  created:
    - src/phaze/cert_bootstrap.py
    - src/phaze/entrypoint.py
    - tests/test_cert_bootstrap.py
    - tests/test_services/test_agent_client_tls.py
  modified:
    - pyproject.toml (added cryptography dep)
    - uv.lock (cryptography 48.0.0 + cffi + pycparser)
    - src/phaze/config.py (BaseSettings.api_tls_sans, AgentSettings.agent_ca_file)
    - src/phaze/services/agent_client.py (PhazeAgentClient verify= kwarg)
    - src/phaze/tasks/_shared/agent_bootstrap.py (construct_agent_client CA-file fail-fast + verify pass-through)
    - tests/test_task_split.py (added test_cert_bootstrap_stays_postgres_free)

key-decisions:
  - "ECDSA P-256 over RSA-3072 for CA + leaf keys (CONTEXT D-discretion: faster + smaller; verified compat with httpx + Python 3.13 ssl)"
  - "AuthorityKeyIdentifier + SubjectKeyIdentifier + ExtendedKeyUsage(SERVER_AUTH) added during integration testing (Rule 1 bug fix; Python 3.13 ssl rejects chain without them)"
  - "verify= default True preserves all existing respx tests (Pitfall 10 confirmed in CI)"
  - "Banner literal references only phaze-ca.crt path; never templates the private key (Pitfall 4 + Test 3 + Test 7)"
  - "WARNING-8 7th test case added per CONTEXT D-02 D-discretion 'Both': caplog-level assertion that banner emission via logger.warning() is independently mandatory (Test 3 covers print path)"
  - "cryptography is NOT a transitive dep (RESEARCH Critical Discovery #1 verified via uv pip list); explicit pyproject.toml add was non-negotiable"

patterns-established:
  - "Pre-uvicorn entrypoint shim invoked via `uv run python -m phaze.entrypoint`; reads env vars directly (no get_settings() at that layer); execvp's uvicorn with --ssl-keyfile / --ssl-certfile pointing at the freshly-generated leaf cert"
  - "x509.CertificateBuilder pattern with full chain extensions (BasicConstraints, KeyUsage, SubjectKeyIdentifier, AuthorityKeyIdentifier on leaf, ExtendedKeyUsage(SERVER_AUTH))"
  - "Test pattern for real-TLS integration: uvicorn.Server in background asyncio task, two independent tmp_path CA bundles to prove both wrong-CA → ConnectError and correct-CA → 200"
  - "AgentSettings fail-fast pattern (D-03) for CA file: ca_path.exists() AND st_size > 0 at construction time; RuntimeError with operator-actionable message"

requirements-completed: [AUTH-02]

duration: ~45min
completed: 2026-05-16
---

# Phase 29 Plan 01: Cert Bootstrap + Agent TLS Verify Summary

**Self-signed CA + leaf cert auto-generation infrastructure (Postgres-free) plus end-to-end `verify=` plumbing on every agent's httpx client, with a CI integration test that proves untrusted certs are rejected.**

## What Shipped

### Cert generation primitive
- New module `src/phaze/cert_bootstrap.py` (220 lines). Exports `ensure_certs_present(certs_dir, cn, sans_csv)`. Idempotent: re-running on a populated directory parses the existing CA + leaf and returns immediately. On generation, writes 4 files:
  - `phaze-ca.crt` 0o644 (public; distributed to agents)
  - `phaze-ca.key` 0o600 (private CA signing key)
  - `phaze-server.crt` 0o644
  - `phaze-server.key` 0o600
- ECDSA P-256 keys. 10-year CA, 2-year leaf. Full chain extensions: BasicConstraints (critical), KeyUsage (critical), SubjectKeyIdentifier on both, AuthorityKeyIdentifier on leaf, ExtendedKeyUsage(SERVER_AUTH) on leaf, SubjectAlternativeName from the operator-supplied SAN list.
- IMPORT-BOUNDARY INVARIANT: no `phaze.database` / `phaze.tasks.session` / `sqlalchemy.ext.asyncio` imports. Verified by `tests/test_task_split.py::test_cert_bootstrap_stays_postgres_free`.

### Pre-uvicorn entrypoint shim
- New module `src/phaze/entrypoint.py` (70 lines). Invoked as `uv run python -m phaze.entrypoint`. Reads `PHAZE_CERTS_DIR` / `PHAZE_API_HOST` / `PHAZE_API_TLS_SANS` env vars (all with safe defaults), calls `ensure_certs_present`, then `os.execvp`'s uvicorn with `--ssl-keyfile` / `--ssl-certfile`. Process replacement (not subprocess) so signals + PID-1 propagate cleanly.

### Settings + client wiring
- `BaseSettings.api_tls_sans` (D-02): default `"localhost,127.0.0.1,api"`. Env alias `PHAZE_API_TLS_SANS`.
- `AgentSettings.agent_ca_file` (D-03): default `"/certs/phaze-ca.crt"`. Env alias `PHAZE_AGENT_CA_FILE`.
- `PhazeAgentClient.__init__` accepts `verify: ssl.SSLContext | str | bool = True` (kw-only, default `True` preserves Pitfall 10). Threaded to `httpx.AsyncClient(verify=...)`.
- `construct_agent_client(cfg)` validates `cfg.agent_ca_file` at construction time: missing OR zero-byte → `RuntimeError("CA file empty or unreadable: ...")`. Passes `verify=cfg.agent_ca_file` to the client.

### Tests
- `tests/test_cert_bootstrap.py` — **7 LOCKED cases**:
  1. First call generates 4 files; all parse via `x509.load_pem_x509_certificate` + `serialization.load_pem_private_key`.
  2. Second call leaves mtimes unchanged (idempotency).
  3. Banner stdout contains "GENERATED NEW PHAZE INTERNAL CA"; never "BEGIN" or "PRIVATE KEY" (Pitfall 4).
  4. File modes: 0o644 / 0o600.
  5. Leaf SubjectAlternativeName matches sans_csv (3 entries for default).
  6. `_parse_san_entries` DNSName vs IPAddress dispatch.
  7. **WARNING-8** — banner emitted via `logger.warning()` at level WARNING with logger name `phaze.cert_bootstrap`; banner records also never leak `BEGIN` / `PRIVATE KEY` (parity with Test 3 for the logger channel).
- `tests/test_services/test_agent_client_tls.py` — **4 cases**:
  - `test_wrong_ca_raises_connect_error`: real uvicorn smoke server presenting one CA's cert; `httpx.AsyncClient(verify=other_ca)` → `httpx.ConnectError`. **D-04 success criterion**.
  - `test_correct_ca_succeeds`: same server, correct CA → 200 OK.
  - `test_construct_agent_client_missing_ca_raises`: D-03 fail-fast on non-existent path.
  - `test_construct_agent_client_empty_ca_raises`: D-03 fail-fast on zero-byte file.
- `tests/test_task_split.py::test_cert_bootstrap_stays_postgres_free` — D-22 extension of D-25.

## Verification Results

```
uv run pytest tests/test_cert_bootstrap.py tests/test_services/test_agent_client_tls.py tests/test_task_split.py tests/test_services/test_agent_client.py tests/test_services/test_agent_client_exec_batch_progress.py -q
36 passed, 2 warnings in 15.14s
```

- 7 cert_bootstrap cases pass (RED → GREEN cycle: ffdbf5f → 5840bfe).
- 4 TLS integration cases pass (RED → GREEN: 57d9843 → 25c4ca4).
- 5 task_split cases pass (incl. new cert_bootstrap-Postgres-free case).
- 20 existing respx-based `test_agent_client*` cases pass unchanged — **Pitfall 10 confirmed**: `verify=True` default preserves transport-layer mocking.
- `uv run ruff check` + `uv run ruff format --check` + `uv run mypy` clean on all touched modules.
- `uv run bandit -x tests -s B608` clean on `cert_bootstrap.py` + `entrypoint.py`.
- `uv run python -c "import phaze.cert_bootstrap; ensure=phaze.cert_bootstrap.ensure_certs_present; print('ok')"` — module imports cleanly.
- Default settings verified: `AgentSettings.agent_ca_file == "/certs/phaze-ca.crt"`, `BaseSettings.api_tls_sans == "localhost,127.0.0.1,api"`.

## Commits

| Hash    | Type | Phase | Subject                                                                |
| ------- | ---- | ----- | ---------------------------------------------------------------------- |
| ffdbf5f | test | RED   | add failing tests for cert_bootstrap + Postgres-free guard             |
| 5840bfe | feat | GREEN | implement cert_bootstrap + entrypoint shim                             |
| 57d9843 | test | RED   | add TLS integration tests + fix CA chain extensions (Rule 1 bug fix)   |
| 25c4ca4 | feat | GREEN | wire verify= through PhazeAgentClient + AgentSettings                  |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Missing AuthorityKeyIdentifier / SubjectKeyIdentifier / ExtendedKeyUsage extensions on the leaf cert**

- **Found during:** Task 2 (running `test_correct_ca_succeeds` against the real cert chain).
- **Issue:** Python 3.13's `ssl` module rejects the leaf cert with "Missing Authority Key Identifier" (and would similarly reject without SubjectKeyIdentifier on CA + ExtendedKeyUsage(SERVER_AUTH) on the leaf) when used in a TLS chain. The RESEARCH §Pattern 1 source snippet in 29-RESEARCH.md (lines 297-339) does NOT include these extensions — verified by re-reading the source — yet Python 3.13's strict TLS validation requires them.
- **Fix:** Added `SubjectKeyIdentifier.from_public_key(...)` to both CA and leaf, `AuthorityKeyIdentifier.from_issuer_public_key(...)` to the leaf, and `ExtendedKeyUsage([SERVER_AUTH])` to the leaf. All 7 cert_bootstrap unit tests still pass (the assertions don't probe these extensions specifically) and the integration test now succeeds with the cert chain validating end-to-end.
- **Files modified:** `src/phaze/cert_bootstrap.py` (lines ~85-95 CA, ~125-145 leaf).
- **Commit:** 57d9843 (folded into the Task 2 RED commit since the bug was discovered while writing the RED test for Task 2 and the fix unblocked test_correct_ca_succeeds).

### Authentication gates

None.

### Architectural decisions

None — the integration-test setup uses real uvicorn + asyncio.Task per the plan's `<action>` block (RESEARCH §Pattern 3 Option 1). No design change.

## Threat Flags

None. The plan's `<threat_model>` already enumerates the surfaces this plan touches (T-29-01-01..T-29-01-SC). No new surface was introduced beyond what the model anticipated.

## Known Stubs

None. All wiring is end-to-end functional:
- `cert_bootstrap.ensure_certs_present` writes real x509 files that pass Python 3.13's ssl chain validation.
- `entrypoint.main()` invokes a real `os.execvp` (not a no-op).
- `PhazeAgentClient(verify=...)` flows to `httpx.AsyncClient(verify=...)`.
- `construct_agent_client` raises `RuntimeError` on missing CA path (tested in test 3 and test 4 of `test_agent_client_tls.py`).

The remaining AUTH-02 work — switching `docker-compose.yml` api command to `python -m phaze.entrypoint` and mounting `./certs/` — lands in **Plan 03** per the plan's `<success_criteria>` note: "AUTH-02 fully closed in Plan 03 once docker-compose.yml api command switches to `python -m phaze.entrypoint` and the cert-mounted volume is configured".

## TDD Gate Compliance

Both tasks followed the RED → GREEN cycle:
- Task 1: ffdbf5f (test) → 5840bfe (feat). RED state confirmed: pytest reported `ModuleNotFoundError: No module named 'phaze.cert_bootstrap'` before the GREEN commit. GREEN state confirmed: 7/7 tests pass.
- Task 2: 57d9843 (test, includes Rule 1 bug fix) → 25c4ca4 (feat). RED state confirmed: 2 of 4 cases failed (`test_construct_agent_client_*`) with `TypeError: AgentSettings.__init__() got an unexpected keyword argument 'agent_ca_file'` (and prior, before the cert chain fix, `test_correct_ca_succeeds` also failed with the AKI error). GREEN state confirmed: 4/4 tests pass.

No REFACTOR commit was needed — both modules landed in their final shape in the GREEN commit.

## Self-Check: PASSED

Files claimed to be created — all present:
- `src/phaze/cert_bootstrap.py` — FOUND
- `src/phaze/entrypoint.py` — FOUND
- `tests/test_cert_bootstrap.py` — FOUND
- `tests/test_services/test_agent_client_tls.py` — FOUND

Commits claimed — all present in `git log --oneline`:
- ffdbf5f — FOUND
- 5840bfe — FOUND
- 57d9843 — FOUND
- 25c4ca4 — FOUND

Test count matches plan success criteria: 7 (cert_bootstrap, incl. WARNING-8) + 4 (TLS, plan said 3 but the second fail-fast case for empty-CA was added to fully cover D-03's "missing OR empty" predicate) + 1 (task_split extension) = 12 net new tests passing.
