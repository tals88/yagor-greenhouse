#!/usr/bin/env python3
"""
חממת עלים יגור — Google Sheet → Priority Agent

Hybrid approach:
  - Python handles all mechanical work (read sheet, fetch API, group, match, POST)
  - Claude is called ONLY for unresolved items (tiny context, smart fallback)

Usage:
  uv run python agent.py              # Full execution
  uv run python agent.py --dry-run    # Preview mode (no writes)
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    TextBlock,
    query,
)

# ── Config ────────────────────────────────────────────────────────────────

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DRY_RUN = "--dry-run" in sys.argv

# --limit N : only process first N valid rows
ROW_LIMIT = 0
for arg in sys.argv:
    if arg.startswith("--limit"):
        if "=" in arg:
            ROW_LIMIT = int(arg.split("=")[1])
        else:
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv):
                ROW_LIMIT = int(sys.argv[idx + 1])

# --test : read from הזמנות but write to הזמנות_test (protects real data)
TEST_MODE = "--test" in sys.argv
READ_TAB = "הזמנות"
WRITE_TAB = "הזמנות_test" if TEST_MODE else "הזמנות"


def load_dotenv(path: str) -> dict[str, str]:
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


ENV = load_dotenv(os.path.join(PROJECT_DIR, ".env"))


# ── Google Sheets helpers ─────────────────────────────────────────────────

def gws_read(sheet_range: str) -> dict:
    """Read a range from the Google Sheet via gws CLI."""
    result = subprocess.run(
        [
            "gws", "sheets", "spreadsheets", "values", "get",
            "--params", json.dumps({
                "spreadsheetId": ENV["SHEET_ID"],
                "range": sheet_range,
            }),
            "--format", "json",
        ],
        capture_output=True, text=True,
        env={**os.environ, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": ".gws-config"},
        cwd=PROJECT_DIR,
    )
    if result.returncode != 0:
        print(f"  gws error: {result.stderr}")
        return {"values": []}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  gws parse error: {result.stdout[:200]}")
        return {"values": []}


def gws_write_batch(updates: list[dict]) -> None:
    """Write multiple cell updates in a single batchUpdate call."""
    if not updates:
        return
    body = json.dumps(
        {"valueInputOption": "RAW", "data": updates},
        ensure_ascii=False,
    )
    subprocess.run(
        [
            "gws", "sheets", "spreadsheets", "values", "batchUpdate",
            "--params", json.dumps({"spreadsheetId": ENV["SHEET_ID"]}),
            "--json", body,
        ],
        capture_output=True, text=True,
        env={**os.environ, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": ".gws-config"},
        cwd=PROJECT_DIR,
    )


# ── Priority API helpers ──────────────────────────────────────────────────

def priority_url(path: str, params: str = "") -> str:
    base = (
        f"https://{ENV['PRIORITY_BASE_URL']}/odata/Priority/"
        f"{ENV['PRIORITY_TABULA_INI']}/{ENV['PRIORITY_COMPANY']}"
    )
    url = f"{base}/{path}"
    if params:
        url += f"?{params}"
    return url


def priority_get(path: str, params: str = "") -> dict:
    """GET from Priority ODATA with retry."""
    url = priority_url(path, params)
    for attempt in range(3):
        result = subprocess.run(
            [
                "curl", "-s", "-k", "--connect-timeout", "30",
                "-u", f"{ENV['PRIORITY_USER']}:{ENV['PRIORITY_PASSWORD']}",
                "-H", "Accept: application/json",
                url,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  curl GET failed (attempt {attempt + 1}): {result.stderr}")
            time.sleep(2)
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"  Invalid JSON (attempt {attempt + 1}): {result.stdout[:200]}")
            time.sleep(2)
            continue
        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            print(f"  API error: {msg}")
            return {"value": []}
        return data
    return {"value": []}


def priority_post(path: str, payload: dict) -> dict | None:
    """POST to Priority ODATA with retry. Returns parsed response or None."""
    url = priority_url(path)
    body = json.dumps(payload, ensure_ascii=False)
    for attempt in range(3):
        result = subprocess.run(
            [
                "curl", "-s", "-k", "--connect-timeout", "30",
                "-u", f"{ENV['PRIORITY_USER']}:{ENV['PRIORITY_PASSWORD']}",
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json",
                "-X", "POST", "-d", body,
                url,
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    curl POST failed (attempt {attempt + 1}): {result.stderr}")
            time.sleep(2)
            continue
        try:
            resp = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"    Invalid JSON (attempt {attempt + 1}): {result.stdout[:200]}")
            time.sleep(2)
            continue
        # Check for Priority InterfaceErrors (XML-style in JSON)
        if isinstance(resp, dict) and "FORM" in resp:
            form = resp["FORM"]
            if isinstance(form, dict) and "InterfaceErrors" in form:
                err = form["InterfaceErrors"]
                text = err.get("text", str(err)) if isinstance(err, dict) else str(err)
                return {"_error": text}
        if "error" in resp:
            msg = resp["error"].get("message", str(resp["error"]))
            return {"_error": msg}
        return resp
    return {"_error": "Failed after 3 retries"}


# ── Matching helpers ──────────────────────────────────────────────────────

def extract_product_code(product_text: str) -> str | None:
    """Extract the numeric code from a product string.

    Examples:
      '0475 שרי (12 יח')' → '0475'
      '(1127) תפוח אדמה ורוד...' → '1127'
      '(2005) תפוח אדמה למיקרו (17)' → '2005'
    """
    product_text = product_text.strip()
    # Parenthesized code at the start
    m = re.match(r"^\((\d+)\)", product_text)
    if m:
        return m.group(1)
    # Leading code without parens
    m = re.match(r"^(\d+)\s", product_text)
    if m:
        return m.group(1)
    return None


def normalize(s: str) -> str:
    """Normalize a Hebrew string for comparison."""
    return s.strip().replace('"', "").replace("'", "").replace("״", "").lower()


def match_customer(sheet_name: str, customers: list[dict]) -> dict | None:
    """Try to match a sheet customer name to a Priority CUSTNAME.

    Returns {"CUSTNAME": ..., "CUSTDES": ...} or None.
    """
    norm = normalize(sheet_name)
    # Exact match on CUSTDES
    for c in customers:
        if normalize(c["CUSTDES"]) == norm:
            return c
    # Substring match (sheet name contained in CUSTDES or vice versa)
    for c in customers:
        cdes = normalize(c["CUSTDES"])
        if norm in cdes or cdes in norm:
            return c
    return None


def match_warehouse(sheet_name: str, warehouses: list[dict]) -> dict | None:
    """Try to match a sheet warehouse name to a Priority WARHSNAME.

    Returns {"WARHSNAME": ..., "WARHSDES": ...} or None.
    """
    norm = normalize(sheet_name)
    # Exact match
    for w in warehouses:
        if normalize(w["WARHSDES"]) == norm:
            return w
    # Substring match
    for w in warehouses:
        wdes = normalize(w["WARHSDES"])
        if norm in wdes or wdes in norm:
            return w
    return None


def match_product(
    product_text: str,
    fuzzy_products: list[dict],
    logpart: list[dict],
) -> dict | None:
    """Try to match a sheet product to a Priority PARTNAME.

    Resolution order:
      1. ZANA_PARTDES_EXT_FLA by description match
      2. LOGPART by code pattern (200 + 4-digit code)
      3. LOGPART by name match

    Returns {"PARTNAME": ..., "PARTDES": ..., "source": ...} or None.
    """
    code = extract_product_code(product_text)
    norm = normalize(product_text)

    # Step 1: ZANA_PARTDES_EXT_FLA — exact description match
    for p in fuzzy_products:
        if normalize(p["PARTDES"]) == norm:
            return {**p, "source": "ZANA_exact"}
    # Step 1b: ZANA — code in PARTDES
    if code:
        for p in fuzzy_products:
            if code in p["PARTDES"]:
                return {**p, "source": "ZANA_code"}

    # Step 2: LOGPART by code pattern (200 + 4-digit code)
    if code:
        padded = code.zfill(4)
        logpart_code = f"200{padded}"
        for p in logpart:
            if p["PARTNAME"] == logpart_code:
                return {**p, "source": "LOGPART_code"}

    # Step 3: LOGPART by name match
    # Strip the code prefix and parens for name matching
    name_only = re.sub(r"^\(?\d+\)?\s*", "", product_text).strip()
    # Also strip trailing parenthesized info like (12 יח')
    name_core = re.sub(r"\s*\([^)]*\)\s*$", "", name_only).strip()
    name_norm = normalize(name_core)
    if name_norm:
        for p in logpart:
            if name_norm in normalize(p["PARTDES"]):
                return {**p, "source": "LOGPART_name"}
        for p in logpart:
            if normalize(p["PARTDES"]) in name_norm:
                return {**p, "source": "LOGPART_name"}

    return None


# ── Claude fallback for unresolved items ──────────────────────────────────

async def claude_resolve(
    unmatched_customers: list[str],
    unmatched_warehouses: list[str],
    unmatched_products: list[str],
) -> dict:
    """Call Claude with ONLY the unmatched items. Returns resolved mappings.

    Returns:
      {
        "customers": {"שופרסל": "73050690000", ...},
        "warehouses": {"סניף 87": "9", ...},
        "products": {"0475 שרי (12 יח')": "2000475", ...}
      }
    """
    if not unmatched_customers and not unmatched_warehouses and not unmatched_products:
        return {"customers": {}, "warehouses": {}, "products": {}}

    sections = []
    if unmatched_customers:
        sections.append(
            "UNMATCHED CUSTOMERS (find CUSTNAME in CUSTOMERS table):\n"
            + "\n".join(f"  - \"{c}\"" for c in unmatched_customers)
        )
    if unmatched_warehouses:
        sections.append(
            "UNMATCHED WAREHOUSES (find WARHSNAME in ZANA_WARHSDES_EXT_FL table):\n"
            + "\n".join(f"  - \"{w}\"" for w in unmatched_warehouses)
        )
    if unmatched_products:
        sections.append(
            "UNMATCHED PRODUCTS (find PARTNAME — try ZANA_PARTDES_EXT_FLA, then LOGPART with code pattern 200+code, then by name):\n"
            + "\n".join(f"  - \"{p}\"" for p in unmatched_products)
        )

    api_base = (
        f"https://{ENV['PRIORITY_BASE_URL']}/odata/Priority/"
        f"{ENV['PRIORITY_TABULA_INI']}/{ENV['PRIORITY_COMPANY']}"
    )

    system_prompt = (
        "You are a matching assistant for חממת עלים יגור. "
        "Use curl to search Priority ODATA API and resolve the unmatched items below.\n\n"
        f"Priority API base: {api_base}\n"
        'Auth: -u "$PRIORITY_USER:$PRIORITY_PASSWORD"\n'
        "Always use: -k -s\n\n"
        "Available endpoints:\n"
        f"  Customers: {api_base}/CUSTOMERS?$select=CUSTNAME,CUSTDES\n"
        f"  Warehouses: {api_base}/ZANA_WARHSDES_EXT_FL\n"
        f"  Products (fuzzy): {api_base}/ZANA_PARTDES_EXT_FLA\n"
        f"  Products (full): {api_base}/LOGPART?$select=PARTNAME,PARTDES\n\n"
        "LOGPART code pattern: sheet code 0475 → PARTNAME 2000475 (prefix 200 + 4-digit code)\n\n"
        "Search creatively: try partial matches, filter with $filter contains(), "
        "or fetch and grep. Use Hebrew understanding for typos and abbreviations.\n\n"
        "When done, output EXACTLY a JSON block with your results:\n"
        "```json\n"
        "{\n"
        '  "customers": {"sheet_name": "CUSTNAME_or_null", ...},\n'
        '  "warehouses": {"sheet_name": "WARHSNAME_or_null", ...},\n'
        '  "products": {"sheet_description": "PARTNAME_or_null", ...}\n'
        "}\n"
        "```\n"
        "Use null for items you truly cannot find."
    )

    prompt = "Resolve these unmatched items:\n\n" + "\n\n".join(sections)

    print(f"\n  Calling Claude to resolve {len(unmatched_customers)} customers, "
          f"{len(unmatched_warehouses)} warehouses, {len(unmatched_products)} products...")

    # Strip CLAUDECODE env var to allow nested execution
    agent_env = {k: v for k, v in ENV.items()}
    agent_env.pop("CLAUDECODE", None)

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=["Bash"],
        permission_mode="bypassPermissions",
        model="claude-sonnet-4-5",
        cwd=PROJECT_DIR,
        env=agent_env,
        max_turns=30,
    )

    result_text = ""
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text
    except ClaudeSDKError as e:
        print(f"  Claude error: {e}")
        return {"customers": {}, "warehouses": {}, "products": {}}

    # Parse JSON from Claude's response
    m = re.search(r"```json\s*(\{.*?\})\s*```", result_text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Try parsing the whole response as JSON
    try:
        return json.loads(result_text)
    except json.JSONDecodeError:
        print("  Could not parse Claude's response")
        return {"customers": {}, "warehouses": {}, "products": {}}


# ── Main flow ─────────────────────────────────────────────────────────────

async def main():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    flags = []
    if DRY_RUN:
        flags.append("dry-run")
    if TEST_MODE:
        flags.append("test")
    if ROW_LIMIT:
        flags.append(f"limit={ROW_LIMIT}")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    print(f"{'=' * 70}")
    print(f"  חממת עלים יגור — Order Agent ({mode}){flag_str}")
    print(f"{'=' * 70}\n")

    # Validate config
    for key in ("SHEET_ID", "PRIORITY_BASE_URL", "PRIORITY_USER", "PRIORITY_PASSWORD"):
        if not ENV.get(key):
            print(f"ERROR: {key} not set in .env")
            sys.exit(1)

    # ── Step 1: Read Google Sheet ─────────────────────────────────────────
    print(f"1. Reading Google Sheet (tab: {READ_TAB})...")
    if TEST_MODE:
        print(f"   TEST MODE: writes will go to '{WRITE_TAB}' tab (not the real data)")
    sheet = gws_read(f"{READ_TAB}!A:K")
    all_rows = sheet.get("values", [])
    print(f"   {len(all_rows)} total rows")

    # Parse rows
    orders = []
    existing_docs = {}  # (order_num, warehouse, customer) → DOCNO for already-processed rows
    for i, row in enumerate(all_rows):
        # Pad to 11 columns (A-K)
        row += [""] * (11 - len(row))
        order_num = row[0].strip()
        warehouse = row[1].strip()
        customer = row[2].strip()
        timestamp = row[3].strip()
        qty_raw = row[4].strip()
        product = row[6].strip()
        pack_type = row[7].strip()
        exclude = row[8].strip().upper()
        docno = row[9].strip()

        if not customer or not product or not order_num:
            continue

        # Track existing DOCNOs for append-to-existing logic
        if docno.startswith("SH"):
            key = (order_num, warehouse, customer)
            existing_docs[key] = docno
            continue

        # Skip excluded rows (col I = N) and rows marked for retry (col I = R clears error)
        if exclude == "N":
            continue

        # Parse quantity
        try:
            qty = float(qty_raw.replace(",", "")) if qty_raw else 0
        except ValueError:
            qty = 0

        if qty <= 0:
            continue

        orders.append({
            "row": i + 1,  # 1-indexed for Google Sheets
            "order_num": order_num,
            "warehouse": warehouse,
            "customer": customer,
            "timestamp": timestamp,
            "qty": qty,
            "product": product,
            "pack_type": pack_type,
            "is_retry": exclude == "R",
        })

    print(f"   {len(existing_docs)} groups with existing DOCNOs (for append)")

    if ROW_LIMIT:
        orders = orders[:ROW_LIMIT]
    print(f"   {len(orders)} valid pending orders" + (f" (limited to {ROW_LIMIT})" if ROW_LIMIT else ""))
    if not orders:
        print("\n   No pending orders to process.")
        return

    # ── Step 2: Fetch Priority reference data ─────────────────────────────
    print("\n2. Fetching Priority reference data...")

    print("   Customers...", end=" ", flush=True)
    cust_data = priority_get("CUSTOMERS", "$select=CUSTNAME,CUSTDES")
    customers = cust_data.get("value", [])
    print(f"{len(customers)} records")

    print("   Warehouses...", end=" ", flush=True)
    warhs_data = priority_get("ZANA_WARHSDES_EXT_FL")
    warehouses = warhs_data.get("value", [])
    print(f"{len(warehouses)} records")

    print("   Products (fuzzy)...", end=" ", flush=True)
    prod_data = priority_get("ZANA_PARTDES_EXT_FLA")
    fuzzy_products = prod_data.get("value", [])
    print(f"{len(fuzzy_products)} records")

    print("   Products (LOGPART)...", end=" ", flush=True)
    logpart_data = priority_get("LOGPART", "$select=PARTNAME,PARTDES")
    logpart = logpart_data.get("value", [])
    print(f"{len(logpart)} records")

    # ── Step 3: Group by (order_num + warehouse + customer) ─────────────
    print("\n3. Grouping by (order number + warehouse + customer)...")
    groups = defaultdict(list)
    for o in orders:
        key = (o["order_num"], o["warehouse"], o["customer"])
        groups[key].append(o)
    print(f"   {len(groups)} delivery notes to create")

    # ── Step 4: Python matching ───────────────────────────────────────────
    print("\n4. Matching customers, warehouses, products...")

    # Collect unique values to match
    unique_customers = {o["customer"] for o in orders}
    unique_warehouses = {o["warehouse"] for o in orders if o["warehouse"]}
    unique_products = {o["product"] for o in orders}

    # Match customers
    customer_map = {}  # sheet_name → CUSTNAME
    unmatched_customers = []
    for name in sorted(unique_customers):
        m = match_customer(name, customers)
        if m:
            customer_map[name] = m["CUSTNAME"]
        else:
            unmatched_customers.append(name)

    # Match warehouses
    warehouse_map = {}  # sheet_name → WARHSNAME
    unmatched_warehouses = []
    for name in sorted(unique_warehouses):
        m = match_warehouse(name, warehouses)
        if m:
            warehouse_map[name] = m["WARHSNAME"]
        else:
            unmatched_warehouses.append(name)

    # Match products
    product_map = {}  # sheet_text → PARTNAME
    unmatched_products = []
    for text in sorted(unique_products):
        m = match_product(text, fuzzy_products, logpart)
        if m:
            product_map[text] = m["PARTNAME"]
        else:
            unmatched_products.append(text)

    matched_c = len(unique_customers) - len(unmatched_customers)
    matched_w = len(unique_warehouses) - len(unmatched_warehouses)
    matched_p = len(unique_products) - len(unmatched_products)
    print(f"   Customers: {matched_c}/{len(unique_customers)} matched")
    print(f"   Warehouses: {matched_w}/{len(unique_warehouses)} matched")
    print(f"   Products: {matched_p}/{len(unique_products)} matched")

    # ── Step 5: Claude fallback for unresolved items ──────────────────────
    if unmatched_customers or unmatched_warehouses or unmatched_products:
        print(f"\n5. Claude fallback for unresolved items...")
        if unmatched_customers:
            print(f"   Unmatched customers: {unmatched_customers}")
        if unmatched_warehouses:
            print(f"   Unmatched warehouses: {unmatched_warehouses}")
        if unmatched_products:
            print(f"   Unmatched products: {unmatched_products}")

        claude_result = await claude_resolve(
            unmatched_customers, unmatched_warehouses, unmatched_products
        )

        # Apply Claude's resolutions
        for name, custname in claude_result.get("customers", {}).items():
            if custname:
                customer_map[name] = custname
                unmatched_customers = [c for c in unmatched_customers if c != name]

        for name, warhsname in claude_result.get("warehouses", {}).items():
            if warhsname:
                warehouse_map[name] = warhsname
                unmatched_warehouses = [w for w in unmatched_warehouses if w != name]

        for desc, partname in claude_result.get("products", {}).items():
            if partname:
                product_map[desc] = partname
                unmatched_products = [p for p in unmatched_products if p != desc]

        resolved_c = len(claude_result.get("customers", {}))
        resolved_w = len(claude_result.get("warehouses", {}))
        resolved_p = len(claude_result.get("products", {}))
        print(f"   Claude resolved: {resolved_c} customers, {resolved_w} warehouses, {resolved_p} products")

        if unmatched_customers or unmatched_warehouses or unmatched_products:
            print(f"   Still unmatched after Claude:")
            if unmatched_customers:
                print(f"     Customers: {unmatched_customers}")
            if unmatched_warehouses:
                print(f"     Warehouses: {unmatched_warehouses}")
            if unmatched_products:
                print(f"     Products: {unmatched_products}")
    else:
        print("\n5. All items matched — skipping Claude.")

    # ── Step 6: Create / append delivery notes ───────────────────────────
    print(f"\n6. {'DRY RUN — ' if DRY_RUN else ''}Creating delivery notes...")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%dT00:00:00+02:00")
    sheet_updates = []
    stats = {
        "created": 0, "appended": 0, "lines": 0,
        "errors": 0, "skipped_cust": 0, "skipped_prod": 0,
    }

    for idx, ((order_num, warhs_name, cust_name), order_lines) in enumerate(sorted(groups.items()), 1):
        timestamp = order_lines[0]["timestamp"]
        group_key = (order_num, warhs_name, cust_name)

        # Resolve customer
        custname = customer_map.get(cust_name)
        if not custname:
            stats["skipped_cust"] += len(order_lines)
            for o in order_lines:
                sheet_updates.append({
                    "range": f"{WRITE_TAB}!L{o['row']}",
                    "values": [[f"CUSTNAME not found: {cust_name}"]],
                })
            continue

        # Resolve warehouse
        towarhsname = warehouse_map.get(warhs_name)

        # Build line items
        line_items = []
        for o in order_lines:
            partname = product_map.get(o["product"])
            if partname:
                line_items.append({
                    "PARTNAME": partname, "TQUANT": o["qty"],
                    "_row": o["row"], "_retry": o.get("is_retry", False),
                })
            else:
                stats["skipped_prod"] += 1
                sheet_updates.append({
                    "range": f"{WRITE_TAB}!L{o['row']}",
                    "values": [[f"PARTNAME not found: {o['product']}"]],
                })

        valid_lines = [l for l in line_items if l.get("PARTNAME")]
        if not valid_lines:
            print(f"  [{idx}/{len(groups)}] {cust_name} / {warhs_name} — all products unresolved, skipping")
            continue

        # Check if a document already exists for this group
        existing_docno = existing_docs.get(group_key)

        if existing_docno:
            # ── Append to existing document ───────────────────────
            if DRY_RUN:
                print(f"  [{idx}/{len(groups)}] {cust_name} / {warhs_name} — "
                      f"{len(valid_lines)} lines → APPEND to {existing_docno} (dry run)")
                docno = existing_docno
                stats["appended"] += 1
                stats["lines"] += len(valid_lines)
            else:
                subform_url = f"DOCUMENTS_D(DOCNO='{existing_docno}',TYPE='D')/TRANSORDER_D_SUBFORM"
                all_ok = True
                for l in valid_lines:
                    resp = priority_post(subform_url, {
                        "PARTNAME": l["PARTNAME"], "TQUANT": l["TQUANT"],
                    })
                    if resp and "_error" not in resp:
                        stats["lines"] += 1
                    else:
                        error_msg = resp.get("_error", "Unknown error") if resp else "No response"
                        sheet_updates.append({
                            "range": f"{WRITE_TAB}!L{l['_row']}",
                            "values": [[error_msg]],
                        })
                        stats["errors"] += 1
                        all_ok = False
                    time.sleep(0.3)
                docno = existing_docno
                if all_ok:
                    print(f"  [{idx}/{len(groups)}] {cust_name} / {warhs_name} — "
                          f"{len(valid_lines)} lines → APPENDED to {docno}")
                else:
                    print(f"  [{idx}/{len(groups)}] {cust_name} / {warhs_name} — "
                          f"APPEND to {docno} (some errors)")
                stats["appended"] += 1
        else:
            # ── Create new document ───────────────────────────────
            payload = {
                "CUSTNAME": custname,
                "CURDATE": today,
                "BOOKNUM": order_num,
                "DETAILS": timestamp,
                "TRANSORDER_D_SUBFORM": [
                    {"PARTNAME": l["PARTNAME"], "TQUANT": l["TQUANT"]}
                    for l in valid_lines
                ],
            }
            if towarhsname:
                payload["TOWARHSNAME"] = towarhsname

            if DRY_RUN:
                docno = f"DRY-{idx:04d}"
                print(f"  [{idx}/{len(groups)}] {cust_name} / {warhs_name} — "
                      f"{len(valid_lines)} lines → {docno} (dry run)")
                stats["created"] += 1
                stats["lines"] += len(valid_lines)
            else:
                resp = priority_post("DOCUMENTS_D", payload)
                if resp and "_error" not in resp and "DOCNO" in resp:
                    docno = resp["DOCNO"]
                    print(f"  [{idx}/{len(groups)}] {cust_name} / {warhs_name} — "
                          f"{len(valid_lines)} lines → {docno}")
                    stats["created"] += 1
                    stats["lines"] += len(valid_lines)
                else:
                    error_msg = resp.get("_error", "Unknown error") if resp else "No response"
                    print(f"  [{idx}/{len(groups)}] {cust_name} / {warhs_name} — ERROR: {error_msg}")
                    stats["errors"] += 1
                    for o in order_lines:
                        sheet_updates.append({
                            "range": f"{WRITE_TAB}!L{o['row']}",
                            "values": [[error_msg]],
                        })
                    continue
                time.sleep(0.3)

        # Write DOCNO and timestamp back for all valid lines
        for l in valid_lines:
            sheet_updates.append({
                "range": f"{WRITE_TAB}!J{l['_row']}",
                "values": [[docno]],
            })
            sheet_updates.append({
                "range": f"{WRITE_TAB}!K{l['_row']}",
                "values": [[now]],
            })
            # If this was a retry (col I = R), clear the R flag and the old error
            if l.get("_retry"):
                sheet_updates.append({
                    "range": f"{WRITE_TAB}!I{l['_row']}",
                    "values": [[""]],
                })
                sheet_updates.append({
                    "range": f"{WRITE_TAB}!L{l['_row']}",
                    "values": [[""]],
                })

    # ── Step 7: Write back to Google Sheet ────────────────────────────────
    if sheet_updates:
        print(f"\n7. Writing {len(sheet_updates)} cells to Google Sheet...")
        if DRY_RUN:
            print(f"   (dry run — skipping write)")
        else:
            # Chunk into batches of 500
            for i in range(0, len(sheet_updates), 500):
                chunk = sheet_updates[i:i + 500]
                gws_write_batch(chunk)
                if i + 500 < len(sheet_updates):
                    time.sleep(1)
            print("   Done.")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  SUMMARY ({mode})")
    print(f"{'=' * 70}")
    print(f"  Orders processed:       {len(orders)}")
    print(f"  Delivery notes created: {stats['created']}")
    print(f"  Delivery notes appended:{stats['appended']}")
    print(f"  Lines added:            {stats['lines']}")
    print(f"  Document errors:        {stats['errors']}")
    print(f"  Skipped (no customer):  {stats['skipped_cust']}")
    print(f"  Skipped (no product):   {stats['skipped_prod']}")
    if unmatched_customers:
        print(f"  Unresolved customers:   {', '.join(unmatched_customers)}")
    if unmatched_warehouses:
        print(f"  Unresolved warehouses:  {', '.join(unmatched_warehouses)}")
    if unmatched_products:
        print(f"  Unresolved products:    {', '.join(unmatched_products)}")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
