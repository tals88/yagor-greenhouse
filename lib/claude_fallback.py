"""Claude AI fallback for unresolved matching items.

Uses the Anthropic API directly (no Claude Code needed).
The reference data is already fetched in Python — we pass it in the prompt
and let Claude reason about Hebrew names, typos, and abbreviations.
"""
import json
import re

import anthropic

from lib.config import ENV


def _filter_candidates(unmatched: list[str], records: list[dict], key: str) -> list[dict]:
    """Pre-filter reference records to only those that might match unmatched items.

    Keeps records where any 2+ character Hebrew substring from the unmatched item
    appears in the record's description, or vice versa.
    """
    # Extract meaningful words (2+ chars) from all unmatched items
    words = set()
    for item in unmatched:
        # Strip numbers and punctuation, split into words
        clean = re.sub(r"[0-9\[\]()\"'\u200f]", " ", item)
        for w in clean.split():
            if len(w) >= 2:
                words.add(w)

    if not words:
        return records[:200]  # fallback: return first 200

    candidates = []
    for rec in records:
        desc = (rec.get(key) or "").lower()
        if any(w in desc for w in words):
            candidates.append(rec)

    # Also include records where the description contains part of any unmatched item
    for rec in records:
        if rec in candidates:
            continue
        desc = (rec.get(key) or "").lower()
        for item in unmatched:
            clean_item = re.sub(r"[0-9\[\]()\"'\u200f]", "", item).strip().lower()
            if len(clean_item) >= 2 and (clean_item in desc or desc in clean_item):
                candidates.append(rec)
                break

    return candidates if candidates else records[:100]


def _build_prompt(
    unmatched_customers: list[str],
    unmatched_warehouses: list[str],
    unmatched_products: list[str],
    ref_data: dict,
) -> str:
    """Build a prompt with unmatched items + filtered reference data."""
    sections = []

    if unmatched_customers:
        candidates = _filter_candidates(unmatched_customers, ref_data["customers"], "CUSTDES")
        cust_sample = [
            {"CUSTNAME": c["CUSTNAME"], "CUSTDES": c["CUSTDES"]}
            for c in candidates
        ]
        sections.append(
            "UNMATCHED CUSTOMERS (match to CUSTNAME):\n"
            + "\n".join(f'  - "{c}"' for c in unmatched_customers)
            + f"\n\nLikely matching candidates from Priority ({len(cust_sample)} of {len(ref_data['customers'])} total):\n"
            + json.dumps(cust_sample, ensure_ascii=False, indent=None)
        )

    if unmatched_warehouses:
        candidates = _filter_candidates(unmatched_warehouses, ref_data["warehouses"], "WARHSDES")
        warhs_sample = [
            {"WARHSNAME": w["WARHSNAME"], "WARHSDES": w["WARHSDES"]}
            for w in candidates
        ]
        sections.append(
            "UNMATCHED WAREHOUSES (match to WARHSNAME):\n"
            + "\n".join(f'  - "{w}"' for w in unmatched_warehouses)
            + f"\n\nLikely matching candidates from Priority ({len(warhs_sample)} of {len(ref_data['warehouses'])} total):\n"
            + json.dumps(warhs_sample, ensure_ascii=False, indent=None)
        )

    if unmatched_products:
        prod_sample = [
            {"PARTNAME": p["PARTNAME"], "PARTDES": p["PARTDES"]}
            for p in ref_data["logpart"]
        ]
        fuzzy_sample = [
            {"PARTNAME": p.get("PARTNAME", ""), "PARTDES": p.get("PARTDES", "")}
            for p in ref_data["fuzzy_products"]
        ]
        custparts_sample = []
        for cp_list in ref_data.get("customerparts", {}).values():
            for cp in cp_list:
                custparts_sample.append({
                    "PARTNAME": cp.get("PARTNAME", ""),
                    "PARTDES": cp.get("PARTDES", ""),
                    "CUSTPARTNAME": cp.get("CUSTPARTNAME", ""),
                    "CUSTPARTDES": cp.get("CUSTPARTDES", ""),
                })

        sections.append(
            "UNMATCHED PRODUCTS (match to PARTNAME):\n"
            + "\n".join(f'  - "{p}"' for p in unmatched_products)
            + "\n\nCode pattern: sheet code 0475 → PARTNAME 2000475 (prefix 200 + 4-digit code)\n"
            + "\nProducts in LOGPART:\n"
            + json.dumps(prod_sample, ensure_ascii=False, indent=None)
            + "\n\nProducts in fuzzy table:\n"
            + json.dumps(fuzzy_sample, ensure_ascii=False, indent=None)
            + ("\n\nCustomer-specific parts (CUSTPART_SUBFORM):\n"
               + json.dumps(custparts_sample, ensure_ascii=False, indent=None)
               if custparts_sample else "")
        )

    return "Resolve these unmatched items:\n\n" + "\n\n".join(sections)


SYSTEM_PROMPT = (
    "You are a matching assistant for חממת עלים יגור (Israeli greenhouse/produce supplier).\n"
    "You receive unmatched items from a Google Sheet and a list of available records from Priority ERP.\n"
    "Your job: find the BEST POSSIBLE match for each item using Hebrew understanding.\n\n"
    "IMPORTANT: Be aggressive about matching. Prefer returning a best-guess match over null.\n"
    "Only return null for complete garbage (random letters) or test entries (טסט).\n\n"
    "Matching strategies:\n"
    "- Customer names:\n"
    "  - Typos: קארפור→קרפור, קרפוחעעי→קרפור\n"
    "  - Numbers appended: שופרסל290→שופרסל, 7144שופרסל→שופרסל\n"
    "  - Branch names: שופרסל שהם תרשיש→שופרסל (strip the branch/location)\n"
    "  - Abbreviations: ביתן→יינות ביתן, מגה→מגה בעיר\n"
    "  - Double letters: הייפר→היפר (common Hebrew keyboard issue)\n\n"
    "- Warehouse names:\n"
    "  - IMPORTANT: WARHSNAME codes are max 4 characters (e.g. 1, 10, 215, 9117, Abc, ABCD). NEVER invent long codes.\n"
    "  - You MUST pick a WARHSNAME from the candidates list provided. Do NOT make up codes.\n"
    "  - Match by LOCATION NAME even if prefix/suffix differs\n"
    "  - הייפר עפולה → look for ANY warehouse containing עפולה\n"
    "  - היפר קרפור אשדוד → look for warehouses with אשדוד or קרפור+אשדוד\n"
    "  - Numbers alone (105, 854) → match if a warehouse has that number in WARHSNAME\n"
    "  - If no match exists in the candidates list, return null\n\n"
    "- Product names:\n"
    "  - Code pattern: sheet 0475 → PARTNAME 2000475 (prefix 200 + 4-digit code)\n"
    "  - Hebrew name matching, partial matches\n"
    "  - Produce context: this is a greenhouse selling vegetables and herbs\n\n"
    "Output EXACTLY a JSON block:\n"
    "```json\n"
    "{\n"
    '  "customers": {"sheet_name": "CUSTNAME_or_null", ...},\n'
    '  "warehouses": {"sheet_name": "WARHSNAME_or_null", ...},\n'
    '  "products": {"sheet_description": "PARTNAME_or_null", ...}\n'
    "}\n"
    "```\n"
    "CRITICAL: Only return codes that appear in the candidate lists provided.\n"
    "NEVER invent or guess codes. If you cannot find an exact match in the list, return null.\n"
    "Only output the JSON block, nothing else."
)


async def claude_resolve(
    unmatched_customers: list[str],
    unmatched_warehouses: list[str],
    unmatched_products: list[str],
    ref_data: dict | None = None,
) -> dict:
    """Call Claude API to resolve unmatched items.

    Args:
        unmatched_customers: Customer names not found in Priority
        unmatched_warehouses: Warehouse names not found in Priority
        unmatched_products: Product descriptions not found in Priority
        ref_data: Reference data dict with customers, warehouses, logpart, fuzzy_products

    Returns:
        {
            "customers": {"sheet_name": "CUSTNAME_or_null", ...},
            "warehouses": {"sheet_name": "WARHSNAME_or_null", ...},
            "products": {"sheet_description": "PARTNAME_or_null", ...}
        }
    """
    if not unmatched_customers and not unmatched_warehouses and not unmatched_products:
        return {"customers": {}, "warehouses": {}, "products": {}}

    if ref_data is None:
        ref_data = {"customers": [], "warehouses": [], "logpart": [], "fuzzy_products": []}

    api_key = ENV.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  ANTHROPIC_API_KEY not set — skipping Claude fallback")
        return {"customers": {}, "warehouses": {}, "products": {}}

    print(f"\n  Calling Claude API to resolve {len(unmatched_customers)} customers, "
          f"{len(unmatched_warehouses)} warehouses, {len(unmatched_products)} products...")

    prompt = _build_prompt(
        unmatched_customers, unmatched_warehouses, unmatched_products, ref_data
    )

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        print(f"  Claude API error: {e}")
        return {"customers": {}, "warehouses": {}, "products": {}}

    result_text = ""
    for block in response.content:
        if block.type == "text":
            result_text += block.text

    # Parse JSON from response
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


def apply_claude_results(
    claude_result: dict,
    customer_map: dict,
    warehouse_map: dict,
    product_map: dict,
    unmatched_customers: list[str],
    unmatched_warehouses: list[str],
    unmatched_products: list[str],
    ref_data: dict | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Apply Claude's resolutions to the maps. Returns updated unmatched lists.

    Validates each code against reference data to reject hallucinated codes.
    """
    # Build valid code sets for validation
    valid_customers = set()
    valid_warehouses = set()
    valid_products = set()
    if ref_data:
        valid_customers = {c["CUSTNAME"] for c in ref_data.get("customers", [])}
        valid_warehouses = {w["WARHSNAME"] for w in ref_data.get("warehouses", [])}
        valid_products = {p["PARTNAME"] for p in ref_data.get("logpart", [])}
        valid_products |= {p.get("PARTNAME", "") for p in ref_data.get("fuzzy_products", [])}

    rejected = 0

    for name, custname in claude_result.get("customers", {}).items():
        if custname:
            if valid_customers and custname not in valid_customers:
                claude_result["customers"][name] = None
                rejected += 1
                continue
            customer_map[name] = custname
            unmatched_customers = [c for c in unmatched_customers if c != name]

    for name, warhsname in claude_result.get("warehouses", {}).items():
        if warhsname:
            if len(str(warhsname)) > 4:
                claude_result["warehouses"][name] = None
                rejected += 1
                continue
            if valid_warehouses and str(warhsname) not in valid_warehouses:
                claude_result["warehouses"][name] = None
                rejected += 1
                continue
            warehouse_map[name] = warhsname
            unmatched_warehouses = [w for w in unmatched_warehouses if w != name]

    for desc, partname in claude_result.get("products", {}).items():
        if partname:
            if valid_products and partname not in valid_products:
                claude_result["products"][desc] = None
                rejected += 1
                continue
            product_map[desc] = partname
            unmatched_products = [p for p in unmatched_products if p != desc]

    if rejected:
        print(f"   Rejected {rejected} hallucinated codes (not found in Priority)")

    return unmatched_customers, unmatched_warehouses, unmatched_products
