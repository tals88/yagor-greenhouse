"""SQLite storage for operational settings and run history."""
import json
import os
import sqlite3
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "data", "agent.db")

# Default operational settings (overridable via dashboard)
DEFAULTS = {
    "LOAD_TIME": "14:00",
    "MONITOR_INTERVAL": "5",
    "MONITOR_UNTIL": "18:00",
    "SHEET_TAB": "הזמנות",
    "REPORT_EMAILS": "greenhouse@yagur.com,Yaron.Sh@abra-it.com,tal.sh@abra-it.com",
}


def _ensure_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            mode       TEXT,
            status     TEXT DEFAULT 'running',
            orders     INTEGER DEFAULT 0,
            created    INTEGER DEFAULT 0,
            appended   INTEGER DEFAULT 0,
            lines      INTEGER DEFAULT 0,
            errors     INTEGER DEFAULT 0,
            skipped_cust INTEGER DEFAULT 0,
            skipped_prod INTEGER DEFAULT 0,
            unresolved TEXT,
            active_tab TEXT,
            duration_s REAL
        )
    """)
    conn.commit()
    return conn


def get_setting(key: str) -> str:
    conn = _ensure_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row:
        return row["value"]
    return DEFAULTS.get(key, "")


def get_all_settings() -> dict[str, str]:
    conn = _ensure_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    result = dict(DEFAULTS)
    for row in rows:
        result[row["key"]] = row["value"]
    return result


def set_setting(key: str, value: str) -> None:
    conn = _ensure_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()


def start_run(mode: str, active_tab: str) -> int:
    conn = _ensure_db()
    cur = conn.execute(
        "INSERT INTO runs (started_at, mode, active_tab) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), mode, active_tab),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id


def finish_run(
    run_id: int,
    status: str,
    stats: dict,
    unresolved: dict | None = None,
    duration_s: float = 0,
) -> None:
    conn = _ensure_db()
    conn.execute(
        """UPDATE runs SET
            finished_at = ?, status = ?,
            orders = ?, created = ?, appended = ?, lines = ?,
            errors = ?, skipped_cust = ?, skipped_prod = ?,
            unresolved = ?, duration_s = ?
        WHERE id = ?""",
        (
            datetime.now(timezone.utc).isoformat(),
            status,
            stats.get("orders", 0),
            stats.get("created", 0),
            stats.get("appended", 0),
            stats.get("lines", 0),
            stats.get("errors", 0),
            stats.get("skipped_cust", 0),
            stats.get("skipped_prod", 0),
            json.dumps(unresolved, ensure_ascii=False) if unresolved else None,
            duration_s,
            run_id,
        ),
    )
    conn.commit()
    conn.close()


def get_last_runs(n: int = 20) -> list[dict]:
    conn = _ensure_db()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_last_run() -> dict | None:
    runs = get_last_runs(1)
    return runs[0] if runs else None
