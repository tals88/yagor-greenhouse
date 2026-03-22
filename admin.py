#!/usr/bin/env python3
"""
Admin CLI for managing the agent configuration.

Usage:
  uv run python admin.py config                     # Show current config
  uv run python admin.py config set LOAD_TIME 13:00  # Update a value
  uv run python admin.py config set MONITOR_INTERVAL 10
  uv run python admin.py run                         # Trigger agent now (one-off)
  uv run python admin.py run --test --limit 50       # Trigger with flags
  uv run python admin.py status                      # Show last run info
"""
import os
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(PROJECT_DIR, ".env")

# Editable keys (safe to change at runtime)
EDITABLE_KEYS = {
    "LOAD_TIME", "MONITOR_INTERVAL", "MONITOR_UNTIL",
    "SHEET_ID", "SHEET_TAB",
}


def load_env() -> list[str]:
    with open(ENV_PATH) as f:
        return f.readlines()


def show_config():
    print("Current configuration:\n")
    for line in load_env():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key, _, val = stripped.partition("=")
            # Mask secrets
            if any(s in key for s in ("KEY", "PASSWORD", "USER")):
                val = val[:4] + "..." if len(val) > 4 else "***"
            print(f"  {key:25s} = {val}")


def set_config(key: str, value: str):
    if key not in EDITABLE_KEYS:
        print(f"Error: '{key}' is not editable via admin CLI.")
        print(f"Editable keys: {', '.join(sorted(EDITABLE_KEYS))}")
        sys.exit(1)

    lines = load_env()
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            k, _, _ = stripped.partition("=")
            if k.strip() == key:
                new_lines.append(f"{key}={value}\n")
                found = True
                continue
        new_lines.append(line)

    if not found:
        # Add before the last empty line or at end
        new_lines.append(f"{key}={value}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)

    print(f"Updated: {key} = {value}")
    print("Restart the scheduler for changes to take effect:")
    print("  docker compose restart")


def run_agent(extra_args: list[str]):
    cmd = [sys.executable, os.path.join(PROJECT_DIR, "agent.py")] + extra_args
    print(f"Running: {' '.join(cmd)}\n")
    subprocess.run(cmd, cwd=PROJECT_DIR)


def show_status():
    # Check if scheduler is running (docker)
    result = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        capture_output=True, text=True, cwd=PROJECT_DIR,
    )
    if result.returncode == 0 and result.stdout.strip():
        print("Docker containers:")
        print(f"  {result.stdout.strip()}")
    else:
        print("Docker: not running")

    print()
    show_config()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "config":
        if len(sys.argv) >= 5 and sys.argv[2] == "set":
            set_config(sys.argv[3], sys.argv[4])
        else:
            show_config()

    elif cmd == "run":
        run_agent(sys.argv[2:])

    elif cmd == "status":
        show_status()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
