# Phase 18: Unified Search - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-02
**Phase:** 18-unified-search
**Areas discussed:** Results layout, Search interaction, Nav placement, Empty/edge states

---

## Results Layout

| Option | Description | Selected |
|--------|-------------|----------|
| Card list | Each result is a card (like Duplicates page) — more visual, expandable | |
| Table rows | Dense table (like Proposals page) — more results visible, sortable, compact | ✓ |
| Grouped by type | Results split into sections: Files first, then Tracklists | |

**User's choice:** Table rows
**Notes:** Consistent with existing Proposals page pattern.

---

### Type Distinction

| Option | Description | Selected |
|--------|-------------|----------|
| Color-coded badge | Small colored pill badge: blue for files, green for tracklists | ✓ |
| Icon prefix | Music note icon for files, list icon for tracklists | |
| You decide | Claude picks based on existing patterns | |

**User's choice:** Color-coded badge
**Notes:** Similar to source badges already used on tracklist cards.

---

## Search Interaction

| Option | Description | Selected |
|--------|-------------|----------|
| Form submit | Type query, hit Enter or Search button. Standard HTMX swap. | ✓ |
| Live filter | Results update as you type (debounced 300ms). No existing pattern. | |

**User's choice:** Form submit

---

### Facet Filters

| Option | Description | Selected |
|--------|-------------|----------|
| Sidebar filters | Left sidebar with filter sections — always visible | |
| Inline above results | Filter row above table (like Proposals filter tabs) | |
| Collapsible panel | "Advanced filters" toggle that expands above results | ✓ |

**User's choice:** Collapsible panel
**Notes:** Keeps page clean when not filtering. Alpine.js toggle.

---

## Nav Placement

| Option | Description | Selected |
|--------|-------------|----------|
| First tab | Search as leftmost nav item — primary entry point | ✓ |
| Last tab | After Audit Log — least disruptive | |
| After Pipeline | Second position — discovery-oriented ordering | |

**User's choice:** First tab
**Notes:** Makes Search the primary landing experience for the app.

---

## Empty/Edge States

### Initial State

| Option | Description | Selected |
|--------|-------------|----------|
| Just the search box | Clean page with search input and collapsed filters | |
| Search + recent stats | Search box plus summary counts as overview | ✓ |
| You decide | Claude picks based on existing patterns | |

**User's choice:** Search + recent stats

### No Results

| Option | Description | Selected |
|--------|-------------|----------|
| Simple message | "No results found" with suggestion to broaden filters | ✓ |
| You decide | Claude handles empty state design | |

**User's choice:** Simple message

---

## Claude's Discretion

- Pagination approach (offset vs cursor)
- FTS configuration (simple vs english)
- GIN index column selection
- Result row detail level

## Deferred Ideas

None.
