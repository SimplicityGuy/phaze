"""``.env.example``'s "Watcher tunables (optional -- defaults shown below)" block must actually
show the shipped defaults (phaze-7li3t).

The block's header promises "defaults shown below". ``PHAZE_SCAN_STALL_SECONDS`` drifted from
that promise: the template showed ``600`` (the value PR4 originally shipped), while
``config.py``'s real default was later raised to ``86400`` after a live incident (a scan on a
multi-GB archive was falsely reaped at the tight 600s threshold) -- and the template was never
updated. An operator who uncommented the line believing they were pinning the existing default
instead cut it 144x, silently reaping every healthy long-running scan.

This test parses every commented ``# PHAZE_*=<value>`` line in the watcher tunables block and
asserts it matches the corresponding ``phaze.config`` field's real default, so ANY future value
drift in this block -- not just the one instance already fixed -- fails the build instead of
waiting for another live incident to surface it.

No DB, no Redis required.
"""

from __future__ import annotations

from pathlib import Path
import re


_BLOCK_HEADER = "# Watcher tunables (optional -- defaults shown below)"
_SECTION_DELIMITER = "# ====================================================================="
_COMMENTED_VAR_RE = re.compile(r"^# (PHAZE_[A-Z0-9_]+)=(.+)$", re.MULTILINE)


def _read_env_example() -> str:
    """Read .env.example from the repo root (sibling of pyproject.toml)."""
    repo_root = Path(__file__).resolve().parents[3]
    return (repo_root / ".env.example").read_text(encoding="utf-8")


def _extract_watcher_tunables_block(text: str) -> str:
    """Slice out the watcher-tunables section body (between its header box and the next one).

    Each section in ``.env.example`` is boxed by a `# ===` delimiter line immediately above
    AND below its title (``# ===\\n# <title>\\n# ===\\n<body>``), so the first delimiter after
    the header text is the header's OWN closing bar, not the start of the next section --
    the body we want starts only after that.
    """
    start = text.index(_BLOCK_HEADER)
    header_closing_bar = text.index(_SECTION_DELIMITER, start + len(_BLOCK_HEADER))
    end = text.index(_SECTION_DELIMITER, header_closing_bar + len(_SECTION_DELIMITER))
    return text[header_closing_bar:end]


def _commented_defaults(block: str) -> dict[str, str]:
    """Extract every `# PHAZE_NAME=value` commented line in the block as {alias: value}."""
    return dict(_COMMENTED_VAR_RE.findall(block))


def _field_for_alias(alias: str) -> tuple[str, object] | None:
    """Find the `phaze.config.AgentSettings` field whose validation_alias includes `alias`.

    `AgentSettings` is used (rather than `BaseSettings`/`ControlSettings`) because it is a
    strict superset for this block: `scan_stall_seconds` lives on `BaseSettings`, the other
    five watcher/scan knobs live on `AgentSettings` -- inheriting the shared ones.
    """
    from phaze.config import AgentSettings

    for name, field in AgentSettings.model_fields.items():
        choices = getattr(field.validation_alias, "choices", None)
        if choices is not None and alias in choices:
            return name, field
    return None


def test_watcher_tunables_block_is_non_empty() -> None:
    """Precondition for the drift check below -- fails loudly if the block gets renamed/moved."""
    text = _read_env_example()
    block = _extract_watcher_tunables_block(text)
    defaults = _commented_defaults(block)
    assert defaults, "expected at least one commented `# PHAZE_*=value` line in the watcher tunables block"


def test_watcher_tunables_block_matches_config_defaults() -> None:
    """Every commented default in the watcher tunables block must equal config.py's real default.

    phaze-7li3t: this generalizes the specific PHAZE_SCAN_STALL_SECONDS fix (600 -> 86400) into
    a standing guard over the whole block, so a future PR that changes one of these Field
    defaults without updating .env.example fails the build instead of silently misleading the
    next operator who reads "defaults shown below".
    """
    text = _read_env_example()
    block = _extract_watcher_tunables_block(text)
    defaults = _commented_defaults(block)

    for alias, shown_value in defaults.items():
        found = _field_for_alias(alias)
        assert found is not None, f".env.example's watcher tunables block documents unknown env var {alias}"
        field_name, field = found
        actual_default = field.default
        expected_value = str(actual_default).lower() if isinstance(actual_default, bool) else str(actual_default)
        assert shown_value == expected_value, (
            f".env.example's watcher tunables block shows {alias}={shown_value!r} but "
            f"config.py's {field_name!r} field default is {expected_value!r} (phaze-7li3t) -- "
            f"the block header promises 'defaults shown below'; update the template."
        )
