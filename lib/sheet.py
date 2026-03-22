"""Google Sheets read/write via gws CLI."""
import json
import os
import subprocess

from lib.config import ENV, PROJECT_DIR


def gws_read(sheet_range: str) -> dict:
    """Read a range from the Google Sheet."""
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


def parse_orders(all_rows: list[list[str]]) -> tuple[list[dict], dict[tuple, str]]:
    """Parse sheet rows into orders and existing DOCNO map.

    Returns:
        (orders, existing_docs)
        orders: list of order dicts ready for processing
        existing_docs: {(order_num, warehouse, customer) → DOCNO}
    """
    orders = []
    existing_docs = {}

    for i, row in enumerate(all_rows):
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

        if docno.startswith("SH"):
            existing_docs[(order_num, warehouse, customer)] = docno
            continue

        if exclude == "N":
            continue

        try:
            qty = float(qty_raw.replace(",", "")) if qty_raw else 0
        except ValueError:
            qty = 0

        if qty <= 0:
            continue

        orders.append({
            "row": i + 1,
            "order_num": order_num,
            "warehouse": warehouse,
            "customer": customer,
            "timestamp": timestamp,
            "qty": qty,
            "product": product,
            "pack_type": pack_type,
            "is_retry": exclude == "R",
        })

    return orders, existing_docs
