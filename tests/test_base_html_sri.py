"""Regression tests for base.html CDN script integrity (Phase 27 UAT Gap 11).

When a `<script src=...>` tag uses Subresource Integrity (SRI), the browser
refuses to execute the script if the served file's SHA hash does not match
the `integrity` attribute. For the phaze admin UI this means: if any pinned
SRI hash drifts out of sync with the actual CDN content, the entire page
renders unstyled (Tailwind blocked) or non-interactive (HTMX/Alpine blocked).

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


_BASE_HTML = Path(__file__).resolve().parents[1] / "src" / "phaze" / "templates" / "base.html"
_SCRIPT_TAG = re.compile(
    r"<script\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*?\bintegrity=[\"']([^\"']+)[\"']",
    re.IGNORECASE | re.DOTALL,
)

# Bounded retry for the network SRI check: tolerates transient jsdelivr edge
# inconsistency (a one-off differently-served body) without masking a real,
# persistent hash drift, which mismatches on every attempt.
_MAX_FETCH_ATTEMPTS = 3
_FETCH_RETRY_DELAY_SECONDS = 1.0


def _extract_cdn_scripts() -> list[tuple[str, str]]:
    """Return (src, integrity) tuples for every <script> in base.html with both attrs."""
    html = _BASE_HTML.read_text()
    return _SCRIPT_TAG.findall(html)


def test_base_html_has_at_least_one_cdn_script_with_integrity() -> None:
    """Sanity: regression test would be vacuously satisfied otherwise."""
    scripts = _extract_cdn_scripts()
    assert len(scripts) >= 1, f"no SRI-protected scripts found in {_BASE_HTML}"


def test_every_cdn_script_pins_a_specific_version() -> None:
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
    for src, _ in _extract_cdn_scripts():
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
def test_cdn_sri_hashes_match_served_content() -> None:
    """Phase 27 UAT Gap 11: every pinned SRI hash must match what the CDN actually serves.

    This is the strongest form of the check — it fetches each URL and
    recomputes SHA-384. Slow (network) and skip-able offline, but it catches
    SRI drift even when the URL is already pinned to a specific version
    (e.g., someone edited the hash by hand without updating the URL, or the
    CDN's specific-version response actually changed under us).

    Bounded retry: jsdelivr edge nodes occasionally serve a transiently
    different (e.g. differently-minified or partial) body for the same
    versioned URL, which makes a single-fetch assertion flaky. Each URL is
    fetched up to ``_MAX_FETCH_ATTEMPTS`` times and passes as soon as ONE
    fetch matches the pinned hash. A genuinely drifted pin mismatches on
    every attempt, so real-drift detection is preserved; only one-off edge
    inconsistencies are tolerated.
    """
    import httpx

    failures: list[str] = []
    for src, integrity in _extract_cdn_scripts():
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
                response = httpx.get(src, timeout=10.0, follow_redirects=True)
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
