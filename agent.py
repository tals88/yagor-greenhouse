#!/usr/bin/env python3
"""
חממת עלים יגור — Google Sheet → Priority Agent

Hybrid approach:
  - Python handles all mechanical work (read sheet, fetch API, group, match, POST)
  - Claude is called ONLY for unresolved items (tiny context, smart fallback)

Usage:
  uv run python agent.py              # Full execution
  uv run python agent.py --dry-run    # Preview mode (no writes)
  uv run python agent.py --test       # Write to test tab only
  uv run python agent.py --limit 50   # Process first 50 rows
"""
import asyncio
import sys
import time
from collections import defaultdict
from datetime import datetime

from lib.config import DRY_RUN, ENV, ROW_LIMIT, READ_TAB, TEST_MODE, WRITE_TAB
from lib.sheet import gws_read, gws_write_batch, parse_orders
from lib.priority import fetch_reference_data, priority_post
from lib.matching import resolve_all
from lib.claude_fallback import apply_claude_results, claude_resolve


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

    orders, existing_docs = parse_orders(all_rows)
    print(f"   {len(existing_docs)} groups with existing DOCNOs (for append)")

    if ROW_LIMIT:
        orders = orders[:ROW_LIMIT]
    print(f"   {len(orders)} valid pending orders" + (f" (limited to {ROW_LIMIT})" if ROW_LIMIT else ""))
    if not orders:
        print("\n   No pending orders to process.")
        return

    # ── Step 2: Fetch Priority reference data ─────────────────────────────
    ref_data = fetch_reference_data()

    # ── Step 3: Group by (order_num + warehouse + customer) ───────────────
    print("\n3. Grouping by (order number + warehouse + customer)...")
    groups = defaultdict(list)
    for o in orders:
        groups[(o["order_num"], o["warehouse"], o["customer"])].append(o)
    print(f"   {len(groups)} delivery notes to create")

    # ── Step 4: Python matching ───────────────────────────────────────────
    print("\n4. Matching customers, warehouses, products...")
    (
        customer_map, warehouse_map, product_map,
        unmatched_customers, unmatched_warehouses, unmatched_products,
    ) = resolve_all(orders, ref_data)

    # ── Step 5: Claude fallback for unresolved items ──────────────────────
    if unmatched_customers or unmatched_warehouses or unmatched_products:
        print("\n5. Claude fallback for unresolved items...")
        if unmatched_customers:
            print(f"   Unmatched customers: {unmatched_customers}")
        if unmatched_warehouses:
            print(f"   Unmatched warehouses: {unmatched_warehouses}")
        if unmatched_products:
            print(f"   Unmatched products: {unmatched_products}")

        claude_result = await claude_resolve(
            unmatched_customers, unmatched_warehouses, unmatched_products
        )
        unmatched_customers, unmatched_warehouses, unmatched_products = apply_claude_results(
            claude_result,
            customer_map, warehouse_map, product_map,
            unmatched_customers, unmatched_warehouses, unmatched_products,
        )

        resolved = sum(len(claude_result.get(k, {})) for k in ("customers", "warehouses", "products"))
        print(f"   Claude resolved: {resolved} items")

        if unmatched_customers or unmatched_warehouses or unmatched_products:
            print("   Still unmatched after Claude:")
            if unmatched_customers:
                print(f"     Customers: {unmatched_customers}")
            if unmatched_warehouses:
                print(f"     Warehouses: {unmatched_warehouses}")
            if unmatched_products:
                print(f"     Products: {unmatched_products}")
    else:
        print("\n5. All items matched — skipping Claude.")

    # ── Step 6: Create / append delivery notes ────────────────────────────
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

        custname = customer_map.get(cust_name)
        if not custname:
            stats["skipped_cust"] += len(order_lines)
            for o in order_lines:
                sheet_updates.append({
                    "range": f"{WRITE_TAB}!L{o['row']}",
                    "values": [[f"CUSTNAME not found: {cust_name}"]],
                })
            continue

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
                status = "APPENDED" if all_ok else "APPEND (some errors)"
                print(f"  [{idx}/{len(groups)}] {cust_name} / {warhs_name} — "
                      f"{len(valid_lines)} lines → {status} to {docno}")
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

        # Write DOCNO and timestamp back
        for l in valid_lines:
            sheet_updates.append({"range": f"{WRITE_TAB}!J{l['_row']}", "values": [[docno]]})
            sheet_updates.append({"range": f"{WRITE_TAB}!K{l['_row']}", "values": [[now]]})
            if l.get("_retry"):
                sheet_updates.append({"range": f"{WRITE_TAB}!I{l['_row']}", "values": [[""]]})
                sheet_updates.append({"range": f"{WRITE_TAB}!L{l['_row']}", "values": [[""]]})

    # ── Step 7: Write back to Google Sheet ────────────────────────────────
    if sheet_updates:
        print(f"\n7. Writing {len(sheet_updates)} cells to Google Sheet...")
        if DRY_RUN:
            print("   (dry run — skipping write)")
        else:
            for i in range(0, len(sheet_updates), 500):
                gws_write_batch(sheet_updates[i:i + 500])
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
