"""THE sortable-column whitelist for the RenameProposal table (phaze-a6hm.10).

One table, one whitelist -- but that table is currently served by TWO surfaces, which is the only
reason this lives in its own module instead of beside a handler as ``column_sort`` rule 6 asks:

* ``/s/propose`` (``routers/shell.py``) -- the v7 propose workspace, whose headers are rendered by
  the shared ``_file_table.html`` and therefore spelled by ``SortState.url_for``;
* ``/proposals/`` (``routers/proposals.py``) -- the legacy list, whose template still hand-rolls its
  header URLs. phaze-a6hm.12 retires that family.

Rule 6's INTENT is "one contract object per table, constructed and validated at import time, never
per request", and both constants below satisfy it. What rule 6 guards against is a whitelist built
from request data or duplicated per call site; a module that both surfaces import is the opposite of
that. Putting the columns in ``shell.py`` and importing them from ``proposals.py`` (or vice versa)
would have coupled two routers to each other's import order for no benefit.

WHAT IS AND IS NOT SHARED
-------------------------
:data:`PROPOSAL_SORT_COLUMNS` -- the safety-critical part, the mapping from wire key to real column
object (``column_sort`` rule 2) -- is shared verbatim. The two contracts differ ONLY in ``endpoint``
and ``target``, because a header click must re-request the surface it was clicked on and swap that
surface's own container. Sharing the columns is what makes "no bespoke second implementation" true:
there is exactly one enumeration of which columns a proposal may be ordered by, and exactly one
place a direction becomes SQL (``SortState.order_by``).

The legacy contract exists so that surface's ``sort``/``order`` are RESOLVED against this same
whitelist even though its template still writes its own URLs. That split is deliberate: the
injection surface (rule 2) and the ORDER BY are closed now, for both surfaces; the cosmetic URL
duplication is left for .12 to delete along with the template.

``Model`` is absent from the whitelist on purpose. The propose workspace renders it from
``settings.llm_model`` -- one configured value for every row on the page -- so it is not a column,
and offering to sort by it would promise an ordering that cannot exist.
"""

from __future__ import annotations

from typing import Final

from phaze.models.file import FileRecord
from phaze.models.proposal import RenameProposal
from phaze.routers.column_sort import SortableColumn, SortContract


__all__ = ["LEGACY_PROPOSAL_SORT", "PROPOSAL_SORT_COLUMNS", "PROPOSE_SORT"]


PROPOSAL_SORT_COLUMNS: Final[tuple[SortableColumn, ...]] = (
    SortableColumn(key="original_filename", label="File", expression=FileRecord.original_filename),
    SortableColumn(key="proposed_filename", label="Proposed name", expression=RenameProposal.proposed_filename),
    SortableColumn(key="proposed_path", label="Proposed path", expression=RenameProposal.proposed_path),
    SortableColumn(key="confidence", label="Conf", expression=RenameProposal.confidence),
)
"""The columns an operator may order proposals by, bound to real column objects at import time.

The ``label`` of each entry MUST match the header string ``_propose_list.html`` passes in its
``columns`` list exactly -- that string is how ``_file_table.html`` recognises a header as sortable,
so a typo here silently degrades the header to plain text rather than raising. The ``key`` is the
public wire spelling that appears in bookmarks and history entries; the three inherited from the
pre-cutover implementation (``original_filename``, ``proposed_filename``, ``confidence``) keep their
names so existing URLs survive.
"""

PROPOSE_SORT: Final = SortContract(
    endpoint="/s/propose",
    target="#propose-workspace-list",
    columns=PROPOSAL_SORT_COLUMNS,
    default_key="confidence",
)
"""The v7 propose workspace's contract.

``target`` is the EXISTING ``#propose-workspace-list`` container (``PROPOSE_LIST_CONTAINER_ID`` in
``routers/shell.py``), deliberately NOT the legacy ``#proposal-list-container`` and deliberately not
a new id: this contract introduces no swap target and no out-of-band fragment of its own, so it
cannot contribute a duplicate id (the phaze-gzrd / op6f / 7j50 defect class). Aiming at the list
container rather than the workspace is also what stops a sort click from re-emitting the search
input mid-keystroke and destroying focus -- ``/s/propose`` already routes a container-targeted swap
to ``_propose_list.html``, so a header click lands in the narrow shape for free.

``default_key``/``default_order`` (``confidence`` ascending) restate the defaults ``ListViewState``
already carries, and MUST keep restating them. The router seeds ``resolve`` from ``view.sort`` /
``view.order``, and ``ListViewState`` is TOTAL -- it substitutes its own defaults for anything
absent or unparseable, so ``resolve`` never actually sees ``None`` on this path and these two values
would be dead code if they disagreed. They are declared anyway because ``resolve`` is also reachable
with a whitelisted-but-unknown key, and because a contract that cannot state its own default is one
that fails ``__post_init__``. Lowest-confidence-first is the pre-cutover landing order and is kept
deliberately: this is a review queue, so the proposals the model was LEAST sure of are the ones
worth an operator's attention first.
"""

LEGACY_PROPOSAL_SORT: Final = SortContract(
    endpoint="/proposals/",
    target="#proposal-list-container",
    columns=PROPOSAL_SORT_COLUMNS,
    default_key="confidence",
)
"""The legacy list's contract -- same whitelist, its own endpoint and container.

Kept ``asc`` by default to preserve that surface's existing behaviour exactly; it is being retired
by phaze-a6hm.12 and this is not the bead to change what it shows. Its template still spells header
URLs by hand, so only ``resolve`` and ``order_by`` flow through the shared contract here -- which is
the half that matters, since it is the half that touches a column.
"""
