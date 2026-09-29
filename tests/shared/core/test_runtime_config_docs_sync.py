"""Ties docs/configuration.md's reloadable-key table to RELOADABLE_KEYS so they cannot drift.

``phaze-mvq8z.12``'s acceptance criteria required "a test or generated table ties the doc list to
the code's reloadable set so they cannot drift". ``phaze.runtime_config.RELOADABLE_KEYS`` is
derived from ``RuntimeConfig.model_fields`` (see that module's docstring), so it is the sole
authority for which keys a hot-reload actually accepts; the operator-facing "Reloadable keys"
table in ``docs/configuration.md`` is hand-written prose that a future ``phaze-mvq8z.*`` follow-up
bead could add or remove a key from without touching the doc, or vice versa.

This test parses the ``| Key | ... |`` markdown table under the "### Reloadable keys" heading and
asserts its first-column key set is *exactly* ``RELOADABLE_KEYS`` -- not a superset or subset in
either direction, so a key added to the code with no doc row, or a doc row for a key the code no
longer reloads, both fail the build rather than silently drifting apart. Follows the filesystem-only,
fixture-free prose-guard pattern in ``test_dead_template_guard.py`` / ``test_docs_ia_current.py``.
"""

from __future__ import annotations

from pathlib import Path
import re

from phaze.runtime_config import RELOADABLE_KEYS


_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIGURATION_DOC = _REPO_ROOT / "docs" / "configuration.md"

#: The table lives under this heading, up to the next ``###``/``##`` heading.
_SECTION_HEADING = "### Reloadable keys"
_NEXT_HEADING = re.compile(r"^#{2,3} ", re.MULTILINE)
#: A markdown table row whose first cell is a backtick-quoted key, e.g. "| `log_level` | ... |".
_TABLE_KEY_ROW = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|", re.MULTILINE)


def _reloadable_keys_section() -> str:
    text = _CONFIGURATION_DOC.read_text(encoding="utf-8")
    start = text.index(_SECTION_HEADING) + len(_SECTION_HEADING)
    next_heading = _NEXT_HEADING.search(text, start)
    end = next_heading.start() if next_heading is not None else len(text)
    return text[start:end]


def _documented_reloadable_keys() -> set[str]:
    return set(_TABLE_KEY_ROW.findall(_reloadable_keys_section()))


def test_configuration_doc_has_a_reloadable_keys_section() -> None:
    assert _SECTION_HEADING in _CONFIGURATION_DOC.read_text(encoding="utf-8")


def test_documented_reloadable_keys_match_the_code_exactly() -> None:
    documented = _documented_reloadable_keys()
    assert documented, "no `key` rows found under '### Reloadable keys' in docs/configuration.md -- table format changed?"
    missing_from_docs = RELOADABLE_KEYS - documented
    stale_in_docs = documented - RELOADABLE_KEYS
    assert not missing_from_docs, (
        f"RELOADABLE_KEYS has keys with no row in docs/configuration.md's 'Reloadable keys' table: "
        f"{sorted(missing_from_docs)} -- document how each applies (immediately / next job / new children / drain by attrition)."
    )
    assert not stale_in_docs, (
        f"docs/configuration.md documents keys as reloadable that RELOADABLE_KEYS no longer contains: "
        f"{sorted(stale_in_docs)} -- update or remove the stale row(s)."
    )
