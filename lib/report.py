"""HTML report generation and email sending via gws CLI."""
import base64
import json
import os
import subprocess
from datetime import datetime

from lib.config import PROJECT_DIR


def _build_lookup(ref_data: dict | None) -> dict:
    """Build code→description lookup dicts from reference data."""
    if not ref_data:
        return {"customers": {}, "warehouses": {}, "products": {}}
    return {
        "customers": {
            c["CUSTNAME"]: c.get("CUSTDES", c["CUSTNAME"])
            for c in ref_data.get("customers", [])
        },
        "warehouses": {
            w["WARHSNAME"]: w.get("WARHSDES", w["WARHSNAME"])
            for w in ref_data.get("warehouses", [])
        },
        "products": {
            p["PARTNAME"]: p.get("PARTDES", p["PARTNAME"])
            for p in ref_data.get("logpart", [])
        },
    }


def generate_html_report(
    stats: dict,
    mode: str,
    unmatched_customers: list[str],
    unmatched_warehouses: list[str],
    unmatched_products: list[str],
    claude_resolved: dict | None = None,
    duration_s: float = 0,
    ref_data: dict | None = None,
    unresolved_rows_info: dict | None = None,
) -> str:
    """Generate an HTML report for the run results."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    is_dry = mode == "DRY RUN"
    mode_label = "סימולציה (ללא כתיבה לפריוריטי)" if is_dry else "טעינה אמיתית"

    total_orders = stats.get("orders", 0)
    total_skipped = stats.get("skipped_cust", 0) + stats.get("skipped_prod", 0)
    match_pct = ((total_orders - total_skipped) / total_orders * 100) if total_orders else 0
    unresolved_count = len(unmatched_customers) + len(unmatched_warehouses) + len(unmatched_products)

    duration_str = f"{duration_s:.0f} שניות" if duration_s else ""

    # Status banner
    if stats.get("errors", 0) > 0:
        status_bg = "linear-gradient(135deg, #7f1d1d, #991b1b)"
        status_text = f"הטעינה הסתיימה עם {stats['errors']} שגיאות — יש לבדוק"
    elif unresolved_count > 0:
        status_bg = "linear-gradient(135deg, #78350f, #92400e)"
        status_text = f"הטעינה הסתיימה — {unresolved_count} שמות לא מוכרים"
    else:
        status_bg = "linear-gradient(135deg, #2d6a4f, #40916c)"
        status_text = "הטעינה הסתיימה בהצלחה"

    # ── Build resolved section ──────────────────────────────────────
    lookup = _build_lookup(ref_data)
    resolved_sections = ""
    if claude_resolved:
        for category, cat_label, explanation in [
            ("customers", "לקוחות", "השם בגיליון היה שונה מהשם בפריוריטי — המערכת זיהתה את הלקוח הנכון"),
            ("warehouses", "סניפים / מחסנים", "השם בגיליון היה שונה מהשם בפריוריטי — המערכת זיהתה את המחסן הנכון"),
            ("products", "מוצרים", "השם בגיליון היה שונה מהשם בפריוריטי — המערכת זיהתה את המוצר הנכון"),
        ]:
            items = claude_resolved.get(category, {})
            resolved_items = {k: v for k, v in items.items() if v}
            if not resolved_items:
                continue

            rows = ""
            for sheet_name, priority_code in resolved_items.items():
                # Show the human-readable description, not the code
                code_str = str(priority_code)
                description = lookup.get(category, {}).get(code_str, "")
                if description and description != code_str:
                    display = _esc(description)
                else:
                    # Code not found in reference data — flag it
                    display = f'<span style="color:#b45309">{_esc(code_str)} (לא אומת)</span>'
                rows += f'<tr><td>{_esc(sheet_name)}</td><td>&#8594;</td><td>{display}</td></tr>\n'

            resolved_sections += f'''
<h3>{cat_label} ({len(resolved_items)})</h3>
<p style="color:#666; font-size:13px; margin-bottom:8px;">{explanation}</p>
<table>
<tr><th>מה כתוב בגיליון</th><th></th><th>מה שהמערכת הבינה</th></tr>
{rows}
</table>'''

    resolved_total = 0
    if claude_resolved:
        for v in claude_resolved.values():
            if isinstance(v, dict):
                resolved_total += sum(1 for val in v.values() if val)

    # ── Build unresolved section ────────────────────────────────────
    rows_info = unresolved_rows_info or {}
    unresolved_rows = ""
    for name in unmatched_customers:
        row_nums = rows_info.get(("לקוח", name), [])
        count = len(row_nums)
        examples = ", ".join(str(r) for r in row_nums[:5])
        if count > 5:
            examples += f" (+{count - 5} נוספות)"
        row_cell = f'<td style="color:#888; font-size:12px;">{count} הזמנות — שורות: {examples}</td>' if row_nums else '<td></td>'
        unresolved_rows += f'<tr><td>לקוח</td><td>{_esc(name)}</td>{row_cell}</tr>\n'
    for name in unmatched_warehouses:
        row_nums = rows_info.get(("סניף", name), [])
        count = len(row_nums)
        examples = ", ".join(str(r) for r in row_nums[:5])
        if count > 5:
            examples += f" (+{count - 5} נוספות)"
        row_cell = f'<td style="color:#888; font-size:12px;">{count} הזמנות — שורות: {examples}</td>' if row_nums else '<td></td>'
        unresolved_rows += f'<tr><td>סניף</td><td>{_esc(name)}</td>{row_cell}</tr>\n'
    for name in unmatched_products:
        row_nums = rows_info.get(("מוצר", name), [])
        count = len(row_nums)
        examples = ", ".join(str(r) for r in row_nums[:5])
        if count > 5:
            examples += f" (+{count - 5} נוספות)"
        row_cell = f'<td style="color:#888; font-size:12px;">{count} הזמנות — שורות: {examples}</td>' if row_nums else '<td></td>'
        unresolved_rows += f'<tr><td>מוצר</td><td>{_esc(name)}</td>{row_cell}</tr>\n'

    # ── Assemble HTML ───────────────────────────────────────────────
    html = f'''<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="utf-8">
<style>
body {{
  font-family: 'Segoe UI', Tahoma, Arial, sans-serif;
  direction: rtl; text-align: right;
  background: #f5f5f5; margin: 0; padding: 20px; color: #333;
}}
.container {{
  max-width: 800px; margin: 0 auto; background: #fff;
  border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); padding: 32px;
}}
h1 {{ color: #2d6a4f; border-bottom: 3px solid #2d6a4f; padding-bottom: 12px; font-size: 22px; }}
h2 {{ color: #40916c; margin-top: 28px; font-size: 17px; }}
h3 {{ color: #52796f; margin-top: 16px; font-size: 15px; }}
.stat-grid {{ display: flex; gap: 12px; margin: 16px 0; flex-wrap: wrap; }}
.stat-card {{
  flex: 1; min-width: 120px; background: linear-gradient(135deg, #d8f3dc, #b7e4c7);
  border-radius: 10px; padding: 14px; text-align: center;
}}
.stat-card.warn {{ background: linear-gradient(135deg, #ffecd2, #fcb69f); }}
.stat-card .number {{ font-size: 28px; font-weight: bold; color: #1b4332; }}
.stat-card .label {{ font-size: 12px; color: #555; margin-top: 2px; }}
.status-banner {{
  background: {status_bg}; color: white;
  padding: 16px; border-radius: 10px; text-align: center;
  margin: 16px 0; font-size: 18px;
}}
table {{ width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 13px; }}
th {{ background: #2d6a4f; color: white; padding: 8px 10px; text-align: right; }}
td {{ padding: 6px 10px; border-bottom: 1px solid #e0e0e0; }}
tr:nth-child(even) {{ background: #f8f9fa; }}
.section {{
  background: #f8f9fa; border-radius: 8px; padding: 14px; margin: 12px 0;
  border-right: 4px solid #2d6a4f;
}}
.section.warn {{ border-right-color: #f59e0b; }}
.footer {{
  margin-top: 24px; padding-top: 12px; border-top: 1px solid #ddd;
  font-size: 11px; color: #999; text-align: center;
}}
</style>
</head>
<body>
<div class="container">

<h1>&#127793; דוח טעינת הזמנות — חממת עלים יגור</h1>
<p>
  <strong>תאריך:</strong> {now} &nbsp;|&nbsp;
  <strong>מצב:</strong> {mode_label}
  {f" &nbsp;|&nbsp; <strong>משך:</strong> {duration_str}" if duration_str else ""}
</p>

<div class="status-banner">{status_text}</div>

<h2>מה קרה בטעינה הזו?</h2>
<div class="stat-grid">
  <div class="stat-card">
    <div class="number">{total_orders:,}</div>
    <div class="label">הזמנות בגיליון</div>
  </div>
  <div class="stat-card">
    <div class="number">{stats.get("created", 0):,}</div>
    <div class="label">תעודות משלוח חדשות</div>
  </div>
  <div class="stat-card">
    <div class="number">{stats.get("appended", 0):,}</div>
    <div class="label">תעודות שנוספו אליהן שורות</div>
  </div>
  <div class="stat-card">
    <div class="number">{stats.get("lines", 0):,}</div>
    <div class="label">שורות שנטענו לפריוריטי</div>
  </div>'''

    if total_skipped:
        html += f'''
  <div class="stat-card warn">
    <div class="number">{total_skipped}</div>
    <div class="label">שורות שדולגו (שם לא מוכר)</div>
  </div>'''

    if stats.get("errors", 0):
        html += f'''
  <div class="stat-card warn">
    <div class="number">{stats.get("errors", 0)}</div>
    <div class="label">שגיאות</div>
  </div>'''

    html += f'''
</div>

<div class="section">
  <p style="font-size:14px;">
    <strong>אחוז הצלחה: {match_pct:.1f}%</strong> —
    מתוך {total_orders:,} הזמנות, {total_orders - total_skipped:,} נטענו בהצלחה.
  </p>
</div>'''

    # ── Unresolved items ────────────────────────────────────────────
    if unresolved_count:
        html += f'''

<h2>&#9888; דורש טיפול — שמות לא מוכרים ({unresolved_count})</h2>
<div class="section warn">
<p style="font-size:13px; color:#92400e; margin-bottom:10px;">
  השמות הבאים מופיעים בגיליון אבל לא נמצאו בפריוריטי.
  <br>ההזמנות שלהם <strong>לא נטענו</strong>.
  <br><br>
  <strong>מה לעשות?</strong> הוסיפו שורה בטאב <strong>מיפוי</strong> עם הקוד הנכון מפריוריטי,
  או סמנו <strong>N</strong> בעמודה I כדי לדלג עליהם.
</p>
<table>
<tr><th>סוג</th><th>מה כתוב בגיליון</th><th>היכן בגיליון</th></tr>
{unresolved_rows}
</table>
</div>'''
    else:
        html += '''

<div class="section">
  <p style="font-size:14px;">&#9989; <strong>כל השמות זוהו</strong> — אין צורך בפעולה.</p>
</div>'''

    # ── Resolved items ──────────────────────────────────────────────
    if resolved_total:
        html += f'''

<h2>&#9989; זוהו אוטומטית ({resolved_total})</h2>
<div class="section">
<p style="font-size:13px; color:#666; margin-bottom:10px;">
  השמות הבאים היו כתובים בגיליון בצורה שונה מפריוריטי
  (שגיאות כתיב, קיצורים, מספרים מודבקים וכו').
  <br>המערכת זיהתה אותם וטענה אותם כרגיל.
  <br><br>
  <strong>&#128161; אם משהו פה לא נכון</strong> — הוסיפו תיקון בטאב <strong>מיפוי</strong>.
</p>
{resolved_sections}
</div>'''

    html += f'''

<div class="footer">
  דוח אוטומטי — חממת עלים יגור &nbsp;|&nbsp; {now}
</div>

</div>
</body>
</html>'''

    return html


def save_report(html: str) -> str:
    """Save HTML report to data/ directory. Returns the file path."""
    date_str = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = os.path.join(PROJECT_DIR, "data", f"report-{date_str}.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def send_report_email(html: str, emails: list[str]) -> bool:
    """Send HTML report via gws gmail. Returns True on success."""
    if not emails:
        print("   No email recipients configured — skipping send")
        return False

    to_header = ", ".join(emails)
    subject_b64 = base64.b64encode(
        f"דוח טעינת הזמנות — חממת עלים יגור {datetime.now().strftime('%d.%m.%Y %H:%M')}".encode()
    ).decode()

    raw_email = (
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"To: {to_header}\r\n"
        f"Subject: =?UTF-8?B?{subject_b64}?=\r\n"
        f"\r\n"
        f"{html}"
    )

    raw_b64 = base64.urlsafe_b64encode(raw_email.encode()).decode()
    payload = json.dumps({"raw": raw_b64})

    result = subprocess.run(
        [
            "gws", "gmail", "users", "messages", "send",
            "--params", json.dumps({"userId": "me"}),
            "--json", payload,
        ],
        capture_output=True, text=True,
        env={**os.environ, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": ".gws-config"},
        cwd=PROJECT_DIR,
    )

    if result.returncode != 0:
        error = result.stderr or result.stdout
        if "insufficientPermissions" in error or "authentication scopes" in error:
            print("   Gmail scope not configured — report saved locally only")
            print("   To enable: gws auth login -s sheets,gmail")
        elif "not been used in project" in error or "accessNotConfigured" in error:
            print("   Gmail API not enabled in GCP project — report saved locally only")
        else:
            print(f"   Email send failed: {error[:200]}")
        return False

    print(f"   Report sent to: {to_header}")
    return True


def _esc(s: str) -> str:
    """Escape HTML entities."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
