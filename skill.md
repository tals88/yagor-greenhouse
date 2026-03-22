# חממת עלים יגור — Order Loading Skill

## Overview

You are a data integration agent for חממת עלים יגור (Yagor Greenhouse).
Read orders from a Google Sheet, match customers/products/warehouses against Priority ERP,
and create draft delivery notes (תעודות משלוח) via Priority ODATA API.

You only have the **Bash** tool. Use it to call `gws` (Google Sheets CLI) and `curl` (Priority ODATA API).

---

## Google Sheet

**Sheet ID:** from `$SHEET_ID` environment variable
**Credentials:** always prefix gws commands with `GOOGLE_WORKSPACE_CLI_CONFIG_DIR=.gws-config`

### Reading the sheet

The `!` character in sheet ranges gets mangled by bash. Always read via a Python one-liner:

```bash
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=.gws-config python3 -c "
import subprocess, json
result = subprocess.run([
    'gws','sheets','spreadsheets','values','get',
    '--params', json.dumps({'spreadsheetId':'$SHEET_ID','range':'הזמנות!A:K'}),
    '--format','json'
], capture_output=True, text=True)
print(result.stdout)
" > /tmp/sheet_orders.json
```

### Writing back to the sheet

After creating a delivery note, write back to columns J and K for every row in that group:
- **Column J**: the `DOCNO` from the Priority response
- **Column K**: the current date and time (when the document was created), format `YYYY-MM-DD HH:MM:SS`

```bash
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=.gws-config python3 -c "
import subprocess, json
from datetime import datetime
now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
# On success: write DOCNO to J, timestamp to K
# On error: write error text to L
body = json.dumps({
    'valueInputOption': 'RAW',
    'data': [
        {'range': 'הזמנות!J{ROW}', 'values': [['{DOCNO}']]},
        {'range': 'הזמנות!K{ROW}', 'values': [[now]]},
        {'range': 'הזמנות!L{ROW}', 'values': [['{ERROR_TEXT_OR_EMPTY}']]}
    ]
})
subprocess.run([
    'gws','sheets','spreadsheets','values','batchUpdate',
    '--params', json.dumps({'spreadsheetId':'$SHEET_ID'}),
    '--json', body
], capture_output=True, text=True)
"
```

For batch writes (multiple rows at once), combine all updates into a single `data` array.

### Tab: הזמנות (orders — main data)

| Column | Field | Description |
|--------|-------|-------------|
| A | Order Number | Customer's order number (e.g. `12161406167302`). → `BOOKNUM` on `DOCUMENTS_D` |
| B | Warehouse Name | Branch/warehouse name in Hebrew (e.g. `קריון`, `שופרסל 39`). → lookup `WARHSNAME` via `ZANA_WARHSDES_EXT_FL` → `TOWARHSNAME` on `DOCUMENTS_D` |
| C | Customer Name | Hebrew name (e.g. `שופרסל`, `סטופ מרקט`). → lookup `CUSTNAME` via `CUSTOMERS` → `CUSTNAME` on `DOCUMENTS_D` |
| D | Timestamp | When the order was received (e.g. `26-2-25T7:19:19:0Z`). → `DETAILS` on `DOCUMENTS_D` (pass as-is) |
| E | Quantity | Units ordered → `TQUANT` on `TRANSORDER_D_SUBFORM` |
| F | (unused) | Usually `undefined` — ignore |
| G | Product Description | Product name (e.g. `0475 שרי (12 יח')`, `(1127) תפוח אדמה ורוד...`). → lookup `PARTNAME` via `ZANA_PARTDES_EXT_FLA` |
| H | Pack Type | `קרטון` or `יחידות` |

| I | Exclude Flag | Customer sets `N` or `n` to exclude this line from processing — skip it |

**Columns the agent writes back:**

| Column | Field | Description |
|--------|-------|-------------|
| J | DOCNO | Priority document number (e.g. `SH2630000712`). Written after document creation. |
| K | Created At | Date and time the document was created (e.g. `2026-03-22 14:30:00`). |
| L | Error | Error message from Priority if document creation failed. Extract the `text` field from the error response. |

**No header row.** Row 1 is data.

**Skip invalid rows:** rows with only column A filled (no customer/product), or empty/missing customer or product.

**Skip excluded rows:** if column I contains `N` or `n`, the customer has marked this line for exclusion — skip it entirely.

**Skip already-processed rows:** if column J already contains a `DOCNO` (starts with `SH`), this row was already loaded — skip it.

**Grouping:** Rows with the same **(column A + column B + column C)** belong to the same delivery note. Each unique combination of (order number + warehouse + customer) = 1 delivery note in Priority.

### Tab: לקוחות (customers reference)

| Column | Field |
|--------|-------|
| A | שם החברה (company name) |
| B | שם הסניף (branch name) |

Use for additional matching context when resolving customer names.

### Tab: מוצרים (products reference)

| Column | Field |
|--------|-------|
| A | ID |
| B | שם (product name, e.g. `0475 שרי (12 יח')`) |

Use for additional matching context when resolving product names.

---

## Priority ODATA API

**Base URL:** `https://$PRIORITY_BASE_URL/odata/Priority/$PRIORITY_TABULA_INI/$PRIORITY_COMPANY`
**Auth:** `-u "$PRIORITY_USER:$PRIORITY_PASSWORD"`
**Always add:** `-k` (skip SSL verify)

### curl template

```bash
curl -s -k \
  -u "$PRIORITY_USER:$PRIORITY_PASSWORD" \
  -H "Accept: application/json" \
  "https://$PRIORITY_BASE_URL/odata/Priority/$PRIORITY_TABULA_INI/$PRIORITY_COMPANY/{ENDPOINT}"
```

### Endpoint 1: Customer Lookup — `CUSTOMERS`

```
GET .../CUSTOMERS?$select=CUSTNAME,CUSTDES
```

Returns all customers. Match **column C** (customer name from sheet) against `CUSTDES` (Hebrew description).
Return the corresponding `CUSTNAME` (code like `73050690000`).

**Response example:**
```json
{"CUSTNAME": "73050690000", "CUSTDES": "המפרץ הזוהר בע\"מ"}
```

**~4,973 customers.** Fetch once, cache for the session.

### Endpoint 2: Warehouse Lookup — `ZANA_WARHSDES_EXT_FL`

```
GET .../ZANA_WARHSDES_EXT_FL
```

This is a **fuzzy search table**: multiple `WARHSDES` values point to the same `WARHSNAME`.
Match **column B** (warehouse/branch name from sheet) against `WARHSDES`.
Return the corresponding `WARHSNAME` (code like `1`, `3`, `9`).

**Response example:**
```json
{"WARHSNAME": "1", "WARHSDES": "ExampleBranch", "WARHS": 1}
{"WARHSNAME": "1", "WARHSDES": "1 ExampleBranch", "WARHS": 1}
{"WARHSNAME": "1", "WARHSDES": "ExampleBranch 1", "WARHS": 1}
```

**~869 entries, 189 unique warehouse codes.** Fetch once, cache.

Multiple WARHSDES strings map to the same WARHSNAME — use your Hebrew understanding to find the best match.

### Endpoint 3: Product Lookup — `ZANA_PARTDES_EXT_FLA`

```
GET .../ZANA_PARTDES_EXT_FLA
```

This is a **fuzzy search table**: multiple `PARTDES` values point to the same `PARTNAME`.
Match **column G** (product description from sheet) against `PARTDES`.
Return the corresponding `PARTNAME` (code like `2000422`).

**Response example:**
```json
{"PARTNAME": "2000422", "PARTDES": "0422 בזיליקום", "PART": 2809}
{"PARTNAME": "2000422", "PARTDES": "בזיליקום", "PART": 2809}
```

**~40 entries, 30 unique product codes.** Fetch once, cache.

### Endpoint 4: Product Fallback — `LOGPART`

```
GET .../LOGPART?$select=PARTNAME,PARTDES
```

The full parts catalog. Use this when `ZANA_PARTDES_EXT_FLA` doesn't have a match.

**378 records.** Fetch once, cache to `/tmp/priority_logpart.json`.

**Key pattern:** Sheet product codes map to LOGPART as `200` + 4-digit code:
- Sheet `0475 שרי` → LOGPART `2000475` (עגבניות שרי)
- Sheet `0525 מגי` → LOGPART `2000525` (עגבניות מגי)
- Sheet `0422 בזיליקום` → LOGPART `2000422` (בזיליקום)
- Sheet `0464 לאליק` → LOGPART `2000464` (חסה לאליק מסולסלת)

**Resolution order for products:**
1. First try `ZANA_PARTDES_EXT_FLA` (fuzzy search table, best for matching free-text descriptions)
2. If not found → try `LOGPART` with code pattern `200{4-digit-code}` (e.g. `0475` → `2000475`)
3. If not found → try `LOGPART` by Hebrew name match against `PARTDES`
4. If still not found → this is where Claude steps in to search creatively with Bash
5. If truly not found → write `PARTNAME not found: {description}` to column L

**Response example:**
```json
{"PARTNAME": "2000475", "PARTDES": "עגבניות שרי"}
{"PARTNAME": "2000422", "PARTDES": "בזיליקום"}
```

---

### Endpoint 5: Create Delivery Note — `DOCUMENTS_D`

```bash
curl -s -k \
  -u "$PRIORITY_USER:$PRIORITY_PASSWORD" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -X POST \
  "https://$PRIORITY_BASE_URL/odata/Priority/$PRIORITY_TABULA_INI/$PRIORITY_COMPANY/DOCUMENTS_D" \
  -d '{
    "CUSTNAME": "{resolved_custname}",
    "CURDATE": "{today}T00:00:00+02:00",
    "BOOKNUM": "{order_number_from_col_A}",
    "DETAILS": "{timestamp_from_col_D}",
    "TOWARHSNAME": "{resolved_warhsname}",
    "TRANSORDER_D_SUBFORM": [
      {"PARTNAME": "{resolved_partname_1}", "TQUANT": {qty_1}},
      {"PARTNAME": "{resolved_partname_2}", "TQUANT": {qty_2}}
    ]
  }'
```

**CRITICAL RULES:**
- `CUSTNAME`: the customer code from `CUSTOMERS` endpoint (NOT the Hebrew name)
- `CURDATE`: today's date in ISO format with Israel timezone `+02:00` (or `+03:00` during DST)
- `BOOKNUM`: the order number from column A — this links the Priority doc back to the Google Sheet order
- `DETAILS`: the timestamp from column D — pass as-is from the sheet
- `TOWARHSNAME`: the warehouse code from `ZANA_WARHSDES_EXT_FL` — this is the destination warehouse
- `TRANSORDER_D_SUBFORM`: array of order lines, each with `PARTNAME` (product code) and `TQUANT` (quantity)
- **Initial creation**: include all known lines in the `TRANSORDER_D_SUBFORM` array
- **Appending later**: POST individual lines to `DOCUMENTS_D(DOCNO='...',TYPE='D')/TRANSORDER_D_SUBFORM`
- Returns `DOCNO` (e.g. `SH2630000712`) — write this back to **column J** in the Google Sheet
- Write the current date/time to **column K** (when the document was created)
- Documents are created as draft (`טיוטא`)

**If `PARTNAME` is not found**, use a catch-all part code and set `PDES` to the original product text:
```json
{"PARTNAME": "000", "TQUANT": 5, "PDES": "0475 שרי (12 יח')"}
```

**Error handling:** Priority may return errors as XML-in-JSON. Extract the `text` field and write it to **column L** for all rows in the failed group:
```json
{
    "FORM": {
        "@TYPE": "LOGPART",
        "InterfaceErrors": {
            "text": "שורה 1- מק\"ט 000: אינך מורשה להקליד מק\"ט זה."
        }
    }
}
```
Parse the response: if it contains `InterfaceErrors`, extract `FORM.InterfaceErrors.text` (or `error.message` for standard OData errors) and write that string to column L.

### Endpoint 6: Append Line to Existing Document

```bash
curl -s -k \
  -u "$PRIORITY_USER:$PRIORITY_PASSWORD" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -X POST \
  "https://$PRIORITY_BASE_URL/odata/Priority/$PRIORITY_TABULA_INI/$PRIORITY_COMPANY/DOCUMENTS_D(DOCNO='{docno}',TYPE='D')/TRANSORDER_D_SUBFORM" \
  -d '{"PARTNAME": "{resolved_partname}", "TQUANT": {qty}}'
```

Use this when a delivery note already exists for the same group (A+B+C) and new lines need to be added.
Returns 201 on success.

### Endpoint 7: Verify a Created Document

```
GET .../DOCUMENTS_D?$filter=DOCNO eq '{docno}'&$expand=TRANSORDER_D_SUBFORM
```

Use this to verify the document was created correctly with all lines.

---

## Matching Logic (YOUR Intelligence)

You are the matching brain. No hardcoded mapping tables — use your Hebrew language understanding.

### Customer Matching (Column C → CUSTNAME)

1. Fetch all `CUSTOMERS` once
2. For each unique customer name in column C, find the best match in `CUSTDES`
3. Handle: abbreviations, typos, missing words, spelling variants
4. Examples: `שופרסל` matches CUSTDES containing `שופרסל`; `סטוב מרקט` → `סטופ מרקט` (typo)
5. If no match found → flag as `CUST_NOT_FOUND`
6. **Note:** some customers may not exist in Priority yet (pipeline not complete)

### Warehouse Matching (Column B → WARHSNAME)

1. Fetch all `ZANA_WARHSDES_EXT_FL` once
2. For each unique warehouse name in column B, find the best match in `WARHSDES`
3. The table already contains fuzzy variants (typos, number prefixes, etc.)
4. If column B contains a number + name (e.g. `שופרסל 39`), try matching both the full string and parts
5. If no match found → flag as `WARHS_NOT_FOUND`, still create document without `TOWARHSNAME`

### Product Matching (Column G → PARTNAME)

1. Fetch `ZANA_PARTDES_EXT_FLA` and `LOGPART` once, cache both
2. For each product in column G, extract the numeric code (e.g. `0475` from `0475 שרי (12 יח')`, or `1127` from `(1127) תפוח אדמה ורוד...`)
3. **Step 1 — ZANA_PARTDES_EXT_FLA:** try matching against `PARTDES` (fuzzy table with multiple descriptions per PARTNAME)
4. **Step 2 — LOGPART by code:** prepend `200` to the 4-digit code → try `LOGPART` (e.g. `0475` → `2000475`)
5. **Step 3 — LOGPART by name:** search `PARTDES` in LOGPART for Hebrew name match
6. **Step 4 — Claude fallback:** if Python can't match, hand off to Claude with Bash tool to search Priority creatively
7. **Step 5 — Not found:** write `PARTNAME not found: {description}` to column L

### Sanity Checks

- If a product code resolves to a completely different description, flag as suspicious
- If quantity is 0 or non-numeric, flag as `BAD_QTY`
- Skip rows with missing customer or product

---

## Execution Flow

### Step 1: Read Google Sheet

Fetch all rows from `הזמנות` tab. Also fetch `לקוחות` and `מוצרים` tabs for reference context.

### Step 2: Fetch Priority Reference Data

Fetch and cache (save to `/tmp/` as JSON files):
- `CUSTOMERS` → `/tmp/priority_customers.json`
- `ZANA_WARHSDES_EXT_FL` → `/tmp/priority_warehouses.json`
- `ZANA_PARTDES_EXT_FLA` → `/tmp/priority_products.json`
- `LOGPART?$select=PARTNAME,PARTDES` → `/tmp/priority_logpart.json`

### Step 3: Filter & Group Orders

- Skip invalid rows (missing customer/product/quantity, rows with only col A)
- Group rows by **(col A + col B + col C)** — each unique combination of order number + warehouse + customer = one delivery note
- Within each group, collect col E (quantity) and col G (product) as line items

### Step 4: Resolve Matches

For each group:
1. Resolve customer (col C → CUSTNAME via CUSTOMERS)
2. Resolve warehouse (col B → WARHSNAME via ZANA_WARHSDES_EXT_FL)
3. For each line: resolve product (col G → PARTNAME via ZANA_PARTDES_EXT_FLA)

### Step 5: Create Delivery Notes

**In DRY RUN mode:** just report what would be created. Do NOT call POST.

**In LIVE mode:** for each group (unique A+B+C):
1. POST to `DOCUMENTS_D` with `CUSTNAME` (from col C), `BOOKNUM` (col A), `DETAILS` (col D), `TOWARHSNAME` (from col B), `CURDATE`, and all lines in `TRANSORDER_D_SUBFORM` (col E + col G)
2. Get back `DOCNO` from response
3. Write `DOCNO` to **column J** for all rows in this group
4. Write the current date/time (`YYYY-MM-DD HH:MM:SS`) to **column K** for all rows in this group

### Step 6: Summary Report

Print:
- Total orders processed
- Documents created (count + list of DOCNOs)
- Lines added
- Unmatched customers (with names)
- Unmatched products (with descriptions)
- Unmatched warehouses (with names)
- Any errors

---

## Error Handling

- If a Priority API call fails, retry up to 3 times with 2s backoff
- If document creation fails, log the error, flag rows, and continue with next group
- Throttle: 0.3s pause between Priority POST calls
- If a gws call fails, retry once
- Never crash — always log and continue

---

## API Constraints (Verified on This System)

1. **Composite key required** — `DOCUMENTS_D` has a composite key: `DOCNO` + `TYPE`.
   Always use both: `DOCUMENTS_D(DOCNO='SH26...',TYPE='D')`
   Using only `DOCNO` returns 404.

2. **Appending lines to existing documents works** via subform POST:
   ```
   POST .../DOCUMENTS_D(DOCNO='SH26...',TYPE='D')/TRANSORDER_D_SUBFORM
   Content-Type: application/json
   {"PARTNAME": "2000426", "TQUANT": 1}
   ```
   Returns 201 on success.

3. **$expand works for reading** — `$expand=TRANSORDER_D_SUBFORM` works on both key access and `$filter` queries.

4. **$filter also works** for querying: `$filter=DOCNO eq 'SH26...'`

5. **Maximum 2,000 records per GET** — Priority caps results at 2,000 (MAXAPILINES). For large result sets, implement pagination.

6. **Rate limiting** — Max 100 API calls per minute, max 15 parallel requests, 3-minute timeout per request.

7. **Transaction counting** — Each POST counts as a transaction. Creating a doc with 5 lines = 6 transactions (1 parent + 5 lines).

---

## Important Notes

- The customer's Priority pipeline is still being set up — some customers may not exist yet. This is expected. Flag them and move on.
- DOCUMENTS_D initial POST can include all lines. Additional lines can be appended later via subform POST.
- **Composite key required**: always use `DOCUMENTS_D(DOCNO='...',TYPE='D')` — single key returns 404.
- Environment variables are already loaded: `$PRIORITY_BASE_URL`, `$PRIORITY_TABULA_INI`, `$PRIORITY_COMPANY`, `$PRIORITY_USER`, `$PRIORITY_PASSWORD`, `$SHEET_ID`, `$SHEET_TAB`
- Always use `-k` flag on curl (SSL cert is self-signed)
- Date format for CURDATE: `YYYY-MM-DDT00:00:00+02:00`
- Responses cap at 350MB. Blank fields return as null or empty string.
- Dates in responses use DateTimeOffset format: `YYYY-MM-DDTHH:MM:SS+HH:MM`
