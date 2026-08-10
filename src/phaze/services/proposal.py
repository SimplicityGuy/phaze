"""Proposal service — LLM calling, rate limiting, proposal storage, and context building."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any
import uuid

from litellm import acompletion
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.schemas.agent_tasks import CompanionReadItem, ReadCompanionFilesPayload
from phaze.services.date_convention import CONTEXT_KEY as DATE_CONVENTION_CONTEXT_KEY
from phaze.services.enqueue_router import lane_for_task
from phaze.services.pg_text import sanitize_pg_text
from phaze.services.text_repair import repair_mojibake


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.analysis import AnalysisResult
    from phaze.models.metadata import FileMetadata
    from phaze.services.agent_task_router import AgentTaskRouter


logger = structlog.get_logger(__name__)

# Module constant: max chars for companion file content sent to LLM
MAX_COMPANION_CHARS = 3000

# phaze-6bkk: the bounded-read helper and its margin constant moved to services/companion_read.py
# (stdlib-only, agent-importable) because the read itself now happens on the AGENT. Re-imported
# above under their original private names so any existing importer is unchanged.

# How long the controller waits for an agent to return companion text before giving up and
# proposing without it. Companion sidecars are a few KB; the budget is dominated by lane queueing,
# not by the read. Bounded on purpose: companion context is an ENRICHMENT, and generate_proposals
# must never wedge a whole proposal batch behind one unresponsive file server.
_COMPANION_READ_TIMEOUT_S = 30.0

# Matches ReadCompanionFilesPayload.companions' max_length wire bound, so a media file with an
# unusual number of sidecars is chunked rather than 422'd.
_COMPANION_CHUNK = 200

# Path to the prompts directory (sibling of services/)
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Regex to match ASCII art lines (10+ repeated non-alphanumeric chars)
_ASCII_ART_RE = re.compile(r"^[\s\-=_*#~|/\\]{10,}$")


# ---------------------------------------------------------------------------
# Pydantic response models for structured LLM output
# ---------------------------------------------------------------------------


class FileProposalResponse(BaseModel):
    """LLM response for a single file in a batch.

    Note: confidence is typed as plain ``float`` with NO Field(ge=, le=)
    constraints due to a known litellm bug with Anthropic models (GitHub
    issue #21016).  Post-parse clamping should be applied by the caller.
    """

    file_index: int
    proposed_filename: str
    proposed_path: str | None = None
    confidence: float
    artist: str | None = None
    event_name: str | None = None
    venue: str | None = None
    date: str | None = None
    source_type: str | None = None
    stage: str | None = None
    day_number: int | None = None
    b2b_partners: list[str] = []
    reasoning: str


class BatchProposalResponse(BaseModel):
    """LLM response for a batch of files."""

    proposals: list[FileProposalResponse]


# ---------------------------------------------------------------------------
# Prompt template loading
# ---------------------------------------------------------------------------


def load_prompt_template(name: str = "naming") -> str:
    """Load a prompt template from the ``prompts/`` directory.

    Args:
        name: Template name (without ``.md`` extension).  Defaults to ``"naming"``.

    Returns:
        The full text of the template.

    Raises:
        FileNotFoundError: If the template file does not exist.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        msg = f"Prompt template not found: {path}"
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Companion content cleaning
# ---------------------------------------------------------------------------


def _sanitize_json(value: Any) -> Any:
    """Recursively strip PostgreSQL-unstorable chars (NUL / lone surrogates) from a JSON-able value.

    phaze-qj9e: applied to the ``context_used`` dict before it is written to the JSONB column so a
    single NUL byte anywhere in the nested structure (companion text, LLM metadata fields, tags)
    cannot abort the batch's write. Scalars other than ``str`` pass through unchanged.
    """
    if isinstance(value, str):
        return sanitize_pg_text(value)
    if isinstance(value, dict):
        return {k: _sanitize_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(v) for v in value]
    return value


def clean_companion_content(text: str, max_chars: int = MAX_COMPANION_CHARS) -> str:
    """Clean and truncate companion file content for LLM context.

    Strips ASCII art lines (runs of 10+ non-alphanumeric characters) and
    truncates the result to *max_chars*, appending ``"[...truncated]"`` when
    the content is cut.

    Args:
        text: Raw companion file text.
        max_chars: Maximum character count before truncation.

    Returns:
        Cleaned and (possibly) truncated text.
    """
    lines = text.splitlines()
    cleaned = [line for line in lines if not _ASCII_ART_RE.match(line)]
    result = "\n".join(cleaned).strip()
    if len(result) > max_chars:
        result = result[:max_chars] + "\n[...truncated]"
    return result


# ---------------------------------------------------------------------------
# File context assembly
# ---------------------------------------------------------------------------


def build_file_context(
    file_record: FileRecord,
    analysis: AnalysisResult | None,
    companion_contents: list[dict[str, str]],
    metadata: FileMetadata | None = None,
) -> dict[str, object]:
    """Assemble a context dict for a single file to be sent to the LLM.

    Args:
        file_record: The ``FileRecord`` ORM instance.
        analysis: The associated ``AnalysisResult``, or ``None`` if not analyzed.
        companion_contents: List of dicts with ``"filename"`` and ``"content"`` keys
            for each companion file.
        metadata: The associated ``FileMetadata``, or ``None`` if not extracted.

    Returns:
        A dict ready to be serialized to JSON for the LLM prompt.  The
        ``"index"`` key defaults to ``0`` — callers should overwrite it with
        the actual batch index.
    """
    analysis_dict: dict[str, object] | None = None
    if analysis is not None:
        analysis_dict = {
            "bpm": analysis.bpm,
            "musical_key": analysis.musical_key,
            "mood": analysis.mood,
            "style": analysis.style,
            "features": analysis.features,
        }

    tags_dict: dict[str, object] | None = None
    if metadata is not None:
        tags_dict = {
            "artist": metadata.artist,
            "title": metadata.title,
            "album": metadata.album,
            "year": metadata.year,
            "genre": metadata.genre,
            "raw_tags": metadata.raw_tags,
        }

    return {
        "index": 0,
        # phaze-x4ux: repair mojibake before it reaches the LLM prompt. This is the ONE call
        # site that must never carry garbled text forward -- a rename proposal built on
        # "VÃƒÂ¤th" would write that corruption to disk permanently under a "cleanup" action.
        # `original_path` is left raw (it is not proposed as a display name and does not feed
        # search/matching), and `FileRecord.original_filename` itself is never rewritten --
        # this repairs only the copy handed to the LLM.
        "original_filename": repair_mojibake(file_record.original_filename),
        "original_path": file_record.original_path,
        "file_type": file_record.file_type,
        "analysis": analysis_dict,
        "tags": tags_dict,
        "companions": companion_contents,
    }


# ---------------------------------------------------------------------------
# Date-convention prompt guidance (phaze-5fta.4)
# ---------------------------------------------------------------------------

# The placeholder line in prompts/naming.md, INCLUDING its trailing newline. Substituting the empty
# string therefore removes the whole line, leaving the prompt byte-identical to the pre-phaze-5fta.4
# template -- which is what makes "with the flag off, proposals are byte-identical to today's
# behavior" true of the LLM INPUT too, not merely of the stored row. Documenting the key
# unconditionally would have changed every prompt the moment this bead landed, flag or no flag.
_DATE_CONVENTION_PLACEHOLDER = "{date_convention_guidance}\n"

# Rendered ONLY when at least one file in the batch actually carries resolved date provenance.
# Ends with a newline so the substituted line matches the shape of the list item it joins.
_DATE_CONVENTION_GUIDANCE = (
    "- `date_convention`: Present only when the filename carries an `NN-NN-YYYY` scene date that was resolved. "
    "`date` is that date as ISO `YYYY-MM-DD` and `raw` is the token it came from. `source` is `filename` when the "
    "token could only be read one way, or `release_group_convention` when the token was genuinely ambiguous and was "
    "resolved from the release group's learned date-order convention -- in which case `scope_value`, "
    "`convention_value`, `supporting_count` and `contradicting_count` are the evidence behind it. Prefer this `date` "
    "over your own reading of that token: it is either a fact about the string or an inference backed by counted "
    "evidence. It is absent for every other date shape; read those yourself as usual.\n"
)


def _date_convention_guidance(files_context: list[dict[str, Any]]) -> str:
    """The guidance line for this batch, or ``""`` when no file in it carries date provenance."""
    if any(DATE_CONVENTION_CONTEXT_KEY in context for context in files_context):
        return _DATE_CONVENTION_GUIDANCE
    return ""


# ---------------------------------------------------------------------------
# ProposalService — LLM calling and confidence clamping
# ---------------------------------------------------------------------------


class ProposalService:
    """Handles LLM-based filename proposal generation."""

    def __init__(self, model: str, prompt_template: str, max_rpm: int) -> None:
        self.model = model
        self.prompt_template = prompt_template
        self.max_rpm = max_rpm

    async def generate_batch(self, files_context: list[dict[str, Any]]) -> BatchProposalResponse:
        """Call the LLM to generate filename proposals for a batch of files.

        Args:
            files_context: List of per-file context dicts (output of build_file_context).

        Returns:
            Parsed ``BatchProposalResponse`` from the LLM.
        """
        # Placeholder BEFORE payload: substituting the file JSON first would let a filename that
        # happened to contain the placeholder text get rewritten by the second pass.
        prompt = self.prompt_template.replace(_DATE_CONVENTION_PLACEHOLDER, _date_convention_guidance(files_context))
        prompt = prompt.replace("{files_json}", json.dumps(files_context, indent=2))
        response = await acompletion(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format=BatchProposalResponse,
        )
        return BatchProposalResponse.model_validate_json(response.choices[0].message.content)

    @staticmethod
    def _clamp_confidence(value: float) -> float:
        """Clamp a confidence value to the 0.0-1.0 range."""
        return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Rate limiting via Redis counter
# ---------------------------------------------------------------------------


# Rolling-window length (seconds) for the LLM requests-per-minute counter.
_RATE_LIMIT_WINDOW_SEC = 60

# phaze-pkgb: atomic INCR + conditional-arm, evaluated server-side so no
# CancelledError (SAQ job timeout / worker shutdown) or dropped connection can land
# BETWEEN the increment and the EXPIRE. The previous two-command form armed the TTL
# only inside an `if count == 1` branch after a separate INCR; a single lost EXPIRE
# left the counter with NO expiry, and — because successful acquisitions never
# release their increment and the over-limit path only DECRs the current iteration —
# the count could never fall back to 1, so the TTL was never re-armed and proposal
# generation wedged permanently until a manual `DEL phaze:llm:rpm`.
#
# This script re-arms whenever the key has no expiry (TTL == -1), which covers BOTH
# the first increment (INCR creates the key without a TTL) AND any pre-existing
# TTL-less key left by the old code path or a prior lost EXPIRE — so the limiter
# self-heals and a lost arm can never permanently wedge it. TTL returns -2 for a
# missing key (never true right after INCR) and -1 for "exists, no expiry".
_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) == -1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def check_rate_limit(redis_pool: Any, max_rpm: int) -> None:
    """Block until an LLM request slot is available.

    Uses an ATOMIC Redis INCR + conditional-EXPIRE (a single ``EVAL`` script) over a
    60-second rolling window. When the counter exceeds *max_rpm*, backs off with a
    2-second sleep and retries.

    The counter's only reset mechanism is its TTL, so the INCR and the EXPIRE MUST be
    inseparable: the script arms the TTL server-side in the same atomic execution as
    the increment (and re-arms any key that somehow lacks an expiry), which is why a
    lost EXPIRE can no longer permanently wedge the limiter (phaze-pkgb).

    Args:
        redis_pool: An async Redis connection (e.g. ``ctx["redis"]``, the dedicated
            cache handle wired in the controller worker startup — never ``queue.redis``,
            which the Postgres broker does not expose).
        max_rpm: Maximum requests allowed per minute.
    """
    key = "phaze:llm:rpm"
    while True:
        count = int(await redis_pool.eval(_RATE_LIMIT_LUA, 1, key, _RATE_LIMIT_WINDOW_SEC))
        if count <= max_rpm:
            return
        # Over limit — undo the increment and wait. The DECR keeps the existing TTL
        # (DECR never clears an expiry), so the window still resets on schedule.
        await redis_pool.decr(key)
        await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Proposal storage
# ---------------------------------------------------------------------------
#
# Pipeline DB-write idempotency audit (Phase 35, D-04 / RESEARCH Q4):
#   * proposals      -> store_proposals (below) is now the partial-index upsert.
#   * execution_log  -> already idempotent: routers/agent_execution.py:77 issues
#                       `pg_insert(ExecutionLog).on_conflict_do_nothing(["id"])`
#                       (agent-supplied PK, D-13). No change needed.
#   * tag_write_log  -> INTENTIONALLY append-only (models/tag_write_log.py): it is
#                       an audit trail recording every write attempt with
#                       before/after snapshots. Adding an upsert would erase that
#                       history, so it MUST stay insert-only. No change needed.
# After this change the proposals table is the last non-idempotent task write to
# be fixed; pipeline DB-write idempotency is complete.


async def store_proposals(
    session: AsyncSession,
    file_ids: list[str],
    batch_response: BatchProposalResponse,
    files_context: list[dict[str, Any]],
) -> int:
    """Upsert LLM proposals as the one active (PENDING) RenameProposal per file (D-04).

    Idempotent: re-running ``generate_proposals`` for a file OVERWRITES its
    existing PENDING proposal in place rather than appending a second pending
    row. The conflict target is the partial unique index
    ``uq_proposals_file_id_pending`` (``ON (file_id) WHERE status = 'pending'``,
    alembic 019). Because that index only covers PENDING rows, an
    APPROVED / EXECUTED / REJECTED / FAILED proposal for the same file is never a
    conflict target and is structurally protected from being overwritten -- human
    approvals survive any number of re-runs.

    PK-STAMP GOTCHA: ``RenameProposal.id`` declares only a Python-side
    ``default=uuid.uuid4``, which fires through ORM ``session.add()`` but NOT
    through ``pg_insert(...).values()``. We therefore stamp ``id`` explicitly on
    every row so a fresh INSERT does not raise ``NotNullViolationError``. The
    ``ON CONFLICT DO UPDATE`` set_ deliberately omits ``id`` and ``file_id`` so an
    overwrite keeps the existing row's identity.

    Args:
        session: Active async database session.
        file_ids: Ordered list of file ID strings matching the batch.
        batch_response: Parsed LLM response containing proposals.
        files_context: The input contexts sent to the LLM (for context_used JSONB).

    Returns:
        Number of proposals upserted.
    """
    count = 0
    for proposal in batch_response.proposals:
        # WR-01: file_index is an unbounded int the LLM emits. An index >= len(file_ids) would
        # crash the whole batch with IndexError; a NEGATIVE index would silently wrap (Python
        # negative indexing) and write the proposal against the WRONG file -- silent data
        # corruption. Reject out-of-range indices and skip the proposal.
        idx = proposal.file_index
        if not (0 <= idx < len(file_ids)):
            logger.warning("proposal file_index out of range — skipping", file_index=idx, batch_size=len(file_ids))
            continue
        fid = file_ids[idx]
        confidence = ProposalService._clamp_confidence(proposal.confidence)
        context_used = {
            "artist": proposal.artist,
            "event_name": proposal.event_name,
            "venue": proposal.venue,
            "date": proposal.date,
            "source_type": proposal.source_type,
            "stage": proposal.stage,
            "day_number": proposal.day_number,
            "b2b_partners": proposal.b2b_partners,
            "input_context": files_context[idx],
        }
        # phaze-5fta.4: lift the date provenance to a TOP-LEVEL key. It is already inside
        # `input_context`, but the approval UI (phaze-5fta.6) must render "date inferred from
        # release-group convention -- N supporting, M contradicting" without reaching into the raw
        # LLM payload, and a top-level key is the stable contract for that. Added ONLY when the
        # gated fallback actually resolved a date: with `convention_date_fallback_enabled` off, or
        # on but with no group clearing the bars, the key is absent from the context and
        # `context_used` is byte-identical to what this function has always written.
        date_provenance = files_context[idx].get(DATE_CONVENTION_CONTEXT_KEY)
        if date_provenance is not None:
            context_used[DATE_CONVENTION_CONTEXT_KEY] = date_provenance
        # phaze-qj9e: deep-sanitize every string reaching the context_used JSONB column. A raw NUL
        # (U+0000) -- classically from a UTF-16LE .nfo companion, but also possible in any
        # LLM-supplied field (artist/event_name/...) -- is rejected outright by PostgreSQL jsonb and
        # aborts store_proposals for the WHOLE batch, poisoning every retry with identical content.
        # load_companion_contents sanitizes the companion text at the read boundary; this closes the
        # remaining LLM-string sinks in the same row.
        context_used = _sanitize_json(context_used)
        path_raw = proposal.proposed_path
        if path_raw:
            path_raw = path_raw.strip("/")
            while "//" in path_raw:
                path_raw = path_raw.replace("//", "/")

        row = {
            # Explicit PK stamp -- pg_insert bypasses the Python-side default.
            "id": uuid.uuid4(),
            "file_id": uuid.UUID(fid),
            # phaze-qj9e: proposed_filename/proposed_path/reasoning come from LLM JSON that may
            # legally carry U+0000 escapes; sanitize them too so no NUL reaches a text/jsonb column.
            "proposed_filename": sanitize_pg_text(proposal.proposed_filename),
            "proposed_path": sanitize_pg_text(path_raw) if path_raw else path_raw,
            "confidence": confidence,
            "status": ProposalStatus.PENDING,
            "context_used": context_used,
            "reason": sanitize_pg_text(proposal.reasoning),
        }
        stmt = pg_insert(RenameProposal).values(**row)
        stmt = stmt.on_conflict_do_update(
            # Match the partial unique index uq_proposals_file_id_pending: the
            # index_where makes the conflict fire ONLY against an existing PENDING
            # row, so approvals (outside the index) are never overwritten.
            index_elements=["file_id"],
            index_where=(RenameProposal.status == "pending"),
            set_={
                "proposed_filename": stmt.excluded.proposed_filename,
                "proposed_path": stmt.excluded.proposed_path,
                "confidence": stmt.excluded.confidence,
                "context_used": stmt.excluded.context_used,
                "reason": stmt.excluded.reason,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)

        count += 1
    return count


# ---------------------------------------------------------------------------
# Companion content loading
# ---------------------------------------------------------------------------


async def load_companion_targets(
    session: AsyncSession,
    media_file_id: uuid.UUID,
    task_router: AgentTaskRouter | None = None,
) -> dict[str, list[CompanionReadItem]]:
    """Look up a media file's companions and group them by OWNING agent (DB reads only).

    phaze-potg5: split out of ``load_companion_contents`` so the DB portion -- the two
    ``session.execute`` calls that resolve which agent owns which companion path -- can run
    inside a short read session/transaction, while the network round-trip that follows
    (``fetch_companion_contents``) runs with NO session checked out. Callers that need both
    (e.g. a standalone/unit caller) can use ``load_companion_contents`` below; callers that must
    not hold a session across network I/O (``generate_proposals``) call this, close the session,
    then call ``fetch_companion_contents`` separately.

    Args:
        session: Active async database session.
        media_file_id: UUID of the media file.
        task_router: The per-agent SAQ enqueuer (``ctx["task_router"]``). ``None`` means "no
            dispatcher available"; skips the DB query entirely and returns ``{}`` since the
            targets would never be used.

    Returns:
        Dict of ``agent_id -> [CompanionReadItem, ...]``. Empty when there is no dispatcher or no
        companions.
    """
    if task_router is None:
        # No dispatcher available (a unit-level caller, or a deployment without agents wired). The
        # proposal is still generated -- companion text is enrichment, never a precondition.
        logger.debug("companion_read_skipped_no_task_router", media_file_id=str(media_file_id))
        return {}

    result = await session.execute(select(FileCompanion).where(FileCompanion.media_id == media_file_id))
    companions = result.scalars().all()
    if not companions:
        return {}

    # Group by OWNING agent (phaze-c9w9 affinity): a companion path only means anything on the
    # mount it was reported from, so each owner reads its own files. In practice a media file and
    # its sidecars share one agent, so this is normally a single group.
    by_agent: dict[str, list[CompanionReadItem]] = {}
    for comp in companions:
        rec_result = await session.execute(select(FileRecord).where(FileRecord.id == comp.companion_id))
        rec = rec_result.scalar_one_or_none()
        if rec is None:
            continue
        by_agent.setdefault(rec.agent_id, []).append(CompanionReadItem(filename=rec.original_filename, path=rec.current_path))

    return by_agent


async def fetch_companion_contents(
    by_agent: dict[str, list[CompanionReadItem]],
    max_chars: int,
    task_router: AgentTaskRouter | None,
    media_file_id: uuid.UUID,
) -> list[dict[str, str]]:
    """Fetch and clean companion file contents, reading them ON THE OWNING AGENT.

    phaze-potg5: this is the NETWORK-I/O half of what ``load_companion_contents`` used to do in
    one call. It performs the ``read_companion_files`` request/response round trip (up to
    ``_COMPANION_READ_TIMEOUT_S`` per chunk) against each owning agent's ``meta`` lane and takes
    NO ``AsyncSession`` -- it must be safe to call with no DB session/transaction held open, per
    the codebase rule that no database connection is ever held across network I/O
    (phaze-1bcc/phaze-igwi). Callers collect ``by_agent`` from ``load_companion_targets`` inside a
    short read session, close that session, and only then await this.

    Args:
        by_agent: Companion targets grouped by owning agent id (``load_companion_targets``'s
            return value).
        max_chars: Maximum chars per companion file (passed to ``clean_companion_content``).
        task_router: The per-agent SAQ enqueuer (``ctx["task_router"]``). ``None`` or an empty
            ``by_agent`` short-circuits to ``[]`` -- companion text is ENRICHMENT, so its absence
            must degrade the proposal, never block it.
        media_file_id: UUID of the media file (for logging only).

    Returns:
        List of dicts with ``"filename"`` and ``"content"`` keys.
    """
    if task_router is None or not by_agent:
        return []

    contents: list[dict[str, str]] = []
    for agent_id, items in by_agent.items():
        # phaze-6bkk: request/response over the agent's meta lane. The containment check that used
        # to run here (phaze-eycl) moved WITH the read, onto the agent, and is re-run there against
        # that agent's own configured scan_roots -- the only place `Path.resolve()` can answer
        # honestly, since the controller has no such filesystem. Chunked to the payload's wire bound
        # so an unusually companion-heavy media file cannot 422 the whole read.
        for start in range(0, len(items), _COMPANION_CHUNK):
            chunk = items[start : start + _COMPANION_CHUNK]
            try:
                queue = task_router.queue_for(agent_id, lane_for_task("read_companion_files"))
                await queue.connect()
                payload = ReadCompanionFilesPayload(agent_id=agent_id, companions=chunk, max_chars=max_chars)
                job_result = await queue.apply("read_companion_files", timeout=_COMPANION_READ_TIMEOUT_S, **payload.model_dump(mode="json"))
            except Exception:
                # An offline agent, a saturated lane, or a timeout. Log and propose WITHOUT the
                # companion context rather than failing the batch -- the pre-phaze-6bkk behavior on
                # an unreadable companion, minus the silence (it used to be an unconditional
                # `except OSError: continue` that hid a permanent, topology-level failure).
                logger.warning("companion_read_unavailable", agent_id=agent_id, media_file_id=str(media_file_id), exc_info=True)
                continue

            for entry in (job_result or {}).get("contents", []):
                # phaze-qj9e: strip NUL/lone-surrogate bytes before the content flows into the
                # context_used JSONB column. A UTF-16LE .nfo/.txt companion decodes to text riddled
                # with U+0000, which PostgreSQL jsonb rejects outright -- aborting store_proposals for
                # the WHOLE batch and poisoning every retry with identical content. Both the cleaning
                # and the sanitizing stay control-side: they are pure functions, and this is where the
                # value is persisted.
                cleaned = sanitize_pg_text(clean_companion_content(entry["content"], max_chars))
                contents.append({"filename": sanitize_pg_text(entry["filename"]), "content": cleaned})

    return contents


async def load_companion_contents(
    session: AsyncSession,
    media_file_id: uuid.UUID,
    max_chars: int,
    task_router: AgentTaskRouter | None = None,
) -> list[dict[str, str]]:
    """Load and clean companion file contents for a media file, reading them ON THE OWNING AGENT.

    phaze-6bkk (DIST-01): this used to ``open()`` the companion path directly. ``generate_proposals``
    runs on the CONTROLLER worker, which docker-compose.yml documents as "fileless -- never touches
    SCAN_PATH", so that read could not succeed in any documented production topology. It failed
    invisibly: the ``except OSError: continue`` treated a permanent, architectural failure exactly
    like a single unreadable sidecar, and every proposal was silently generated with an empty
    companion context. The read now goes to the agent that owns the file, via a bounded
    request/response ``read_companion_files`` job on its ``meta`` lane.

    phaze-potg5: this is now a thin convenience wrapper over ``load_companion_targets`` (the DB
    read) followed immediately by ``fetch_companion_contents`` (the agent network round-trip). It
    remains correct for a standalone/unit caller with no other session to protect, but it is NOT
    safe to call from inside a session/transaction that must stay open only for local reads --
    ``generate_proposals`` calls the two halves separately instead, with the read session closed
    in between (see ``src/phaze/tasks/proposal.py``).

    Args:
        session: Active async database session.
        media_file_id: UUID of the media file.
        max_chars: Maximum chars per companion file (passed to ``clean_companion_content``).
        task_router: The per-agent SAQ enqueuer (``ctx["task_router"]``). ``None`` means "no
            dispatcher available" and yields an empty list -- companion text is ENRICHMENT, so its
            absence must degrade the proposal, never block it.

    Returns:
        List of dicts with ``"filename"`` and ``"content"`` keys.
    """
    by_agent = await load_companion_targets(session, media_file_id, task_router=task_router)
    return await fetch_companion_contents(by_agent, max_chars, task_router, media_file_id)
