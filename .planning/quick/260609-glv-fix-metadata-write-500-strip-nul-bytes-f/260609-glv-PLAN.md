---
phase: quick-260609-glv
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/services/metadata.py
  - tests/test_services/test_metadata.py
autonomous: true
requirements: [QUICK-260609-glv]

must_haves:
  truths:
    - "Every string in ExtractedTags normalized fields (artist/title/album/genre) has NUL (\\x00) stripped"
    - "Every key, scalar value, and list item in raw_tags has NUL (\\x00) stripped"
    - "_first_str still returns None for None input (no regression)"
    - "A regression test fails before the fix and passes after"
  artifacts:
    - path: "src/phaze/services/metadata.py"
      provides: "NUL-sanitized tag extraction (_strip_nul helper applied in _first_str and _serialize_tags)"
      contains: "_strip_nul"
    - path: "tests/test_services/test_metadata.py"
      provides: "Regression test asserting no \\x00 in any extracted string field or raw_tags"
  key_links:
    - from: "_first_str"
      to: "_strip_nul"
      via: "applied to return value before returning the string"
      pattern: "_strip_nul"
    - from: "_serialize_tags"
      to: "_strip_nul"
      via: "applied to each key, scalar value, and list item"
      pattern: "_strip_nul"
---

<objective>
Fix the metadata-write 500 (asyncpg `UntranslatableCharacterError: unsupported Unicode escape sequence`) by stripping NUL bytes (`\x00`, U+0000) from every mutagen-extracted tag string before it leaves the agent. PostgreSQL `text`/`jsonb` columns cannot store U+0000; messy archive files surface NUL bytes in ID3/Vorbis/MP4 tag values, which flow into both the normalized string fields (via `_first_str`) and the `raw_tags` JSONB dict (via `_serialize_tags`).

Purpose: Unblock the metadata-extraction pipeline so `PUT /api/internal/agent/metadata/<id>` stops returning 500 for files containing NUL-tainted tags.
Output: A `_strip_nul` helper in `src/phaze/services/metadata.py` applied at both sanitization points, plus a regression test.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@./CLAUDE.md
@src/phaze/services/metadata.py
@tests/test_services/test_metadata.py

<interfaces>
<!-- Key functions the executor edits. Already in context — no exploration needed. -->

src/phaze/services/metadata.py:
  def _first_str(val: Any) -> str | None
      # None -> None; list -> str(val[0]) or None; else str(val)
  def _serialize_tags(tags: Any) -> dict[str, Any]
      # skips bytes + APIC frames; str-coerces scalars; list -> list of str(item)

tests/test_services/test_metadata.py:
  # Class-based, MagicMock-driven. TestSerializeTags builds a MagicMock
  # whose .items.return_value is a list of (key, value) tuples and calls
  # _serialize_tags directly. TestExtractTagsID3 patches
  # phaze.services.metadata.mutagen.File and sets mock_tags.get + .items.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Add _strip_nul helper and apply at both sanitization points</name>
  <files>src/phaze/services/metadata.py</files>
  <behavior>
    - _strip_nul("a\x00b") == "ab"
    - _strip_nul("clean") == "clean"
    - _first_str("a\x00b") == "ab"; _first_str(["x\x00y"]) == "xy"; _first_str(None) is None
    - _serialize_tags scalar value "v\x00" serializes to "v"
    - _serialize_tags list item "i\x00" serializes to "i"
    - _serialize_tags key "K\x00EY" serializes under key "KEY"
  </behavior>
  <action>
    Add a small private helper `def _strip_nul(s: str) -> str:` returning `s.replace("\x00", "")`, with a one-line docstring. Place it near the other private helpers (above `_first_str`).

    Apply it at both points:
    (a) In `_first_str`: wrap the two string return paths so the returned string is NUL-stripped. Keep `None` handling intact — `None` input and empty-list input must still return `None`. The list branch becomes `return _strip_nul(str(val[0])) if val else None`; the final fallthrough becomes `return _strip_nul(str(val))`.
    (b) In `_serialize_tags`: strip NUL from the key (`str_key = _strip_nul(str(key))` before the APIC check so the stripped key is used everywhere), from the scalar branch (`result[str_key] = _strip_nul(str(val))`), and from each list item (`serialized.append(_strip_nul(str(item)))`).

    Year/track parse to int and are unaffected — do not touch `_parse_year` or `_parse_track`. Match existing module style: double quotes, type hints, 150-char lines.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze && uv run ruff check src/phaze/services/metadata.py && uv run mypy src/phaze/services/metadata.py && grep -n "_strip_nul" src/phaze/services/metadata.py</automated>
  </verify>
  <done>`_strip_nul` exists and is referenced in both `_first_str` and `_serialize_tags`; ruff and mypy pass on the file; `_first_str(None)` still returns None.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Add regression test for NUL stripping</name>
  <files>tests/test_services/test_metadata.py</files>
  <behavior>
    - _first_str path: ID3 frame text ["Test\x00Artist"] -> ExtractedTags.artist == "TestArtist" (no \x00)
    - _serialize_tags path: scalar value "Song\x00Title", list value ["Art\x00ist"], and a key containing \x00 all emerge with no \x00 in keys, values, or list items
    - assert no \x00 anywhere in artist/title/album/genre or raw_tags (keys, scalar values, list items)
  </behavior>
  <action>
    Mirror the existing class-based / MagicMock style in this file. Add a new test class (e.g. `TestStripsNulBytes`).

    Add a direct `_serialize_tags` test: build a MagicMock with `tags.items.return_value` containing a scalar `("TIT2", "Song\x00Title")`, a list `("artist", ["Art\x00ist"])`, and a NUL-tainted key `("KE\x00Y", "val")`; call `_serialize_tags` and assert no `"\x00"` appears in any key, any scalar value, or any list item.

    Add an `extract_tags` ID3 test patching `phaze.services.metadata.mutagen.File` (copy the setup from `TestExtractTagsID3`) but with a frame whose `.text = ["Test\x00Artist"]`; assert `result.artist == "TestArtist"` and `"\x00" not in result.artist`, and assert no `"\x00"` in any `result.raw_tags` value.

    Import `_serialize_tags` and `extract_tags` from `phaze.services.metadata` (already imported at top of file). Add `_first_str` to that import if you also test it directly.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze && uv run pytest tests/test_services/test_metadata.py -q && uv run ruff check tests/test_services/test_metadata.py</automated>
  </verify>
  <done>New regression test(s) pass; full `test_metadata.py` suite green; ruff clean on the test file.</done>
</task>

</tasks>

<verification>
- `cd /Users/Robert/Code/public/phaze && uv run pytest tests/test_services/test_metadata.py -q` passes
- `uv run ruff check . && uv run ruff format --check .` clean
- `uv run mypy .` passes
- `pre-commit run --all-files` passes (NEVER bypass with --no-verify)
</verification>

<success_criteria>
- NUL bytes (`\x00`) are stripped from all normalized string fields and from raw_tags (keys, scalar values, list items) before leaving the agent.
- `_first_str(None)` still returns None; year/track int parsing unchanged.
- Regression test would fail without the fix and passes with it.
- No version bump, no pyproject changes.
</success_criteria>

<output>
Create `.planning/quick/260609-glv-fix-metadata-write-500-strip-nul-bytes-f/260609-glv-SUMMARY.md` when done
</output>
