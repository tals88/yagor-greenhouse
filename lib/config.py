"""Configuration loader — secrets from .env, operational settings from SQLite."""
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


# Secrets from .env (API keys, passwords, base URLs)
ENV = load_dotenv(os.path.join(PROJECT_DIR, ".env"))


def get_setting(key: str) -> str:
    """Get an operational setting. Priority: SQLite → .env → default."""
    from lib.db import get_setting as db_get
    val = db_get(key)
    if val:
        return val
    return ENV.get(key, "")


# CLI flags
DRY_RUN = "--dry-run" in sys.argv
TEST_MODE = "--test" in sys.argv
READ_TAB = "הזמנות"
WRITE_TAB = "הזמנות_test" if TEST_MODE else "הזמנות"

ROW_LIMIT = 0
for _arg in sys.argv:
    if _arg.startswith("--limit"):
        if "=" in _arg:
            ROW_LIMIT = int(_arg.split("=")[1])
        else:
            _idx = sys.argv.index(_arg)
            if _idx + 1 < len(sys.argv):
                ROW_LIMIT = int(sys.argv[_idx + 1])
