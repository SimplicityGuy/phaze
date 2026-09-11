"""Make modeled SAQ task payloads JSON-native by construction.

Every model crossing ``queue.enqueue`` or ``queue.apply`` inherits :class:`WirePayload`, so UUIDs
cannot reach ``PostgresQueue``'s ``json.dumps`` serializer. The contract is enforced by
``tests/shared/schemas/test_wire_payload_contract.py`` and exercised against broker bytes by
``tests/integration/test_pg_payload_type_fidelity.py``.

The measured seam has 16 producers: 11 model dumps, 4 explicit string conversions, and 1 JSONB
replay. Eight of the nine task payload models carry UUIDs; ``ReadCompanionFilesPayload`` is already
JSON-native. This base protects modeled payloads only. Loose kwargs, JSONB replay, and container-type
drift remain separate responsibilities. Serialization failure is synchronous in the producer's
``await queue.enqueue(...)`` before insertion; a refused raw UUID leaves 0 ``saq_jobs`` rows.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class WirePayload(BaseModel):
    """Base for every model whose instances become SAQ task kwargs. ``model_dump`` is ALWAYS JSON-mode.

    Subclass this instead of ``BaseModel`` for a task payload, and the producer's
    ``**payload.model_dump()`` is correct whether or not its author remembered ``mode="json"``.
    Every subclass still declares its own field set; this base contributes ``extra="forbid"``
    (the D-16 convention every one of these models already declared individually) and
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
