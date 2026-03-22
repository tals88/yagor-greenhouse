"""Customer, warehouse, and product matching logic."""
import re


def extract_product_code(product_text: str) -> str | None:
    """Extract the numeric code from a product string.

    Examples:
      '0475 שרי (12 יח')' → '0475'
      '(1127) תפוח אדמה ורוד...' → '1127'
    """
    product_text = product_text.strip()
    m = re.match(r"^\((\d+)\)", product_text)
    if m:
        return m.group(1)
    m = re.match(r"^(\d+)\s", product_text)
    if m:
        return m.group(1)
    return None


def normalize(s: str) -> str:
    """Normalize a Hebrew string for comparison."""
    return s.strip().replace('"', "").replace("'", "").replace("״", "").lower()


def match_customer(sheet_name: str, customers: list[dict]) -> dict | None:
    """Match sheet customer name → Priority CUSTNAME.

    Returns {"CUSTNAME": ..., "CUSTDES": ...} or None.
    """
    norm = normalize(sheet_name)
    for c in customers:
        if normalize(c["CUSTDES"]) == norm:
            return c
    for c in customers:
        cdes = normalize(c["CUSTDES"])
        if norm in cdes or cdes in norm:
            return c
    return None


def match_warehouse(sheet_name: str, warehouses: list[dict]) -> dict | None:
    """Match sheet warehouse name → Priority WARHSNAME.

    Returns {"WARHSNAME": ..., "WARHSDES": ...} or None.
    """
    norm = normalize(sheet_name)
    for w in warehouses:
        if normalize(w["WARHSDES"]) == norm:
            return w
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
    """Match sheet product → Priority PARTNAME.

    Resolution order:
      1. ZANA_PARTDES_EXT_FLA by description
      2. LOGPART by code pattern (200 + 4-digit code)
      3. LOGPART by name

    Returns {"PARTNAME": ..., "PARTDES": ..., "source": ...} or None.
    """
    code = extract_product_code(product_text)
    norm = normalize(product_text)

    # Step 1: ZANA exact
    for p in fuzzy_products:
        if normalize(p["PARTDES"]) == norm:
            return {**p, "source": "ZANA_exact"}
    # Step 1b: ZANA code in PARTDES
    if code:
        for p in fuzzy_products:
            if code in p["PARTDES"]:
                return {**p, "source": "ZANA_code"}

    # Step 2: LOGPART code pattern
    if code:
        logpart_code = f"200{code.zfill(4)}"
        for p in logpart:
            if p["PARTNAME"] == logpart_code:
                return {**p, "source": "LOGPART_code"}

    # Step 3: LOGPART name match
    name_only = re.sub(r"^\(?\d+\)?\s*", "", product_text).strip()
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


def resolve_all(
    orders: list[dict],
    ref_data: dict,
    manual_maps: dict | None = None,
) -> tuple[dict, dict, dict, list[str], list[str], list[str]]:
    """Run matching on all unique values from orders.

    Resolution order per item:
      1. Manual mapping (מיפוי tab) — instant, maintained by customer
      2. Python fuzzy matching — substring/code matching against Priority data
      3. Returns as unmatched — Claude fallback handles these in agent.py

    Returns:
        (customer_map, warehouse_map, product_map,
         unmatched_customers, unmatched_warehouses, unmatched_products)
    """
    if manual_maps is None:
        manual_maps = {"customer": {}, "warehouse": {}, "product": {}}

    customers = ref_data["customers"]
    warehouses = ref_data["warehouses"]
    fuzzy_products = ref_data["fuzzy_products"]
    logpart = ref_data["logpart"]

    unique_customers = {o["customer"] for o in orders}
    unique_warehouses = {o["warehouse"] for o in orders if o["warehouse"]}
    unique_products = {o["product"] for o in orders}

    # ── Customers ──
    customer_map = {}
    unmatched_customers = []
    for name in sorted(unique_customers):
        # 1. Manual mapping
        if name in manual_maps["customer"]:
            customer_map[name] = manual_maps["customer"][name]
            continue
        # 2. Python fuzzy
        m = match_customer(name, customers)
        if m:
            customer_map[name] = m["CUSTNAME"]
        else:
            unmatched_customers.append(name)

    # ── Warehouses ──
    warehouse_map = {}
    unmatched_warehouses = []
    for name in sorted(unique_warehouses):
        if name in manual_maps["warehouse"]:
            warehouse_map[name] = manual_maps["warehouse"][name]
            continue
        m = match_warehouse(name, warehouses)
        if m:
            warehouse_map[name] = m["WARHSNAME"]
        else:
            unmatched_warehouses.append(name)

    # ── Products ──
    product_map = {}
    unmatched_products = []
    for text in sorted(unique_products):
        if text in manual_maps["product"]:
            product_map[text] = manual_maps["product"][text]
            continue
        m = match_product(text, fuzzy_products, logpart)
        if m:
            product_map[text] = m["PARTNAME"]
        else:
            unmatched_products.append(text)

    print(f"   Customers: {len(customer_map)}/{len(unique_customers)} matched")
    print(f"   Warehouses: {len(warehouse_map)}/{len(unique_warehouses)} matched")
    print(f"   Products: {len(product_map)}/{len(unique_products)} matched")

    return (
        customer_map, warehouse_map, product_map,
        unmatched_customers, unmatched_warehouses, unmatched_products,
    )
