"""Claude AI fallback for unresolved matching items.

Uses the Anthropic API directly (no Claude Code needed). For each unmatched
item, the Python matcher pre-ranks the top-N Priority candidates by LCS
similarity, and Claude decides: pick one, or return null. This tight shortlist
keeps the context small and eliminates the "pick any of 100 customers" failure
mode that caused גליל ירוק → גלבוע (a 22%-similarity hallucination).
"""
import json
import re

import anthropic

from lib.config import ENV
from lib.matching import rank_candidates


# How many Priority candidates to show Claude per unmatched item.
_TOP_N_CANDIDATES = 5
# Skip candidates with extremely low LCS similarity (< 0.2 = < 20%). If the top
# similarity is below this, Claude sees an empty candidate list and should return null.
_MIN_SIM = 0.2


def _customer_items(unmatched: list[str], ref: dict) -> list[dict]:
    items = []
    for name in unmatched:
        ranked = rank_candidates(name, ref.get("customers", []),
                                 "CUSTDES", top_n=_TOP_N_CANDIDATES,
                                 min_similarity=_MIN_SIM)
        items.append({
            "sheet_name": name,
            "candidates": [
                {"CUSTNAME": c["CUSTNAME"], "CUSTDES": c.get("CUSTDES", ""),
                 "similarity": f"{sim:.0%}"}
                for c, sim in ranked
            ],
        })
    return items


def _warehouse_items(unmatched: list[str], ref: dict) -> list[dict]:
    items = []
    for name in unmatched:
        ranked = rank_candidates(name, ref.get("warehouses", []),
                                 "WARHSDES", top_n=_TOP_N_CANDIDATES,
                                 min_similarity=_MIN_SIM)
        items.append({
            "sheet_name": name,
            "candidates": [
                {"WARHSNAME": w["WARHSNAME"], "WARHSDES": w.get("WARHSDES", ""),
                 "similarity": f"{sim:.0%}"}
                for w, sim in ranked
            ],
        })
    return items


def _product_items(unmatched: list[str], ref: dict) -> list[dict]:
    """Products: rank against LOGPART + fuzzy_products + all customerparts."""
    pool: list[dict] = []
    seen: set[str] = set()
    for source_key in ("logpart", "fuzzy_products"):
        for p in ref.get(source_key, []):
            pn = p.get("PARTNAME", "")
            if pn and pn not in seen:
                pool.append({"PARTNAME": pn, "PARTDES": p.get("PARTDES", "")})
                seen.add(pn)
    # Customer-specific parts may have richer descriptions
    for cp_list in ref.get("customerparts", {}).values():
        for cp in cp_list:
            pn = cp.get("PARTNAME", "")
            if not pn:
                continue
            # Build a combined description so LCS can match either the Priority
            # name or the customer-specific name from the sheet.
            combined = " ".join(filter(None, [
                cp.get("PARTDES", ""),
                cp.get("CUSTPARTDES", ""),
                cp.get("CUSTPARTNAME", ""),
            ]))
            if pn in seen:
                continue
            pool.append({"PARTNAME": pn, "PARTDES": combined})
            seen.add(pn)

    items = []
    for name in unmatched:
        ranked = rank_candidates(name, pool, "PARTDES",
                                 top_n=_TOP_N_CANDIDATES,
                                 min_similarity=_MIN_SIM)
        items.append({
            "sheet_description": name,
            "candidates": [
                {"PARTNAME": p["PARTNAME"], "PARTDES": p.get("PARTDES", ""),
                 "similarity": f"{sim:.0%}"}
                for p, sim in ranked
            ],
        })
    return items


def _build_prompt(
    unmatched_customers: list[str],
    unmatched_warehouses: list[str],
    unmatched_products: list[str],
    ref_data: dict,
) -> str:
    """Build a prompt with per-item ranked candidate shortlists."""
    sections = []

    if unmatched_customers:
        sections.append(
            "UNMATCHED CUSTOMERS — for each `sheet_name`, pick one CUSTNAME from its\n"
            "`candidates` list or return null. Candidates are pre-ranked by LCS similarity.\n\n"
            + json.dumps(_customer_items(unmatched_customers, ref_data),
                         ensure_ascii=False, indent=2)
        )

    if unmatched_warehouses:
        sections.append(
            "UNMATCHED WAREHOUSES — for each `sheet_name`, pick one WARHSNAME from its\n"
            "`candidates` list or return null.\n\n"
            + json.dumps(_warehouse_items(unmatched_warehouses, ref_data),
                         ensure_ascii=False, indent=2)
        )

    if unmatched_products:
        sections.append(
            "UNMATCHED PRODUCTS — for each `sheet_description`, pick one PARTNAME from\n"
            "its `candidates` list or return null. Products may match by code pattern\n"
            "(sheet '0475' → PARTNAME '2000475') or by Hebrew description.\n\n"
            + json.dumps(_product_items(unmatched_products, ref_data),
                         ensure_ascii=False, indent=2)
        )

    return "Resolve these unmatched items:\n\n" + "\n\n".join(sections)


SYSTEM_PROMPT = (
    "You are a matching arbiter for חממת עלים יגור (Israeli greenhouse/produce supplier).\n"
    "For each unmatched item from a Google Sheet, you receive a pre-ranked shortlist of\n"
    "Priority ERP candidates (top ~5, ranked by longest-common-substring similarity).\n"
    "Your job: pick ONE candidate code, or return null.\n\n"
    "═══ GOLDEN RULE: PREFER NULL OVER A WRONG MATCH ═══\n"
    "A wrong customer/warehouse silently ships goods to the wrong business. A null result\n"
    "triggers a human to add a manual mapping (cheap). A wrong match corrupts data (expensive).\n"
    "When in doubt → null. NEVER guess.\n\n"
    "REAL FAILURE we fixed: old prompt said 'prefer best-guess over null'. That gave\n"
    "'גליל ירוק' → 'גלבוע עבודות חקלאיות' (73030220000): they share only 'גל' (22% sim).\n"
    "Two delivery notes went to the wrong business. Don't repeat this.\n\n"
    "═══ HOW TO USE THE SIMILARITY SCORES ═══\n"
    "Each candidate has a `similarity` like '44%' or '100%'. This is a mechanical letter-overlap\n"
    "score — it is a hint, not the answer. Use Hebrew meaning to decide:\n"
    "  • High similarity (≥70%) + matching word/root → usually the right match.\n"
    "  • Medium similarity (40–70%) → ONLY match if Hebrew meaning clearly aligns.\n"
    "    (e.g. 'שופרסל 343 ראש העין' sim 35% vs 'קטיף בעמ (שופרסל)' — same word שופרסל,\n"
    "     and no other candidate has שופרסל → pick it.)\n"
    "  • Low similarity (<40%) → almost always null. Don't rescue with creative reasoning.\n"
    "  • If two candidates are semantically plausible → null (ambiguous).\n\n"
    "═══ CUSTOMERS ═══\n"
    "Strict rules. Prefer null aggressively.\n"
    "  GOOD (pick the candidate):\n"
    "    'שופרסל290'  → CUSTDES containing 'שופרסל'     (numeric suffix = branch tag, unambiguous)\n"
    "    '7144שופרסל' → CUSTDES containing 'שופרסל'     (numeric prefix, unambiguous)\n"
    "    'קארפור'     → 'קרפור' variant                 (clear Hebrew typo, same word)\n"
    "    'הייפר'      → 'היפר' variant                  (keyboard double-letter)\n"
    "  BAD (return null):\n"
    "    'גליל ירוק'      → 'גלבוע עבודות חקלאיות'       (only 2 shared letters)\n"
    "    'לב נתניה'       → 'לבנה שחר ויוחאי'            (shared 'לב' only; wrong entity)\n"
    "    'מינמרקט בקיבוץ' → any 'מיני מרקט' customer     (generic phrase, ambiguous)\n"
    "    Two שופרסל candidates, unclear which branch → null.\n\n"
    "═══ WAREHOUSES ═══\n"
    "Same strict rules as customers. Require meaningful LOCATION-name overlap (שם יישוב/סניף),\n"
    "not just letters. WARHSNAME codes are short (≤4 chars typically). If two warehouses both\n"
    "plausibly fit → null.\n\n"
    "═══ PRODUCTS ═══\n"
    "More permissive — wrong products fail visibly at picking and are easy to catch.\n"
    "  • Code pattern: sheet '0475' → PARTNAME '2000475' (prefix '200' + 4-digit code). Trust this.\n"
    "  • Otherwise Hebrew name / partial description. Produce-shop context (vegetables, herbs).\n"
    "  • If candidates list is empty or all <30% → null is fine.\n\n"
    "═══ OUTPUT ═══\n"
    "Return EXACTLY one JSON block, nothing else:\n"
    "```json\n"
    "{\n"
    '  "customers":  {"sheet_name": "CUSTNAME_or_null", ...},\n'
    '  "warehouses": {"sheet_name": "WARHSNAME_or_null", ...},\n'
    '  "products":   {"sheet_description": "PARTNAME_or_null", ...}\n'
    "}\n"
    "```\n"
    "Use the EXACT `sheet_name` / `sheet_description` strings from the input as JSON keys.\n"
    "Only return codes that appear in that item's candidate list. NEVER invent codes."
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
