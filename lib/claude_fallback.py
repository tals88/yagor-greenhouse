"""Claude AI fallback for unresolved matching items."""
import json
import re

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    TextBlock,
    query,
)

from lib.config import ENV, PROJECT_DIR


async def claude_resolve(
    unmatched_customers: list[str],
    unmatched_warehouses: list[str],
    unmatched_products: list[str],
) -> dict:
    """Call Claude with ONLY the unmatched items. Returns resolved mappings.

    Returns:
      {
        "customers": {"sheet_name": "CUSTNAME_or_null", ...},
        "warehouses": {"sheet_name": "WARHSNAME_or_null", ...},
        "products": {"sheet_description": "PARTNAME_or_null", ...}
      }
    """
    if not unmatched_customers and not unmatched_warehouses and not unmatched_products:
        return {"customers": {}, "warehouses": {}, "products": {}}

    sections = []
    if unmatched_customers:
        sections.append(
            "UNMATCHED CUSTOMERS (find CUSTNAME in CUSTOMERS table):\n"
            + "\n".join(f'  - "{c}"' for c in unmatched_customers)
        )
    if unmatched_warehouses:
        sections.append(
            "UNMATCHED WAREHOUSES (find WARHSNAME in ZANA_WARHSDES_EXT_FL table):\n"
            + "\n".join(f'  - "{w}"' for w in unmatched_warehouses)
        )
    if unmatched_products:
        sections.append(
            "UNMATCHED PRODUCTS (find PARTNAME — try ZANA_PARTDES_EXT_FLA, then LOGPART with code pattern 200+code, then by name):\n"
            + "\n".join(f'  - "{p}"' for p in unmatched_products)
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
) -> tuple[list[str], list[str], list[str]]:
    """Apply Claude's resolutions to the maps. Returns updated unmatched lists."""
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

    return unmatched_customers, unmatched_warehouses, unmatched_products
