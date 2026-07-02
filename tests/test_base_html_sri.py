"""Regression tests for base.html CDN script integrity (Phase 27 UAT Gap 11).

When a `<script src=...>` tag uses Subresource Integrity (SRI), the browser
refuses to execute the script if the served file's SHA hash does not match
the `integrity` attribute. For the phaze admin UI this means: if any pinned
SRI hash drifts out of sync with the actual CDN content, the page is left
non-interactive (HTMX/Alpine blocked).

NOTE: Tailwind is no longer a CDN `<script>` — it is compiled at image-build
time by the standalone Tailwind binary and served as a same-origin
`/static/css/app.css` `<link>` (no SRI needed). The `@tailwindcss/browser@4`
examples below are retained as the *historical* motivation for the version-pin
rule; the assertions now cover only the remaining CDN scripts (HTMX, the HTMX
SSE extension, and Alpine).

Two failure modes the tests below cover:

1. **Floating major-version URL** — e.g., `@tailwindcss/browser@4` without a
   minor.patch suffix. The CDN silently ships newer point releases under the
   same URL, and the previously-computed SRI hash becomes invalid the next
   time the file is regenerated. The static test asserts every CDN <script>
   that carries an `integrity=` attribute also pins a specific
   `<name>@<major>.<minor>.<patch>` (or commit/sha) — not just `@<major>`.

2. **Drifted SRI hash** — even with a fully pinned version, the SRI can be
   wrong from the start (typo, copy-paste error, manual edit). The
   network-using test fetches each pinned URL and asserts the SHA-384 of
   the response body matches the inline `integrity=` value. Marked
   `integration` so offline / CI-sandboxed runs can skip it.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re
import socket
import time

import pytest


_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "phaze" / "templates"
_BASE_HTML = _TEMPLATES_DIR / "base.html"
# RESEARCH Pitfall 1: the v7.0 shell runs on shell.html (its OWN <head> block), where the
# record slide-in + ⌘K focus-traps actually load @alpinejs/focus. A stale/missing SRI hash
# there was previously invisible to this test — guard BOTH templates.
_SHELL_HTML = _TEMPLATES_DIR / "shell" / "shell.html"
_ALL_TEMPLATES = (_BASE_HTML, _SHELL_HTML)
_SCRIPT_TAG = re.compile(
    r"<script\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*?\bintegrity=[\"']([^\"']+)[\"']",
    re.IGNORECASE | re.DOTALL,
)

# Bounded retry for the network SRI check: tolerates transient jsdelivr edge
# inconsistency (a one-off differently-served body) without masking a real,
# persistent hash drift, which mismatches on every attempt.
_MAX_FETCH_ATTEMPTS = 3
_FETCH_RETRY_DELAY_SECONDS = 1.0


def _extract_cdn_scripts(template: Path = _BASE_HTML) -> list[tuple[str, str]]:
    """Return (src, integrity) tuples for every <script> in ``template`` with both attrs."""
    html = template.read_text()
    return _SCRIPT_TAG.findall(html)


@pytest.mark.parametrize("template", _ALL_TEMPLATES, ids=lambda p: p.name)
def test_base_html_has_at_least_one_cdn_script_with_integrity(template: Path) -> None:
    """Sanity: regression test would be vacuously satisfied otherwise."""
    scripts = _extract_cdn_scripts(template)
    assert len(scripts) >= 1, f"no SRI-protected scripts found in {template}"


@pytest.mark.parametrize("template", _ALL_TEMPLATES, ids=lambda p: p.name)
def test_every_cdn_script_pins_a_specific_version(template: Path) -> None:
    """Phase 27 UAT Gap 11: SRI-protected URLs must NOT use floating major-version pins.

    `@tailwindcss/browser@4` was the culprit — jsdelivr served a newer 4.x build
    than the one the SRI was computed against, and the browser blocked Tailwind
    entirely (page rendered unstyled). The fix is to pin to `@4.3.0` etc.

    This static check passes regardless of network state. Acceptable forms:
        - `@4.3.0`, `@1.2.3` (full semver)
        - `@4.3.0-beta.1`, `@4.3.0-rc.1` (semver with pre-release)
        - `@<40-char-sha>` (git commit pin)
    Rejected forms:
        - `@4`, `@4.3` (incomplete version — CDN can ship newer point releases)
    """
    bad: list[tuple[str, str]] = []
    full_semver = re.compile(r"@\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?(?:/|$)")
    full_sha = re.compile(r"@[0-9a-f]{40}(?:/|$)")
    for src, _ in _extract_cdn_scripts(template):
        # The version pin sits between the package name and the trailing path
        # segment. Search the URL for any acceptable form; if none found, flag.
        if full_semver.search(src) or full_sha.search(src):
            continue
        bad.append((src, "missing or incomplete version pin"))
    assert not bad, (
        f"SRI-protected scripts must pin a specific version (not @<major>) so the hash doesn't drift on CDN point-release bumps. Offenders: {bad}"
    )


def _has_internet() -> bool:
    try:
        socket.create_connection(("cdn.jsdelivr.net", 443), timeout=3).close()
    except OSError:
        return False
    return True


@pytest.mark.integration
@pytest.mark.skipif(not _has_internet(), reason="network unavailable")
@pytest.mark.parametrize("template", _ALL_TEMPLATES, ids=lambda p: p.name)
def test_cdn_sri_hashes_match_served_content(template: Path) -> None:
    """Phase 27 UAT Gap 11: every pinned SRI hash must match what the CDN actually serves.

    This is the strongest form of the check — it fetches each URL and
    recomputes SHA-384. Slow (network) and skip-able offline, but it catches
    SRI drift even when the URL is already pinned to a specific version
    (e.g., someone edited the hash by hand without updating the URL, or the
    CDN's specific-version response actually changed under us).

    Encoding: the browser computes SRI over the DECODED resource bytes, so the
    test must hash the same thing. We request ``Accept-Encoding: identity`` to
    force the CDN to return the uncompressed file. Without this, a client that
    advertises an encoding it cannot transparently decode (e.g. ``br`` when no
    brotli library is installed — the situation on CI runners) ends up hashing
    the *compressed* bytes, which never match the SRI hash a browser validates.
    That mismatch was a false positive, not a real drift; forcing identity makes
    the comparison deterministic across environments.

    Bounded retry: a one-off network blip / partial response is retried up to
    ``_MAX_FETCH_ATTEMPTS`` times; a genuinely drifted pin mismatches on every
    attempt, so real-drift detection is preserved.
    """
    import httpx

    failures: list[str] = []
    for src, integrity in _extract_cdn_scripts(template):
        # Reject anything other than https:// so a malicious commit cannot smuggle
        # in a file:// or http:// URL (semgrep CWE-939: improper handler for custom URL schemes).
        if not src.startswith("https://"):
            failures.append(f"{src}: refusing non-https URL scheme in SRI verification")
            continue
        algo, _, b64hash = integrity.partition("-")
        if algo not in ("sha256", "sha384", "sha512"):
            failures.append(f"{src}: unsupported SRI algo {algo!r}")
            continue
        last_error: str | None = None
        for attempt in range(_MAX_FETCH_ATTEMPTS):
            try:
                # Accept-Encoding: identity → hash the decoded bytes the browser
                # validates SRI against, not a compression-dependent encoding the
                # client may fail to decode (CI brotli false positive).
                response = httpx.get(src, timeout=10.0, follow_redirects=True, headers={"Accept-Encoding": "identity"})
                response.raise_for_status()
                body = response.content
            except httpx.HTTPError as exc:
                last_error = f"{src}: fetch failed {exc!r}"
            else:
                actual = base64.b64encode(hashlib.new(algo, body).digest()).decode("ascii")
                if actual == b64hash:
                    last_error = None
                    break
                last_error = f"{src}: SRI {algo}={b64hash} but CDN serves {actual}"
            if attempt < _MAX_FETCH_ATTEMPTS - 1:
                time.sleep(_FETCH_RETRY_DELAY_SECONDS)
        if last_error is not None:
            failures.append(f"{last_error} (after {_MAX_FETCH_ATTEMPTS} attempts)")
    assert not failures, "Pinned SRI hashes do not match served content:\n  " + "\n  ".join(failures)
