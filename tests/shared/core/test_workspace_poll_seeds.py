"""Guards for the OOB seed-target contract (surfaced by Phase-87 live UAT).

The v7.0 shell fires ONE ``/pipeline/stats`` poll from chrome; each tick emits hidden
``hx-swap-oob="true"`` seeds (``dag-seed-<key>``) that re-push live counts into
``$store.pipeline``. htmx OOB swaps land ONLY on ids ALREADY present in the DOM, so:

1. every ``$store.pipeline`` key seeded in ``base.html`` MUST have a matching
   ``dag-seed-<key>`` placeholder in ``_workspace_poll_seeds.html`` — otherwise that seed
   no-ops on EVERY workspace and the bound badge sticks at its initial 0 (this is exactly how
   the Phase-87 ``metadataOrphan``/``analyzeOrphan``/``fingerprintOrphan`` badges were dead), and
2. every workspace mounted into ``#stage-workspace`` (including the Phase-87 ``/s/files``
   workspace) MUST host that seed placeholder block, or the poll's OOB swaps log
   ``htmx:oobErrorNoTarget`` every 5s and none of the chrome-driven counts update on that page.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from httpx import AsyncClient


_TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "phaze" / "templates"
_BASE_HTML = _TEMPLATES / "base.html"
_SEEDS_HTML = _TEMPLATES / "pipeline" / "partials" / "_workspace_poll_seeds.html"


def _store_keys() -> set[str]:
    """The keys initialised in base.html's ``Alpine.store('pipeline', { ... })`` object."""
    src = _BASE_HTML.read_text(encoding="utf-8")
    block = re.search(r"Alpine\.store\(\s*['\"]pipeline['\"]\s*,\s*\{(.*?)\}\s*\)", src, re.DOTALL)
    assert block, "could not locate Alpine.store('pipeline', {...}) in base.html"
    # Each key is `name: <number>` — strip JS line comments first so a commented key never counts.
    body = re.sub(r"//[^\n]*", "", block.group(1))
    return set(re.findall(r"(\w+)\s*:", body))


def _seed_target_keys() -> set[str]:
    """The ``dag-seed-<key>`` placeholder ids hosted in _workspace_poll_seeds.html."""
    src = _SEEDS_HTML.read_text(encoding="utf-8")
    return set(re.findall(r'id="dag-seed-([A-Za-z0-9]+)"', src))


def test_every_store_key_has_a_poll_seed_target() -> None:
    """Every $store.pipeline key must have a dag-seed-<key> OOB landing target.

    A store key without a seed target means the /pipeline/stats poll's OOB seed for it no-ops on
    every workspace and the bound badge sticks at 0 (the Phase-87 orphan-badge failure mode).
    """
    missing = _store_keys() - _seed_target_keys()
    assert not missing, (
        "these $store.pipeline keys (base.html) have NO dag-seed-<key> target in "
        f"_workspace_poll_seeds.html, so the /pipeline/stats poll can never seed them: {sorted(missing)}"
    )


def test_orphan_seed_targets_present() -> None:
    """Explicit lock on the three Phase-87 (UI-05) orphan seeds the live UAT found missing."""
    targets = _seed_target_keys()
    for key in ("metadataOrphan", "fingerprintOrphan", "analyzeOrphan"):
        assert key in targets, f"dag-seed-{key} placeholder missing from _workspace_poll_seeds.html"


@pytest.mark.asyncio
async def test_files_workspace_hosts_poll_seeds_but_filter_fragment_does_not(client: AsyncClient) -> None:
    """/s/files (workspace mount) hosts the seed placeholders; /pipeline/files (filter fragment) does not.

    The workspace render must carry the seeds (so the chrome poll's OOB swaps land); the inner
    filter/pagination fragment that swaps into #files-table-view must NOT re-emit them (a duplicate
    seed id would split the OOB target).
    """
    workspace = (await client.get("/s/files")).text
    fragment = (await client.get("/pipeline/files")).text

    assert 'id="dag-seed-fingerprintOrphan"' in workspace, "/s/files workspace must host the orphan seed target"
    assert 'id="dag-seed-metadataDone"' in workspace, "/s/files workspace must host the poll-seed block"
    assert 'id="dag-seed-fingerprintOrphan"' not in fragment, "the /pipeline/files filter fragment must NOT re-emit seed targets"
