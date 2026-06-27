---
phase: quick-260606-qgu
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/templates/base.html
  - tests/test_base_html_sri.py
autonomous: true
requirements: [QGU-SRI-MULTIHASH]

must_haves:
  truths:
    - "base.html's Tailwind <script> integrity attribute lists BOTH sha384 hashes, space-separated"
    - "The live SRI test passes whether the runner's edge serves the d5Pc0U2 or AIH1kL7 body"
    - "A genuinely drifted body (matching neither pinned hash) still FAILS the test"
    - "htmx, htmx-sse, and alpine scripts keep their single unchanged hashes"
  artifacts:
    - path: "src/phaze/templates/base.html"
      provides: "Tailwind script with multi-hash SRI + explanatory comment"
      contains: "sha384-AIH1kL7JmmReCxSwiSyaRNwzSFO7h4Ir4F/PO28EKezqFza1LwIMgEPLd83KwmZ4"
    - path: "tests/test_base_html_sri.py"
      provides: "Set-membership SRI verification across multiple pinned hashes"
  key_links:
    - from: "tests/test_base_html_sri.py"
      to: "src/phaze/templates/base.html"
      via: "_SCRIPT_TAG regex capturing full integrity attr value"
      pattern: "integrity=.*split"
---

<objective>
Fix the flaky/failing live CDN SRI test by pinning BOTH stable jsdelivr edge variants of the
Tailwind browser bundle as space-separated SHA-384 hashes, and update the test to verify the
served body matches ANY pinned hash (set membership) rather than a single equality.

Purpose: jsdelivr serves two stable-but-different response bodies for the same versioned URL
depending on which edge/POP answers (on-the-fly minification differences). A single SRI hash
fails the live test from CI's edge AND is a latent production bug — a real browser routed to the
other edge would have Tailwind blocked by SRI and render the page unstyled. The SRI spec allows
multiple space-separated hashes; the browser accepts the resource if ANY listed hash matches.

Output: Multi-hash Tailwind SRI in base.html; set-membership verification in the test module.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md
@src/phaze/templates/base.html
@tests/test_base_html_sri.py

<interfaces>
<!-- Current test parsing logic (the part that breaks under multi-hash). -->
<!-- tests/test_base_html_sri.py line ~126: -->
<!--   algo, _, b64hash = integrity.partition("-")  -->
<!--   if algo not in ("sha256","sha384","sha512"): ...  -->
<!--   actual = base64.b64encode(hashlib.new(algo, body).digest()).decode("ascii")  -->
<!--   if actual == b64hash: break  -->
<!-- The _SCRIPT_TAG regex captures the FULL integrity attr value (group 2), so a -->
<!-- space-separated multi-hash string arrives intact and just needs whitespace splitting. -->

<!-- Two legitimate, stable SHA-384 values for the Tailwind URL: -->
<!--   d5Pc0U2WLIrcpPz/5dTNl91xwJLvbdD+V3+kao+QnRyFRNDW1mKqxoM9b6fzPxsS  (local/dev egress) -->
<!--   AIH1kL7JmmReCxSwiSyaRNwzSFO7h4Ir4F/PO28EKezqFza1LwIMgEPLd83KwmZ4  (GitHub Actions egress) -->
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add second SHA-384 hash to Tailwind SRI in base.html</name>
  <files>src/phaze/templates/base.html</files>
  <action>
    On the Tailwind `<script>` (currently line ~24), change the `integrity` attribute from the
    single hash to BOTH hashes, space-separated, same sha384 algo:
    `integrity="sha384-d5Pc0U2WLIrcpPz/5dTNl91xwJLvbdD+V3+kao+QnRyFRNDW1mKqxoM9b6fzPxsS sha384-AIH1kL7JmmReCxSwiSyaRNwzSFO7h4Ir4F/PO28EKezqFza1LwIMgEPLd83KwmZ4"`.
    Keep `src` (the `@4.3.0` pinned URL) and `crossorigin="anonymous"` unchanged. Update/extend the
    existing HTML comment above the script (lines ~21-23) to note WHY it carries two hashes:
    jsdelivr serves two stable but different edge variants of this versioned URL (on-the-fly
    minification differs by POP); multi-hash SRI lets both validate so the browser never blocks
    Tailwind regardless of which edge a user is routed to. Leave the htmx, htmx-sse, and alpine
    scripts (lines ~30, 33, 36) untouched — they are byte-stable and keep their single hash.
  </action>
  <verify>
    <automated>grep -c "sha384-AIH1kL7JmmReCxSwiSyaRNwzSFO7h4Ir4F/PO28EKezqFza1LwIMgEPLd83KwmZ4" src/phaze/templates/base.html | grep -qx 1 && grep -q "sha384-d5Pc0U2WLIrcpPz/5dTNl91xwJLvbdD+V3+kao+QnRyFRNDW1mKqxoM9b6fzPxsS sha384-AIH1kL7" src/phaze/templates/base.html && echo OK</automated>
  </verify>
  <done>Tailwind script's integrity lists both sha384 hashes space-separated; other three CDN scripts unchanged; comment explains the dual-edge rationale.</done>
</task>

<task type="auto">
  <name>Task 2: Make SRI test verify served hash against the SET of pinned hashes</name>
  <files>tests/test_base_html_sri.py</files>
  <action>
    In `test_cdn_sri_hashes_match_served_content`, replace the single-hash parse
    (`algo, _, b64hash = integrity.partition("-")`) with parsing the captured `integrity` string
    as a SET of `algo-b64` tokens split on whitespace. For each token, partition on the FIRST "-"
    into (algo, b64hash); validate algo against the existing allowlist ("sha256","sha384","sha512")
    and skip/flag unsupported algos as before. Within the bounded-retry loop, after computing the
    served body's hash, accept the fetch if the served hash matches ANY pinned token whose algo
    equals the algo used to hash the body — i.e. compute the body digest per distinct algo present
    in the token set and test membership against that algo's accepted b64 values. The test passes
    if the served body matches at least one pinned token; it fails only if the served body matches
    NONE of the pinned hashes after `_MAX_FETCH_ATTEMPTS`. Preserve the https-only guard, the algo
    allowlist, and the existing bounded-retry loop (now mostly redundant but harmless — keep it).
    Update the failure/diagnostic message to print the served hash and the full SET of accepted
    hashes for that URL. Confirm `test_every_cdn_script_pins_a_specific_version` and
    `test_base_html_has_at_least_one_cdn_script_with_integrity` still pass: the `_SCRIPT_TAG` regex
    captures the full integrity attr value (the multi-hash string lives in the integrity group),
    and the version-pin test only inspects `src`, so it is unaffected — no regex change is required
    for capture, only the downstream parsing of the integrity group splits on whitespace.
    Use double quotes, full type hints, 150-char line length per CLAUDE.md.
  </action>
  <verify>
    <automated>uv run pytest tests/test_base_html_sri.py -m "" -q && uv run pytest tests/test_base_html_sri.py -q && uv run ruff check tests/test_base_html_sri.py && uv run mypy tests/test_base_html_sri.py</automated>
  </verify>
  <done>Live test passes against the local d5Pc0U2 edge variant via set membership; static tests pass unchanged; a body matching neither pinned hash would still produce a failure; ruff + mypy clean.</done>
</task>

</tasks>

<verification>
- `uv run pytest tests/test_base_html_sri.py -m "" -q` passes (live integration test included).
- `uv run pytest tests/test_base_html_sri.py -q` passes (static tests).
- `pre-commit run --all-files` passes (frozen-SHA hooks: ruff, ruff-format, mypy, etc.). NEVER --no-verify.
- No other test asserts on base.html's Tailwind integrity (grep confirmed only this module and an
  unrelated agent-task file-hash check reference "integrity").
</verification>

<success_criteria>
- base.html's Tailwind script lists both SHA-384 hashes (d5Pc0U2 and AIH1kL7), space-separated.
- The live SRI test passes whether the runner's edge serves the d5Pc0U2 or AIH1kL7 body.
- A genuinely wrong/drifted body (matching neither) still fails the test.
- src URL, htmx/htmx-sse/alpine hashes, and the retry loop are all unchanged.
</success_criteria>

<output>
Create `.planning/quick/260606-qgu-fix-flaky-cdn-sri-test-jsdelivr-serves-t/260606-qgu-SUMMARY.md` when done.
</output>
