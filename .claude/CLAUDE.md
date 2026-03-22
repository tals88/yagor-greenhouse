# חממת עלים יגור — Google Sheet → Priority Agent
## Implementation Plan | March 2026

---

## 1. Project Summary

**Client:** חממת עלים יגור (greenhouse/produce supplier)

**Problem:** Orders arrive from retail customers into a Google Sheet. Today the data is manually copied to Excel for picking/delivery planning, with minimal use of Priority.

**Solution:** A Claude Code agent that reads the Google Sheet via `gws` CLI, intelligently matches customers and products against Priority data, and creates/updates draft delivery notes (תעודות משלוח) via Priority ODATA API.

**Key Design Decision:** Claude is the matching engine. No mapping tables, no fuzzy algorithms. The agent uses Claude's natural language understanding to resolve customer names and product codes — leveraging its Hebrew comprehension and domain reasoning.

---

## 2. Architecture

```
┌─────────────┐     gws CLI      ┌──────────────┐
│ Google Sheet │ ◄──────────────► │              │
│ (customer's) │   read rows /    │  Claude Code │
└─────────────┘   write DOCNO +   │  (orchestrator│
                  flags back      │   + brain)    │
                                  │              │
┌─────────────┐   curl / ODATA   │              │
│ Priority ERP│ ◄──────────────► │              │
│  (חממת עלים) │   REST API       └──────────────┘
└─────────────┘
```

**Runtime:** Claude Code with bash tools
- `gws` — Google Workspace CLI for Sheets read/write (JSON output)
- `curl` — Priority ODATA REST API calls
- No Python SDK, no custom code — Claude orchestrates via bash

**Deployment path:**
1. Phase 0: Local PC (your machine) — develop and test
2. Phase 1: Docker container — portable, reproducible
3. Phase 2: Server/VPS — scheduled or on-demand execution

---

## 3. Google Sheet Structure (Source Data)

| Column | Field | Usage |
|--------|-------|-------|
| A | Branch ID (e.g. `1216139476`) | → lookup BRANCHES → WARHSNAME → TOWARHSNAME on delivery note |
| B | Branch name (e.g. `אלוני השרון`) | Human-readable, for logging/display |
| C | Customer (e.g. `שופרסל`, `מגה בעיר`) | → Claude matches to CUSTNAME in Priority |
| D | Timestamp | Order time from Google Sheet |
| E | Quantity | Units ordered (boxes or individual) |
| F | (unused / `undefined`) | — |
| G | Product (e.g. `0425 תירס לבן`) | → Claude matches via CUSTOMERPARTS / PARTNAME |
| H | Pack type (`קרטון` / `יחידות`) | Determines unit of measure on delivery note |

**Columns we add (agent writes back):**

| Column | Field | Purpose |
|--------|-------|---------|
| I (or next free) | `DOCNO` | Priority document number (e.g. `SH2600001`). Serves as both "loaded" flag and key for appending |
| J (or next free) | `MATCH_FLAG` | `OK` / `PART_NOT_FOUND` / `CUST_NOT_FOUND` — for rows that need manual review |

---

## 4. Priority ODATA Endpoints

### 4.1 Customer Resolution (Col C → CUSTNAME)

```
GET /odata/Priority/tabula.ini/{company}/CUSTOMERS?$filter=CUSTNAME ne ''&$select=CUSTNAME,CUSTDES
```

Agent fetches the customer list. Claude reads the results and determines which CUSTNAME matches the Google Sheet value. For example: sheet says "שופרסל" → Claude sees `שופרסל` and `שופרסל דיל` → picks `שופרסל` because the context is a produce delivery, not a discount chain.

**Caching:** Customer list changes rarely. Agent can fetch once per session and reuse.

### 4.2 Branch → Warehouse Lookup (Col A → TOWARHSNAME)

```
GET /odata/Priority/tabula.ini/{company}/BRANCHES?$filter=BRANCHNAME eq '{col_A_value}'&$select=BRANCHNAME,WARHSNAME
```

Returns the warehouse code (`WARHSNAME`) associated with that branch. This becomes `TOWARHSNAME` (מחסן קונסיגנציה) on the delivery note.

### 4.3 Product Resolution (Col G → PARTNAME)

**Step 1 — Search CUSTOMERPARTS for this customer:**

```
GET /odata/Priority/tabula.ini/{company}/CUSTOMERS('{custname}')/CUSTOMERPARTS_SUBFORM?$select=PARTNAME,CUSTPARTNAME,CUSTPARTDES,PARTDES
```

Agent extracts the leading number from col G (e.g. `0452` from `0452 חסה ירוקה`). Claude searches the results for a match on `CUSTPARTNAME` or `PARTNAME`.

**Step 2 — If not found, search LOGPART table (same as PART entity):**

```
GET /odata/Priority/tabula.ini/{company}/LOGPART?$filter=contains(PARTNAME,'{number}')&$select=PARTNAME,PARTDES
```

Note: `LOGPART` and `PART` are the same entity in Priority. `contains()` may not work server-side in Priority ODATA. If not, fetch full result set to file and filter with `jq` (see constitution Principle VII).

Claude reads results and picks the one whose description makes sense given the Hebrew name from the sheet.

**Step 3 — If still not found:**
- Use PARTNAME = `000` (general/catch-all part)
- Set custom description = original col G text (e.g. `0452 חסה ירוקה`)
- Write `PART_NOT_FOUND` to the match flag column in Google Sheet

### 4.4 Create Delivery Note (New Document)

```
POST /odata/Priority/tabula.ini/{company}/DOCUMENTS_D
Content-Type: application/json

{
  "CUSTNAME": "{resolved_custname}",
  "TOWARHSNAME": "{resolved_warehouse}",
  "CURDATE": "2026-03-15T00:00:00+03:00"
}
```

Returns: `DOCNO` (e.g. `SH2600001`) — saved back to Google Sheet col I.

**Notes:**
- `CURDATE` is mandatory — set to today's date in ISO format with Israel timezone offset (`+03:00`)
- No need to set draft status — Priority assigns draft automatically on creation

### 4.5 Add Line to Delivery Note

```
POST /odata/Priority/tabula.ini/{company}/DOCUMENTS_D(DOCNO='SH2600001')/TRANSORDER_D_SUBFORM
Content-Type: application/json

{
  "PARTNAME": "{resolved_partname}",
  "TQUANT": {quantity},
  "PDES": "{description_if_000}"
}
```

### 4.6 Append to Existing Draft

When col I already contains a DOCNO for the same customer+branch:

```
POST /odata/Priority/tabula.ini/{company}/DOCUMENTS_D(DOCNO='{existing_docno}')/TRANSORDER_D_SUBFORM
```

Same payload as 4.5. No need to create a new document.

---

## 5. Agent Flow (Step by Step)

```
START
  │
  ▼
┌─────────────────────────────────┐
│ 1. READ GOOGLE SHEET            │
│    gws sheets ... read all rows │
│    Filter: col I (DOCNO) empty  │
│    = not yet loaded             │
└─────────────┬───────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│ 2. FETCH REFERENCE DATA         │
│    - CUSTOMERS list (cache)     │
│    - CUSTOMERPARTS per customer │
│    - BRANCHES as needed          │
└─────────────┬───────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│ 3. GROUP ROWS                   │
│    Group by: Customer + Branch  │
│    = one delivery note per group│
└─────────────┬───────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│ 4. FOR EACH GROUP:              │
│                                 │
│  a. Resolve customer (col C)    │
│     Claude picks correct        │
│     CUSTNAME from Priority list │
│                                 │
│  b. Resolve branch (col A)      │
│     BRANCHNAME → WARHSNAME      │
│                                 │
│  c. Check: does a DOCNO already │
│     exist for this group in the │
│     sheet from a previous load? │
│     YES → append to existing    │
│     NO  → create new draft      │
│                                 │
│  d. For each row in group:      │
│     - Resolve product (col G)   │
│       via CUSTOMERPARTS → or    │
│       PARTNAME → or 000         │
│     - POST line to delivery note│
│     - Write DOCNO + flag back   │
│       to Google Sheet row       │
└─────────────┬───────────────────┘
              │
              ▼
┌─────────────────────────────────┐
│ 5. SUMMARY REPORT               │
│    - X documents created        │
│    - Y lines added              │
│    - Z unmatched products       │
│    Output to terminal/log       │
└─────────────────────────────────┘
```

---

## 6. Claude's Intelligence Role

Claude is NOT just an orchestrator calling APIs. It's the **decision-making brain**:

### Customer Matching
- Sees: `מגה בעיר` in sheet, and `מגה`, `מגה בול`, `מגה בעיר` in Priority
- Decides: which is the correct entity based on context

### Product Matching
- Sees: `0425 תירס לבן` in sheet
- Searches CUSTOMERPARTS for `0425`
- If multiple results: reads Hebrew descriptions, picks the one that means "white corn"
- If no results: searches PARTNAME, applies same reasoning
- If still nothing: `000` + original text + flag

### Sanity Checking
- If col G says `0422 בזיליקום` (basil) but CUSTOMERPARTS returns `0422` = `עגבניות` (tomatoes), Claude should flag this as suspicious rather than blindly using it
- If a branch ID returns no warehouse, Claude reports it rather than crashing

### Error Recovery
- If an ODATA call fails (network, auth, server error), Claude retries with backoff
- If a document creation fails, Claude logs the error and continues with remaining groups

---

## 7. gws CLI Setup

### Installation
```bash
npm install -g @googleworkspace/cli
```

### Authentication (one-time, on your Google account)
```bash
gws auth setup  # or gws auth login
```

### Key Commands for This Project

**Read sheet rows:**
```bash
gws sheets spreadsheets.values.get \
  --params '{"spreadsheetId":"SHEET_ID","range":"ordersNew!A:J"}'
```

**Write DOCNO back to a cell:**
```bash
gws sheets spreadsheets.values.update \
  --params '{"spreadsheetId":"SHEET_ID","range":"ordersNew!I{row}","valueInputOption":"RAW"}' \
  --json '{"values":[["SH2600001"]]}'
```

**Write match flag:**
```bash
gws sheets spreadsheets.values.update \
  --params '{"spreadsheetId":"SHEET_ID","range":"ordersNew!J{row}","valueInputOption":"RAW"}' \
  --json '{"values":[["PART_NOT_FOUND"]]}'
```

### Credential Migration
Develop on your Google account → later just swap credentials to client's account. The `gws auth` flow handles this cleanly.

---

## 8. Implementation Phases

### Phase 0: Setup & Prove-Out (Days 1-2)
- [ ] Install `gws`, authenticate, verify read/write to a test Google Sheet
- [ ] Verify Priority ODATA connectivity — test GET customers, GET branches, POST a test delivery note
- [ ] Confirm CUSTOMERPARTS structure and field names with Yaron/Chen
- [ ] Confirm DOCUMENTS_D field names (TOWARHSNAME, CURDATE format)
- [ ] Test the "append line to existing document" flow in ODATA

### Phase 1: Core Agent — Read Only (Days 3-5)
- [ ] Claude Code reads Google Sheet via `gws`
- [ ] Resolves all customers and products (logs results, doesn't write to Priority yet)
- [ ] Groups by customer+branch
- [ ] Outputs a "dry run" report: what it would create
- [ ] Review with Yaron — validate matching accuracy

### Phase 2: Core Agent — Write (Days 6-8)
- [ ] Create draft delivery notes in Priority
- [ ] Add lines to documents
- [ ] Write DOCNO + flags back to Google Sheet
- [ ] Test the "append to existing draft" flow (simulate 12:00/13:00 loads)
- [ ] End-to-end test with sample data

### Phase 3: Production Hardening (Days 9-12)
- [ ] Error handling and retry logic
- [ ] Logging (what was created, what failed, what was flagged)
- [ ] Test with real customer Google Sheet data
- [ ] Dockerize for portability
- [ ] Document the CLAUDE.md / system prompt for the agent

### Phase 4: Priority Side (Parallel with Yaron)
- [ ] Add קו חלוקה field to מחסנים table
- [ ] Add קו חלוקה + סדר ליקוט fields to delivery note form
- [ ] Build the special picking printout (5×2 layout per page)
- [ ] Test full flow: load → plan routes → print picking lists

---

## 9. Open Items (Verify with Yaron / Chen)

| # | Question | Who | Status |
|---|----------|-----|--------|
| 1 | Confirm CUSTOMERPARTS field names — is the customer SKU in `CUSTPARTNAME`? | Yaron/Chen | ⏳ |
| 2 | CURDATE format confirmed: `2025-10-30T00:00:00+03:00` (ISO + Israel TZ) | — | ✅ |
| 3 | Confirm BRANCHES entity — is `BRANCHNAME` the numeric ID field? (entity is `BRANCHES` not `BRANCHS`) | Yaron | ⏳ |
| 4 | Are all ~120 branches already set up as warehouses in Priority? | Yaron/Chen | ⏳ |
| 5 | What is the PARTNAME for the catch-all part? Is `000` already created? | — | ✅ Confirmed: `000` = "מוצר כללי" exists |
| 6 | Does the customer have Priority ODATA API license purchased? (Gali helping Yossi) | Gali | ⏳ |
| 7 | TQUANT unit — when H=`קרטון`, is the quantity in col E already in carton units? | Chen | ⏳ |
| 8 | Which DOCUMENTS_D type code for delivery note? (typically `D` or specific type) | Yaron | ⏳ |

---

## 10. Cost Estimate

| Component | Monthly Cost |
|-----------|-------------|
| Claude Code API usage (Anthropic) | $5–30 (depends on frequency) |
| Google Workspace CLI | Free (OAuth, no API costs for Sheets) |
| Priority ODATA API | Client's existing license |
| Docker/VPS (Phase 2+) | $6–18 (DigitalOcean) |
| **Total** | **~$15–50/month** |

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Priority ODATA doesn't support server-side text filtering | Can't search parts efficiently | Fetch full CUSTOMERPARTS list per customer, Claude filters client-side |
| Claude mismatches a product | Wrong item on delivery note | Phase 1 dry-run validates accuracy before going live. Sanity check descriptions. |
| Google Sheet structure changes | Agent breaks | Agent validates expected columns on startup, fails fast with clear error |
| ODATA rate limiting or timeouts | Partial loads | Retry logic + write DOCNO per row (so partial progress is preserved) |
| Customer adds new product not in Priority | 000 catch-all | Flag column alerts team to add the product to Priority |

---

## 12. CLAUDE.md Prompt Structure (for the agent)

The Claude Code agent will need a well-crafted system prompt covering:

1. **Role:** You are a data integration agent for חממת עלים יגור
2. **Tools:** `gws` for Google Sheets, `curl` for Priority ODATA
3. **Priority ODATA base URL and auth** (environment variables)
4. **Google Sheet ID** (environment variable)
5. **The matching rules** described in section 6
6. **The flow** described in section 5
7. **Error handling rules** — never crash, always log, always flag
8. **Hebrew context** — you understand Israeli retail chains, produce items, Priority ERP terminology

---

## 13. Phase 1 Findings (Dry Run — March 13, 2026)

### Connectivity & CLI

- **Priority DNS workaround**: `curl` cannot resolve `YOUR_PRIORITY_HOST` via DNS. Must use `--resolve YOUR_PRIORITY_HOST:443:YOUR_PRIORITY_IP` on every curl call.
- **Priority requires HTTPS**: HTTP returns `403 SSL is required`.
- **Loading .env**: `source .env` doesn't work reliably in the bash tool. Use: `export $(grep -v '^#' .env | grep -v '^$' | xargs)`
- **gws CLI syntax**: Use `gws sheets +read --spreadsheet ID --range "ordersNew!A:J" --format json` (not the `spreadsheets.values.get` subcommand from older docs).
- **gws write syntax**: Use `gws sheets spreadsheets values update` for writing back (verify exact syntax in Phase 2).

### Entity Names (Verified via ODATA $metadata)

| Planned Name | Actual Name | Notes |
|--------------|-------------|-------|
| `CUSTOMERPARTS_SUBFORM` | **`CUSTPART_SUBFORM`** | Important: different from plan! |
| `CUSTOMERS` | `CUSTOMERS` | Correct |
| `LOGPART` | `LOGPART` | Correct, same as `PART` |
| `BRANCHES` | `BRANCHES` | Correct |
| `DOCUMENTS_D` | `DOCUMENTS_D` | Correct (not yet tested POST) |
| `TRANSORDER_D_SUBFORM` | `TRANSORDER_D_SUBFORM` | Not yet verified |

### CUSTPART_SUBFORM Fields

| Field | Purpose |
|-------|---------|
| `PARTNAME` | Priority product code (the key) |
| `PARTDES` | Product description (auto-filled from LOGPART) |
| `CUSTPARTNAME` | Customer's own SKU code for this product |
| `CUSTPARTDES` | Customer's own description |
| `CUSTPARTBARCODE` | Customer's barcode |

### Customer Matching Results (100% match rate)

Sheet customers map to Priority CUSTNAME as follows:

| Sheet Name | Normalized To | Priority CUSTNAME |
|------------|---------------|-------------------|
| שופרסל | — | YG-SHUF |
| מגה בעיר | — | YG-MEGA |
| מגה | מגה בעיר | YG-MEGA |
| מגה אזורים | — | YG-MGAZ |
| יינות ביתן | — | YG-YBIT |
| ביתן | יינות ביתן | YG-YBIT |
| יונות. ביתן | יינות ביתן | YG-YBIT |
| ינות ביתן | יינות ביתן | YG-YBIT |
| קרפור | — | YG-CARF |
| קורפור | קרפור | YG-CARF |
| קופור | קרפור | YG-CARF |
| קנפור | קרפור | YG-CARF |
| קרפור סיטי | — | YG-CACI |
| סטופ מרקט | — | YG-STOP |
| סטוב מרקט | סטופ מרקט | YG-STOP |
| גלובל ריטייל | — | YG-GLOB |
| שופרסל דיל | — | YG-SHDI |

### Product Matching Strategy

1. **Code match**: Extract leading number from col G (e.g. `0425` from `0425 תירס לבן`) → match against `CUSTPARTNAME` or `PARTNAME`
2. **Name match**: If no code, match Hebrew name against `PARTDES`
3. **Fallback**: Use `000` (מוצר כללי) + set `PDES` to original text + flag as `PART_NOT_FOUND`

### Sheet Statistics

- **2001 total rows**, **864 valid orders**, 1137 empty/header rows
- **115 delivery note groups** (unique customer + branch combinations)
- **38 unique branches** (10-digit IDs like `1216139476`)
- **~49 unique products**

### Demo Data Created (YOUR_PRIORITY_HOST / demo company)

- 9 customers (YG-SHUF, YG-MEGA, YG-YBIT, YG-CARF, YG-STOP, YG-GLOB, YG-SHDI, YG-CACI, YG-MGAZ)
- 37 products in LOGPART (20 original codes + 8 coded like 9022/9039 + 9 custom YG- codes)
- 333 CUSTOMERPARTS links (all products × all customers)
- 6 branches (100-105, short numeric IDs — real system will have 10-digit IDs)
- 5 warehouses (WH-001 through WH-005)

### Files Created

| File | Purpose |
|------|---------|
| `dry_run.py` | Phase 1 dry-run matching script |
| `setup-demo-data.sh` | Creates customers/products/branches in demo Priority |
| `setup-customerparts.sh` | Links all products to all customers via CUSTPART_SUBFORM |
| `data_sheet.json` | Cached Google Sheet data (for offline analysis) |
| `data_customers.json` | Cached Priority customers list |
| `data_parts.json` | Cached Priority products list |

### Phase 2 Entry Point

When starting Phase 2 (write mode), the agent should:
1. Read Google Sheet (filter rows where col I is empty)
2. Fetch Priority reference data (customers, CUSTPART_SUBFORM per customer, branches)
3. Group rows by customer + branch
4. For each group: create `DOCUMENTS_D` → add lines via `TRANSORDER_D_SUBFORM` → write DOCNO back to col I
5. Handle "append to existing draft" (when col I already has a DOCNO for same group)