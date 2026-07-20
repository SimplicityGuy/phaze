"""THE wire-bounds contract: every value crossing the HTTP boundary is bounded to fit where it lands (phaze-btlu).

This module is the SINGLE owner of "how an inbound value is constrained". Every request body field,
path segment, query parameter and form field that ends up in a database column composes the rules
here rather than re-deciding a bound per route. Before adding a new inbound field, read this
docstring; it is the contract, not a suggestion. It is mechanically enforced by
``tests/shared/schemas/test_wire_bounds_contract.py`` -- a new unclassified field FAILS the suite.

WHY THIS EXISTS
---------------
phaze-btlu found ``agent_tracklists`` accepting an unbounded ``timestamp`` into a ``varchar(20)``
and an unbounded ``external_id`` into the ``varchar(50)`` NOT NULL column that is the ON CONFLICT
idempotency key. Postgres answers an over-width value with ``StringDataRightTruncation``; there is
no DB exception handler anywhere in the app, so the aborted transaction escapes as an UNHANDLED
500 rather than a clean 422 -- and, on that route, only AFTER the ``SET NX EX 3600`` idempotency
key is taken, so the failed request poisons its own retry for an hour.

That is one instance of a class, not a one-off. Seven sibling defects share the exact shape: a
value crosses the boundary without a constraint matching the storage type it lands in, Postgres
raises (``StringDataRightTruncation`` / ``NumericValueOutOfRange`` / a cast failure), the
transaction aborts, and a 500 escapes. The convention that prevents it already existed and was
applied exactly (``agent_files.py`` ``max_length=10`` <-> ``String(10)``; ``agent_fingerprint.py``
``max_length=20`` <-> ``String(20)``) -- it was simply never written down, so it was possible to
deviate silently. This module writes it down and the paired test enforces it.

THE CONTRACT
------------

1. A STRING FIELD'S ``max_length`` EQUALS ITS MAPPED ``String(N)`` WIDTH.
   Not less (you would reject values the column accepts), not more (you would hand Postgres a
   value it must truncate-or-raise). Exactly N. This is the rule ``agent_files`` and
   ``agent_fingerprint`` already follow.

2. A ``Text`` COLUMN NEEDS NO ``max_length``.
   ``Text`` is unbounded in Postgres, so a cap would invent a limit the storage does not have and
   reject legitimate values. Do NOT add a cap "for safety" -- say in a comment that the column is
   ``Text`` and move on. (A ``Text`` field that needs a bound for DoS reasons rather than storage
   reasons is rule 7, and must say DoS in the comment so it is not mistaken for a width cap.)

3. AN INTEGER FIELD IS BOUNDED BY ITS DOMAIN WHEN IT HAS ONE, OTHERWISE BY ITS COLUMN.
   A Postgres ``Integer`` is int4: the storage bound is ``ge=-2147483648, le=2147483647``
   (:data:`INT32_MIN` / :data:`INT32_MAX`). Use it ONLY as the fallback. PREFER a real domain bound
   whenever the field has one -- a 0-100 match confidence takes ``ge=0, le=100``, not the int32
   cap. Both prevent the 500; the domain bound additionally rejects the nonsense value that would
   otherwise be stored and read back as if it meant something. The int32 bound is what you use when
   the only thing you actually know is the column type.

4. THE ANSWER IS A 422 AT THE BOUNDARY, NEVER A CAUGHT DB EXCEPTION DOWNSTREAM.  <-- the crux
   Do NOT "fix" a member of this class by wrapping the insert in ``try/except``. The constraint
   belongs in the ``Field`` / ``Path`` / ``Query`` / ``Form`` declaration, where FastAPI rejects the
   request before a transaction is opened, before an idempotency key is taken, and before a worker
   burns a retry. A downstream handler leaves every one of those costs in place: it still admits
   the oversized value, still does the work, and turns a poison-pill payload into an infinite retry
   loop that merely 500s more politely. Reject at the edge or you have not fixed the bug.

5. THE RULE APPLIES TO ALL FOUR ENTRY SHAPES, NOT JUST BODIES.
   Pydantic bodies are the obvious case, but a ``Path`` segment, a ``Query`` parameter and a
   ``Form`` field reach the same columns with the same consequences::

       field: str = Field(max_length=50)                    # body
       engine: str = Path(max_length=30)                     # path segment
       limit: int = Query(default=50, ge=1, le=100)          # query parameter
       confidence: int = Form(ge=0, le=100)                  # form field

   All four accept the same constraint arguments. There is no shape for which "it is only a path
   parameter" is a reason to skip the bound.

6. DECLARE THE TYPE THAT MATCHES THE COLUMN, NOT ONLY THE BOUND.
   A range is not the only way a value can fail to fit. Declaring a ``date``-valued filter as
   ``str`` and forwarding it unparsed against a ``Date``/``DateTime`` column makes every value
   render as ``$1::VARCHAR`` and 500 on the cast -- a bound would not have helped; the TYPE was
   wrong. Declare ``date`` / ``datetime`` / ``uuid.UUID`` / ``Literal[...]`` and let FastAPI parse
   and reject at the boundary. "Bounded" means the declared type and its constraints together
   describe exactly what the column accepts.

7. A BOUND THAT EXISTS FOR DoS REASONS IS NOT A WIDTH CAP, AND SAYS SO.
   ``tracks: list[...] = Field(max_length=2000)`` bounds work per request (T-26-07-DoS), not a
   column width. Keep the two motivations distinguishable in comments so a later reader does not
   "correct" a DoS bound to a column width that does not exist, or vice versa.

8. PAGING PARAMETERS DEFER TO THE PAGING CONTRACT, AND STILL DECLARE THEIR GUARD.
   ``src/phaze/services/pagination.py`` rule 5 CLAMPS out-of-range ``page``/``page_size`` in the
   service layer so a paged read degrades rather than 500s. That clamp is the belt; the route-layer
   ``ge=``/``le=`` is the braces. Declare BOTH -- do not drop a route guard because the service
   clamps, and do not remove the clamp because a route now guards. A raw ``limit``/``offset`` pair
   that does not go through :mod:`phaze.services.pagination` needs its own ``ge=`` guard, because
   a negative ``LIMIT`` reaches Postgres and raises.

USING IT
--------
Bound a string to its column width, and an integer to its domain::

    from phaze.schemas.wire_bounds import INT32_MAX

    status: str = Field(min_length=1, max_length=20)   # -> fingerprints.status String(20), rule 1
    notes: str | None = None                            # -> Text, unbounded, rule 2
    confidence: int = Form(ge=0, le=100)                # 0-100 score, domain bound beats int32, rule 3
    window_index: int = Field(ge=0, le=INT32_MAX)       # no domain bound known -> column bound, rule 3

Where a field maps to a column, register the schema in the test's ``SCHEMA_BINDINGS`` so the check
verifies the cap against the live SQLAlchemy column rather than a number copied by hand -- that is
what stops the cap and the column drifting apart later.
"""

# A Postgres ``Integer`` column is int4. These are the FALLBACK bounds for an integer wire field
# with no narrower real-world domain -- see contract rule 3, which prefers a domain bound.
INT32_MIN = -2147483648
INT32_MAX = 2147483647

# A Postgres ``SmallInteger`` column is int2, for the same fallback purpose.
INT16_MIN = -32768
INT16_MAX = 32767

# A Postgres ``BigInteger`` column is int8.
INT64_MIN = -9223372036854775808
INT64_MAX = 9223372036854775807
