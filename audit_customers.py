#!/usr/bin/env python3
"""
Audit delivery notes for customer mismatches.

For every DOCNO in the הזמנות sheet, fetch the document from Priority, compare
the Priority customer (CUSTNAME / CUSTDES) against the sheet customer name
(column C), and flag any doc where the two share little/no Hebrew content.

Catches past Claude-fallback hallucinations (e.g. גליל ירוק → גלבוע עבודות חקלאיות)
that would otherwise go unnoticed.

Output:
  - Console summary of flagged docs
  - data/audit-customers-YYYY-MM-DD_HHMM.json with full details

Usage:
  uv run python audit_customers.py
  uv run python audit_customers.py --threshold 0.6   # tighter threshold
  uv run python audit_customers.py --limit 50        # first 50 docs only
"""
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from lib.config import PROJECT_DIR
from lib.mapping import load_mappings
from lib.matching import normalize
from lib.priority import priority_get
from lib.sheet import gws_read


def _longest_common_substring_len(a: str, b: str) -> int:
    """Length of the longest common substring between a and b."""
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    # Rolling-row DP to keep memory small
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


def similarity(sheet_name: str, priority_name: str) -> float:
    """Similarity based on longest common substring / shorter-string length.

    Returns 0.0 if either is empty. 1.0 means one is fully contained in the other.
    """
    a = normalize(sheet_name)
    b = normalize(priority_name)
    if not a or not b:
        return 0.0
    lcs = _longest_common_substring_len(a, b)
    return lcs / min(len(a), len(b))


def _parse_args(argv: list[str]) -> tuple[float, int]:
    threshold = 0.5
    limit = 0
    i = 1
    while i < len(argv):
        if argv[i] == "--threshold" and i + 1 < len(argv):
            threshold = float(argv[i + 1]); i += 2
        elif argv[i].startswith("--threshold="):
            threshold = float(argv[i].split("=", 1)[1]); i += 1
        elif argv[i] == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1]); i += 2
        elif argv[i].startswith("--limit="):
            limit = int(argv[i].split("=", 1)[1]); i += 1
        else:
            i += 1
    return threshold, limit


def main() -> int:
    threshold, limit = _parse_args(sys.argv)
    print(f"Customer-mismatch audit (threshold={threshold:.0%}"
          + (f", limit={limit}" if limit else "") + ")\n")

    # ── 1. Read sheet ─────────────────────────────────────────────────────
    print("1. Reading הזמנות...")
    sheet = gws_read("הזמנות!A:M")
    rows = sheet.get("values", [])

    # {docno: {sheet_customer, sheet_warehouse, rows[]}}
    docs: dict[str, dict] = {}
    for i, row in enumerate(rows):
        row = row + [""] * (13 - len(row))
        docno = row[9].strip()
        customer = row[2].strip()
        warehouse = row[1].strip()
        if not docno.startswith("SH") or not customer:
            continue
        entry = docs.setdefault(docno, {
            "sheet_customer": customer,
            "sheet_warehouse": warehouse,
            "rows": [],
        })
        entry["rows"].append(i + 1)

    print(f"   {len(docs)} unique DOCNOs to audit")
    if limit:
        docs = dict(list(docs.items())[:limit])
        print(f"   (limited to {len(docs)})")

    # ── 2. Fetch Priority customer list for CUSTDES lookup ────────────────
    print("\n2. Fetching Priority customer list...")
    cust_list = priority_get(
        "CUSTOMERS", "$select=CUSTNAME,CUSTDES&$top=5000"
    ).get("value", [])
    custdes_by_name = {c["CUSTNAME"]: c.get("CUSTDES") or "" for c in cust_list}
    print(f"   {len(custdes_by_name)} customers cached")

    # ── 2b. Load the operator's manual מיפוי (trusted overrides) ──────────
    print("\n2b. Loading מיפוי tab...")
    manual = load_mappings().get("customer", {})
    print(f"    {len(manual)} manual customer mappings (trusted)")

    # ── 3. Parallel fetch of each DOCUMENTS_D ─────────────────────────────
    print(f"\n3. Fetching {len(docs)} documents from Priority (parallel)...")

    def fetch(docno: str) -> tuple[str, dict]:
        resp = priority_get(
            f"DOCUMENTS_D(DOCNO='{docno}',TYPE='D')",
            "$select=DOCNO,CUSTNAME,CURDATE,BOOKNUM",
        )
        return docno, resp

    results: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch, dn) for dn in docs]
        for fut in as_completed(futures):
            docno, resp = fut.result()
            results[docno] = resp
            done += 1
            if done % 50 == 0 or done == len(docs):
                print(f"   {done}/{len(docs)}")

    # ── 4. Compare & flag ─────────────────────────────────────────────────
    print("\n4. Comparing sheet customers vs Priority customers...")

    flagged: list[dict] = []
    missing: list[str] = []
    grouped_by_sheet_cust: dict[str, list[dict]] = defaultdict(list)

    for docno, info in docs.items():
        resp = results.get(docno, {})
        p_custname = (resp.get("CUSTNAME") or "").strip()
        if not p_custname:
            missing.append(docno)
            continue
        p_custdes = custdes_by_name.get(p_custname, "")
        sim = similarity(info["sheet_customer"], p_custdes)

        # Trusted if the sheet_customer is manually mapped AND the manual code
        # equals the Priority CUSTNAME on the doc. That's the operator's
        # explicit decision — not a hallucination.
        manual_code = manual.get(info["sheet_customer"])
        trusted_manual = bool(manual_code) and manual_code == p_custname
        # Also trust exact CUSTNAME match (i.e. sheet literally has the code).
        trusted_exact_code = info["sheet_customer"] == p_custname

        record = {
            "docno": docno,
            "sheet_customer": info["sheet_customer"],
            "sheet_warehouse": info["sheet_warehouse"],
            "priority_custname": p_custname,
            "priority_custdes": p_custdes,
            "similarity": round(sim, 3),
            "trusted": trusted_manual or trusted_exact_code,
            "trust_reason": (
                "manual-map" if trusted_manual
                else "exact-code" if trusted_exact_code
                else ""
            ),
            "row_count": len(info["rows"]),
            "first_row": info["rows"][0],
        }
        grouped_by_sheet_cust[info["sheet_customer"]].append(record)
        if sim < threshold and not record["trusted"]:
            flagged.append(record)

    flagged.sort(key=lambda r: r["similarity"])

    # ── 5. Print summary ──────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    print(f"  AUDIT SUMMARY")
    print(f"{'=' * 80}")
    print(f"  DOCNOs in sheet:          {len(docs)}")
    print(f"  Docs fetched from Priority: {len(docs) - len(missing)}")
    if missing:
        print(f"  Docs NOT found in Priority: {len(missing)} "
              f"(first: {', '.join(missing[:5])}{'...' if len(missing) > 5 else ''})")
    print(f"  Flagged (sim < {threshold:.0%}):        {len(flagged)}")
    print()

    # Roll-up: which sheet customers are systematically mismatched?
    # Only count UNTRUSTED low-similarity records — manual מיפוי overrides are fine.
    problem_customers: dict[str, dict] = {}
    for sc, recs in grouped_by_sheet_cust.items():
        bad = [r for r in recs if r["similarity"] < threshold and not r["trusted"]]
        if bad:
            problem_customers[sc] = {
                "docs_bad": len(bad),
                "docs_total": len(recs),
                "priority_targets": sorted(
                    {(r["priority_custname"], r["priority_custdes"]) for r in bad}
                ),
            }

    if problem_customers:
        print(f"  Sheet customers with at least one bad mapping ({len(problem_customers)}):")
        for sc, info in sorted(problem_customers.items(),
                               key=lambda kv: -kv[1]["docs_bad"]):
            targets = ", ".join(f"{cn} {cd!r}" for cn, cd in info["priority_targets"])
            print(f"    {sc!r:30} — {info['docs_bad']}/{info['docs_total']} bad → {targets}")
        print()

    if flagged:
        print(f"  First {min(25, len(flagged))} flagged docs (lowest similarity):")
        print(f"  {'DOCNO':<15} {'sim':>5}  {'sheet customer':<25} → {'priority'}")
        for r in flagged[:25]:
            sc = r["sheet_customer"][:25]
            p = f"{r['priority_custname']} {r['priority_custdes']!r}"[:60]
            print(f"  {r['docno']:<15} {r['similarity']:>5.0%}  {sc:<25} → {p}  (row {r['first_row']}, {r['row_count']}x)")
        if len(flagged) > 25:
            print(f"  ... and {len(flagged) - 25} more in the JSON output")

    # ── 6. Save JSON ──────────────────────────────────────────────────────
    out_dir = os.path.join(PROJECT_DIR, "data")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = os.path.join(out_dir, f"audit-customers-{stamp}.json")
    with open(out_path, "w") as f:
        json.dump({
            "threshold": threshold,
            "total_docs": len(docs),
            "missing_from_priority": missing,
            "flagged_count": len(flagged),
            "problem_customers": problem_customers,
            "flagged": flagged,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  Saved detailed report → {out_path}")

    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
