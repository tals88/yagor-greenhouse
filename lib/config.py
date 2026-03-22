"""Configuration loader and CLI flags."""
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


ENV = load_dotenv(os.path.join(PROJECT_DIR, ".env"))

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
