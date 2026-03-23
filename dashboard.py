#!/usr/bin/env python3
"""
Dashboard — lightweight web UI for the order agent.

Serves on localhost:8080. No external dependencies.

Usage:
  uv run python dashboard.py
  uv run python dashboard.py --port 9090
"""
import json
import os
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from lib import db
from lib.config import ENV

PORT = 8080
for i, arg in enumerate(sys.argv):
    if arg == "--port" and i + 1 < len(sys.argv):
        PORT = int(sys.argv[i + 1])


HTML = """<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>חממת עלים יגור — Agent Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 20px; }
  .container { max-width: 900px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 20px; color: #38bdf8; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  .card { background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }
  .card-full { grid-column: 1 / -1; }
  .card h2 { font-size: 0.85rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px;
             margin-bottom: 12px; }
  .stat { font-size: 2rem; font-weight: 700; }
  .stat-label { font-size: 0.8rem; color: #64748b; margin-top: 4px; }
  .status-ok { color: #4ade80; }
  .status-error { color: #f87171; }
  .status-running { color: #facc15; }
  .status-waiting { color: #94a3b8; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: right; color: #94a3b8; padding: 8px 12px; border-bottom: 1px solid #334155;
       font-weight: 500; }
  td { padding: 8px 12px; border-bottom: 1px solid #1e293b; }
  tr:hover td { background: #334155; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem;
           font-weight: 600; }
  .badge-ok { background: #064e3b; color: #4ade80; }
  .badge-error { background: #450a0a; color: #f87171; }
  .badge-running { background: #422006; color: #facc15; }
  .settings-form { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px;
                   align-items: center; }
  .settings-form label { font-size: 0.85rem; color: #94a3b8; }
  .settings-form input { background: #0f172a; border: 1px solid #475569; border-radius: 6px;
                         padding: 6px 10px; color: #e2e8f0; font-size: 0.85rem; }
  .btn { background: #2563eb; color: white; border: none; border-radius: 6px; padding: 8px 16px;
         cursor: pointer; font-size: 0.85rem; font-weight: 500; }
  .btn:hover { background: #1d4ed8; }
  .btn-green { background: #059669; }
  .btn-green:hover { background: #047857; }
  .btn-sm { padding: 4px 12px; font-size: 0.8rem; }
  .actions { display: flex; gap: 8px; margin-top: 16px; }
  .mono { font-family: 'Courier New', monospace; font-size: 0.8rem; }
  .unresolved-list { list-style: none; padding: 0; }
  .unresolved-list li { padding: 4px 0; font-size: 0.85rem; color: #fbbf24; }
  .unresolved-list li::before { content: "⚠ "; }
  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="container">
  <h1>🌿 חממת עלים יגור — Agent Dashboard</h1>

  <div class="grid">
    <div class="card" id="status-card">
      <h2>סטטוס</h2>
      <div class="stat" id="status-text">טוען...</div>
      <div class="stat-label" id="status-detail"></div>
    </div>
    <div class="card">
      <h2>ריצה אחרונה</h2>
      <div class="stat mono" id="last-run-time">—</div>
      <div class="stat-label" id="last-run-duration"></div>
    </div>
    <div class="card">
      <h2>תעודות משלוח</h2>
      <div class="stat" id="docs-count">—</div>
      <div class="stat-label" id="docs-detail"></div>
    </div>
    <div class="card">
      <h2>שורות</h2>
      <div class="stat" id="lines-count">—</div>
      <div class="stat-label" id="lines-detail"></div>
    </div>
  </div>

  <div class="grid">
    <div class="card card-full" id="unresolved-card" style="display:none">
      <h2>פריטים לא מזוהים</h2>
      <ul class="unresolved-list" id="unresolved-list"></ul>
    </div>
  </div>

  <div class="grid">
    <div class="card card-full">
      <h2>הגדרות</h2>
      <div class="settings-form" id="settings-form"></div>
      <div class="actions">
        <button class="btn" onclick="saveSettings()">שמור הגדרות</button>
        <button class="btn btn-green" onclick="triggerRun()">הפעל עכשיו</button>
        <button class="btn btn-green" onclick="triggerRun('--dry-run')">הפעל דמו</button>
      </div>
    </div>
  </div>

  <div class="grid">
    <div class="card card-full">
      <h2>היסטוריה</h2>
      <table>
        <thead><tr><th>זמן</th><th>מצב</th><th>סטטוס</th><th>תעודות</th><th>שורות</th><th>שגיאות</th><th>משך</th></tr></thead>
        <tbody id="history-body"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const SETTINGS_LABELS = {
  LOAD_TIME: 'שעת טעינה',
  MONITOR_INTERVAL: 'מרווח ניטור (דקות)',
  MONITOR_UNTIL: 'ניטור עד שעה',
  SHEET_TAB: 'שם טאב הזמנות'
};

async function refresh() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    // Status
    const el = document.getElementById('status-text');
    const card = document.getElementById('status-card');
    if (d.last_run) {
      const s = d.last_run.status;
      el.textContent = s === 'ok' ? 'תקין' : s === 'error' ? 'שגיאות' : s === 'running' ? 'רץ...' : s;
      el.className = 'stat status-' + s;
      document.getElementById('status-detail').textContent = 'טאב: ' + (d.last_run.active_tab || '—');

      const t = d.last_run.finished_at || d.last_run.started_at;
      document.getElementById('last-run-time').textContent = t ? t.replace('T', ' ').slice(0, 19) : '—';
      document.getElementById('last-run-duration').textContent =
        d.last_run.duration_s ? d.last_run.duration_s.toFixed(1) + ' שניות' : '';

      document.getElementById('docs-count').textContent = (d.last_run.created || 0) + (d.last_run.appended || 0);
      document.getElementById('docs-detail').textContent =
        (d.last_run.created || 0) + ' חדשות, ' + (d.last_run.appended || 0) + ' עדכון';

      document.getElementById('lines-count').textContent = d.last_run.lines || 0;
      document.getElementById('lines-detail').textContent =
        (d.last_run.orders || 0) + ' הזמנות, ' +
        (d.last_run.skipped_cust || 0) + ' דילוג לקוח, ' +
        (d.last_run.skipped_prod || 0) + ' דילוג מוצר';

      // Unresolved
      const uc = document.getElementById('unresolved-card');
      const ul = document.getElementById('unresolved-list');
      if (d.last_run.unresolved) {
        const u = JSON.parse(d.last_run.unresolved);
        ul.innerHTML = '';
        for (const [type, items] of Object.entries(u)) {
          const label = type === 'customers' ? 'לקוח' : type === 'warehouses' ? 'מחסן' : 'מוצר';
          for (const item of items) {
            const li = document.createElement('li');
            li.textContent = label + ': ' + item;
            ul.appendChild(li);
          }
        }
        uc.style.display = ul.children.length ? 'block' : 'none';
      } else {
        uc.style.display = 'none';
      }
    } else {
      el.textContent = 'טרם הופעל';
      el.className = 'stat status-waiting';
    }

    // Settings
    const sf = document.getElementById('settings-form');
    sf.innerHTML = '';
    for (const [key, label] of Object.entries(SETTINGS_LABELS)) {
      sf.innerHTML += '<label>' + label + '</label>' +
        '<input name="' + key + '" value="' + (d.settings[key] || '') + '">' +
        '<span></span>';
    }

    // History
    const tbody = document.getElementById('history-body');
    tbody.innerHTML = '';
    for (const run of d.history) {
      const t = (run.started_at || '').replace('T', ' ').slice(0, 16);
      const badge = run.status === 'ok' ? 'badge-ok' : run.status === 'error' ? 'badge-error' : 'badge-running';
      const statusText = run.status === 'ok' ? 'תקין' : run.status === 'error' ? 'שגיאה' : run.status;
      tbody.innerHTML += '<tr>' +
        '<td class="mono">' + t + '</td>' +
        '<td>' + (run.mode || '') + '</td>' +
        '<td><span class="badge ' + badge + '">' + statusText + '</span></td>' +
        '<td>' + ((run.created || 0) + (run.appended || 0)) + '</td>' +
        '<td>' + (run.lines || 0) + '</td>' +
        '<td>' + (run.errors || 0) + '</td>' +
        '<td>' + (run.duration_s ? run.duration_s.toFixed(0) + 's' : '—') + '</td>' +
        '</tr>';
    }
  } catch (e) { console.error(e); }
}

async function saveSettings() {
  const inputs = document.querySelectorAll('#settings-form input');
  const data = {};
  inputs.forEach(i => data[i.name] = i.value);
  await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)});
  refresh();
}

async function triggerRun(flags) {
  const r = await fetch('/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({flags: flags || ''})});
  const d = await r.json();
  alert(d.message || 'הופעל');
  setTimeout(refresh, 2000);
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default logging

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._html(HTML)
        elif self.path == "/api/status":
            self._json({
                "settings": db.get_all_settings(),
                "last_run": db.get_last_run(),
                "history": db.get_last_runs(20),
            })
        else:
            self.send_error(404)

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode() if content_len else "{}"

        if self.path == "/api/settings":
            data = json.loads(body)
            for key, value in data.items():
                if key in ("LOAD_TIME", "MONITOR_INTERVAL", "MONITOR_UNTIL", "SHEET_TAB"):
                    db.set_setting(key, value)
            self._json({"ok": True})

        elif self.path == "/api/run":
            data = json.loads(body)
            flags = data.get("flags", "")
            cmd = [sys.executable, os.path.join(PROJECT_DIR, "agent.py")]
            if flags:
                cmd.extend(flags.split())
            # Run in background thread
            def _run():
                subprocess.run(cmd, cwd=PROJECT_DIR)
            threading.Thread(target=_run, daemon=True).start()
            self._json({"message": "Agent started", "command": " ".join(cmd)})

        else:
            self.send_error(404)


def main():
    server = HTTPServer(("127.0.0.1", PORT), DashboardHandler)
    print(f"Dashboard running at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
