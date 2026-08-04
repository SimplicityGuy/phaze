"""The LEDGER REPLAY-SAFETY INVARIANT: a ``scheduling_ledger`` payload must be replayable at
an arbitrary FUTURE time (phaze-71nz).

WHY THIS MODULE EXISTS. ``tasks/reenqueue.recover_orphaned_work`` -- the controller startup hook
AND the operator-facing "Recover" button (``POST /pipeline/recover``) -- replays each orphaned
ledger row's STORED payload verbatim through its keyed producer. That is correct ONLY when the
payload is TIME-INVARIANT. On 2026-07-31 a single Recover replayed 430 orphaned ``s3_upload``
rows whose payloads embedded presigned S3 PUT URLs captured at the ORIGINAL enqueue time; 428 ran
their retries out to terminal ``failed`` against the object store (122x HTTP 403, 257x HTTP 400)
and ZERO succeeded. The same run's 2,512 ``process_file`` rows replayed fine, because that payload
(``file_id`` / paths / caps / ``agent_id`` / ``models_path``) carries nothing that expires.

So the defect is not "replay is wrong"; it is "SOME payloads carry time-limited material and
nothing said so". This module is where that is said, in three enforced pieces:

1. :data:`LEDGER_REPLAY_TIME_INVARIANT` / :data:`LEDGER_REPLAY_REGENERATED` -- a TOTAL, DISJOINT
   classification of every keyed producer (``deterministic_key._KEY_BUILDERS``). Adding a keyed
   producer without classifying it FAILS the totality test, so the next producer to store expiring
   material cannot slip through by omission the way ``s3_upload`` did.
2. :func:`find_time_limited_paths` -- a CONTENT detector over an actual payload. It recognises the
   shape of expiring material (presigned-URL query params in either SigV2 or SigV4 form, plus
   token / signature / credential / expiry key names) rather than the name of any one producer.
3. The detector is applied at REPLAY time by ``reenqueue._replay_row`` as a hard refusal, so a
   producer that is classified time-invariant but writes expiring material anyway is caught by the
   substrate instead of burning a stage into ``failed``. Classification is the declaration; the
   detector is the proof.

WHY THE DETECTOR IS A DENY-LIST OF SHAPES AND NOT A SCHEMA CHECK. The keyed producers do not share
a payload base class (some pass a Pydantic ``model_dump``, some a bare dict), and the material that
expires is a *value* property, not a type property -- ``part_urls: list[str]`` is a perfectly
ordinary annotation whose contents happen to be credentials. A value-shape detector is the only
form that generalises to a producer nobody has written yet.

Postgres-free and dependency-free by construction: it lives under ``tasks/_shared`` because the
``before_enqueue`` chokepoint that writes the ledger (``deterministic_key.apply_deterministic_key``)
runs on the agent worker too, where importing ``phaze.models`` / ``sqlalchemy.ext.asyncio`` would
break the control-vs-agent import boundary (``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit


if TYPE_CHECKING:
    from collections.abc import Mapping


# --- The classification (TOTAL over _KEY_BUILDERS, asserted in tests) ---------------------

LEDGER_REPLAY_TIME_INVARIANT: frozenset[str] = frozenset(
    {
        "process_file",
        "extract_file_metadata",
        "match_tracklist_to_discogs",
        "generate_proposals",
        "push_file",
        "submit_cloud_job",
        "write_file_tags",
        # phaze-5fta.3: the payload is at most a ``batch_size`` page-size knob -- the sweep reads
        # the corpus at RUN time and names no file, no credential and no clock. A row replayed a
        # week later recomputes against the corpus as it stands then, which is the only thing a
        # full-refresh cache could ever mean.
        "learn_filename_conventions",
    }
)
"""Keyed producers whose stored payload is TIME-INVARIANT -- safe to replay verbatim, forever.

Every member's payload is durable identity (``file_id`` / ``tracklist_id`` / ``log_id``), paths, and
tuning knobs. None of it is minted against a clock, so an orphaned row replayed a week later means
exactly what it meant when it was written. ``push_file`` belongs here despite touching the cloud:
its payload names the file and the destination, and the rsync credential is resolved by the agent at
RUN time, not baked into the job.

Adding a keyed producer to ``_KEY_BUILDERS`` without adding it here or to
:data:`LEDGER_REPLAY_REGENERATED` fails ``test_ledger_replay_safety.py``'s totality assertion.
"""

LEDGER_REPLAY_REGENERATED: frozenset[str] = frozenset({"s3_upload"})
"""Keyed producers whose stored payload carries TIME-LIMITED material and must NEVER be replayed
verbatim -- recovery regenerates it from durable inputs instead.

``s3_upload`` payloads carry ``part_urls``: presigned multipart PUT URLs, each bounded by
``s3_presign_put_ttl_sec`` and signed at the original enqueue. Recovery re-derives them through the
SAME producer that mints them live (``services/cloud_staging.redrive_upload`` -> ``_stage_file_to_s3``),
keyed off durable inputs only (the ``file_id`` and the row's ``cloud_job.staging_bucket``).

A member here MUST have a registered regenerator in ``reenqueue._REPLAY_REGENERATORS`` -- asserted,
so a function can never be marked "do not replay verbatim" with no alternative path, which would
silently drop the stage from recovery entirely.
"""


# --- The content detector ---------------------------------------------------------------

_PRESIGN_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        # SigV4 (AWS, MinIO, and most S3-compatible stores; what boto3 mints by default).
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-expires",
        "x-amz-security-token",
        # SigV2 -- still emitted by several S3-compatible backends, and the exact form observed on
        # the 2026-07-31 incident's dead URLs (AWSAccessKeyId / Expires / Signature).
        "awsaccesskeyid",
        "signature",
        "expires",
        # Google Cloud Storage V2 signed URLs.
        "googleaccessid",
        # Azure Blob SAS: se = expiry, sig = signature, sp = permissions.
        "se",
        "sig",
        # Generic bearer material smuggled through a query string.
        "token",
        "access_token",
        "x-goog-signature",
    }
)
"""Query-parameter names that make a URL a CREDENTIAL rather than an address.

Matched case-insensitively and ONLY inside a parsed ``http``/``https`` query string, so an ordinary
field whose *name* happens to be ``expires`` in some unrelated payload is not caught here -- that
case is the key-name rule below, which is deliberately narrower.
"""

_TIME_LIMITED_KEY_MARKERS: tuple[str, ...] = (
    "presigned",
    "presign",
    "signed_url",
    "signature",
    "credential",
    "access_token",
    "auth_token",
    "bearer",
    "sas_token",
    "expires_at",
    "expires_in",
    "valid_until",
    "not_after",
)
"""Substrings in a payload KEY name that mark its value as time-limited.

Deliberately specific: bare ``token`` is excluded (too many innocent compounds), bare ``expires`` is
excluded in favour of ``expires_at`` / ``expires_in`` / ``valid_until`` / ``not_after``, which are the
forms an actual deadline takes. The URL rule above is what catches the credential-in-a-string case
that bare names would miss anyway.
"""

_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Depth ceiling for the recursive walk. Ledger payloads are shallow by construction (SAQ kwargs are
# JSON-serialisable and one or two levels deep at most); the bound exists so a pathological or
# self-referential structure can never turn a best-effort safety check into a stack overflow that
# aborts an enqueue or a recovery run.
_MAX_DEPTH = 8


def _string_is_time_limited(value: str) -> bool:
    """Return True when ``value`` is a URL whose query string carries presign/credential material."""
    if "?" not in value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme.lower() not in _URL_SCHEMES or not parts.query:
        return False
    return any(name.lower() in _PRESIGN_QUERY_PARAMS for name, _ in parse_qsl(parts.query, keep_blank_values=True))


def _key_is_time_limited(key: str) -> bool:
    """Return True when a payload key NAME declares its value time-limited."""
    lowered = key.lower()
    return any(marker in lowered for marker in _TIME_LIMITED_KEY_MARKERS)


def _walk(value: Any, path: str, depth: int, found: list[str]) -> None:
    """Recursively collect the dotted paths of time-limited material under ``value``."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, str):
        if _string_is_time_limited(value):
            found.append(path)
        return
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            if _key_is_time_limited(key):
                found.append(child_path)
                continue
            _walk(child, child_path, depth + 1, found)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]", depth + 1, found)


def find_time_limited_paths(payload: Mapping[str, Any] | None) -> list[str]:
    """Return the dotted payload paths carrying TIME-LIMITED material, in encounter order.

    An empty list means the payload satisfies the replay-safety invariant: nothing in it was minted
    against a clock, so replaying it at an arbitrary future time means what it meant when written.

    Two independent rules, either of which flags a path:

    - a string value that parses as an ``http(s)`` URL whose query string carries a presign /
      credential parameter (:data:`_PRESIGN_QUERY_PARAMS`) -- this is the ``s3_upload`` ``part_urls``
      shape, in both the SigV4 and the SigV2 form the incident actually produced;
    - a key whose NAME declares a token / signature / credential / explicit expiry
      (:data:`_TIME_LIMITED_KEY_MARKERS`), regardless of its value's type.

    Total on any JSON-shaped payload and never raises: an unparseable URL, an exotic scalar, or a
    structure deeper than :data:`_MAX_DEPTH` simply does not match. It is a detector, not a
    validator -- callers decide what a hit means (recovery refuses to replay; the tests fail the
    build).
    """
    if not payload:
        return []
    found: list[str] = []
    _walk(dict(payload), "", 0, found)
    return found
