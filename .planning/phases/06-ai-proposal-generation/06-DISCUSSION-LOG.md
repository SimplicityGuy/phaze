# Phase 6: AI Proposal Generation - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-28
**Phase:** 06-ai-proposal-generation
**Areas discussed:** Naming format, Prompt design, Batch strategy, LLM provider & model

---

## Naming Format

| Option | Description | Selected |
|--------|-------------|----------|
| Event-focused | e.g., 'Coachella 2024 - DJ Shadow - Live Set' | |
| Artist-focused | e.g., 'DJ Shadow - Coachella 2024 Live Set' | |
| Date-focused | e.g., '2024-04-14 - DJ Shadow - Coachella' | |
| Mix of all | LLM decides per file | ✓ |

**User's choice:** Mix of all — LLM decides best format per file based on available metadata
**Notes:** User provided preferred format: `{Artist} - Live @ {Venue|Event} {day/stage} {YYYY.MM.DD}.{ext}`

| Option | Description | Selected |
|--------|-------------|----------|
| LLM decides per file | Prompt includes guidelines, not rigid template | ✓ |
| Fixed template with fallbacks | One template with missing field rules | |

**User's choice:** LLM decides per file (Recommended)

| Option | Description | Selected |
|--------|-------------|----------|
| Filename only (v1 scope) | AIP-03 deferred per REQUIREMENTS.md | ✓ |
| Filename + folder path | Both in Phase 6 | |

**User's choice:** Filename only (v1 scope)

| Option | Description | Selected |
|--------|-------------|----------|
| Always keep original extension | Never touch extension | ✓ |
| LLM can suggest extension changes | Allow normalization | |

**User's choice:** Always keep original extension

| Option | Description | Selected |
|--------|-------------|----------|
| Best effort from filename | Parse original for clues | |
| Skip — don't propose | Mark as insufficient_data | |
| Flag for manual review | Low-confidence proposal, flagged | ✓ |

**User's choice:** Flag for manual review with low confidence

### Naming Guidance Doc
User provided `prototype/naming/mp3rules4.1.txt` (Official MP3 Release Rules 4.1) as reference for understanding scene naming conventions in existing filenames. This is NOT the target format.

### Album Track Format
User specified: `{Artist} - {Track #} - {Track Title}.{ext}` in directory `{Album Name}`

### Date Convention
`YYYY.MM.DD` with `x` for unknown parts: `2013.05.xx`, `2005.xx.xx`, `xxxx.03.14`

### Directory Structure (for future v2 path proposals)
User shared `prototype/naming/dirs.json` showing:
- `performances/artists/{Artist Name}/`
- `performances/festivals/{Festival Name} {Year}/`
- `performances/concerts/{Concert Name} {Year}/`
- `performances/radioshows/{Radioshow Name}/`
- `performances/raid party/{Date}/`

### Filesystem Limits
User specified: ignore scene-era 255 char dirname+filename limit. Modern Linux ext4 supports 255 bytes/component, 4096 bytes/path.

---

## Prompt Design

| Option | Description | Selected |
|--------|-------------|----------|
| All available context | Filename, path, analysis, companions | ✓ |

**User's choice:** All of the above — send everything available per file

| Option | Description | Selected |
|--------|-------------|----------|
| Structured JSON (Pydantic) | Typed fields with schema enforcement | ✓ |
| Free text with parsing | Natural language, parse filename out | |
| Template fill | LLM fills template fields | |

**User's choice:** Structured JSON (Recommended)

| Option | Description | Selected |
|--------|-------------|----------|
| Include examples (few-shot) | 3-5 before/after examples | |
| Rules only | Naming rules in prompt, no examples | ✓ |
| Examples + rules | Both | |

**User's choice:** Rules only — fully automated agentic pipeline, no interactive prompting
**Notes:** User emphasized this is agentic: batch of filenames + context submitted to LLM, results processed automatically.

| Option | Description | Selected |
|--------|-------------|----------|
| Static in code | Python string/template in service layer | |
| Configurable in DB/config | Changeable without code deploy | |
| Markdown file on disk | Easy to edit, version-controlled | ✓ |

**User's choice:** Prompt template as a markdown file on disk, loaded at runtime

---

## Batch Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| By directory | Related files in same batch | |
| Fixed-size chunks | N files per batch regardless of origin | ✓ |
| Smart grouping | Cluster by similarity pre-analysis | |

**User's choice:** Fixed-size chunks — most of collection is unorganized, directory grouping provides little benefit
**Notes:** User noted only a small percentage of files have useful directory context

| Option | Description | Selected |
|--------|-------------|----------|
| 10-20 files | Moderate batch, room for detail | |
| 50-100 files | Larger, leaner context per file | |
| You decide | Claude's discretion | ✓ |

**User's choice:** Claude's discretion on batch size

| Option | Description | Selected |
|--------|-------------|----------|
| Through arq workers | One job per batch, existing infra | ✓ |
| Direct service call | Synchronous or simple async | |

**User's choice:** Through arq workers (Recommended)

---

## LLM Provider & Model

| Option | Description | Selected |
|--------|-------------|----------|
| Claude (Anthropic) | Haiku or Sonnet | |
| OpenAI (GPT-4o-mini) | Cheap, good at JSON | |
| Local model | Zero per-token cost | |
| Configurable via litellm | Switch between any model | ✓ |

**User's choice:** Claude and OpenAI via litellm — wants to experiment with both

| Option | Description | Selected |
|--------|-------------|----------|
| Environment variables | LLM_MODEL + API keys via env vars | ✓ |
| Config file | YAML/JSON config | |
| pydantic-settings | Add to existing Settings class | |

**User's choice:** Environment variables (Recommended)

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — configurable limit | Max requests/min via env var | ✓ |
| No — trust batch size | Simple, but risky | |
| You decide | Claude's discretion | |

**User's choice:** Yes — configurable rate limiting

---

## Claude's Discretion

- Optimal batch size
- Rate limiting implementation approach
- Companion file content handling (full vs truncated)
- Pydantic response model structure
- Prompt markdown file location
- litellm dependency management

## Deferred Ideas

- AIP-03: Directory path proposals (v2) — metadata from Phase 6 enables this later
- Few-shot prompt tuning if rules-only is inconsistent
