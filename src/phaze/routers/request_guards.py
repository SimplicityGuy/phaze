"""THE untrusted-input contract for every request path in phaze (phaze-wkqk).

This module is the SINGLE owner of "how a handler survives a payload it did not write". Every
router that parses a raw client string, or looks up a row an earlier request told it about,
composes the helpers here rather than re-deriving a guard. Before adding a handler that touches
client-supplied text or a client-supplied id, read this docstring; it is the contract, not a
suggestion.

WHY THIS EXISTS
---------------
phaze-wkqk found ``/duplicates/{hash}/undo`` and ``/duplicates/undo-all`` calling
``json.loads(file_states)`` bare on a raw ``Form(...)`` string, feeding the result straight into
``undo_resolve``, whose docstring advertises a threat mitigation: "a malformed id is dropped (no
HTTP 500)". That claim was true only for the id VALUE inside a well-formed dict entry. It said
nothing about a payload that is not JSON at all (``JSONDecodeError`` -> 500), nor about valid JSON
of the WRONG SHAPE (``[1, 2]`` -> ``int.get`` ``AttributeError`` -> 500; a non-empty object ->
iterating keys -> ``str.get`` ``AttributeError`` -> 500). A stale browser tab or an agent replaying
a truncated payload reached a 500 through a handler that documented a graceful no-op.

The defect class is one sentence: **a raw parse or a row lookup on a request path is written as
though it cannot fail, so a malformed payload or a concurrently-deleted row escapes as an unhandled
500 -- often in a handler that DOCUMENTS a graceful contract it does not actually honour.**

THE CONTRACT
------------

1. NO BARE PARSE OF A CLIENT STRING. ENVELOPE FAILURES ARE ``422``.
   ``json.loads``, ``uuid.UUID``, ``int``, ``float``, ``datetime.fromtimestamp`` and friends all
   raise on hostile input. None of them may be called unguarded on a value that arrived from the
   wire (``Form``, ``Query``, ``Path``, a request body, an SSE resume token).

   When the failure is in the ENVELOPE -- the whole payload is unparseable or is not the declared
   container -- the request is rejected with **HTTP 422**, not 400 and not a silent success. 422 is
   the code FastAPI itself already returns for a request that is syntactically well-formed but
   semantically invalid, so a hand-written guard and a generated one are indistinguishable to the
   client. Do NOT introduce a parallel 400; a client that must branch on two codes for one meaning
   is a client that will branch wrong.

   :func:`parse_json_array_payload` is the standard shape for a JSON-in-a-form-field envelope. It
   also bounds ``len(parsed)`` -- a well-formed array that is simply enormous is still an envelope
   failure (a DoS bound per ``schemas/wire_bounds.py`` rule 7, not a width cap), because nothing
   downstream chunks its own expanding-``IN`` statements against the database driver's bind-
   parameter cap.

2. PARSING SUCCESSFULLY IS NOT RECEIVING THE EXPECTED STRUCTURE.  <-- the crux
   ``json.loads("[1,2]")`` succeeds. ``json.loads("{}")`` succeeds. ``json.loads("null")``
   succeeds. Every one of those then explodes at the first attribute access downstream, in a
   service that reasonably assumed its caller validated. A parse guard alone converts a
   ``JSONDecodeError`` 500 into a ``AttributeError`` 500; it does not fix the bug.

   So shape is asserted SEPARATELY from parse, and at the SAME boundary. Where the shape has named
   fields, declare a Pydantic model and validate against it -- Pydantic is already a hard
   dependency via FastAPI and gives a typed result plus a machine-readable error for free. Where
   the shape is a loose container of best-effort entries (the dedup undo payload), assert the
   container in the router and drop unusable ELEMENTS in the service.

   The ELEMENT rule is deliberately different from the ENVELOPE rule:

   * **Envelope malformed -> 422.** The operator's whole request is unintelligible; failing loudly
     is the only honest answer, and there is nothing partial to do.
   * **Element malformed -> skip it, keep going, return the count actually acted on.** These
     payloads are browser-held id-sets replayed from a tab that may be arbitrarily stale. One id
     that no longer parses must not void an otherwise valid bulk action, and the operator already
     reads the returned count as the authority on what happened.

   Pick per boundary using that rule and nothing else, so every handler in the repo answers
   "malformed id: 4xx or skip?" the same way.

3. ``scalar_one()`` IS THE BUG; ``scalar_one_or_none()`` PLUS A GUARD IS THE FIX.
   ``scalar_one()`` raises ``NoResultFound`` when a row a previous request named has since been
   deleted -- a routine race in this app, where scans and agents delete file rows underneath an
   open operator tab. On a request path that is an unhandled 500 for an ordinary, expected event.

   Use ``scalar_one_or_none()`` and branch on ``None``. What the ``None`` branch RETURNS is the
   handler's own decision -- a clean 200 "nothing to do" hold, a 404, or a skipped entry -- but it
   is always an explicit branch, never an exception escaping.

   The correct pattern is already in-repo: ``report_push_mismatch`` in ``routers/agent_push.py``
   does exactly this in its over-cap branch and wrongly uses ``scalar_one()`` in its under-cap
   branch (phaze-zdej). Copy the over-cap branch.

4. INTEGRITY ERRORS: CATCH THE RACE, NOT THE TYPO.
   An FK violation surfaces as ``IntegrityError`` at flush/commit. ``ON CONFLICT (id) DO NOTHING``
   does NOT absorb it -- that clause covers a UNIQUE collision on the conflict target, not a
   foreign key whose referent does not exist. Catch ``IntegrityError`` around the flush, roll back
   only the nested scope (rule 5), and convert to the same explicit branch rule 3 defines.

   **The boundary against phaze-btlu (constrain-at-the-wire), stated once so no handler is covered
   by both contracts:**

   * If the value COULD have been rejected before it ever reached the database -- out of a
     declared range, wrong type, wrong length, not a member of an enum, absent -- it belongs at the
     wire boundary and must ``422`` there. That is phaze-btlu's contract. Catching an
     ``IntegrityError`` for such a value is WRONG: it launders a validation bug into a race, hides
     it from the client's error body, and burns a database round trip to learn something the
     signature already knew.
   * If the value was VALID when it was checked and became invalid because another transaction
     committed in between -- a proposal deleted between render and POST, a file row removed by a
     concurrent scan -- no wire-level check could have caught it. That is a genuine race and
     ``IntegrityError`` is the right and only layer. That is THIS contract.

   The test: could a stricter signature have rejected it? Yes -> phaze-btlu, 422 at the boundary.
   No -> here, catch and branch.

   :func:`execute_guarding_vanished_file` is the standard shape for rules 4 and 5 together
   (phaze-bk9el.9): it runs the statement in a SAVEPOINT, catches the race, logs the hold through
   the CALLER's own logger, and returns ``None`` so the caller renders its own no-op 200. Use it
   rather than re-deriving the try/``begin_nested``/except shape at a new call site.

5. A FAILED STATEMENT POISONS THE TRANSACTION -- USE A SAVEPOINT, NOT A ROLLBACK.
   Postgres aborts the whole transaction on any failed statement; every subsequent statement on
   that session raises ``PendingRollbackError`` until it is unwound. So a handler that catches an
   error and keeps rendering MUST have run the risky statement inside ``session.begin_nested()``.
   Rolling back the nested SAVEPOINT alone discards the failed statement and leaves the outer
   request transaction usable for the rest of the response. This is the same mechanism the paging
   contract's rule 6 uses for degrade-safe render reads (``services/pagination.py``); stay
   consistent with it.

   Do NOT reach for a full ``session.rollback()`` as the recovery. It expires every already-loaded
   ORM object on the session, so the next attribute access on a row the handler loaded BEFORE the
   failure triggers a refresh against the aborted transaction and 500s -- on exactly the hiccup the
   rollback was added to survive. phaze-5tsj and phaze-yfj1 track that as a live bug; do not write
   more of it.

6. A DOCSTRING THAT PROMISES "NO HTTP 500" IS A TEST OBLIGATION, NOT PROSE.
   Any handler or service whose docstring claims a graceful contract -- "never 500s", "a malformed
   id is dropped", "degrades to an empty result" -- MUST ship a regression test that sends the
   malformed input and asserts the promised status. An asserted invariant with no test is an
   assumption, and this module exists because such an assumption was false in production code for
   as long as it took someone to send ``not-json``.

   Enumerate the shapes in the test, at minimum: unparseable input, valid-but-wrong-shape input
   (both the wrong container and the wrong element type), and the specific malformed value the
   docstring names. If a shape is not tested, the docstring may not claim it.

USING IT
--------
::

    from phaze.routers.request_guards import parse_json_array_payload

    @router.post("/undo-all")
    async def bulk_undo(file_states: str = Form(...)) -> HTMLResponse:
        parsed_states = parse_json_array_payload(file_states, field="file_states")
        restored = await undo_resolve(session, parsed_states)  # drops unusable ELEMENTS itself
"""

import json
from typing import Any, cast
import uuid

from fastapi import HTTPException
from sqlalchemy import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog


__all__ = [
    "MALFORMED_PAYLOAD_STATUS",
    "MAX_ARRAY_ITEMS",
    "execute_guarding_vanished_file",
    "parse_json_array_payload",
]


MALFORMED_PAYLOAD_STATUS = 422
"""The ONE status code for an envelope-level malformed payload (contract rule 1).

Spelled once, here, so a second handler cannot quietly answer 400 for the same meaning. Matches
what FastAPI's own request validation returns, so a hand-written guard and a generated one look
identical to the client.

Spelled as a literal rather than a ``starlette.status`` constant: Starlette deprecated
``HTTP_422_UNPROCESSABLE_ENTITY`` in favour of ``HTTP_422_UNPROCESSABLE_CONTENT``, and importing
either pins this contract to one side of a rename that does not change the wire code.
"""


MAX_ARRAY_ITEMS = 5000
"""The envelope-level DoS bound on ``parse_json_array_payload``'s decoded array (phaze-17ut).

This is a DoS bound (``schemas/wire_bounds.py`` rule 7), NOT a domain limit on how many items a
bulk action could legitimately need -- do not "correct" it to match some observed real-world
count. It exists because ``services/dedup.undo_resolve`` expands every entry into a 2-column
``tuple_(...).in_(...)`` DELETE (2 bind parameters per entry), and asyncpg refuses a statement past
32767 total bind parameters -- so an array anywhere near that size reaches the database driver and
raises there as an unhandled 500, one module past this guard. 5000 items is comfortably under that
ceiling (10000 binds) with headroom for other array-shaped payloads this helper may later guard,
while still rejecting the pathological case at the cheapest possible point: before the array is
even handed to a service.
"""


def parse_json_array_payload(raw: str, *, field: str, max_items: int = MAX_ARRAY_ITEMS) -> list[Any]:
    """Parse a client-supplied JSON array out of a raw form/query string, or raise ``422``.

    The standard envelope guard for contract rules 1 and 2. Guards ALL of the failure class:
    ``raw`` may not be valid JSON at all, valid JSON may not be the array the caller declared, and
    a well-formed array may simply be too large for anything downstream to safely consume. All
    three are envelope failures, so all three reject the whole request with
    :data:`MALFORMED_PAYLOAD_STATUS` rather than 500ing on the first attribute access -- or the
    first bind-parameter-cap violation -- downstream.

    This deliberately does NOT validate the ELEMENTS. Per rule 2 an unusable element is skipped by
    the consuming service, not escalated -- one stale id must not void an otherwise valid bulk
    action. Callers whose payload has named fields should declare a Pydantic model instead of using
    this helper.

    Args:
        raw: The untrusted string exactly as it arrived from the wire.
        field: The request field name, echoed into the error detail so a client can tell which of
            several form fields it got wrong.
        max_items: The DoS bound on ``len(parsed)`` (see :data:`MAX_ARRAY_ITEMS`). Override only
            with a call-site-specific DoS rationale, never to accommodate one observed payload.

    Returns:
        The decoded list. Elements are ``Any`` and are NOT guaranteed to be dicts.

    Raises:
        HTTPException: ``422`` if ``raw`` is not valid JSON, decodes to anything but an array,
            decodes to an array longer than ``max_items``, contains an integer literal wide enough
            to trip CPython's integer-string conversion limit, or is nested deep enough to blow
            the interpreter's recursion limit.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=MALFORMED_PAYLOAD_STATUS,
            detail=f"{field} is not valid JSON: {exc.msg}",
        ) from exc
    except RecursionError as exc:
        # CPython's json parser (C scanner and pure-Python fallback alike) raises RecursionError,
        # not JSONDecodeError, once nesting exceeds the interpreter's recursion limit -- e.g.
        # `"[" * 50000`. RecursionError is not a ValueError/JSONDecodeError subclass, so it is not
        # caught above; without this arm it escapes as a 500, defeating rule 1 (envelope failures
        # are 422, never a bare parse left to fail unguarded).
        raise HTTPException(
            status_code=MALFORMED_PAYLOAD_STATUS,
            detail=f"{field} is nested too deeply to parse",
        ) from exc
    except ValueError as exc:
        # phaze-06zm: CPython's integer-string conversion limit (sys.set_int_max_str_digits, a
        # denial-of-service hardening added in 3.11+) makes the json module's own int() conversion
        # raise a bare ValueError -- not a JSONDecodeError subclass -- for an integer literal with
        # enough digits (e.g. `"9" * 5000`). Neither except arm above catches it, so a single huge
        # integer literal anywhere in the payload previously escaped as an unhandled 500. This arm
        # is deliberately last and deliberately broad: JSONDecodeError IS a ValueError subclass, so
        # ordering it after the specific arm keeps that branch's more precise detail message.
        raise HTTPException(
            status_code=MALFORMED_PAYLOAD_STATUS,
            detail=f"{field} contains a number that is too large to parse",
        ) from exc

    if not isinstance(parsed, list):
        raise HTTPException(
            status_code=MALFORMED_PAYLOAD_STATUS,
            detail=f"{field} must be a JSON array, got {type(parsed).__name__}",
        )

    if len(parsed) > max_items:
        raise HTTPException(
            status_code=MALFORMED_PAYLOAD_STATUS,
            detail=f"{field} has too many items ({len(parsed)}); at most {max_items} are accepted",
        )

    return parsed


async def execute_guarding_vanished_file(
    session: AsyncSession,
    stmt: Any,
    *,
    logger: structlog.stdlib.BoundLogger,
    handler_name: str,
    file_id: uuid.UUID,
    agent_id: str,
) -> CursorResult[Any] | None:
    """Execute ``stmt`` inside a SAVEPOINT, holding a clean ``None`` on a vanished-FileRecord race.

    The standard shape for contract rules 4 (catch the race, not the typo) and 5 (a SAVEPOINT, not
    a full rollback). ``stmt`` targets a row whose FileRecord parent can be removed concurrently by
    ``services.scan_deletion.delete_scan_cascade`` while a multi-hour agent run (analysis, metadata
    extraction, ...) is still in flight -- a routine, expected race (phaze-wn1l / phaze-1lnzo), not a
    bug. Running it inside ``session.begin_nested()`` means a caught ``IntegrityError`` (the FK
    violation from the vanished parent) unwinds only the nested scope, leaving the caller's outer
    transaction usable for the rest of the response, per rule 5.

    Originally duplicated verbatim across ``routers/agent_analysis.py`` (``put_analysis``,
    ``post_analysis_progress``, ``report_analysis_failed``) and ``routers/agent_metadata.py``
    (``put_metadata``, ``report_metadata_failed``) -- five call sites, one shape, consolidated here
    per this module's own docstring ("composes the helpers here rather than re-deriving a guard").

    Args:
        session: The caller's session; ``stmt`` runs in a SAVEPOINT nested inside its current
            transaction.
        stmt: The INSERT/UPDATE/DELETE statement to execute.
        logger: The CALLING module's own bound logger (never this module's) -- passed through so
            the emitted warning still attributes to the router that actually raced, matching what
            each call site logged before this helper existed. No behaviour change: the log line's
            module attribution is unchanged from before extraction.
        handler_name: The calling function's own name, echoed into the warning so the hold is
            traceable to the specific endpoint that raced (mirrors each site's original message,
            e.g. ``"put_analysis file vanished mid-write; ..."``).
        file_id: The PATH file id the statement targeted (AUTH-01) -- logged, never re-derived.
        agent_id: The authenticated agent id -- logged alongside ``file_id``.

    Returns:
        The ``CursorResult`` on success (an INSERT .. ON CONFLICT statement returns one at runtime;
        the async stubs type it as the base ``Result``, hence the cast here rather than at every
        call site). ``None`` on the caught race -- rendering the no-op 200 response stays the
        caller's own decision (rule 3), not this helper's.
    """
    try:
        async with session.begin_nested():
            return cast("CursorResult[Any]", await session.execute(stmt))
    except IntegrityError:
        logger.warning(f"{handler_name} file vanished mid-write; holding with a no-op 200", file_id=str(file_id), agent_id=agent_id)
        return None
