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

def _parse_int_flag(name: str) -> int:
    """Read --{name} N or --{name}=N from sys.argv. Returns 0 if absent."""
    prefix = f"--{name}"
    for _i, _arg in enumerate(sys.argv):
        if _arg == prefix and _i + 1 < len(sys.argv):
            return int(sys.argv[_i + 1])
        if _arg.startswith(prefix + "="):
            return int(_arg.split("=", 1)[1])
    return 0


ROW_LIMIT = _parse_int_flag("limit")
# --max-groups N: after grouping by (A+B+C), keep only the first N groups.
# Useful for testing the full pipeline against a tiny slice (1 group = 1 doc).
GROUP_LIMIT = _parse_int_flag("max-groups")
