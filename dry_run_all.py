#!/usr/bin/env python3
"""
Dry-run over ALL rows in the sheet (including already-loaded ones).

Unlike `agent.py --dry-run` which only processes pending rows, this script shows
matching results for every row — so you can verify what the agent would have
done (or would do) for rows that already have a DOCNO in column J.

No writes to Priority. No writes to the sheet.

Usage:
  uv run python dry_run_all.py                # All rows
  uv run python dry_run_all.py --limit 100    # First 100 rows
  uv run python dry_run_all.py --no-claude    # Skip Claude fallback (faster, cheaper)

Output (in data/):
  dry-run-all-YYYY-MM-DD_HHMM.json  — full machine-readable
  dry-run-all-YYYY-MM-DD_HHMM.tsv   — tab-separated, opens in Excel
  dry-run-all-YYYY-MM-DD_HHMM.txt   — readable summary + unmatched lists
"""
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

from lib.config import PROJECT_DIR
from lib.sheet import gws_read
from lib.priority import fetch_customerparts, fetch_reference_data
from lib.mapping import load_mappings
from lib.matching import resolve_all, extract_product_code, normalize
from lib.claude_fallback import apply_claude_results, claude_resolve

# Distributor whose orders resolve the end-customer from col B (not col C)
# and have no destination warehouse. Keep in sync with agent.py.
GALIL_YAROK_CUSTOMER = "גליל ירוק"


ROW_LIMIT = 0
NO_CLAUDE = "--no-claude" in sys.argv
for i, arg in enumerate(sys.argv):
    if arg.startswith("--limit"):
        if "=" in arg:
            ROW_LIMIT = int(arg.split("=")[1])
        elif i + 1 < len(sys.argv):
            ROW_LIMIT = int(sys.argv[i + 1])


def parse_all_rows(all_rows: list[list[str]]) -> list[dict]:
    """Parse every row — including ones with DOCNO, exclude=N, or zero qty."""
    from lib.sheet import extract_date_key
    rows = []
    for i, row in enumerate(all_rows):
        row += [""] * (13 - len(row))
        order_num = row[0].strip()
        warehouse = row[1].strip()
        customer = row[2].strip()
        timestamp = row[3].strip()
        date_key = extract_date_key(timestamp)
        qty_raw = row[4].strip()
        product = row[6].strip()
        pack_type = row[7].strip()
        exclude = row[8].strip().upper()
        docno = row[9].strip()
        created_at = row[10].strip()
        error = row[11].strip()
        use_today = row[12].strip().upper() == "Y"

        if not customer or not product or not order_num:
            continue

        try:
            qty = float(qty_raw.replace(",", "")) if qty_raw else 0
        except ValueError:
            qty = 0

        if docno.startswith("SH"):
            status = "ALREADY_LOADED"
        elif exclude == "N":
            status = "EXCLUDED"
        elif qty <= 0:
            status = "NO_QTY"
        elif exclude == "R":
            status = "PENDING_RETRY"
        else:
            status = "PENDING"

        rows.append({
            "row": i + 1,
            "order_num": order_num,
            "warehouse": warehouse,
            "customer": customer,
            "timestamp": timestamp,
            "date_key": date_key,
            "qty": qty,
            "product": product,
            "pack_type": pack_type,
            "exclude": exclude,
            "existing_docno": docno if docno.startswith("SH") else "",
            "existing_created_at": created_at,
            "existing_error": error,
            "use_today": use_today,
            "status": status,
        })
    return rows


def _decide_action(
    r: dict, custname: str, warhsname: str, partname: str,
    existing_docno: str, is_galil_yarok: bool = False,
    chanel_y: bool = False,
) -> str:
    if r["status"] == "ALREADY_LOADED":
        return f"כבר נטען ל-{r['existing_docno']}"
    if r["status"] == "EXCLUDED":
        return "דילוג — מוחרג (I=N)"
    if r["status"] == "NO_QTY":
        return "דילוג — אין כמות"
    if not custname:
        if is_galil_yarok:
            return f"דילוג — גליל ירוק: לקוח סופי לא במיפוי ({r['warehouse']})"
        return "דילוג — לקוח לא פוענח"
    # Strict: no new-doc creation without a destination warehouse. Matches
    # agent.py's behavior — fires when col B has text but didn't resolve, OR
    # when col B is empty for a CHANEL=Y (consignment) customer.
    # Exception: גליל ירוק distributor flow intentionally has no warehouse.
    if not is_galil_yarok and not existing_docno and not warhsname and (r.get("warehouse") or chanel_y):
        if not r.get("warehouse"):
            return "דילוג — לקוח CHANEL=Y דורש מחסן (עמודה B ריקה)"
        return "דילוג — מחסן לא פוענח"
    if not partname:
        return "דילוג — מוצר לא פוענח"
    if existing_docno:
        return f"יוסף ל-{existing_docno}"
    if is_galil_yarok:
        return "ייצור תעודה חדשה (גליל ירוק, ללא מחסן)"
    return "ייצור תעודה חדשה"


async def main():
    print("=" * 70)
    print("  חממת עלים יגור — Dry Run (ALL ROWS)")
    print("=" * 70)

    print("\n1. Reading Google Sheet (tab: הזמנות)...")
    sheet = gws_read("הזמנות!A:M")
    raw = sheet.get("values", [])
    print(f"   {len(raw)} total rows")

    rows = parse_all_rows(raw)
    if ROW_LIMIT:
        rows = rows[:ROW_LIMIT]
    print(f"   {len(rows)} parseable rows" + (f" (limited to {ROW_LIMIT})" if ROW_LIMIT else ""))

    status_counts = defaultdict(int)
    for r in rows:
        status_counts[r["status"]] += 1
    for s, c in sorted(status_counts.items()):
        print(f"   {s}: {c}")

    ref_data = fetch_reference_data()

    print("\n3. Loading manual mappings (מיפוי tab)...")
    manual_maps = load_mappings()
    print(f"   {len(manual_maps['customer'])} customers, "
          f"{len(manual_maps['warehouse'])} warehouses, "
          f"{len(manual_maps['product'])} products")

    print("\n4. Matching all rows...")
    (
        customer_map, warehouse_map, product_map,
        unmatched_customers, unmatched_warehouses, unmatched_products,
    ) = resolve_all(rows, ref_data, manual_maps)

    # גליל ירוק distributor flow: col B is the real end customer, no warehouse.
    # Remove from unmatched lists so Claude doesn't hallucinate matches for them.
    unmatched_customers = [c for c in unmatched_customers if c.strip() != GALIL_YAROK_CUSTOMER]
    galil_warehouses = {r["warehouse"] for r in rows
                         if r["customer"].strip() == GALIL_YAROK_CUSTOMER and r["warehouse"]}
    non_galil_warehouses = {r["warehouse"] for r in rows
                             if r["customer"].strip() != GALIL_YAROK_CUSTOMER and r["warehouse"]}
    galil_only_warehouses = galil_warehouses - non_galil_warehouses
    unmatched_warehouses = [w for w in unmatched_warehouses if w not in galil_only_warehouses]

    # CUSTOMERPARTS search for unmatched products (same logic as agent.py)
    if unmatched_products:
        resolved_custnames = list(set(customer_map.values()))
        if resolved_custnames:
            print(f"\n5a. Searching CUSTOMERPARTS for {len(unmatched_products)} unmatched products "
                  f"across {len(resolved_custnames)} customers...")
            custparts = fetch_customerparts(resolved_custnames)
            ref_data["customerparts"] = custparts
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
                        if code and cp_code == code and partname:
                            product_map[prod_text] = partname
                            found = True
                            break
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

    # Claude fallback for anything still unresolved.
    # Warehouses are intentionally excluded — operator must add to מיפוי or
    # ZANA_WARHSDES_EXT_FL (matches agent.py behavior).
    claude_resolved = None
    if not NO_CLAUDE and (unmatched_customers or unmatched_products):
        print("\n5b. Claude API fallback for unresolved customers/products...")
        claude_resolved = await claude_resolve(
            unmatched_customers, [], unmatched_products,
            ref_data=ref_data,
        )
        unmatched_customers, _, unmatched_products = apply_claude_results(
            claude_resolved,
            customer_map, warehouse_map, product_map,
            unmatched_customers, [], unmatched_products,
            ref_data=ref_data,
        )

    # Build existing-docs map for "would append vs create" determination
    existing_docs: dict[tuple, str] = {}
    for r in rows:
        if r["existing_docno"]:
            existing_docs[(r["order_num"], r["warehouse"], r["customer"], r["date_key"])] = r["existing_docno"]

    # CHANEL lookup: customer CUSTNAME → CHANEL value (Y means warehouse mandatory)
    chanel_map = {c["CUSTNAME"]: c.get("CHANEL", "") for c in ref_data["customers"]}

    # Build per-row result records
    results = []
    for r in rows:
        is_galil_yarok = r["customer"].strip() == GALIL_YAROK_CUSTOMER
        if is_galil_yarok:
            # End customer comes from col B via מיפוי; no warehouse on the doc.
            custname = manual_maps["customer"].get(r["warehouse"], "")
            warhsname = ""
        else:
            custname = customer_map.get(r["customer"], "")
            warhsname = warehouse_map.get(r["warehouse"], "") if r["warehouse"] else ""
        partname = product_map.get(r["product"], "")
        existing_docno = existing_docs.get((r["order_num"], r["warehouse"], r["customer"], r["date_key"]), "")
        # Don't double-count "would append" for rows that are themselves already loaded
        effective_existing = "" if r["status"] == "ALREADY_LOADED" else existing_docno
        chanel_y = chanel_map.get(custname) == "Y"
        action = _decide_action(r, custname, warhsname, partname, effective_existing,
                                is_galil_yarok, chanel_y)
        results.append({
            **r,
            "matched_custname": custname,
            "matched_warhsname": warhsname,
            "matched_partname": partname,
            "action": action,
        })

    # Summary stats
    action_counts = defaultdict(int)
    for res in results:
        action_counts[res["action"].split(" —")[0].split(" ל-")[0]] += 1

    # Save outputs
    data_dir = os.path.join(PROJECT_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    json_path = os.path.join(data_dir, f"dry-run-all-{stamp}.json")
    tsv_path = os.path.join(data_dir, f"dry-run-all-{stamp}.tsv")
    txt_path = os.path.join(data_dir, f"dry-run-all-{stamp}.txt")

    # --- JSON (full) ---
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "total_rows": len(rows),
            "status_counts": dict(status_counts),
            "action_counts": dict(action_counts),
            "unique_counts": {
                "customers": len({r["customer"] for r in rows}),
                "warehouses": len({r["warehouse"] for r in rows if r["warehouse"]}),
                "products": len({r["product"] for r in rows}),
            },
            "matched_counts": {
                "customers": len(customer_map),
                "warehouses": len(warehouse_map),
                "products": len(product_map),
            },
            "customer_map": customer_map,
            "warehouse_map": warehouse_map,
            "product_map": product_map,
            "unmatched_customers": unmatched_customers,
            "unmatched_warehouses": unmatched_warehouses,
            "unmatched_products": unmatched_products,
            "claude_resolved": claude_resolved,
            "rows": results,
        }, f, ensure_ascii=False, indent=2)

    # --- TSV (Excel-friendly) ---
    tsv_cols = [
        "row", "status", "order_num", "warehouse", "matched_warhsname",
        "customer", "matched_custname", "product", "matched_partname",
        "qty", "pack_type", "exclude", "existing_docno", "existing_created_at",
        "existing_error", "use_today", "action",
    ]
    with open(tsv_path, "w", encoding="utf-8") as f:
        f.write("\t".join(tsv_cols) + "\n")
        for res in results:
            f.write("\t".join(str(res.get(k, "")) for k in tsv_cols) + "\n")

    # --- TXT (summary + unmatched + per-row) ---
    lines = []
    lines.append("=" * 80)
    lines.append(f"Dry Run (ALL ROWS) — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 80)
    lines.append(f"Total rows processed: {len(rows)}")
    lines.append("")
    lines.append("Status breakdown:")
    for s, c in sorted(status_counts.items()):
        lines.append(f"  {s:<18} {c}")
    lines.append("")
    lines.append("Action breakdown:")
    for a, c in sorted(action_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {a:<30} {c}")
    lines.append("")
    lines.append("Matching:")
    lines.append(f"  Customers:  {len(customer_map)}/{len({r['customer'] for r in rows})} matched")
    lines.append(f"  Warehouses: {len(warehouse_map)}/{len({r['warehouse'] for r in rows if r['warehouse']})} matched")
    lines.append(f"  Products:   {len(product_map)}/{len({r['product'] for r in rows})} matched")
    lines.append("")
    if unmatched_customers:
        lines.append(f"Unmatched customers ({len(unmatched_customers)}):")
        for x in unmatched_customers:
            lines.append(f"  - {x}")
        lines.append("")
    if unmatched_warehouses:
        lines.append(f"Unmatched warehouses ({len(unmatched_warehouses)}):")
        for x in unmatched_warehouses:
            lines.append(f"  - {x}")
        lines.append("")
    if unmatched_products:
        lines.append(f"Unmatched products ({len(unmatched_products)}):")
        for x in unmatched_products:
            lines.append(f"  - {x}")
        lines.append("")

    lines.append("=" * 80)
    lines.append("PER-ROW DETAIL")
    lines.append("=" * 80)
    for res in results:
        lines.append(
            f"Row {res['row']:>5} | {res['status']:<15} | Order# {res['order_num']}"
        )
        lines.append(f"  לקוח:  {res['customer']}  →  {res['matched_custname'] or '(לא פוענח)'}")
        lines.append(f"  מחסן:  {res['warehouse']}  →  {res['matched_warhsname'] or '(לא פוענח)'}")
        lines.append(f"  מוצר:  {res['product']}  →  {res['matched_partname'] or '(לא פוענח)'}")
        lines.append(f"  כמות:  {res['qty']}  |  אריזה: {res['pack_type']}")
        if res["existing_docno"]:
            lines.append(f"  DOCNO קיים: {res['existing_docno']}  (נוצר: {res['existing_created_at']})")
        if res["existing_error"]:
            lines.append(f"  שגיאה קיימת: {res['existing_error']}")
        lines.append(f"  פעולה: {res['action']}")
        lines.append("")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("\n" + "=" * 70)
    print("  DONE")
    print("=" * 70)
    print(f"  JSON:  {json_path}")
    print(f"  TSV:   {tsv_path}")
    print(f"  TXT:   {txt_path}")
    print(f"\n  Total rows: {len(rows)}")
    for s, c in sorted(status_counts.items()):
        print(f"    {s}: {c}")
    print(f"\n  Matched:   customers={len(customer_map)}  warehouses={len(warehouse_map)}  products={len(product_map)}")
    print(f"  Unmatched: customers={len(unmatched_customers)}  warehouses={len(unmatched_warehouses)}  products={len(unmatched_products)}")


if __name__ == "__main__":
    asyncio.run(main())
