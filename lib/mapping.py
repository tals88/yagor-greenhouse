"""Manual mapping table from Google Sheet 'מיפוי' tab.

The mapping tab has 3 columns:
  A: type (customer / warehouse / product)
  B: sheet_name (name as it appears in the orders sheet)
  C: priority_code (CUSTNAME / WARHSNAME / PARTNAME in Priority)

Empty priority_code means "not yet mapped" — skip it.
"""
from lib.sheet import gws_read


def load_mappings() -> dict[str, dict[str, str]]:
    """Load manual mappings from the מיפוי tab.

    Returns:
        {
            "customer":  {"שופרסל": "73193780000", ...},
            "warehouse": {"הייפר עפולה": "45", ...},
            "product":   {"באקצ'וי (10)": "2000438", ...},
        }
    """
    sheet = gws_read("מיפוי!A:C")
    rows = sheet.get("values", [])

    mappings = {"customer": {}, "warehouse": {}, "product": {}}

    for row in rows[1:]:  # skip header
        row += [""] * (3 - len(row))
        map_type = row[0].strip().lower()
        sheet_name = row[1].strip()
        priority_code = row[2].strip()

        if map_type in mappings and sheet_name and priority_code:
            mappings[map_type][sheet_name] = priority_code

    return mappings
