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


def normalize(s: str | None) -> str:
    """Normalize a Hebrew string for comparison.

    Handles:
      - Quotes, punctuation, and brackets (" ' ״ . , ; : ! ? - ( ) [ ] { })
      - Leading/trailing digits and spaces (7144שופרסל → שופרסל, שופרסל290 → שופרסל)
      - Double Hebrew letters only (שופררסל → שופרסל, הייפר → היפר)
      - Whitespace collapse
    """
    if not s:
        return ""
    s = s.strip().lower()
    # Strip quotes, punctuation, and brackets — so "(שופרסל)" becomes "שופרסל"
    s = re.sub(r"[\"'״`.,;:!?\-()\[\]{}]", "", s)
    # Strip leading/trailing digits+spaces (branch numbers, prefix garbage)
    s = re.sub(r"^[\d\s]+", "", s)
    s = re.sub(r"[\d\s]+$", "", s)
    # Collapse double Hebrew letters only (יי→י, רר→ר). Do NOT touch Latin doubles.
    s = re.sub(r"([\u0590-\u05FF])\1", r"\1", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# Minimum normalized length for fuzzy substring matching. Shorter strings are
# too ambiguous (e.g. "קו", "מה") and false-positive easily.
_MIN_FUZZY_LEN = 3


def _disambiguate(sheet_name: str, candidates: list[dict], key: str) -> dict | None:
    """Pick a single candidate; return None if ambiguous.

    Precedence (each tier stops if it has a unique winner):
      1. Exact match on normalized key.
      2. Substring match in either direction (norm ⊆ cdes OR cdes ⊆ norm),
         requiring cdes ≥ 3 chars to avoid absurdly short substrings.
      3. If >1 candidate survives any tier → None (prefer null over wrong).
    """
    norm = normalize(sheet_name)
    if not norm or len(norm) < _MIN_FUZZY_LEN:
        return None

    # Tier 1: exact normalized match
    exacts = [c for c in candidates if normalize(c.get(key, "")) == norm]
    if len(exacts) == 1:
        return exacts[0]
    if len(exacts) > 1:
        return None  # ambiguous exact — prefer null

    # Tier 2: substring match, either direction
    matches: list[dict] = []
    seen_keys: set[str] = set()
    for c in candidates:
        cdes = normalize(c.get(key, ""))
        if not cdes or len(cdes) < _MIN_FUZZY_LEN:
            continue
        if norm in cdes or cdes in norm:
            dedup_key = c.get(key, "")
            if dedup_key not in seen_keys:
                matches.append(c)
                seen_keys.add(dedup_key)

    if len(matches) == 1:
        return matches[0]

    # Zero or multiple → null (operator should add to מיפוי)
    return None


def match_customer(sheet_name: str, customers: list[dict]) -> dict | None:
    """Match sheet customer name → Priority CUSTNAME.

    Returns {"CUSTNAME": ..., "CUSTDES": ...} or None.
    Prefers None over an ambiguous guess — rely on מיפוי for ambiguous cases.
    """
    return _disambiguate(sheet_name, customers, "CUSTDES")


def match_warehouse(sheet_name: str, warehouses: list[dict]) -> dict | None:
    """Match sheet warehouse name → Priority WARHSNAME.

    Returns {"WARHSNAME": ..., "WARHSDES": ...} or None.

    Unlike customers, Priority's ZANA_WARHSDES_EXT_FL is a fuzzy lookup
    table: ~4.5 WARHSDES rows per WARHSNAME (typo variants all point to the
    same warehouse). Three-tier matching:

      1. Exact WARHSDES match — if all matching rows share one WARHSNAME, pick it.
         (Customer-style "ambiguous → null" would wrongly reject all dup variants.)
      2. Substring WARHSDES match, same one-code-wins rule.
      3. Code fallback — extract any numeric substring from the sheet value and
         look it up as a WARHSNAME directly. Catches pure-number cells like '134'
         and prefixed cells like '914 נווה אביבים' whose normalize() strips digits.
    """
    norm = normalize(sheet_name)

    # Tier 1: exact WARHSDES match with duplicate collapse
    if norm and len(norm) >= _MIN_FUZZY_LEN:
        exacts = [w for w in warehouses if normalize(w.get("WARHSDES", "")) == norm]
        if exacts:
            codes = {w.get("WARHSNAME", "") for w in exacts}
            if len(codes) == 1:
                return exacts[0]
            # Multiple different WARHSNAMEs → genuinely ambiguous, fall through to code

    # Tier 2: substring WARHSDES match with duplicate collapse
    if norm and len(norm) >= _MIN_FUZZY_LEN:
        matches: list[dict] = []
        for w in warehouses:
            cdes = normalize(w.get("WARHSDES", ""))
            if not cdes or len(cdes) < _MIN_FUZZY_LEN:
                continue
            if norm in cdes or cdes in norm:
                matches.append(w)
        if matches:
            codes = {w.get("WARHSNAME", "") for w in matches}
            if len(codes) == 1:
                return matches[0]
            # Ambiguous across codes → fall through to code fallback

    # Tier 3: extract numeric and match against WARHSNAME directly
    m = re.search(r"\d+", sheet_name or "")
    if m:
        code = m.group(0)
        direct = [w for w in warehouses if w.get("WARHSNAME", "") == code]
        if direct:
            return direct[0]

    return None


# ── Candidate ranking (used by Claude fallback and audit) ─────────────────


def _lcs_len(a: str, b: str) -> int:
    """Length of the longest common substring between a and b (DP, rolling row)."""
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    best = 0
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        ai = a[i - 1]
        for j in range(1, n + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _tokenize(s: str) -> list[str]:
    """Split on whitespace, drop 1-char tokens and pure-digit tokens."""
    return [t for t in s.split() if len(t) >= 2 and not t.isdigit()]


def _best_token_lcs(a_toks: list[str], b_toks: list[str]) -> int:
    """Length of the longest character run shared by ANY token pair.

    Matching within tokens (not across) filters out cross-word LCS noise like
    'ל ירו' being found in both 'גליל ירוק' and 'אשל ירון רון'.
    """
    best = 0
    for ta in a_toks:
        for tb in b_toks:
            lcs = _lcs_len(ta, tb)
            if lcs > best:
                best = lcs
    return best


# Minimum within-token LCS to count as "meaningful word overlap".
_MIN_TOKEN_LCS = 3


def similarity(sheet_name: str, candidate_name: str) -> float:
    """Token-aware similarity ∈ [0, 1].

    Requires at least one pair of tokens (one from each side) to share a
    run of ≥ _MIN_TOKEN_LCS characters. Without a meaningful within-token
    overlap, returns 0 — this filters random letter collisions that used to
    inflate short junk candidates above real matches.

    Score = full-string LCS / longer-string length (penalizes length mismatch).
    """
    a = normalize(sheet_name)
    b = normalize(candidate_name)
    if not a or not b:
        return 0.0
    a_toks = _tokenize(a) or [a]
    b_toks = _tokenize(b) or [b]
    if _best_token_lcs(a_toks, b_toks) < _MIN_TOKEN_LCS:
        return 0.0
    return _lcs_len(a, b) / max(len(a), len(b))


def rank_candidates(
    sheet_name: str,
    records: list[dict],
    key: str,
    top_n: int = 5,
    min_similarity: float = 0.15,
) -> list[tuple[dict, float]]:
    """Return top-N records ranked by token-aware similarity to sheet_name.

    Args:
        sheet_name: The string from the Google Sheet to find matches for.
        records: Priority records (customers / warehouses / products).
        key: Field in each record to compare against (CUSTDES / WARHSDES / PARTDES).
        top_n: Max number of candidates to return.
        min_similarity: Skip candidates below this similarity. Note: candidates
            without any token-pair sharing ≥3 chars automatically score 0.0.

    Returns:
        [(record, similarity), ...] sorted by similarity descending.
    """
    if not normalize(sheet_name):
        return []
    scored: list[tuple[dict, float]] = []
    for r in records:
        sim = similarity(sheet_name, r.get(key, ""))
        if sim >= min_similarity:
            scored.append((r, sim))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_n]


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
