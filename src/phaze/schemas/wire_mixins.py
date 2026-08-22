"""Small, purely-additive base models shared across wire schemas that would otherwise duplicate a
field + validator pair byte-for-byte (phaze-bk9el.14).

A mixin here is a Pydantic ``BaseModel`` in its own right, not a plain Python mixin -- Pydantic
needs the field to be declared on a model in the MRO to include it. Each concrete schema still
declares its own ``model_config`` and its own field set; a mixin adds ONLY the field(s) and
validator(s) named in its docstring, never ``extra=...`` or anything else that would change a
subclass's behavior by omission.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from phaze.services.pg_text import sanitize_pg_text


class SanitizedErrorMessageMixin(BaseModel):
    """Adds ``error_message: str | None`` with the shared sanitize-at-the-wire-boundary validator.

    Before this bead, ``ExecutionLogCreate``, ``ExecutionLogPatch`` (``schemas/agent_execution.py``)
    and ``ScanBatchPatch`` (``schemas/agent_scan_batches.py``) each carried a byte-identical copy of
    this field and validator -- the 26-line clone `get_health` flagged between the two files. All
    three sink into a ``Text`` column via ``pg_insert(...).values(...)`` / ``setattr`` + commit,
    where a NUL byte or lone Unicode surrogate aborts with ``CharacterNotInRepertoireError``
    (phaze-hvve5). Sanitizing (stripping), not rejecting, is deliberate here: unlike a filesystem
    path, free text losing an invalid byte does not change its meaning -- the same rationale as the
    sibling writers at ``agent_metadata.py``, ``agent_analysis.py`` and ``agent_tag_writes.py``,
    which sanitize their own `error_message`-shaped fields the same way but were not part of this
    clone (different field names / classes) and so are out of scope for this mixin.
    """

    error_message: str | None = None

    @field_validator("error_message", mode="after")
    @classmethod
    def _sanitize_error_message(cls, v: str | None) -> str | None:
        return sanitize_pg_text(v) if v is not None else v
