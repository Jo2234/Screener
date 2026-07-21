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

# Find the generated PDF
PDF_FILE=$(ls -t "$SCREENER_DIR/output/"*.pdf 2>/dev/null | head -1)

if [ -z "$PDF_FILE" ]; then
    echo "$(date): ERROR - No PDF report found!"
    exit 1
fi

echo "$(date): Sending email with $PDF_FILE..."

# Send email using Python
"$PYTHON_PATH" "$SCREENER_DIR/send_email.py" "$PDF_FILE"

echo "$(date): Done!"
