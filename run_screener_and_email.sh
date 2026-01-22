#!/bin/bash
# Weekly EMA Crossover Screener - Run and Email Script
# Runs every Saturday at 5:45 PM Singapore time

set -e

# Configuration
SCREENER_DIR="/Users/johan/Downloads/Screener"
PYTHON_PATH="/Users/johan/.pyenv/versions/3.13.5/bin/python3"

# Load email config from local file (not in git)
source "$SCREENER_DIR/.email_config"

# Change to screener directory
cd "$SCREENER_DIR"

# Run the screener
echo "$(date): Starting EMA Crossover Screener..."
"$PYTHON_PATH" screener.py

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
