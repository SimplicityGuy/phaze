---
phase: 25
slug: internal-agent-http-api-bearer-auth
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-11
---

# Phase 25 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_routers/test_agent_*.py -x` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~10s quick / ~60s full |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_routers/test_agent_*.py -x`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd-verify-work`:** Full suite must be green AND ≥85% coverage
- **Max feedback latency:** 10 seconds (quick run)

---

## Per-Task Verification Map

Authoritative mapping lives in `25-RESEARCH.md` §"Phase Requirements → Test Map" (lines 866–891). The planner assigns task IDs (e.g. `25-01-01`) once plans are written; this table is filled in during plan-checker review. Until then, the requirement-anchored map below is the contract.

| Req / Decision | Behavior | Test Type | Automated Command | File Exists |
|---|---|---|---|---|
| AUTH-01 (1/4) | Missing Authorization header → 401 + `WWW-Authenticate: Bearer` | integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_missing_header_returns_401 -x` | ❌ W0 |
| AUTH-01 (2/4) | Malformed header ("Token foo") → 401 | integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_malformed_header_returns_401 -x` | ❌ W0 |
| AUTH-01 (3/4) | Valid bearer with unknown hash → 403 | integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_unknown_token_returns_403 -x` | ❌ W0 |
| AUTH-01 (4/4) | `agent_id` in request body rejected (422) by `extra="forbid"` | integration | `uv run pytest tests/test_routers/test_agent_files.py::test_agent_id_in_body_rejected -x` | ❌ W0 |
| AUTH-04 (1/2) | Setting `revoked_at = NOW()` mid-test → next call returns 403 (no restart) | integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_revoke_blocks_next_call -x` | ❌ W0 |
| AUTH-04 (2/2) | New agent row + new token_hash → that agent authenticates | integration | `uv run pytest tests/test_routers/test_agent_auth.py::test_new_token_authenticates -x` | ❌ W0 |
| DIST-04 (1/5) | POST /files round-trips with auth + idempotent upsert | integration | `uv run pytest tests/test_routers/test_agent_files.py::test_upsert_happy_path -x` | ❌ W0 |
| DIST-04 (2/5) | PUT /metadata/{file_id} round-trips | integration | `uv run pytest tests/test_routers/test_agent_metadata.py::test_metadata_put_happy_path -x` | ❌ W0 |
| DIST-04 (3/5) | PUT /fingerprints/{file_id}/{engine} round-trips | integration | `uv run pytest tests/test_routers/test_agent_fingerprint.py::test_fingerprint_put_happy_path -x` | ❌ W0 |
| DIST-04 (4/5) | POST + PATCH /execution-log round-trips | integration | `uv run pytest tests/test_routers/test_agent_execution.py::test_execution_log_create_and_patch -x` | ❌ W0 |
| DIST-04 (5/5) | POST /heartbeat returns 204 and persists `last_status` JSONB | integration | `uv run pytest tests/test_routers/test_agent_heartbeat.py::test_heartbeat_persists_status -x` | ❌ W0 |
| DIST-05 (1/5) | File upsert replay: same chunk twice → one row | integration | `uv run pytest tests/test_routers/test_agent_files.py::test_replay_no_duplicates -x` | ❌ W0 |
| DIST-05 (2/5) | Metadata replay: same payload twice → one row, latest values | integration | `uv run pytest tests/test_routers/test_agent_metadata.py::test_replay_overwrites -x` | ❌ W0 |
| DIST-05 (3/5) | Fingerprint replay: same `(file_id, engine)` twice → one row | integration | `uv run pytest tests/test_routers/test_agent_fingerprint.py::test_replay_overwrites -x` | ❌ W0 |
| DIST-05 (4/5) | ExecutionLog POST replay: same agent-supplied id twice → one row, no error | integration | `uv run pytest tests/test_routers/test_agent_execution.py::test_create_replay_no_op -x` | ❌ W0 |
| DIST-05 (5/5) | ExecutionLog monotonic PATCH: IN_PROGRESS → PENDING returns 409 | integration | `uv run pytest tests/test_routers/test_agent_execution.py::test_monotonic_regress_returns_409 -x` | ❌ W0 |
| D-15 | Terminal state COMPLETED rejects further PATCH with 409 | integration | `uv run pytest tests/test_routers/test_agent_execution.py::test_terminal_state_rejects_patch -x` | ❌ W0 |
| D-16 | Extra body field returns 422 with `extra_forbidden` error type | integration | `uv run pytest tests/test_routers/test_agent_files.py::test_extra_body_field_422 -x` | ❌ W0 |
| D-20 (1/2) | After POST /files with 2 INSERTed music files, 2 enqueue calls on `phaze-agent-<id>` queue | integration (mocked Queue) | `uv run pytest tests/test_routers/test_agent_files.py::test_auto_enqueue_only_for_inserts -x` | ❌ W0 |
| D-20 (2/2) | After POST /files where all rows are UPDATEs, 0 enqueue calls | integration (mocked Queue) | `uv run pytest tests/test_routers/test_agent_files.py::test_no_enqueue_for_updates -x` | ❌ W0 |
| D-21 | `RETURNING (xmax = 0)` regression: new key → `inserted=True`; same key → `inserted=False` | integration (real Postgres) | `uv run pytest tests/test_services/test_agent_upsert.py::test_xmax_inserted_flag -x` | ❌ W0 |
| D-22 | `Queue.from_url` called with `name=f"phaze-agent-{agent.id}"` exactly | integration (mocked) | covered by `test_auto_enqueue_only_for_inserts` | ❌ W0 |
| OpenAPI | `/openapi.json` includes `components.securitySchemes.bearerAuth` (`type: http`, `scheme: bearer`) | unit | `uv run pytest tests/test_routers/test_agent_auth.py::test_openapi_bearer_scheme -x` | ❌ W0 |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky — task IDs filled in during plan-checker review.*

---

## Wave 0 Requirements

- [ ] `tests/test_routers/test_agent_auth.py` — stubs for AUTH-01, AUTH-04, OpenAPI bearer scheme
- [ ] `tests/test_routers/test_agent_files.py` — stubs for DIST-04 (1/5), DIST-05 (1/5), D-20, D-22, D-16
- [ ] `tests/test_routers/test_agent_metadata.py` — stubs for DIST-04 (2/5), DIST-05 (2/5)
- [ ] `tests/test_routers/test_agent_fingerprint.py` — stubs for DIST-04 (3/5), DIST-05 (3/5)
- [ ] `tests/test_routers/test_agent_execution.py` — stubs for DIST-04 (4/5), DIST-05 (4/5), DIST-05 (5/5), D-15
- [ ] `tests/test_routers/test_agent_heartbeat.py` — stubs for DIST-04 (5/5)
- [ ] `tests/test_services/test_agent_upsert.py` — stubs for D-21 (xmax regression test against real Postgres)
- [ ] `tests/conftest.py` (or `tests/conftest_agent.py`) — extend with `seed_test_agent` + `authenticated_client` shared fixtures
- [ ] Framework install: none needed — pytest + pytest-asyncio + httpx already in `pyproject.toml` `[dependency-groups.dev]`

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|---|---|---|---|
| OpenAPI lock icon renders only on `agent-internal` tag group in Swagger UI | OpenAPI / D-discretion | Visual rendering of `/docs` is a browser concern, not asserted in pytest | Start app: `just dev` → open `http://localhost:8000/docs` → confirm lock icon next to every `agent-internal`-tagged route, absent from operator routes |

*All functional success criteria have automated verification; manual check is documentation-quality only.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (8 test-file stubs above)
- [ ] No watch-mode flags (e.g., `--watch`, `-f`) in any plan
- [ ] Feedback latency < 10s for quick run
- [ ] `nyquist_compliant: true` set in frontmatter (after plan-checker review)

**Approval:** pending — set to `approved YYYY-MM-DD` after plan-checker verifies every PLAN.md task references a row above.
