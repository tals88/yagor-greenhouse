<!--
Sync Impact Report
==================
Version change: 1.0.0 → 1.1.0
Added principles:
  - VII. File-First Filtering (Token Economy)
Modified sections: none
Removed sections: none
Templates requiring updates:
  - .specify/templates/plan-template.md ✅ no changes needed
  - .specify/templates/spec-template.md ✅ no changes needed
  - .specify/templates/tasks-template.md ✅ no changes needed
  - .specify/templates/checklist-template.md ✅ no changes needed
  - .specify/templates/agent-file-template.md ✅ no changes needed
Follow-up TODOs: none
-->

# חממת עלים יגור — Google Sheet → Priority Agent Constitution

## Core Principles

### I. Claude-as-Brain

Claude is the matching and decision-making engine. No fuzzy-match
algorithms, no static mapping tables, no lookup CSVs.

- Customer resolution (Google Sheet name → Priority CUSTNAME) MUST
  use Claude's Hebrew comprehension and domain reasoning.
- Product resolution (sheet code + description → PARTNAME) MUST
  use Claude to evaluate CUSTOMERPARTS results and pick the
  semantically correct match.
- When multiple candidates exist, Claude MUST choose based on
  context (produce domain, customer type, description semantics)
  rather than defaulting to the first result.

### II. Bash-Native Orchestration

All runtime operations use `gws` CLI for Google Sheets and `curl`
for Priority ODATA. No custom application code (no Python SDK,
no Node scripts, no compiled binaries).

- Google Sheet read/write: `gws sheets spreadsheets.values.*`
- Priority ODATA calls: `curl` with JSON payloads
- Configuration via environment variables
  (`SHEET_ID`, `PRIORITY_BASE_URL`, `PRIORITY_AUTH`, `COMPANY`)
- Claude Code orchestrates the flow via bash tool invocations

### III. Idempotent & Resumable

Every agent run MUST be safely re-runnable. Partial progress MUST
be preserved.

- Rows already processed (col I contains DOCNO) MUST be skipped
  on subsequent runs.
- DOCNO MUST be written back to the Google Sheet immediately after
  each successful document creation or line addition.
- If the agent crashes mid-run, restarting it MUST pick up from
  unprocessed rows without duplicating already-created documents
  or lines.

### IV. Flag, Don't Fail

The agent MUST never crash or abort on a single unresolvable row.

- Unmatched customers → write `CUST_NOT_FOUND` to match flag
  column, skip the row, continue processing.
- Unmatched products → use PARTNAME `000` (catch-all) with custom
  description from original sheet text, write `PART_NOT_FOUND` to
  match flag column, continue processing.
- Suspicious matches (code exists but description contradicts
  sheet text) → flag for manual review rather than silently using
  the wrong item.
- All flagged rows MUST appear in the summary report.

### V. Data Integrity First

Every write to Priority MUST be preceded by validation.

- Customer CUSTNAME MUST be confirmed to exist in the CUSTOMERS
  entity before creating a delivery note.
- Branch BRANCHNAME MUST resolve to a valid WARHSNAME before
  being used as TOWARHSNAME.
- CURDATE MUST be in ISO format with Israel timezone offset
  (`+03:00`).
- Quantity (TQUANT) MUST be a positive number.
- The agent MUST NOT create delivery notes with unresolved
  customer or warehouse values.

### VI. Incremental Loading

The agent MUST support multiple load cycles per day (e.g., 12:00
and 13:00 loads).

- When a DOCNO already exists in the sheet for the same
  customer+branch combination, new lines MUST be appended to the
  existing draft document rather than creating a duplicate.
- Grouping logic: rows are grouped by Customer (col C) + Branch
  (col A). One delivery note per group.
- The agent MUST check the sheet for existing DOCNOs before
  deciding whether to POST a new document or append lines.

### VII. File-First Filtering (Token Economy)

Priority ODATA responses (customers, parts, branches) can contain
hundreds or thousands of records. Claude MUST NOT read raw API
responses directly — this wastes tokens and risks context overflow.

- All Priority ODATA responses MUST be saved to a temporary file
  first (e.g., `/tmp/priority_customers.json`).
- Bash filtering (`jq`, `grep`) MUST be applied to extract only
  the relevant subset before Claude reads the results.
- Claude reads ONLY the filtered output (typically 1-10 rows
  instead of 500+).

**Standard pattern:**

```bash
# 1. Fetch full dataset to file
curl -s "$PRIORITY_URL/CUSTOMERS?..." -o /tmp/customers.json

# 2. Filter with jq for the customer name we need
jq '.value[] | select(.CUSTDES | test("שופרסל"))' \
  /tmp/customers.json

# 3. Claude reads only the filtered output to make the match
```

**Applies to:**

| Query | Save to | Filter by |
|-------|---------|-----------|
| CUSTOMERS list | `/tmp/priority_customers.json` | `.CUSTDES` substring match |
| CUSTOMERPARTS per customer | `/tmp/priority_parts_{custname}.json` | `.CUSTPARTNAME` code prefix |
| BRANCHES lookup | `/tmp/priority_branches.json` | `.BRANCHNAME` exact match |
| LOGPART (= PART) fallback | `/tmp/priority_logpart.json` | `.PARTNAME` code prefix |

**Caching benefit:** These files also serve as the session cache
(Principle II). Fetch once, filter many times with different
queries — no repeat API calls.

## Technology Constraints

- **Google Sheets access**: `gws` CLI (`@googleworkspace/cli`)
  authenticated via OAuth. No Google API client libraries.
- **Priority ERP access**: ODATA REST API via `curl`. Base URL
  and credentials stored in environment variables.
- **No server-side text filtering**: Priority ODATA may not
  support `contains()` server-side. When needed, fetch full
  result sets to file and filter with `jq`/`grep` before Claude
  reads them (see Principle VII).
- **Caching**: Customer list and CUSTOMERPARTS per customer MAY
  be cached within a single agent session. Branch lookups MAY
  be cached.
- **Deployment path**: Local PC → Docker container → VPS.
  The agent MUST remain portable across all three environments.

## Error Handling & Recovery

- **Network/API failures**: Retry with exponential backoff
  (3 attempts max). Log the error and continue with remaining
  groups.
- **Authentication failures**: Fail fast with a clear error
  message indicating which credential is invalid.
- **Google Sheet structure changes**: Validate expected column
  layout on startup. Fail fast with descriptive error if columns
  do not match expected structure.
- **Partial ODATA failures**: If document creation succeeds but
  a line addition fails, the DOCNO is still written back. The
  failed line is flagged and reported.
- **Summary report**: Every run MUST produce a terminal summary:
  documents created, lines added, unmatched products, unmatched
  customers, errors encountered.

## Governance

- This constitution supersedes all ad-hoc practices for the
  yagor-greenhouse agent.
- Amendments require: (1) description of the change,
  (2) rationale, (3) version bump per semantic versioning.
- All feature specs and implementation plans MUST pass a
  Constitution Check verifying alignment with these principles
  before implementation begins.
- Complexity beyond what is described here MUST be justified
  in the plan's Complexity Tracking table.
- Runtime development guidance lives in `.claude/CLAUDE.md`.

**Version**: 1.1.1 | **Ratified**: 2026-03-13 | **Last Amended**: 2026-03-13
