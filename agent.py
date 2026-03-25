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
import time as time_mod
from collections import defaultdict
from datetime import datetime

from lib.config import DRY_RUN, ENV, ROW_LIMIT, READ_TAB, TEST_MODE, WRITE_TAB
from lib import db
from lib.sheet import find_active_tab, gws_read, gws_write_batch, parse_orders
from lib.priority import fetch_customerparts, fetch_reference_data, priority_post
from lib.mapping import load_mappings
from lib.matching import resolve_all
from lib.claude_fallback import apply_claude_results, claude_resolve
from lib.report import generate_html_report, save_report, send_report_email


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

    t_start = time_mod.monotonic()

    # ── Step 1: Read Google Sheet ─────────────────────────────────────────
    active_tab = find_active_tab()
    read_tab = active_tab
    write_tab = WRITE_TAB if TEST_MODE else active_tab
    print(f"1. Reading Google Sheet (tab: {read_tab})...")
    if TEST_MODE:
        print(f"   TEST MODE: writes will go to '{write_tab}' tab (not the real data)")
    elif read_tab != READ_TAB:
        print(f"   Auto-detected active tab: {read_tab}")
    sheet = gws_read(f"{read_tab}!A:K")
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

    # ── Step 4: Load manual mappings + Python matching ──────────────────
    print("\n4. Loading manual mappings (מיפוי tab)...")
    manual_maps = load_mappings()
    print(f"   {len(manual_maps['customer'])} customers, "
          f"{len(manual_maps['warehouse'])} warehouses, "
          f"{len(manual_maps['product'])} products")

    print("\n   Matching customers, warehouses, products...")
    (
        customer_map, warehouse_map, product_map,
        unmatched_customers, unmatched_warehouses, unmatched_products,
    ) = resolve_all(orders, ref_data, manual_maps)

    # ── Step 5: CUSTOMERPARTS search for unmatched products ────────────────
    if unmatched_products:
        # Get the resolved customer codes to search their product lists
        resolved_custnames = list(set(customer_map.values()))
        if resolved_custnames:
            print(f"\n5a. Searching CUSTOMERPARTS for {len(unmatched_products)} unmatched products "
                  f"across {len(resolved_custnames)} customers...")
            custparts = fetch_customerparts(resolved_custnames)
            ref_data["customerparts"] = custparts
            total_parts = sum(len(v) for v in custparts.values())
            print(f"    Fetched {total_parts} customer-specific parts")

            # Try matching unmatched products against CUSTOMERPARTS
            from lib.matching import extract_product_code, normalize
            still_unmatched = []
            for prod_text in unmatched_products:
                code = extract_product_code(prod_text)
                norm = normalize(prod_text)
                found = False
                for cp_list in custparts.values():
                    for cp in cp_list:
                        cp_code = cp.get("CUSTPARTNAME", "")
                        cp_name = normalize(cp.get("CUSTPARTDES", ""))
                        partname = cp.get("PARTNAME", "")
                        # Match by customer part code
                        if code and cp_code == code and partname:
                            product_map[prod_text] = partname
                            found = True
                            break
                        # Match by name
                        name_only = normalize(prod_text.split(" ", 1)[-1]) if " " in prod_text else norm
                        if name_only and cp_name and (name_only in cp_name or cp_name in name_only) and partname:
                            product_map[prod_text] = partname
                            found = True
                            break
                    if found:
                        break
                if not found:
                    still_unmatched.append(prod_text)

            newly_matched = len(unmatched_products) - len(still_unmatched)
            if newly_matched:
                print(f"    CUSTOMERPARTS resolved {newly_matched} products")
            unmatched_products = still_unmatched

    # ── Step 5b: Claude fallback for remaining unresolved items ──────────
    if unmatched_customers or unmatched_warehouses or unmatched_products:
        print(f"\n5b. Claude API fallback for unresolved items...")
        if unmatched_customers:
            print(f"   Unmatched customers: {unmatched_customers}")
        if unmatched_warehouses:
            print(f"   Unmatched warehouses: {unmatched_warehouses}")
        if unmatched_products:
            print(f"   Unmatched products: {unmatched_products}")

        claude_result = await claude_resolve(
            unmatched_customers, unmatched_warehouses, unmatched_products,
            ref_data=ref_data,
        )
        unmatched_customers, unmatched_warehouses, unmatched_products = apply_claude_results(
            claude_result,
            customer_map, warehouse_map, product_map,
            unmatched_customers, unmatched_warehouses, unmatched_products,
            ref_data=ref_data,
        )

        resolved = sum(len(v) for v in claude_result.values() if isinstance(v, dict))
        print(f"   Claude resolved: {resolved} items")

        if unmatched_customers or unmatched_warehouses or unmatched_products:
            print("   Still unmatched after Claude:")
            if unmatched_customers:
                print(f"     Customers: {unmatched_customers}")
            if unmatched_warehouses:
                print(f"     Warehouses: {unmatched_warehouses}")
            if unmatched_products:
                print(f"     Products: {unmatched_products}")
        claude_resolved = claude_result
    else:
        print("\n5. All items matched — skipping Claude.")
        claude_resolved = None

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
                    "range": f"{write_tab}!L{o['row']}",
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
                    "range": f"{write_tab}!L{o['row']}",
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
                            "range": f"{write_tab}!L{l['_row']}",
                            "values": [[error_msg]],
                        })
                        stats["errors"] += 1
                        all_ok = False
                    time_mod.sleep(0.3)
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
                            "range": f"{write_tab}!L{o['row']}",
                            "values": [[error_msg]],
                        })
                    continue
                time_mod.sleep(0.3)

        # Write DOCNO and timestamp back
        for l in valid_lines:
            sheet_updates.append({"range": f"{write_tab}!J{l['_row']}", "values": [[docno]]})
            sheet_updates.append({"range": f"{write_tab}!K{l['_row']}", "values": [[now]]})
            if l.get("_retry"):
                sheet_updates.append({"range": f"{write_tab}!I{l['_row']}", "values": [[""]]})
                sheet_updates.append({"range": f"{write_tab}!L{l['_row']}", "values": [[""]]})

    # ── Step 7: Write back to Google Sheet ────────────────────────────────
    if sheet_updates:
        print(f"\n7. Writing {len(sheet_updates)} cells to Google Sheet...")
        if DRY_RUN:
            print("   (dry run — skipping write)")
        else:
            for i in range(0, len(sheet_updates), 500):
                gws_write_batch(sheet_updates[i:i + 500])
                if i + 500 < len(sheet_updates):
                    time_mod.sleep(1)
            print("   Done.")

    # ── Save run to DB ────────────────────────────────────────────────────
    duration = time_mod.monotonic() - t_start
    run_id = db.start_run(mode, read_tab)
    unresolved_data = {}
    if unmatched_customers:
        unresolved_data["customers"] = unmatched_customers
    if unmatched_warehouses:
        unresolved_data["warehouses"] = unmatched_warehouses
    if unmatched_products:
        unresolved_data["products"] = unmatched_products
    db.finish_run(
        run_id,
        status="error" if stats["errors"] > 0 else "ok",
        stats={**stats, "orders": len(orders)},
        unresolved=unresolved_data or None,
        duration_s=duration,
    )

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

    # ── Step 8: Generate and send report ────────────────────────────────
    print(f"\n8. Generating report...")

    # Collect example rows for unresolved items (for debugging in report)
    unresolved_rows_info = {}
    for o in orders:
        if o["customer"] in unmatched_customers:
            key = ("לקוח", o["customer"])
            if key not in unresolved_rows_info:
                unresolved_rows_info[key] = []
            unresolved_rows_info[key].append(o["row"])
        if o["warehouse"] in unmatched_warehouses:
            key = ("סניף", o["warehouse"])
            if key not in unresolved_rows_info:
                unresolved_rows_info[key] = []
            unresolved_rows_info[key].append(o["row"])
        if o["product"] in unmatched_products:
            key = ("מוצר", o["product"])
            if key not in unresolved_rows_info:
                unresolved_rows_info[key] = []
            unresolved_rows_info[key].append(o["row"])

    report_html = generate_html_report(
        stats={**stats, "orders": len(orders)},
        mode=mode,
        unmatched_customers=unmatched_customers,
        unmatched_warehouses=unmatched_warehouses,
        unmatched_products=unmatched_products,
        claude_resolved=claude_resolved,
        duration_s=duration,
        ref_data=ref_data,
        unresolved_rows_info=unresolved_rows_info,
    )
    report_path = save_report(report_html)
    print(f"   Saved to {report_path}")

    emails_str = db.get_setting("REPORT_EMAILS")
    if emails_str:
        emails = [e.strip() for e in emails_str.split(",") if e.strip()]
        if emails:
            print(f"   Sending to {len(emails)} recipients...")
            send_report_email(report_html, emails)


if __name__ == "__main__":
    asyncio.run(main())
