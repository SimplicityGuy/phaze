# Phase 6: AI Proposal Generation - Research

**Researched:** 2026-03-28
**Domain:** LLM integration, structured output, batch processing, prompt engineering
**Confidence:** HIGH

## Summary

Phase 6 integrates an LLM (via litellm) to propose filenames for music files based on their metadata, analysis results, and companion file content. The system sends structured context to the LLM, receives structured JSON responses validated by Pydantic, and stores proposals as immutable records in the existing `RenameProposal` table. The work runs through the existing arq worker infrastructure as batch jobs.

The core technical challenge is threefold: (1) designing an effective prompt template that handles the wide variety of music file types in the collection (live sets, DJ mixes, album tracks, bootlegs), (2) managing token budgets and batch sizes for cost efficiency across 200K files, and (3) implementing robust rate limiting and error handling for LLM API calls. All foundational infrastructure (database models, worker pool, task patterns) already exists from prior phases.

**Primary recommendation:** Use litellm 1.82.6 with `acompletion()` for async LLM calls, Pydantic `response_format` for structured output, a markdown prompt template loaded from disk, fixed-size batches of 10-15 files per LLM call, and Redis-based rate limiting via a simple counter with TTL.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- D-01: LLM decides best naming format per file based on available metadata (adaptive, not rigid template)
- D-02: Live sets/performances: `{Artist} - Live @ {Venue|Event} {day/stage if available} {YYYY.MM.DD}.{ext}`
- D-03: Album tracks: `{Artist} - {Track #} - {Track Title}.{ext}` inside `{Album Name}` directory
- D-04: Date format always `YYYY.MM.DD` with `x` for unknown parts
- D-05: Always preserve original file extension
- D-06: Ignore scene-era 255 char dirname+filename limit
- D-07: Low-confidence proposals for files with very little metadata, flagged for manual review
- D-08: Send all available context per file (filename, path, analysis, companions)
- D-09: Structured JSON via Pydantic: proposed_filename, confidence (0-1), extracted metadata, reasoning
- D-10: No few-shot examples in prompt. Naming rules only.
- D-11: Prompt template stored as markdown file on disk, loaded at runtime
- D-12: LLM extracts structured metadata alongside proposals (event details, artist info, source type, venue)
- D-13: All extracted metadata stored in existing `RenameProposal.context_used` JSONB column
- D-14: Fixed-size batches (files grouped in chunks regardless of directory)
- D-15: Claude's discretion on batch size
- D-16: Batch processing through arq worker pool
- D-17: litellm pinned `>=1.82.6,<1.82.7` for unified LLM access
- D-18: Model name and API keys via environment variables
- D-19: Configurable rate limiting on LLM calls (max RPM via env var)
- D-20: Filename proposals only in v1 (no directory path proposals)

### Claude's Discretion
- Optimal batch size based on token budget analysis
- Rate limiting implementation (Redis-based counter, in-memory, or arq job scheduling)
- How to read companion file content (full text vs summary/truncation for large NFO files)
- Pydantic response model field names and structure
- Where to store the prompt markdown file
- Whether to add litellm to pyproject.toml dependencies or treat as system dependency

### Deferred Ideas (OUT OF SCOPE)
- AIP-03 (Directory path proposals) -- deferred to v2
- Few-shot prompt tuning -- only if rules-only prompting fails at scale
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| AIP-01 | System uses LLM to propose a new filename for each file based on available metadata, analysis results, and companion file content | litellm acompletion() with response_format for structured output; prompt template with naming rules; query FileRecord + AnalysisResult + FileCompanion for context |
| AIP-02 | Proposals are stored as immutable records in PostgreSQL (not regenerated on the fly) | Existing RenameProposal model with proposed_filename, confidence, context_used JSONB, reason; upsert pattern from process_file; FileState.PROPOSAL_GENERATED transition |
</phase_requirements>

## Project Constraints (from CLAUDE.md)

- **Python 3.13 exclusively**, `uv` only for all commands
- **Pre-commit hooks** must pass before commits
- **85% minimum code coverage**
- **Mypy strict mode** (excluding tests)
- **Ruff** line length 150, double quotes, specific rule sets
- **litellm pinned** `>=1.82.6,<1.82.7` -- supply chain attack on 1.82.7/1.82.8 (March 2026)
- **Every feature gets its own PR** -- one PR per feature
- **arq** for async task queue (not Celery)
- **pydantic-settings** for configuration with env var overrides

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| litellm | 1.82.6 (pinned `>=1.82.6,<1.82.7`) | Unified LLM API client | Single interface to Claude and OpenAI. Async via `acompletion()`. Structured output via `response_format`. Last verified safe release (supply chain attack on 1.82.7+). |
| pydantic | >=2.10 (already installed via FastAPI) | Structured LLM output validation | Define response schema as Pydantic model, pass to `response_format`. Validates LLM responses automatically. |
| arq | >=0.27.0 (already installed) | Batch job orchestration | Existing worker infrastructure from Phase 4. One arq job per batch of files. |
| redis | via arq (already installed) | Rate limiting + job queue broker | Simple rate limiting via INCR + EXPIRE on a per-minute key. Already in the stack for arq. |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pydantic-settings | >=2.13.1 (already installed) | LLM config (model name, API keys, rate limits) | Extend existing Settings class with LLM-specific fields |

### New Dependency

Only **litellm** needs to be added to `pyproject.toml`:

```toml
"litellm>=1.82.6,<1.82.7",
```

All other dependencies (pydantic, arq, asyncpg, SQLAlchemy) are already installed.

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| litellm | Direct anthropic/openai SDKs | Less abstraction but loses provider-switching flexibility. litellm is the user's chosen approach. |
| Pydantic response_format | instructor library | instructor adds a dependency and wrapper layer. litellm's native `response_format` with Pydantic models is sufficient and simpler. |
| Redis rate limiting | In-memory asyncio.Semaphore | In-memory resets on worker restart, doesn't survive across multiple worker processes. Redis counter persists and is shared. |

## Architecture Patterns

### Recommended Project Structure
```
src/phaze/
  prompts/
    naming.md              # Prompt template (D-11)
  services/
    proposal.py            # ProposalService: LLM interaction, batch processing
  tasks/
    functions.py           # Add generate_proposals() arq job
  config.py                # Extend Settings with LLM config fields
  models/
    proposal.py            # Already exists (RenameProposal)
tests/
  test_services/
    test_proposal.py       # Unit tests for ProposalService
  test_tasks/
    test_generate.py       # Unit tests for generate_proposals job
```

### Pattern 1: Proposal Service (Service Layer)
**What:** A `ProposalService` class that encapsulates all LLM interaction logic: building prompts, calling litellm, parsing responses, storing proposals.
**When to use:** Always -- separates LLM logic from arq job boilerplate.
**Example:**
```python
# src/phaze/services/proposal.py
from pydantic import BaseModel, Field
from litellm import acompletion
from pathlib import Path

class FileProposal(BaseModel):
    """Structured response for a single file proposal."""
    proposed_filename: str
    confidence: float = Field(ge=0.0, le=1.0)
    artist: str | None = None
    event_name: str | None = None
    venue: str | None = None
    date: str | None = None
    source_type: str | None = None
    stage: str | None = None
    day_number: int | None = None
    reasoning: str

class BatchProposalResponse(BaseModel):
    """Structured response for a batch of file proposals."""
    proposals: list[FileProposal]

class ProposalService:
    def __init__(self, model: str, prompt_template: str, max_rpm: int) -> None:
        self.model = model
        self.prompt_template = prompt_template
        self.max_rpm = max_rpm

    async def generate_batch(
        self, files: list[dict],
    ) -> BatchProposalResponse:
        """Send a batch of files to the LLM and return proposals."""
        prompt = self._build_prompt(files)
        response = await acompletion(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format=BatchProposalResponse,
        )
        return BatchProposalResponse.model_validate_json(
            response.choices[0].message.content
        )
```

### Pattern 2: Arq Batch Job
**What:** An arq job function that processes a batch of file IDs: loads context from DB, calls ProposalService, stores results.
**When to use:** For every batch of files queued for proposal generation.
**Example:**
```python
# In tasks/functions.py or tasks/proposal.py
async def generate_proposals(
    ctx: dict[str, Any], file_ids: list[str], batch_index: int
) -> dict[str, Any]:
    """Generate AI proposals for a batch of files."""
    session = await _get_session()
    try:
        # 1. Load file records + analysis + companions
        files_context = await _build_files_context(session, file_ids)
        # 2. Rate limit check
        await _check_rate_limit(ctx)
        # 3. Call LLM via ProposalService
        result = await ctx["proposal_service"].generate_batch(files_context)
        # 4. Store proposals in DB
        for file_id, proposal in zip(file_ids, result.proposals):
            await _store_proposal(session, file_id, proposal, files_context)
        await session.commit()
        return {"batch": batch_index, "count": len(file_ids), "status": "ok"}
    except Exception as exc:
        raise Retry(defer=ctx["job_try"] * 10) from exc
    finally:
        await session.close()
```

### Pattern 3: Redis Rate Limiting
**What:** Simple Redis INCR + EXPIRE to enforce max requests per minute.
**When to use:** Before every LLM API call.
**Example:**
```python
async def _check_rate_limit(redis_pool, max_rpm: int) -> None:
    """Block until rate limit window allows a request."""
    import asyncio
    key = "phaze:llm:rpm"
    while True:
        count = await redis_pool.incr(key)
        if count == 1:
            await redis_pool.expire(key, 60)
        if count <= max_rpm:
            return
        # Exceeded limit -- wait and retry
        await asyncio.sleep(2)
        await redis_pool.decr(key)
```

### Pattern 4: Prompt Template Loading
**What:** Load prompt from a markdown file at service initialization.
**When to use:** At worker startup or ProposalService construction.
**Example:**
```python
from pathlib import Path

PROMPT_DIR = Path(__file__).parent.parent / "prompts"

def load_prompt_template(name: str = "naming") -> str:
    """Load a prompt template from the prompts/ directory."""
    path = PROMPT_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")
```

### Anti-Patterns to Avoid
- **Hardcoding prompts in Python code:** Prompt iteration is frequent. Keep in markdown files per D-11.
- **One LLM call per file:** Extremely wasteful at 200K files. Always batch.
- **Regenerating proposals on the fly:** Violates AIP-02 (immutability). Store once, read from DB.
- **Using `completion()` (sync):** Blocks the event loop. Always use `acompletion()`.
- **Numeric constraints on Pydantic fields for Anthropic:** litellm has a known bug where `ge`/`le` constraints on Pydantic fields fail with Anthropic models. Use `float` without constraints and validate after parsing.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| LLM provider abstraction | Custom HTTP client per provider | litellm `acompletion()` | Handles auth, retries, provider routing, model name mapping |
| Structured output parsing | Manual JSON parsing + regex | Pydantic `response_format` via litellm | Automatic schema enforcement, validation, type safety |
| Rate limiting | Custom sliding window implementation | Redis INCR + EXPIRE | 2 lines of Redis commands, atomic, shared across workers |
| Retry with backoff | Custom retry loops | arq `Retry(defer=...)` | Already built into the worker infrastructure from Phase 4 |
| Configuration management | Custom env var parsing | pydantic-settings `Settings` | Already in use, type-safe, supports SecretStr for API keys |

## Common Pitfalls

### Pitfall 1: Token Budget Overflow
**What goes wrong:** Batch too many files per LLM call, exceeding context window. The LLM truncates or fails.
**Why it happens:** Each file's context (filename + path + analysis + companion content) can be 200-2000 tokens. Companion NFO files can be very large.
**How to avoid:** Estimate tokens per file conservatively. Use 10-15 files per batch. Truncate companion file content to 2000 chars max. The prompt template itself is ~1000-2000 tokens. Budget: ~4000 tokens per file * 15 files = 60K input tokens. Well within Claude Sonnet's 200K window and GPT-4o's 128K.
**Warning signs:** LLM responses truncated, missing proposals for some files in batch, API errors about context length.

### Pitfall 2: Pydantic Numeric Constraints with Anthropic
**What goes wrong:** Using `Field(ge=0.0, le=1.0)` on the confidence field causes litellm to send unsupported JSON schema properties (`minimum`, `maximum`) to Anthropic's API.
**Why it happens:** Known litellm bug (GitHub issue #21016). litellm doesn't filter out numeric constraints from the JSON schema when targeting Anthropic.
**How to avoid:** Define `confidence: float` without constraints in the Pydantic response model. Add a post-validation step: `confidence = max(0.0, min(1.0, raw_confidence))`.
**Warning signs:** API errors mentioning "unsupported schema property" or "minimum" when using Anthropic models.

### Pitfall 3: Companion File Content Explosion
**What goes wrong:** NFO files from scene releases can contain large ASCII art, tracklists, and descriptions. Reading them in full blows up token counts.
**Why it happens:** Scene NFO files vary wildly in size (500 bytes to 50KB+).
**How to avoid:** Truncate companion file content to a max of 2000-3000 characters. Strip ASCII art (lines of repeated characters). Extract only the informational text.
**Warning signs:** Token usage per batch varies wildly, some batches cost 10x more than others.

### Pitfall 4: LLM Returns Mismatched Batch Size
**What goes wrong:** You send 15 files, LLM returns 14 proposals. Or returns them in a different order.
**Why it happens:** LLMs can skip items or reorder. Structured output helps but doesn't guarantee count.
**How to avoid:** Include a unique index or file_id in each file's context. Require the LLM to echo it back. Validate response count matches input count. If mismatched, fall back to one-by-one processing for the remaining files.
**Warning signs:** Fewer proposals stored than files in batch, proposals assigned to wrong files.

### Pitfall 5: Rate Limit Race Conditions
**What goes wrong:** Multiple arq workers exceed rate limit simultaneously because Redis check-and-increment is not atomic enough.
**Why it happens:** INCR is atomic but the check-sleep-retry loop has a window.
**How to avoid:** Use `arq`'s built-in job scheduling to space out batch jobs. Set `worker_max_jobs` to limit concurrent LLM-calling jobs. Alternatively, use a Lua script for atomic check-and-block.
**Warning signs:** 429 errors from the LLM provider, unexpected cost spikes.

### Pitfall 6: litellm Supply Chain Risk
**What goes wrong:** Installing a compromised litellm version.
**Why it happens:** Supply chain attack on versions 1.82.7 and 1.82.8 (March 2026).
**How to avoid:** Pin `litellm>=1.82.6,<1.82.7` in pyproject.toml. Verify SHA checksums after install. Monitor the litellm GitHub for a verified safe post-incident release.
**Warning signs:** Unexpected network connections, credential exfiltration.

## Batch Size Analysis (Claude's Discretion: D-15)

### Token Budget Estimation

Per-file context (estimated tokens):
- Original filename + path: ~30-80 tokens
- Analysis results (BPM, key, mood, style, features): ~50-100 tokens
- Companion file content (truncated to 2000 chars): ~500-700 tokens
- **Per-file total: ~600-900 tokens**

Prompt overhead:
- System/naming rules: ~1500-2000 tokens
- Batch instructions + output schema: ~500 tokens
- **Fixed overhead: ~2000-2500 tokens**

Output per file:
- Proposed filename + metadata + reasoning: ~150-300 tokens
- **Per-file output: ~200-300 tokens**

### Recommended Batch Size: 10 files

| Batch Size | Input Tokens | Output Tokens | Total | Fits in 128K? | Fits in 200K? |
|------------|-------------|---------------|-------|---------------|---------------|
| 5 | ~7K | ~1.5K | ~8.5K | Yes | Yes |
| 10 | ~11.5K | ~3K | ~14.5K | Yes | Yes |
| 15 | ~16K | ~4.5K | ~20.5K | Yes | Yes |
| 25 | ~25K | ~7.5K | ~32.5K | Yes | Yes |
| 50 | ~47K | ~15K | ~62K | Yes | Yes |

**Recommendation: Start with 10 files per batch.** Conservative enough to handle outlier files with large companion content, while still achieving good amortization of the prompt overhead. At 200K files / 10 per batch = 20,000 LLM calls. At ~14.5K tokens per call:

**Cost estimate (Claude Sonnet 4):**
- Input: 20K calls * 11.5K tokens = 230M tokens = $690
- Output: 20K calls * 3K tokens = 60M tokens = $900
- **Total: ~$1,590** (standard) or **~$795** (batch API)

**Cost estimate (GPT-4o mini):**
- Input: 230M tokens = $34.50
- Output: 60M tokens = $36
- **Total: ~$70.50** (standard) or **~$35** (batch API)

Make batch size configurable via env var so the user can tune based on experience.

## Rate Limiting Recommendation (Claude's Discretion)

**Recommendation: Redis-based counter with configurable RPM.**

Rationale:
- Simple (INCR + EXPIRE, ~10 lines of code)
- Shared across multiple worker processes
- Survives worker restarts (Redis persistence)
- Configurable via `LLM_MAX_RPM` env var (default: 30)

Alternative considered -- arq job scheduling (spacing jobs with `defer`): More complex, harder to tune dynamically, doesn't account for job execution time variability.

Alternative considered -- in-memory semaphore: Doesn't share across workers, resets on restart.

## Companion File Handling Recommendation (Claude's Discretion)

**Recommendation: Read full text, truncate to 3000 characters, strip ASCII art.**

Implementation:
```python
import re

MAX_COMPANION_CHARS = 3000

def clean_companion_content(text: str) -> str:
    """Clean and truncate companion file content for LLM context."""
    # Strip ASCII art lines (repeated non-alphanumeric chars)
    lines = text.splitlines()
    cleaned = [
        line for line in lines
        if not re.match(r'^[\s\-=_*#~|/\\]{10,}$', line)
    ]
    result = "\n".join(cleaned).strip()
    if len(result) > MAX_COMPANION_CHARS:
        result = result[:MAX_COMPANION_CHARS] + "\n[...truncated]"
    return result
```

NFO files typically contain: release info, tracklist, source info, group notes, and ASCII art banners. The informational content is usually within the first 2-3KB. Truncation loses only repeated ASCII art and group credits.

## Pydantic Response Model Recommendation (Claude's Discretion)

```python
from pydantic import BaseModel

class FileProposalResponse(BaseModel):
    """LLM response for a single file in a batch."""
    file_index: int                  # Echo back to match input order
    proposed_filename: str           # Full filename with extension
    confidence: float                # 0.0-1.0 (validated post-parse)
    artist: str | None = None
    event_name: str | None = None
    venue: str | None = None
    date: str | None = None          # YYYY.MM.DD format with x for unknowns
    source_type: str | None = None   # SBD, FM, AUD, WEB, etc.
    stage: str | None = None
    day_number: int | None = None
    b2b_partners: list[str] = []
    reasoning: str                   # Why this filename was chosen

class BatchProposalResponse(BaseModel):
    """LLM response for a batch of files."""
    proposals: list[FileProposalResponse]
```

**Note:** Do NOT use `Field(ge=0.0, le=1.0)` on `confidence` due to Anthropic compatibility issues (Pitfall 2).

## Prompt Template Location Recommendation (Claude's Discretion)

**Recommendation: `src/phaze/prompts/naming.md`**

Rationale:
- Lives inside the package (shipped with the Docker image)
- Version-controlled alongside code
- Discoverable (standard location)
- Loaded via `importlib.resources` or `Path(__file__).parent / "prompts"` for reliability

## litellm as pyproject.toml Dependency (Claude's Discretion)

**Recommendation: Add to `pyproject.toml` dependencies, NOT treat as system dependency.**

Rationale:
- It's a Python package, not a system binary
- Version pinning in pyproject.toml enforces the security constraint
- `uv sync` installs it deterministically
- Lock file captures exact hash for supply chain verification

## Code Examples

### Complete Settings Extension
```python
# Additions to src/phaze/config.py
from pydantic import SecretStr

class Settings(BaseSettings):
    # ... existing fields ...

    # LLM configuration (Phase 6)
    llm_model: str = "claude-sonnet-4-20250514"
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    llm_max_rpm: int = 30           # Max LLM requests per minute
    llm_batch_size: int = 10        # Files per LLM call
    llm_max_companion_chars: int = 3000  # Max chars per companion file
```

### Building File Context for LLM
```python
async def _build_files_context(
    session: AsyncSession, file_ids: list[str]
) -> list[dict]:
    """Load full context for a batch of files."""
    contexts = []
    for i, fid in enumerate(file_ids):
        uid = uuid.UUID(fid)
        # Load file record
        file_rec = (await session.execute(
            select(FileRecord).where(FileRecord.id == uid)
        )).scalar_one()

        # Load analysis
        analysis = (await session.execute(
            select(AnalysisResult).where(AnalysisResult.file_id == uid)
        )).scalar_one_or_none()

        # Load companion content
        companions = await _load_companion_content(session, uid)

        contexts.append({
            "index": i,
            "file_id": fid,
            "original_filename": file_rec.original_filename,
            "original_path": file_rec.original_path,
            "file_type": file_rec.file_type,
            "analysis": {
                "bpm": analysis.bpm if analysis else None,
                "musical_key": analysis.musical_key if analysis else None,
                "mood": analysis.mood if analysis else None,
                "style": analysis.style if analysis else None,
                "features": analysis.features if analysis else None,
            } if analysis else None,
            "companions": companions,
        })
    return contexts
```

### Storing Proposals
```python
async def _store_proposal(
    session: AsyncSession,
    file_id: str,
    proposal: FileProposalResponse,
    file_context: dict,
) -> None:
    """Store an immutable proposal record."""
    uid = uuid.UUID(file_id)
    confidence = max(0.0, min(1.0, proposal.confidence))

    # Build context_used with extracted metadata
    context_used = {
        "artist": proposal.artist,
        "event_name": proposal.event_name,
        "venue": proposal.venue,
        "date": proposal.date,
        "source_type": proposal.source_type,
        "stage": proposal.stage,
        "day_number": proposal.day_number,
        "b2b_partners": proposal.b2b_partners,
        "input_context": file_context,  # What the LLM saw
    }

    record = RenameProposal(
        file_id=uid,
        proposed_filename=proposal.proposed_filename,
        confidence=confidence,
        status=ProposalStatus.PENDING,
        context_used=context_used,
        reason=proposal.reasoning,
    )
    session.add(record)

    # Update file state
    file_rec = (await session.execute(
        select(FileRecord).where(FileRecord.id == uid)
    )).scalar_one()
    file_rec.state = FileState.PROPOSAL_GENERATED
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Direct provider SDKs | litellm unified interface | 2024+ | Single codebase supports Claude, GPT, local models |
| Manual JSON parsing of LLM output | Pydantic `response_format` / structured output | 2024-2025 | Reliable structured responses, automatic validation |
| Sync LLM calls | Async `acompletion()` | litellm 1.x | Non-blocking, works with asyncio event loop |
| Custom prompt strings | Markdown template files | Best practice 2025+ | Version-controlled, editable without code changes |

**Deprecated/outdated:**
- litellm 1.82.7/1.82.8: Compromised by supply chain attack. Do not use.
- Claude 3.5 Sonnet: Retired as of March 2026. Use Claude Sonnet 4 or newer.
- `litellm.completion()` (sync): Blocks event loop. Use `acompletion()`.

## Open Questions

1. **Post-incident litellm release**
   - What we know: 1.82.6 is the last safe version. litellm team paused releases for supply chain review.
   - What's unclear: When a verified safe post-1.82.6 release will be available.
   - Recommendation: Pin `>=1.82.6,<1.82.7` for now. Update when a verified release is announced.

2. **Optimal model choice**
   - What we know: User wants to experiment with both Claude and OpenAI. GPT-4o mini is 20x cheaper than Claude Sonnet 4.
   - What's unclear: Which model produces better filename proposals for this specific domain.
   - Recommendation: Default to a cost-effective model (GPT-4o mini or Claude Haiku 3.5) for initial bulk processing. Make model name configurable. Run comparison tests on a sample.

3. **Files without analysis results**
   - What we know: Some files may not have been analyzed (non-music files, failed analysis).
   - What's unclear: Should these files get proposals based on filename/path/companions alone?
   - Recommendation: Yes -- per D-07, generate low-confidence proposals and flag for review. The original filename often contains rich parseable information (especially scene-named files).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio |
| Config file | `pyproject.toml [tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_services/test_proposal.py -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| AIP-01 | LLM proposes filename from metadata+analysis+companions | unit (mock litellm) | `uv run pytest tests/test_services/test_proposal.py -x` | Wave 0 |
| AIP-01 | Prompt template loads and renders correctly | unit | `uv run pytest tests/test_services/test_proposal.py::test_prompt_template -x` | Wave 0 |
| AIP-01 | Batch context building queries correct data | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::test_build_context -x` | Wave 0 |
| AIP-02 | Proposals stored as immutable records | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::test_store_proposal -x` | Wave 0 |
| AIP-02 | File state transitions to PROPOSAL_GENERATED | unit (mock DB) | `uv run pytest tests/test_services/test_proposal.py::test_state_transition -x` | Wave 0 |
| AIP-01 | Rate limiting prevents excess LLM calls | unit (mock Redis) | `uv run pytest tests/test_services/test_proposal.py::test_rate_limit -x` | Wave 0 |
| AIP-01 | Arq batch job processes files end-to-end | unit (mock all) | `uv run pytest tests/test_tasks/test_generate.py -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_services/test_proposal.py tests/test_tasks/test_generate.py -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_services/test_proposal.py` -- covers AIP-01, AIP-02
- [ ] `tests/test_tasks/test_generate.py` -- covers AIP-01 batch job
- [ ] litellm install: `uv add "litellm>=1.82.6,<1.82.7"` -- new dependency

## Sources

### Primary (HIGH confidence)
- [litellm structured output docs](https://docs.litellm.ai/docs/completion/json_mode) -- Pydantic response_format pattern, supported providers
- [litellm async streaming docs](https://docs.litellm.ai/docs/completion/stream) -- acompletion() async pattern
- [Anthropic pricing page](https://platform.claude.com/docs/en/about-claude/pricing) -- Current model pricing, batch discounts, context windows
- [litellm PyPI page](https://pypi.org/project/litellm/) -- Version 1.82.6 confirmed as latest (March 22, 2026)
- [litellm security update](https://docs.litellm.ai/blog/security-update-march-2026) -- Supply chain incident details

### Secondary (MEDIUM confidence)
- [litellm GitHub issue #21016](https://github.com/BerriAI/litellm/issues/21016) -- Pydantic numeric constraints bug with Anthropic
- [GPT-4o mini pricing](https://pricepertoken.com/pricing-page/model/openai-gpt-4o-mini) -- $0.15/$0.60 per MTok
- [OpenAI pricing page](https://openai.com/api/pricing/) -- GPT-4o mini pricing verified

### Tertiary (LOW confidence)
- Token estimates per file are based on typical scene-named music files and NFO content -- actual usage will vary

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- litellm is the user's locked decision, version verified, API patterns documented
- Architecture: HIGH -- follows established patterns from Phase 4 (arq jobs, service layer, settings)
- Pitfalls: HIGH -- known litellm bugs verified via GitHub issues, token budget math is straightforward
- Batch sizing: MEDIUM -- estimates based on typical file metadata, actual token usage may vary
- Cost estimates: MEDIUM -- based on current pricing, actual costs depend on LLM verbosity and file metadata richness

**Research date:** 2026-03-28
**Valid until:** 2026-04-14 (litellm post-incident release may change version pinning)
