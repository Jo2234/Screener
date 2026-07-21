# EMA Crossover Screener

Automated stock screener that identifies 78-day / 165-day EMA crossovers across multiple markets and sends weekly PDF reports via email.

## Features

- **Multi-Market Scanning**: China, ETFs, NSE (India), NYSE, Singapore
- **EMA Crossover Detection**: Identifies bullish and bearish 78/165 EMA crossovers
- **PDF Reports**: Professional reports with charts and clickable links
- **Automated Emails**: Weekly reports via Resend API
- **Smart Caching**: SQLite-based caching to reduce API calls
- **Parallel Processing**: Multi-threaded for faster scanning

## Prerequisites

- Python 3.9+
- macOS (for scheduled automation)

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/Jo2234/Screener.git
cd Screener
```

### 2. Install Dependencies

```bash
python3 -m pip install -r requirements.txt resend
```

### 3. Configure Email (Required for automation)

Create a `.email_config` file in the project root:

```bash
# .email_config
export RESEND_API_KEY='your_resend_api_key_here'
export RECIPIENT_EMAIL='primary@example.com'
export CC_EMAIL='cc@example.com'  # Optional
```

**To get a Resend API key:**
1. Sign up at [resend.com](https://resend.com) (free tier: 100 emails/day)
2. Go to API Keys in the dashboard
3. Create a new key and paste it above

> ⚠️ **Security**: The `.email_config` file is in `.gitignore` and will NOT be committed.

### 4. Create Required Directories

```bash
mkdir -p output logs Symbol_Data
```

### 5. Add Symbol Data

Place your CSV files in the `Symbol_Data/` directory. Each CSV should have columns:
- `Symbol` - Stock ticker
- `Name` - Company name
- `Exchange` - Exchange code (e.g., NSE, NYSE, SGX)

## Usage

### Manual Run

```bash
# Run with caching (faster, uses cached data if available)
python3 screener.py

# Run with fresh data (no cache)
python3 screener.py --no-cache
```

### Run with Email

```bash
bash run_screener_and_email.sh
```

### Automated Weekly Runs (macOS)

The scheduler runs every Saturday at 9:00 AM in `Asia/Singapore`, regardless of
the host timezone. GitHub Actions runs at Friday 23:00 UTC (Saturday 7:00 AM
Singapore time). Both choose the latest completed Monday–Friday reporting week
using `REPORT_TIMEZONE` (default: `Asia/Singapore`). This setting selects calendar
dates; it does not convert exchange session timestamps.

The scheduler and wrapper locate the checkout from their own script paths. The
scheduler passes its active Python interpreter (`sys.executable`) to the wrapper;
a direct wrapper invocation uses `python3` on `PATH`. Set `PYTHON_PATH` to override
the interpreter, for example `/path/to/venv/bin/python`. The wrapper explicitly
loads `.email_config` from the checkout; set `EMAIL_CONFIG` to an absolute path to
use a different local config file. Paths containing spaces are supported.

**Start the scheduler:**
```bash
nohup python3 scheduler.py > /dev/null 2>&1 &
```

**Set up auto-start at login:**

Create `~/Library/LaunchAgents/com.johan.screener.scheduler.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.johan.screener.scheduler</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/python3</string>
        <string>/path/to/Screener/scheduler.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>/path/to/Screener</string>
</dict>
</plist>
```

Replace all `/path/to` entries with your checkout and Python environment paths.

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.johan.screener.scheduler.plist
```

## Configuration

Edit `scheduler.py` to change the schedule:
```python
RUN_HOUR = 9      # Hour (24-hour format)
RUN_MINUTE = 0    # Minute
RUN_WEEKDAY = 5   # 0=Monday, 5=Saturday, 6=Sunday
```

## Project Structure

```
Screener/
├── screener.py              # Main screener script
├── scheduler.py             # Weekly scheduler
├── send_email.py            # Email sending via Resend
├── run_screener_and_email.sh # Shell script wrapper
├── Symbol_Data/             # CSV files with stock symbols
│   ├── China.csv
│   ├── ETFs.csv
│   ├── NSE.csv
│   ├── NYSE.csv
│   └── Singapore.csv
├── output/                  # Generated PDF reports
├── logs/                    # Log files
├── .email_config            # Email credentials (not in git)
└── .gitignore
```

## Troubleshooting

### Check if scheduler is running
```bash
ps aux | grep scheduler.py | grep -v grep
```

### View logs
```bash
tail -f logs/scheduler.log
```

### Test email sending
```bash
source .email_config && python3 send_email.py output/EMA_Crossover_Report_*.pdf
```

## Offline regression tests

```bash
python3 -m pip install -r requirements.txt pytest
python3 -m pytest -q
```

Tests use synthetic market data and local fake email entrypoints. They do not
fetch Yahoo data, send email, or start the scheduler loop.

## License

MIT
