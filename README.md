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
python3 -m pip install -r requirements.txt
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

### Data quality and partial reports

Every completed scan writes `output/run_summary.json` and a diagnostic PDF,
including when its data quality gate fails. A complete scan exits 0. A partial
scan also exits 0 only when it meets **all** configured limits; its PDF and email
explicitly disclose the missing coverage. An unreliable scan or empty/invalid
input exits 2 and suppresses routine email. PDF generation failure also exits 2,
keeps the JSON evidence and removes the incomplete PDF. GitHub Actions uploads
both available files even after failure.

Default limits use eligible CSV symbol rows as their denominator. Successfully
analyzed symbols include those with no crossover:

| Limit | Whole run | Each CSV market |
| --- | ---: | ---: |
| Minimum analysis coverage | 95% | 90% |
| Maximum operational error rate | 1% | 2% |

Ordinary data gaps are well-formed Yahoo answers that a symbol has no usable
prices: an explicit `Not Found`, a chart with no timestamps or only empty closes,
history that ends before the reporting week (typically suspended or delisted) and
fewer than 165 usable history rows. A chart without timestamps or closes counts
as a gap only when its `meta.symbol` matches the requested symbol; wrong-typed
timestamps, non-finite or unrepresentable dates, misaligned quote series or
unidentified empty results are schema errors. These gaps still reduce analysis
coverage and remain in failure details. A bare HTTP 404 does not prove delisting. Rate limits,
timeouts, transport/HTTP/JSON/schema errors, chart responses without a result or
with an application error, and processing errors count as operational failures. Empty
markets or zero successfully analyzed symbols always fail, even with relaxed
thresholds. Limits include their boundary (95% coverage and 1% errors pass).

These defaults tolerate modest genuine symbol/history gaps without treating a
provider outage as routine success. For example, 166 confirmed ordinary gaps
among 4,127 eligible rows could pass at 96% coverage, **if each market also
passes**; 166 transport failures fail the tighter operational limit. The observed
historical 166 errors cannot be assigned a cause retrospectively because the old
fetcher collapsed the reasons. The policy is a completeness safeguard, not a
claim of financial accuracy or guaranteed delivery.

Override limits explicitly with fractions from 0 to 1; the chosen limits remain
in both artifacts:

```bash
python3 screener.py --no-cache --min-coverage 0.97 --min-market-coverage 0.95 \
  --max-operational-error-rate 0.005 --max-market-operational-error-rate 0.01
```

Transient failures receive at most three attempts for the current symbol with
bounded exponential backoff (up to eight seconds) and a 15-second request timeout.
Aliases are tried only after a non-transient missing/no-data or HTTP 404 response; an alias
cannot cure an exhausted provider outage. Twenty consecutive transient request
failures without a successful fetch pause all new requests for 60 seconds so a
short throttling burst can clear; the third such streak opens a circuit for the
remainder of the run.
The scan also stops starting fetches after a 30-minute budget, allowing time for
diagnostic rendering within the workflow's 45-minute timeout. Cache reads and
already active requests may finish; pending symbols retain explicit circuit or
budget failure reasons. This bounds request work but does not guarantee artifacts
if the operating system kills the process or the workflow itself times out.

Each failed symbol retains its reason and attempted aliases in JSON; the PDF lists
failure messages and coverage. The wrapper accepts only the PDF named by the
current passing summary. Direct manual sending of older PDFs without a summary
remains supported; a present summary must permit that exact report.

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
