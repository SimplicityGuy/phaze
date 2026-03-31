# Phase 13: AI Destination Paths - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-03-31
**Phase:** 13-ai-destination-paths
**Areas discussed:** Path generation strategy, Collision detection & handling, Path display in approval UI, Directory tree preview

---

## Path Generation Strategy

### How should the LLM generate destination paths?

| Option | Description | Selected |
|--------|-------------|----------|
| Extend existing prompt | Add path rules to naming.md, one LLM call for both filename and path | ✓ |
| Separate path-only prompt | Second LLM call dedicated to path generation | |
| Rule-based (no LLM) | Deterministic path from extracted metadata, no LLM cost | |

**User's choice:** Extend existing prompt. User emphasized keeping rules in the prompt markdown file for easy editing. Specified the path logic should be a 3-step decision: (1) which category under performances/, (2) which artist/festival/concert/radioshow, (3) for festivals/concerts, year and nested structure.

### How rigid should path rules be vs LLM discretion?

| Option | Description | Selected |
|--------|-------------|----------|
| Template-guided LLM | Provide templates, LLM picks best one and fills values | ✓ |
| Strict templates only | LLM must match templates exactly, reject otherwise | |
| Freeform LLM paths | LLM proposes any reasonable path structure | |

**User's choice:** Template-guided LLM (recommended)

### Should album tracks follow the same performances/ tree?

| Option | Description | Selected |
|--------|-------------|----------|
| Separate music/ tree | Album tracks under music/{Artist}/{Album}/ | ✓ |
| Under performances/artists/ | Everything under performances/ | |
| You decide | Claude picks | |

**User's choice:** Separate music/ tree (recommended)

### What happens when LLM can't determine a good path?

| Option | Description | Selected |
|--------|-------------|----------|
| Null path, flag for review | Leave proposed_path null, flag for manual review | ✓ |
| Default unsorted/ directory | Put uncertain files in unsorted/ | |
| Still propose best guess | Always propose, use confidence to signal uncertainty | |

**User's choice:** Null path, flag for review (recommended)

---

## Collision Detection & Handling

### When should collision detection run?

| Option | Description | Selected |
|--------|-------------|----------|
| On approval | Check when user approves, compare against other approved | |
| Batch check before execution | Scan all approved proposals before execution pipeline | ✓ |
| Both | Approval-time check plus batch scan safety net | |

**User's choice:** Batch check before execution

### What should happen when a collision is detected?

| Option | Description | Selected |
|--------|-------------|----------|
| Warning banner, block execution | Show warning, refuse to process until resolved | ✓ |
| Warning only, allow execution | Show warning but let execution proceed | |
| Auto-suffix to resolve | Append (1), (2) etc. automatically | |

**User's choice:** Warning banner, block execution (recommended)

---

## Path Display in Approval UI

### Where should the proposed path appear?

| Option | Description | Selected |
|--------|-------------|----------|
| New column in table | "Destination" column with truncated path and tooltip | ✓ |
| In expanded row details only | Path shows on row expansion | |
| Combined with filename | Show as proposed_path/proposed_filename in one column | |

**User's choice:** New column in table (recommended)

### How should null paths be displayed?

| Option | Description | Selected |
|--------|-------------|----------|
| Empty cell with 'No path' badge | Gray badge saying "No path" | ✓ |
| Warning icon | Yellow warning triangle | |
| You decide | Claude picks | |

**User's choice:** Empty cell with 'No path' badge

---

## Directory Tree Preview

### How should the tree preview be presented?

| Option | Description | Selected |
|--------|-------------|----------|
| Dedicated page | Separate /preview page with collapsible folders and file counts | ✓ |
| Side panel on approval page | Slide-out panel visible while reviewing | |
| Modal overlay | Large modal triggered by button | |

**User's choice:** Dedicated page (recommended)

### What scope should the tree cover?

| Option | Description | Selected |
|--------|-------------|----------|
| All approved proposals | "What will happen when I execute" view | ✓ |
| All proposals (approved + pending) | Full picture with different styling | |
| You decide | Claude picks | |

**User's choice:** All approved proposals (recommended)

---

## Claude's Discretion

- Prompt template wording for path rules and examples
- Pydantic response model field additions
- Directory tree rendering approach
- Collision detection query design
- Collision warning visual treatment
- Truncation and tooltip implementation
- Navigation link placement

## Deferred Ideas

None — discussion stayed within phase scope.
