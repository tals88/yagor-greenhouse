# חממת עלים יגור — Order Loading Agent

Reads orders from a Google Sheet, matches customers/products/warehouses against Priority ERP, and creates draft delivery notes (תעודות משלוח).

## Architecture

```
Google Sheet (הזמנות)          Priority ERP
       │                            │
       ▼                            ▼
   Python (agent.py)          ODATA REST API
   ├── Read sheet via gws     ├── CUSTOMERS
   ├── Group by A+B+C         ├── ZANA_WARHSDES_EXT_FL
   ├── Match (Python)         ├── ZANA_PARTDES_EXT_FLA
   ├── Fallback (Claude AI)   ├── LOGPART
   ├── POST DOCUMENTS_D       └── DOCUMENTS_D
   └── Write back J,K,L
```

**Python** handles all mechanical work (read, group, match, POST).
**Claude AI** is called only for unresolved items (~$0.01/run).

---

## Setup — Windows (Customer Machine)

### Step 1: Install Docker Desktop

1. Download from https://www.docker.com/products/docker-desktop/
2. Run the installer, follow prompts
3. Restart the computer when prompted
4. Open Docker Desktop and wait for it to start (green icon in system tray)
5. Open PowerShell and verify:
   ```powershell
   docker --version
   docker compose version
   ```

### Step 2: Install Git (if not installed)

1. Download from https://git-scm.com/download/win
2. Run installer with default settings
3. Verify in PowerShell:
   ```powershell
   git --version
   ```

### Step 3: Clone the project

```powershell
cd C:\Users\YourUser\Documents
git clone https://github.com/tals88/yagor-greenhouse.git
cd yagor-greenhouse
```

### Step 4: Configure

```powershell
# Copy the example config
copy .env.example .env

# Edit .env with notepad — fill in all values
notepad .env
```

### Step 5: Setup Google credentials

The `.gws-config` folder should already contain `client_secret.json` and `credentials.enc` from the initial setup. If not:

```powershell
mkdir .gws-config
copy client_secret_*.json .gws-config\client_secret.json

# Login (opens browser — sign in with the Google account that has sheet access)
set GOOGLE_WORKSPACE_CLI_CONFIG_DIR=.gws-config
npx @googleworkspace/cli auth login -s sheets
```

### Step 6: Build & Run

```powershell
# Build the Docker image
docker compose build

# Start the scheduler (runs daily at LOAD_TIME)
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

---

## Setup — Linux / WSL (Development Machine)

### Step 1: Install dependencies

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Node.js + gws CLI
sudo apt install nodejs npm
npm install -g @googleworkspace/cli

# Install Docker (optional, for container testing)
sudo apt install docker.io docker-compose-v2
```

### Step 2: Clone & configure

```bash
git clone git@github.com:tals88/yagor-greenhouse.git
cd yagor-greenhouse

cp .env.example .env
nano .env  # fill in all values

uv sync  # install Python dependencies
```

### Step 3: Setup Google credentials

```bash
mkdir -p .gws-config
cp client_secret_*.json .gws-config/client_secret.json
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=.gws-config gws auth login -s sheets
```

### Step 4: Run

```bash
# Direct run (no Docker)
uv run python agent.py --test --limit 50

# With Docker
docker compose build
docker compose up -d
```

---

## Usage

### Agent (direct)

```bash
# Dry run — resolve everything, don't write anywhere
uv run python agent.py --dry-run

# Test mode — writes to הזמנות_test tab, not the real data
uv run python agent.py --test --limit 50

# Production — creates Priority docs, writes back to real sheet
uv run python agent.py
```

### Dry-run over ALL rows (including already-loaded)

`agent.py --dry-run` only processes **pending** rows. Use `dry_run_all.py` to
see matching results for **every** row — including ones already loaded with a
DOCNO. Useful for auditing past matches or verifying the matcher before a
production run.

```bash
# Full sheet
uv run python dry_run_all.py

# First 100 rows only — quick smoke test
uv run python dry_run_all.py --limit 100

# Skip Claude fallback (no API cost, no ANTHROPIC_API_KEY needed)
uv run python dry_run_all.py --no-claude
```

Outputs land in `data/`:

| File | Purpose |
|------|---------|
| `dry-run-all-<stamp>.json` | Full machine-readable — every row + matches + unmatched lists |
| `dry-run-all-<stamp>.tsv`  | Tab-separated, open in Excel for row-by-row review |
| `dry-run-all-<stamp>.txt`  | Human-readable Hebrew summary |

### Scheduler

```bash
# Production: wait for LOAD_TIME (default 14:00), run, then monitor
uv run python scheduler.py

# Run immediately, then monitor
uv run python scheduler.py --now

# Run once immediately, no monitoring
uv run python scheduler.py --now --once

# Override start time (one-off, doesn't change .env)
uv run python scheduler.py --time 13:00

# Combine flags
uv run python scheduler.py --test --limit 50 --now --once
```

### Admin CLI

```bash
# Show current config (secrets masked)
uv run python admin.py config

# Change schedule time
uv run python admin.py config set LOAD_TIME 13:00

# Change monitoring interval to 10 minutes
uv run python admin.py config set MONITOR_INTERVAL 10

# Trigger a manual run
uv run python admin.py run --test --limit 50

# Show status
uv run python admin.py status
```

Editable keys: `LOAD_TIME`, `MONITOR_INTERVAL`, `MONITOR_UNTIL`, `SHEET_ID`, `SHEET_TAB`.

### Docker

```bash
# Build
docker compose build

# Start (production — waits for LOAD_TIME, then monitors)
docker compose up -d

# View logs
docker compose logs -f

# One-off test run
docker compose run --rm agent uv run python agent.py --test --limit 50

# Update config without rebuild
docker compose exec agent uv run python admin.py config set LOAD_TIME 13:00
docker compose restart

# Trigger manual run inside container
docker compose exec agent uv run python admin.py run

# Stop
docker compose down
```

---

## Testing

Safe-to-run tests, ordered from least to most impactful. Always start at the top
when validating a new environment.

### Local / WSL (dev machine — no Docker)

Run the Python scripts directly with `uv`. No container involved.

```bash
# 1. Smoke test — Priority + Google Sheet connectivity, no LLM
#    Verifies gws auth, Priority ODATA reachable, matching logic works.
uv run python dry_run_all.py --limit 20 --no-claude

# 2. Full matcher audit with LLM — includes Claude fallback
#    Checks end-to-end including ANTHROPIC_API_KEY and fallback logic.
uv run python dry_run_all.py --limit 50

# 3. Full sheet audit — matching for EVERY row (pending + already-loaded)
#    Catches historical mismatches (e.g. wrong-customer DOCNOs).
uv run python dry_run_all.py

# 4. Pending-only dry run — what the next real run would do
#    Shows only rows the agent would actually process right now.
uv run python agent.py --dry-run

# 5. Test-tab write — writes DOCNOs/lines to הזמנות_test (sheet) and REAL Priority
#    Proves the write path end-to-end without touching the real orders tab.
uv run python agent.py --test --limit 10

# 6. Audit already-loaded DOCNOs against Priority customer data
#    Catches past Claude hallucinations where the wrong customer got goods.
uv run python audit_customers.py --limit 50
```

**What each output looks like:**

- `dry_run_all.py` → 3 files in `data/` (json + tsv + txt)
- `agent.py --dry-run` → stdout only; no files changed
- `agent.py --test` → real Priority DOCNOs + sheet writes to `הזמנות_test` tab
- `audit_customers.py` → `data/audit-customers-<stamp>.json`

### Customer Windows PC (Docker only)

The customer machine has **no source code** — just the Docker image and
`docker-compose.yml`. Run scripts inside the container using `docker compose run`
(throwaway container) or `docker compose exec` (hop into the running scheduler).

```powershell
# 1. Smoke test — no writes, limited rows, no LLM
docker compose run --rm agent uv run python dry_run_all.py --limit 20 --no-claude

# 2. Full matcher audit with LLM
docker compose run --rm agent uv run python dry_run_all.py --limit 50

# 3. Full sheet audit (all rows, including already-loaded)
docker compose run --rm agent uv run python dry_run_all.py

# 4. Pending-only dry run
docker compose run --rm agent uv run python agent.py --dry-run

# 5. Test-tab write — safe end-to-end test
docker compose run --rm agent uv run python agent.py --test --limit 10

# 6. Customer audit
docker compose run --rm agent uv run python audit_customers.py --limit 50

# 7. Hop into the running container for interactive debugging
docker compose exec agent bash
# inside container:
#   uv run python dry_run_all.py --limit 20
#   exit

# 8. View live scheduler logs
docker compose logs -f agent
```

Output files land on the host at `.\data\` thanks to the volume mount. Open
`dry-run-all-*.tsv` in Excel for review (RTL display works in recent Excel).

### Updating scripts on the customer PC (no git access)

If you add a new script (like `dry_run_all.py`) and the customer can't pull from
GitHub, you have three options:

```powershell
# Option A — one-shot bind mount (no rebuild)
# Drop the .py file next to docker-compose.yml, then:
docker compose run --rm `
  -v ${PWD}/dry_run_all.py:/app/dry_run_all.py `
  agent uv run python dry_run_all.py --limit 20

# Option B — copy into the running container (persists until container restart)
docker cp dry_run_all.py $(docker compose ps -q agent):/app/
docker compose exec agent uv run python dry_run_all.py --limit 20

# Option C — rebuild with the updated image
# On your dev machine:
#   docker build -t yagor-greenhouse .
#   docker save yagor-greenhouse | gzip > yagor-greenhouse.tar.gz
# On customer PC:
#   docker load < yagor-greenhouse.tar.gz
#   docker compose up -d
```

---

## CLI Flags

| Flag | Where | Description |
|------|-------|-------------|
| `--dry-run` | agent/scheduler | No writes to Priority or Google Sheet |
| `--test` | agent/scheduler | Writes to `הזמנות_test` tab instead of real data |
| `--limit N` | agent/scheduler/dry_run_all | Process only first N valid rows |
| `--max-groups N` | agent | Keep only the first N delivery-note groups (one A+B+C combo each). Pairs well with `--dry-run` from the dashboard. |
| `--no-claude` | dry_run_all | Skip Claude fallback (no API cost) |
| `--now` | scheduler | Run immediately instead of waiting for LOAD_TIME |
| `--once` | scheduler | Run once, no monitoring after |
| `--time HH:MM` | scheduler | Override LOAD_TIME for this run |

---

## Configuration (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Claude API key for fallback matching | required |
| `PRIORITY_BASE_URL` | Priority server hostname | required |
| `PRIORITY_TABULA_INI` | Priority tabula.ini name | `tabula.ini` |
| `PRIORITY_COMPANY` | Priority company code | required |
| `PRIORITY_USER` | Priority API token | required |
| `PRIORITY_PASSWORD` | Priority auth type | `PAT` |
| `SHEET_ID` | Google Sheet ID | required |
| `SHEET_TAB` | Orders tab name | `הזמנות` |
| `LOAD_TIME` | When to start loading (Israel time) | `14:00` |
| `MONITOR_INTERVAL` | Minutes between monitoring checks | `5` |
| `MONITOR_UNTIL` | Stop monitoring at this time | `18:00` |

---

## Google Sheet Columns

### Input (read by agent)

| Column | Field | Maps to |
|--------|-------|---------|
| A | Order Number | `BOOKNUM` on DOCUMENTS_D |
| B | Warehouse Name | `TOWARHSNAME` via ZANA_WARHSDES_EXT_FL (exact match) or מיפוי tab |
| C | Customer Name | `CUSTNAME` via CUSTOMERS |
| D | Timestamp | `DETAILS` on DOCUMENTS_D |
| E | Quantity | `TQUANT` (יחידות) **or** `NUMPACK` (קרטון) — see col H |
| F | (unused) | — |
| G | Product Description | `PARTNAME` via ZANA_PARTDES_EXT_FLA / LOGPART |
| H | Pack Type | `קרטון` → quantity goes to `NUMPACK` (מס אריזות). `יחידות` / empty → quantity goes to `TQUANT`. |
| I | Exclude/Retry | `N` = skip row, `R` = retry failed row |
| M | Today Override | `Y` = use today's date for CURDATE (see below) |

### Output (written by agent)

| Column | Field | Description |
|--------|-------|-------------|
| J | DOCNO | Priority document number (e.g. `SH2630000712`) |
| K | Created At | Timestamp when document was created |
| L | Error | Error message if failed |

---

## CURDATE Logic

The delivery note date (`CURDATE` on `DOCUMENTS_D`) defaults to **tomorrow**.

To override and use **today's date**, put `Y` (or `y`) in **column M** of the Google Sheet.
Since rows are grouped by columns **A + B + C** (order number + warehouse + customer),
you only need to set column M = `Y` on **one row** in the group — it applies to the entire delivery note.

| Column M | CURDATE |
|----------|---------|
| empty / anything else | Tomorrow (Asia/Jerusalem timezone) |
| `Y` or `y` | Today |

## FLAG / CHANEL Logic

The agent reads the `CHANEL` field from each customer record in Priority.

| Customer CHANEL | FLAG on DOCUMENTS_D | Warehouse requirement |
|-----------------|---------------------|----------------------|
| `Y` | Set to `N` | **Mandatory** — empty col B blocks doc creation, error written to col L |
| empty / anything else | Not set (Priority auto-fills from customer config) | Required only if col B has unresolved text |

CHANEL=Y customers are consignment (קונסיגנציה) — every delivery note must carry
a destination warehouse so the goods can be picked. If col B is empty or its
value can't be resolved to a `WARHSNAME`, the agent refuses to create the doc.

---

## Flow

1. **14:00** — Agent reads all pending rows (col J empty, col I not `N`)
2. Groups by **(A + B + C)** — each unique combo = 1 delivery note
3. Matches customers, warehouses, products (Python first, Claude fallback)
4. Creates `DOCUMENTS_D` in Priority with all lines
5. Writes DOCNO to col J, timestamp to col K
6. **14:05–18:00** — Monitors every 5 min for new rows
7. New rows for existing groups → **appends** lines to existing document
8. New rows for new groups → creates new document
9. Errors → written to col L
10. Customer puts `R` in col I → retry on next run, clears error on success

---

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Main agent — reads sheet, matches, creates Priority docs |
| `scheduler.py` | Scheduler — runs agent on schedule + monitors |
| `admin.py` | Admin CLI — view/update config, trigger runs |
| `dashboard.py` | localhost:8080 RTL web UI — status, config, trigger runs |
| `dry_run_all.py` | Dry-run matcher for ALL rows (incl. already-loaded) — writes 3 files to `data/` |
| `audit_customers.py` | Audits existing DOCNOs vs sheet customers — catches past hallucinations |
| `skill.md` | Reference docs for Claude fallback (API endpoints, matching rules) |
| `.env` | Configuration (credentials, schedule) |
| `.env.example` | Template for `.env` |
| `.gws-config/` | Google Workspace CLI credentials (customer's account) |
| `Dockerfile` | Container image |
| `docker-compose.yml` | Container orchestration |
| `odata.txt` | Priority ODATA API reference notes |
