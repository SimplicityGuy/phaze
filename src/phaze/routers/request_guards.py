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

   :func:`parse_json_array_payload` is the standard shape for a JSON-in-a-form-field envelope.

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
from typing import Any

from fastapi import HTTPException


__all__ = ["MALFORMED_PAYLOAD_STATUS", "parse_json_array_payload"]


MALFORMED_PAYLOAD_STATUS = 422
"""The ONE status code for an envelope-level malformed payload (contract rule 1).

Spelled once, here, so a second handler cannot quietly answer 400 for the same meaning. Matches
what FastAPI's own request validation returns, so a hand-written guard and a generated one look
identical to the client.

Spelled as a literal rather than a ``starlette.status`` constant: Starlette deprecated
``HTTP_422_UNPROCESSABLE_ENTITY`` in favour of ``HTTP_422_UNPROCESSABLE_CONTENT``, and importing
either pins this contract to one side of a rename that does not change the wire code.
"""


def parse_json_array_payload(raw: str, *, field: str) -> list[Any]:
    """Parse a client-supplied JSON array out of a raw form/query string, or raise ``422``.

    The standard envelope guard for contract rules 1 and 2. Guards BOTH halves of the failure
    class: ``raw`` may not be valid JSON at all, and valid JSON may not be the array the caller
    declared. Both are envelope failures, so both reject the whole request with
    :data:`MALFORMED_PAYLOAD_STATUS` rather than 500ing on the first attribute access downstream.

    This deliberately does NOT validate the ELEMENTS. Per rule 2 an unusable element is skipped by
    the consuming service, not escalated -- one stale id must not void an otherwise valid bulk
    action. Callers whose payload has named fields should declare a Pydantic model instead of using
    this helper.

    Args:
        raw: The untrusted string exactly as it arrived from the wire.
        field: The request field name, echoed into the error detail so a client can tell which of
            several form fields it got wrong.

    Returns:
        The decoded list. Elements are ``Any`` and are NOT guaranteed to be dicts.

    Raises:
        HTTPException: ``422`` if ``raw`` is not valid JSON, or decodes to anything but an array.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=MALFORMED_PAYLOAD_STATUS,
            detail=f"{field} is not valid JSON: {exc.msg}",
        ) from exc

    if not isinstance(parsed, list):
        raise HTTPException(
            status_code=MALFORMED_PAYLOAD_STATUS,
            detail=f"{field} must be a JSON array, got {type(parsed).__name__}",
        )

    return parsed
