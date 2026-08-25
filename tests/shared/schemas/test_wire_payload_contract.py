"""THE review-boundary half of phaze-ot3os: a tenth task payload cannot silently opt out of ``WirePayload``.

``schemas/wire_payload.py`` carries the discipline in the TYPE -- ``model_dump()`` on a
:class:`~phaze.schemas.wire_payload.WirePayload` is always JSON-mode, so a producer writing
``**payload.model_dump()`` is correct whether or not its author remembered ``mode="json"``.

That is only structural for models that actually inherit it. This module is what makes inheriting it
non-optional: it derives the population from the SOURCE -- every ``<X>Payload.model_validate(kwargs)``
consumer in ``src/phaze/tasks/`` -- rather than from a list somebody has to remember to extend. A new
task payload model added tomorrow lands in that population automatically and FAILS here until it is
re-based, at ``just check``, before it can reach production.

The source scan is deliberate and mirrors ``test_every_model_validate_consumer_is_covered`` in
``tests/integration/test_pg_payload_type_fidelity.py``: a runtime check over imported classes could
only ever see the models already imported, which is precisely the set that is already correct.

WHAT THIS MODULE DOES NOT CLAIM
-------------------------------
It proves membership and the JSON-native dump property. It does NOT prove the payload survives a
real broker -- that is ``tests/integration/test_pg_payload_type_fidelity.py``, against live bytes off
a live ``PostgresQueue``, which is the ADR-0012 rule 3 obligation. Read the two together; separately
each is weaker than it looks.

The negative controls (:func:`test_the_membership_check_fires_on_a_plain_basemodel_payload` and
:func:`test_the_json_native_check_fires_on_a_plain_basemodel_payload`) exist because a guard that
passes without being able to fail is indistinguishable, on the page, from one that can -- the
phaze-9nz1g discipline.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import uuid

from pydantic import BaseModel, ConfigDict
import pytest

from phaze.schemas import agent_s3, agent_tasks
from phaze.schemas.wire_payload import WirePayload


# Every ``<Something>Payload.model_validate(kwargs)`` in ``src/phaze/tasks/`` -- the same regex, and
# therefore the same population, as ``test_every_model_validate_consumer_is_covered``.
_CONSUMER_RE = re.compile(r"(\w+Payload)\.model_validate\(kwargs\)")

_TASKS_DIR = Path(__file__).resolve().parents[3] / "src" / "phaze" / "tasks"

# The modules a task payload model may be declared in. Both are scanned for WirePayload subclasses
# by :func:`test_every_wire_payload_dumps_json_native`; a payload declared somewhere else would be
# caught by :func:`test_every_task_payload_consumer_is_a_wire_payload` (which resolves by name
# across both) failing to find it.
_SCHEMA_MODULES = (agent_tasks, agent_s3)


def _consumer_payload_names() -> set[str]:
    """Names of every payload model a task function reconstructs from its ``**kwargs``."""
    found: set[str] = set()
    for source in sorted(_TASKS_DIR.rglob("*.py")):
        found.update(_CONSUMER_RE.findall(source.read_text(encoding="utf-8")))
    return found


def _resolve(name: str) -> type[BaseModel] | None:
    for module in _SCHEMA_MODULES:
        candidate = getattr(module, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def test_the_consumer_population_is_not_empty() -> None:
    """Guard the guard: an empty population would make every assertion below vacuously true.

    The regex, the ``tasks/`` path and the ``model_validate(kwargs)`` spelling are all things a
    refactor can move. If any of them drifts, this fails FIRST and names the cause, instead of the
    membership test below silently passing over zero models.
    """
    names = _consumer_payload_names()
    assert len(names) >= 9, f"expected at least the nine known task payload consumers, found {sorted(names)}"


def test_every_task_payload_consumer_is_a_wire_payload() -> None:
    """A model reconstructed from broker ``**kwargs`` MUST inherit ``WirePayload``.

    This is the assertion that makes phaze-ot3os structural rather than a one-time edit: a tenth
    payload model cannot be added, wired to a producer and shipped while still inheriting plain
    ``BaseModel`` -- it fails here, at ``just check``, before it reaches production.
    """
    offenders: list[str] = []
    unresolved: list[str] = []
    for name in sorted(_consumer_payload_names()):
        model = _resolve(name)
        if model is None:
            unresolved.append(name)
        elif not issubclass(model, WirePayload):
            offenders.append(name)

    assert not unresolved, (
        f"task payload model(s) named in src/phaze/tasks/ but not found in {[m.__name__ for m in _SCHEMA_MODULES]}: "
        f"{unresolved}. Declare broker payloads in one of those modules, or extend _SCHEMA_MODULES here."
    )
    assert not offenders, (
        f"task payload model(s) still inheriting plain BaseModel: {offenders}. A model whose instances become SAQ "
        f"task kwargs must inherit phaze.schemas.wire_payload.WirePayload, so that a producer's "
        f'`**payload.model_dump()` is JSON-native whether or not its author wrote `mode="json"`. '
        f"See schemas/wire_payload.py (phaze-ot3os)."
    )


def test_every_wire_payload_dumps_json_native() -> None:
    """Every declared ``WirePayload`` subclass dumps something ``json.dumps`` accepts -- THE property.

    Membership (above) is necessary but not sufficient: inheriting the base does nothing if a field
    is of a type pydantic's JSON mode cannot render either. This walks the concrete instances the
    real-broker module already maintains, so the two stay on one population.

    Constructed via the shared fixtures in ``tests/integration/test_pg_payload_type_fidelity.py`` --
    imported, not duplicated, so a tenth payload added there is checked here for free and cannot
    drift between the two modules.
    """
    from tests.integration.test_pg_payload_type_fidelity import _representative_payloads

    declared = {
        obj
        for module in _SCHEMA_MODULES
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, WirePayload) and obj is not WirePayload
    }
    assert declared, "no WirePayload subclasses found — the scan is broken, not the code"

    instantiated = {type(payload) for _, payload in _representative_payloads()}

    for task_name, payload in _representative_payloads():
        dumped = payload.model_dump()  # THE OMISSION: no mode= at all.
        try:
            json.dumps(dumped)
        except TypeError as exc:  # pragma: no cover - the failure this module exists to prevent
            pytest.fail(f"{type(payload).__name__} (task {task_name!r}) does not dump JSON-native: {exc}")

    # Nested item models are exercised through their parents rather than standalone; name them so the
    # gap is explicit rather than something a reader has to infer from the count.
    nested_only = {m.__name__ for m in declared - instantiated}
    assert nested_only <= {"ExecuteBatchProposalItem", "CompanionReadItem"}, (
        f"WirePayload subclass(es) with no representative instance and not a known nested item: {sorted(nested_only)}. "
        f"Add one to _representative_payloads() in tests/integration/test_pg_payload_type_fidelity.py."
    )


def test_an_explicit_python_mode_is_refused() -> None:
    """``model_dump(mode="python")`` RAISES rather than being silently coerced to JSON mode.

    Refusing is the point: silently honouring the flipped default would hand a caller a ``str`` where
    they explicitly asked for a ``UUID``, which is a worse failure than a loud one. This is also the
    one part of the mechanism ``mypy`` cannot enforce, so it is pinned at runtime here.
    """
    payload = agent_tasks.ScanDirectoryPayload(scan_path="/archive", batch_id=uuid.uuid4(), agent_id="a")
    with pytest.raises(ValueError, match="is refused"):
        payload.model_dump(mode="python")


def test_the_other_dump_options_still_pass_through() -> None:
    """Forcing ``mode`` must not swallow the rest of pydantic's dump surface.

    ``exclude_none`` / ``exclude_unset`` are the ones a producer would plausibly reach for; if the
    override ever stops forwarding ``**kwargs`` they would silently become no-ops, which is exactly
    the kind of quiet degradation phaze-ot3os exists to prevent elsewhere.
    """
    payload = agent_tasks.WriteFileTagsPayload(
        log_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        agent_id="a",
        file_path="/archive/<track-01>.mp3",
        tags={"title": "T", "comment": None},
    )
    assert payload.model_dump(exclude={"tags"}).keys() == {"log_id", "file_id", "agent_id", "file_path"}
    assert payload.model_dump(exclude_unset=True).keys() == {"log_id", "file_id", "agent_id", "file_path", "tags"}


# --- negative controls: the checks above can FAIL, so their passing means something ----------------


class _PlainPayload(BaseModel):
    """A stand-in for the mistake this bead prevents: a broker payload that never inherited the base."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    agent_id: str


def test_the_membership_check_fires_on_a_plain_basemodel_payload() -> None:
    """``issubclass(..., WirePayload)`` — the membership predicate — is False for a plain ``BaseModel``.

    Without this, ``test_every_task_payload_consumer_is_a_wire_payload`` passing would be
    indistinguishable from a predicate that is True for everything.
    """
    assert not issubclass(_PlainPayload, WirePayload)


def test_the_json_native_check_fires_on_a_plain_basemodel_payload() -> None:
    """A plain ``BaseModel`` payload's ``model_dump()`` is NOT ``json.dumps``-able — the failure being prevented.

    This is the same defect the live broker exhibits in
    ``test_the_omitted_conversion_is_still_refused_for_a_plain_basemodel``; asserted here too so the
    hermetic suite carries it without needing a broker.
    """
    dumped = _PlainPayload(file_id=uuid.uuid4(), agent_id="a").model_dump()
    assert isinstance(dumped["file_id"], uuid.UUID)
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(dumped)
