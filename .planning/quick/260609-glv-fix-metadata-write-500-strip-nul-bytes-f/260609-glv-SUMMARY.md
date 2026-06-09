---
phase: quick-260609-glv
plan: 01
subsystem: metadata-extraction
tags: [bugfix, postgres, mutagen, nul-bytes, tdd]
requires: []
provides: "NUL-sanitized tag extraction (_strip_nul applied in _first_str and _serialize_tags)"
affects:
  - src/phaze/services/metadata.py
  - tests/test_services/test_metadata.py
tech-stack:
  added: []
  patterns: ["Strip U+0000 from all DB-bound tag strings at the extraction boundary"]
key-files:
  created: []
  modified:
    - src/phaze/services/metadata.py
    - tests/test_services/test_metadata.py
decisions:
  - "Strip NUL at the two existing sanitization points (_first_str, _serialize_tags) rather than at the DB layer, so messy tags are normalized before leaving the agent."
metrics:
  duration: ~4m
  completed: 2026-06-09
  tasks: 2
  files: 2
---

# Phase quick-260609-glv: Fix metadata-write 500 — strip NUL bytes Summary

Strip NUL bytes (U+0000) from every mutagen-extracted tag string before it leaves the agent, fixing the asyncpg `UntranslatableCharacterError` that returned 500 from `PUT /api/internal/agent/metadata/<id>` for archive files carrying NUL-tainted ID3/Vorbis/MP4 tag values.

## What Was Built

- **`_strip_nul(s: str) -> str`** helper in `src/phaze/services/metadata.py` — returns `s.replace("\x00", "")`, placed above `_first_str`.
- Applied in **`_first_str`**: both string return paths now NUL-strip; `None` input and empty-list input still return `None`.
- Applied in **`_serialize_tags`**: the key (`str_key = _strip_nul(str(key))`, computed before the APIC check so the cleaned key is used everywhere), the scalar value branch, and each list item.
- **`TestStripsNulBytes`** regression class in `tests/test_services/test_metadata.py` covering `_first_str` (scalar, list, None, empty-list), `_serialize_tags` (scalar value, list item, and a NUL-tainted key), and `extract_tags` ID3 end-to-end (`result.artist == "TestArtist"`, no `\x00` anywhere in `raw_tags`).

`_parse_year` / `_parse_track` were left untouched (year/track parse to int, unaffected). No version bump, no `pyproject.toml` changes.

## Tasks Completed

| Task | Name                                          | Commit  | Files                                   |
| ---- | --------------------------------------------- | ------- | --------------------------------------- |
| 1    | Add `_strip_nul` helper, apply at both points | 4b37c13 | src/phaze/services/metadata.py          |
| 2    | Add regression test for NUL stripping         | 80a9dbd | tests/test_services/test_metadata.py    |

## Verification Evidence

`uv run ruff check .`:
```
All checks passed!
```

`uv run ruff format --check .`:
```
274 files already formatted
```

`uv run mypy .`:
```
Success: no issues found in 141 source files
```

`uv run pytest tests/test_services/test_metadata.py -q`:
```
.................................                                        [100%]
33 passed in 0.03s
```

Task-1 file-scoped checks (`uv run ruff check src/phaze/services/metadata.py && uv run mypy src/phaze/services/metadata.py`) also passed; `grep -n "_strip_nul"` confirms references at lines 61 (def), 74-75 (`_first_str`), and 153/165/169 (`_serialize_tags`). Pre-commit hooks (ruff, ruff-format, bandit, mypy) passed on both commits — no `--no-verify` used.

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/phaze/services/metadata.py (modified, `_strip_nul` present)
- FOUND: tests/test_services/test_metadata.py (modified, `TestStripsNulBytes` present)
- FOUND commit: 4b37c13
- FOUND commit: 80a9dbd

## Follow-up (2026-06-09): broadened sanitizer to lone surrogates — commit 69ab2a8

Per PostgreSQL §8.14, broadened the NUL-only sanitizer to cover everything PostgreSQL actually rejects in a UTF8 text/jsonb column:

- Renamed **`_strip_nul` -> `_sanitize_pg_text(s: str) -> str`** and updated all five call sites (two in `_first_str`, three in `_serialize_tags`: key, scalar value, list item).
- Implementation now uses a module-level compiled regex `_PG_INVALID_CHARS = re.compile(r"[\x00\ud800-\udfff]")` and strips **NUL (U+0000)** plus **lone Unicode surrogates (U+D800-U+DFFF)**. Lone surrogates are rejected by jsonb and are unencodable to UTF-8 by asyncpg; in a Python `str`, astral chars are single code points, so any code point in that range is necessarily a lone (invalid) surrogate — stripping the whole range is safe-by-construction.
- **Deliberately preserves** other C0/C1 controls, DEL, and Unicode noncharacters (U+FFFE/U+FFFF/U+FDD0-U+FDEF): these are valid in a UTF8 database and over-stripping would corrupt legitimate tag text. Documented with a comment referencing PostgreSQL §8.14.
- Added `import re` (isort-compliant) and a docstring/comment explaining what is stripped and why.

Test coverage added in `tests/test_services/test_metadata.py`:
- `TestSanitizePgText` — direct unit tests: strips NUL, lone high/low surrogate, the entire U+D800-U+DFFF range; **preserves** U+0007 (bell), U+FFFE (noncharacter), and a real astral emoji U+1F600.
- `TestStripsLoneSurrogates` — regression: lone surrogates fed through `_first_str` (scalar + list), `_serialize_tags` (key, scalar value, list item), and `extract_tags` ID3 end-to-end; asserts no char in U+D800-U+DFFF and no `\x00` survives in any normalized field or `raw_tags`.

Behavior preserved: `_first_str` still returns `None` for `None`/empty list; year/track int parsing untouched.

Gate output (all green):
```
uv run ruff check .   -> All checks passed!
uv run mypy .         -> Success: no issues found in 141 source files
uv run pytest tests/test_services/test_metadata.py -> 44 passed in 0.07s
```

---

## Follow-up (scan_directory unbounded-timeout fix, same branch)

Live incident: scan_directory SAQ job timed out at worker_job_timeout (600s)
after ~2000/30913 files, then SAQ retried FROM SCRATCH (no checkpoint), looping
until 4 retries exhausted — a full ~29 GB archive scan could never complete.

Decision: scan_directory is a long-running BULK job and must NOT have a fixed
SAQ wall-clock timeout. Liveness is delegated entirely to the application-level
progress stall reaper.

Changes:
- `src/phaze/config.py` — `scan_stall_seconds` default 600 → 86400 (24h); UI
  amber warn fires at half (12h). Env override (PHAZE_SCAN_STALL_SECONDS /
  SCAN_STALL_SECONDS) intact.
- `src/phaze/services/agent_task_router.py` — `enqueue_for_agent` /
  `enqueue_for_file` gain keyword-only `timeout: int | None` / `retries:
  int | None`, forwarded to `queue.enqueue(...)` only when not None (build extra
  dict; merged last so explicit overrides win; other tasks keep policy defaults).
  INFO "task enqueued" log now includes timeout/retries.
- `src/phaze/routers/pipeline_scans.py` — scan_directory enqueue passes
  `timeout=0` (→ wait_for unbounded; Job.stuck stays False) and `retries=0`.
- Tests: added forward/omit assertions in test_agent_task_router.py (integration,
  real Redis); asserted timeout=0/retries=0 in test_pipeline_scans.py; added
  scan_stall_seconds==86400 default test in test_config_worker.py; pinned the
  threshold (600) in the two tests that depended on the old default
  (test_scan_reaper.py, test_pipeline_scans is_scan_stalled).

Gates:
```
uv run ruff check .   -> All checks passed!
uv run mypy .         -> Success: no issues found in 141 source files
uv run pytest tests/test_services/test_agent_task_router.py \
  tests/test_routers/test_pipeline_scans.py tests/test_config_worker.py \
  tests/test_tasks/test_scan_reaper.py -> 65 passed (Redis via ephemeral container)
```

Commits: 41cfa91, 983c2ea
