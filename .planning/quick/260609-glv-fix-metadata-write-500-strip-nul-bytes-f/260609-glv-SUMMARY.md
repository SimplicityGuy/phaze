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
