"""Partition guard (Phase 63-02, D-06 — keeps the bucket partition trustworthy for CI-03).

The parallel-CI matrix runs each test bucket as an independent job whose ``.coverage``
shard is later combined into ONE report (CI-03). That combined number is only trustworthy
if every collected test lives under exactly one known bucket directory: a test in *zero*
buckets is silently dropped from coverage; a test in *two* would be double-counted. This
guard fails CI the moment a test file escapes a bucket, so the combined coverage number
Phase 64 raises its gate against cannot quietly drift.

How it works:

* **Bucket set** — loaded from ``tests/buckets.json`` (the single source of truth shared
  with the GitHub matrix and the ``just test-bucket`` recipe). It is NOT hardcoded here:
  hardcoding would let the matrix and the guard drift apart.
* **Collected set** — every file matching pytest's two default ``python_files`` globs,
  ``test_*.py`` AND ``*_test.py``. Globbing BOTH is load-bearing: ``*_test.py`` is the
  exact blind spot that hid ``tests/_queue_fakes_test.py`` (4 real tests) at the repo
  root before the reorg. A ``test_*.py``-only walk would let a reintroduced ``*_test.py``
  escape both the buckets and this guard.
* **Invariant** — for every collected file, the path segment immediately under ``tests/``
  must be a known bucket. A file sitting directly at ``tests/`` root (no bucket segment)
  is an offender too — the four stay-at-root helpers (``conftest.py``, ``_queue_fakes.py``,
  ``_route_introspection.py``, ``kube_fakes.py``) are not test files and so never match
  either glob.

The single assertion enumerates each offender with its (unknown) top segment so a future
unbucketed test fails loud with an actionable message. ``test_meta_guard_flags_unbucketed``
proves the check is not vacuously green by running a synthetic out-of-bucket path through
the same membership logic.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePath


_TESTS_ROOT = Path(__file__).resolve().parents[1]
_BUCKETS_JSON = _TESTS_ROOT / "buckets.json"

# Single source of truth — the same file the CI matrix and `just test-bucket` consume.
KNOWN_BUCKETS: frozenset[str] = frozenset(json.loads(_BUCKETS_JSON.read_text(encoding="utf-8")))

# pytest's default `python_files` globs. BOTH are enforced so a `*_test.py` reintroduction
# cannot silently escape (the historical `_queue_fakes_test.py` blind spot).
_TEST_GLOBS = ("test_*.py", "*_test.py")


def _unknown_bucket_segment(rel_parts: tuple[str, ...], known: frozenset[str]) -> str | None:
    """Return the offending top segment for a tests-relative path, or ``None`` if bucketed.

    ``rel_parts`` is the path of a test file relative to ``tests/`` (e.g.
    ``("shared", "core", "queue_fakes_test.py")``). The bucket is the first segment.
    A file directly under ``tests/`` (one part == just the filename) has no bucket
    segment and is reported as the sentinel ``"<tests-root>"``.
    """
    if len(rel_parts) < 2:
        return "<tests-root>"
    top = rel_parts[0]
    return None if top in known else top


def _collected_test_files() -> list[Path]:
    """Every file pytest would collect under ``tests/`` (both default globs, deduped)."""
    found: set[Path] = set()
    for pattern in _TEST_GLOBS:
        found.update(_TESTS_ROOT.rglob(pattern))
    return sorted(found)


def test_buckets_json_is_the_source_of_truth() -> None:
    """KNOWN_BUCKETS is loaded from tests/buckets.json (not hardcoded) and non-empty."""
    assert _BUCKETS_JSON.is_file(), f"missing single-source-of-truth bucket list: {_BUCKETS_JSON}"
    assert KNOWN_BUCKETS, "buckets.json parsed to an empty set"
    # The nine canonical buckets Plan 01 froze; a rename here must be a deliberate json edit.
    assert frozenset({"discovery", "metadata", "fingerprint", "analyze", "identify", "review", "agents", "integration", "shared"}) == KNOWN_BUCKETS


def test_every_collected_test_lives_in_a_known_bucket() -> None:
    """No test file escapes a bucket dir (D-06). Covers both test_*.py and *_test.py."""
    offenders: list[str] = []
    for path in _collected_test_files():
        rel_parts = path.relative_to(_TESTS_ROOT).parts
        bad = _unknown_bucket_segment(rel_parts, KNOWN_BUCKETS)
        if bad is not None:
            offenders.append(f"tests/{'/'.join(rel_parts)} (top segment: {bad!r})")
    assert not offenders, "Test files outside a known bucket directory (add to a tests/<bucket>/):\n" + "\n".join(sorted(offenders))


def test_meta_guard_flags_unbucketed() -> None:
    """Meta-test: the membership logic flags an out-of-bucket path (guard is not vacuous)."""
    # A crafted path under an unknown top segment must be flagged...
    assert _unknown_bucket_segment(PurePath("not_a_bucket/test_x.py").parts, KNOWN_BUCKETS) == "not_a_bucket"
    # ...a bare root-level test file must be flagged (the `*_test.py`-at-root blind spot)...
    assert _unknown_bucket_segment(PurePath("queue_fakes_test.py").parts, KNOWN_BUCKETS) == "<tests-root>"
    # ...and a real bucketed path must NOT be flagged.
    assert _unknown_bucket_segment(PurePath("shared/core/queue_fakes_test.py").parts, KNOWN_BUCKETS) is None
