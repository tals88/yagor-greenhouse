#!/usr/bin/env python3
"""
חממת עלים יגור — Scheduler & Monitor

Runs agent.py on a schedule:
  1. Waits until LOAD_TIME (default 14:00 Israel time)
  2. Runs the full load
  3. Monitors for new rows every MONITOR_INTERVAL minutes
  4. Stops monitoring at MONITOR_UNTIL (default 18:00)

Usage:
  uv run python scheduler.py                        # Production: wait for 14:00, then monitor
  uv run python scheduler.py --now                  # Run immediately, then monitor
  uv run python scheduler.py --now --once           # Run once immediately, no monitoring
  uv run python scheduler.py --test                 # Write to test tab only
  uv run python scheduler.py --test --limit 50      # Test with 50 rows
  uv run python scheduler.py --dry-run              # No writes at all
  uv run python scheduler.py --time 13:00           # Override start time (one-off)
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load .env ──────────────────────────────────────────────────────────────

def load_dotenv(path: str) -> dict[str, str]:
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


ENV = load_dotenv(os.path.join(PROJECT_DIR, ".env"))

# Israel timezone (IST = UTC+2, IDT = UTC+3)
IST = timezone(timedelta(hours=2))
IDT = timezone(timedelta(hours=3))
ISRAEL_TZ = IDT  # Summer time — adjust if needed or use zoneinfo

# ── Parse CLI args ─────────────────────────────────────────────────────────

def parse_args():
    args = {
        "now": "--now" in sys.argv,
        "once": "--once" in sys.argv,
        "test": "--test" in sys.argv,
        "dry_run": "--dry-run" in sys.argv,
        "limit": 0,
        "time": ENV.get("LOAD_TIME", "14:00"),
        "monitor_interval": int(ENV.get("MONITOR_INTERVAL", "5")),
        "monitor_until": ENV.get("MONITOR_UNTIL", "18:00"),
    }

    for i, arg in enumerate(sys.argv):
        if arg.startswith("--limit"):
            if "=" in arg:
                args["limit"] = int(arg.split("=")[1])
            elif i + 1 < len(sys.argv):
                args["limit"] = int(sys.argv[i + 1])
        if arg.startswith("--time"):
            if "=" in arg:
                args["time"] = arg.split("=")[1]
            elif i + 1 < len(sys.argv):
                args["time"] = sys.argv[i + 1]

    return args


# ── Helpers ────────────────────────────────────────────────────────────────

def now_israel() -> datetime:
    return datetime.now(ISRAEL_TZ)


def parse_time(time_str: str) -> tuple[int, int]:
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def run_agent(args: dict) -> int:
    """Run agent.py with the appropriate flags. Returns exit code."""
    cmd = [sys.executable, os.path.join(PROJECT_DIR, "agent.py")]
    if args["test"]:
        cmd.append("--test")
    if args["dry_run"]:
        cmd.append("--dry-run")
    if args["limit"]:
        cmd.extend(["--limit", str(args["limit"])])

    ts = now_israel().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 70}")
    print(f"  [{ts}] Running agent...")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'=' * 70}\n")

    result = subprocess.run(cmd, cwd=PROJECT_DIR)
    return result.returncode


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    load_h, load_m = parse_time(args["time"])
    until_h, until_m = parse_time(args["monitor_until"])
    interval = args["monitor_interval"]

    mode_parts = []
    if args["dry_run"]:
        mode_parts.append("dry-run")
    if args["test"]:
        mode_parts.append("test")
    if args["limit"]:
        mode_parts.append(f"limit={args['limit']}")
    mode_str = f" [{', '.join(mode_parts)}]" if mode_parts else ""

    print(f"{'=' * 70}")
    print(f"  חממת עלים יגור — Scheduler{mode_str}")
    print(f"{'=' * 70}")
    print(f"  Load time:        {args['time']} Israel")
    print(f"  Monitor interval: {interval} min")
    print(f"  Monitor until:    {args['monitor_until']} Israel")
    if args["now"]:
        print(f"  Mode:             Run immediately" + (" (once)" if args["once"] else " + monitor"))
    else:
        print(f"  Mode:             Wait for {args['time']}" + (" + monitor" if not args["once"] else ""))
    print(f"{'=' * 70}\n")

    # ── Wait for load time (unless --now) ──
    if not args["now"]:
        while True:
            n = now_israel()
            target = n.replace(hour=load_h, minute=load_m, second=0, microsecond=0)
            if n >= target:
                # Already past load time today — if within monitoring window, run now
                # Otherwise wait for tomorrow
                end_today = n.replace(hour=until_h, minute=until_m, second=0, microsecond=0)
                if n < end_today:
                    print(f"  [{n.strftime('%H:%M:%S')}] Past load time, within window — starting now")
                    break
                else:
                    target += timedelta(days=1)
                    wait_secs = (target - n).total_seconds()
                    print(f"  [{n.strftime('%H:%M:%S')}] Past monitoring window. Next run: tomorrow {args['time']}")
                    print(f"  Sleeping {wait_secs / 3600:.1f} hours...")
                    time.sleep(min(wait_secs, 3600))  # Check every hour
                    continue
            else:
                wait_secs = (target - n).total_seconds()
                if wait_secs > 60:
                    print(f"  [{n.strftime('%H:%M:%S')}] Waiting for {args['time']}... ({wait_secs / 60:.0f} min)")
                    time.sleep(min(wait_secs, 300))  # Check every 5 min
                    continue
                else:
                    print(f"  [{n.strftime('%H:%M:%S')}] Almost time...")
                    time.sleep(wait_secs)
                    break

    # ── Initial load ──
    exit_code = run_agent(args)
    if exit_code != 0:
        print(f"\n  Agent exited with code {exit_code}")

    if args["once"]:
        print("\n  --once flag set, exiting.")
        return

    # ── Monitor loop ──
    print(f"\n  Entering monitor mode (every {interval} min until {args['monitor_until']})...")

    while True:
        n = now_israel()
        end_time = n.replace(hour=until_h, minute=until_m, second=0, microsecond=0)

        if n >= end_time:
            print(f"\n  [{n.strftime('%H:%M:%S')}] Past {args['monitor_until']} — stopping monitor.")
            break

        remaining = (end_time - n).total_seconds() / 60
        print(f"\n  [{n.strftime('%H:%M:%S')}] Next check in {interval} min ({remaining:.0f} min until stop)...")
        time.sleep(interval * 60)

        n = now_israel()
        if n >= end_time:
            print(f"\n  [{n.strftime('%H:%M:%S')}] Past {args['monitor_until']} — stopping monitor.")
            break

        exit_code = run_agent(args)
        if exit_code != 0:
            print(f"\n  Agent exited with code {exit_code}")

    print(f"\n{'=' * 70}")
    print(f"  Scheduler finished for today.")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
