#!/bin/bash
# Weekly EMA Crossover Screener - Run and Email Script
# Invoked manually or by scheduler.py

set -e

# Configuration
SCREENER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Use the scheduler's interpreter override or python3 from the active environment.
PYTHON_PATH="${PYTHON_PATH:-$(command -v python3)}"
EMAIL_CONFIG="${EMAIL_CONFIG:-$SCREENER_DIR/.email_config}"

# Load email config from local file (not in git)
source "$EMAIL_CONFIG"

# Change to screener directory
cd "$SCREENER_DIR"

# Run the screener with fresh data (no cache)
echo "$(date): Starting EMA Crossover Screener..."
"$PYTHON_PATH" screener.py --no-cache

# Read only the PDF named by this run's successful quality summary.
PDF_FILE=$("$PYTHON_PATH" - <<'PYCODE'
import json
from pathlib import Path
root = Path.cwd() / "output"
summary = json.loads((root / "run_summary.json").read_text())
name = summary.get("report_path")
if not summary.get("email_allowed") or not summary.get("gate_passed") or not summary.get("report_generated"):
    raise SystemExit("ERROR: Data quality gate or PDF generation failed; email suppressed")
if not name or Path(name).name != name or not (root / name).is_file():
    raise SystemExit("ERROR: Quality summary does not identify a valid report")
print((root / name).resolve())
PYCODE
)

echo "$(date): Sending email with $PDF_FILE..."

# Send email using Python
"$PYTHON_PATH" "$SCREENER_DIR/send_email.py" "$PDF_FILE"

echo "$(date): Done!"
