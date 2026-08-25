"""THE broker-payload contract: a SAQ task payload is JSON-native by CONSTRUCTION, not by convention (phaze-ot3os).

This module is the SINGLE owner of "how a task payload is put on the SAQ broker". Every model whose
instances cross ``queue.enqueue`` / ``queue.apply`` inherits :class:`WirePayload` rather than
re-deciding, per producer, to spell ``model_dump(mode="json")``. It is mechanically enforced by
``tests/shared/schemas/test_wire_payload_contract.py`` -- a new task payload model that does not
inherit it FAILS the suite -- and verified against real broker bytes by
``tests/integration/test_pg_payload_type_fidelity.py``.

WHY THIS EXISTS
---------------
``phaze-9nz1g`` inventoried the seam: 16 producer call sites reach the broker, and the only
non-JSON-native type in any payload is ``uuid.UUID``. SAQ's ``PostgresQueue`` serializes with
``json.dumps``, which refuses a ``UUID`` outright.

**Correction to that inventory, measured here (phaze-ot3os):** phaze-9nz1g and this bead both say
the ``UUID`` is a field on **all nine** task payload models. It is a field on **eight**.
``ReadCompanionFilesPayload`` declares none, and neither does its nested ``CompanionReadItem`` --
that lane's payload was already JSON-native, so its producer could always have omitted
``mode="json"`` harmlessly. The mechanism below is what makes the OTHER EIGHT behave that way.
Re-measure with ``model_fields`` before restating the count; an inherited number nobody re-checked
is how the wrong one got written down twice.
So the only thing standing between every background lane and a failed enqueue was 16 authors
independently remembering to convert: ``model_dump(mode="json")`` at 11 sites, ``str(...)`` at 4,
and one JSONB replay. ``AgentTaskRouter.enqueue_for_agent`` alone fans out to six of those lanes,
so a single dropped ``mode="json"`` there was a six-lane outage.

phaze-9nz1g made a violation DETECTABLE (``tests/_queue_fakes.py`` round-trips every fake enqueue
through the real serializer). This module makes it HARMLESS: with ``mode="json"`` forced on the
base, there is no longer anything for a producer to omit.

WHAT THIS IS NOT
----------------
**It makes omission HARMLESS, not IMPOSSIBLE, and the distinction is the honest one.** Precisely:

* It reaches only payloads that ARE models. The four loose-kwarg producer sites -- ``str(tl.id)``
  at ``routers/pipeline/tracklists.py:80``, ``[str(file_id)]`` at ``:200`` / ``:253``,
  ``str(file_id)`` at ``services/agent_s3_reports.py:304`` and ``tasks/reconcile_cloud_jobs.py:374``,
  and ``[str(f.id)]`` at ``routers/pipeline/proposals.py:39`` -- pass no model, so no type carries
  their discipline. Omitting ``str()`` there still fails, loudly, in the PRODUCER'S OWN
  ``await queue.enqueue(...)`` (see the next section), but neither at review nor at type-check.
* ``tasks/reenqueue.py:685`` replays ``**(row.payload or {})`` from a JSONB column. It is
  JSON-native by construction and is outside any type-carried mechanism.
* It does not address container-type DRIFT -- a ``tuple`` arrives a ``list``, an int-keyed ``dict``
  arrives string-keyed. Those SERIALIZE fine and are pinned separately by
  ``test_container_types_drift_across_the_real_broker``.
* Nothing here stops an author hand-building a kwargs dict, and ``mypy`` does not stop
  ``model_dump(mode="python")`` -- that is refused at RUNTIME, by :meth:`WirePayload.model_dump`.

WHERE THE FAILURE ACTUALLY WAS -- a correction worth keeping
------------------------------------------------------------
phaze-9nz1g and phaze-ot3os both described the un-converted payload as "a worker-side TypeError, in
a worker rather than in a request, which is where failures are least visible". **That is wrong, and
it was measured wrong.** ``saq.Queue.enqueue`` runs ``_before_enqueue(job)`` and then ``_enqueue(job)``
-> ``PostgresQueue.serialize`` -> ``json.dumps``, all SYNCHRONOUSLY inside the producer's own
``await queue.enqueue(...)`` call and BEFORE the ``saq_jobs`` INSERT (``saq/queue/base.py:314-357``,
``:219``). Measured against the live 5433 broker: a refused enqueue leaves **0 ``saq_jobs`` rows**,
and ``test_a_raw_uuid_kwarg_is_refused_by_the_real_broker`` shows it raising at the producer.

So the gap this module closes was never "the failure is invisible until a worker sees it" -- the
failure was always loud and producer-side. The gap is that it happened at RUNTIME IN PRODUCTION
rather than by construction. Keep the distinction: it is the difference between an honest
structural improvement and an overclaim.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class WirePayload(BaseModel):
    """Base for every model whose instances become SAQ task kwargs. ``model_dump`` is ALWAYS JSON-mode.

    Subclass this instead of ``BaseModel`` for a task payload, and the producer's
    ``**payload.model_dump()`` is correct whether or not its author remembered ``mode="json"``.
    Every subclass still declares its own field set; this base contributes ``extra="forbid"``
    (the Phase 25 D-16 convention every one of these models already declared individually) and
    the dump override, and nothing else.
    """

    model_config = ConfigDict(extra="forbid")

    def model_dump(self, *, mode: Literal["json", "python"] | str = "json", **kwargs: Any) -> dict[str, Any]:
        """Dump JSON-native values. ``mode`` defaults to ``"json"``; anything else is REFUSED.

        Two deliberate choices, both of which look like over-engineering until the failure they
        prevent is named:

        * **The default is flipped, not just honoured.** Pydantic's default is ``mode="python"``,
          which returns ``uuid.UUID`` objects that ``json.dumps`` -- and therefore the broker --
          refuses. Flipping it is the entire mechanism: it is what makes a producer that writes
          ``**payload.model_dump()`` correct by construction.
        * **A non-json ``mode`` RAISES rather than being silently ignored.** Silently coercing an
          explicit ``mode="python"`` would hand the caller output that contradicts what they asked
          for, which is a worse failure than refusing: they would be debugging a string that they
          have every reason to believe is a ``UUID``. If a caller genuinely needs Python objects,
          they already have the model itself -- read the attribute.

        The remaining pydantic dump options (``exclude_unset``, ``exclude_none``, ``by_alias``, ...)
        are forwarded untouched.

        **The ``**kwargs: Any`` passthrough is a deliberate trade, not laziness.** Replicating
        pydantic's real 14-parameter signature would let ``mypy`` catch a typo'd dump option, but it
        pins this module to a pydantic minor: ``exclude_computed_fields``, ``fallback`` and
        ``polymorphic_serialization`` are all recent additions, and each such addition turns a
        routine dependency bump into an override-signature error reported at a site that has nothing
        to do with the bump. The passthrough forwards whatever the installed pydantic accepts, and a
        typo'd option still fails -- at runtime, as pydantic's own ``TypeError``, at the call site
        that wrote it. Measured: **zero** call sites in ``src/phaze`` pass any dump option other than
        ``mode="json"`` to these models, so the ``mypy`` catch protects nothing today while the
        coupling would cost on every pydantic bump.

        Raises:
            ValueError: ``mode`` is anything other than ``"json"``.
        """
        if mode != "json":
            raise ValueError(
                f"{type(self).__name__}.model_dump(mode={mode!r}) is refused: a WirePayload is a SAQ broker payload and is "
                f"always JSON-mode, because json.dumps -- and therefore PostgresQueue.serialize -- cannot carry the UUID "
                f"fields these models declare. Read the attribute off the model if you need a Python object."
            )
        return super().model_dump(mode="json", **kwargs)


__all__ = ["WirePayload"]
