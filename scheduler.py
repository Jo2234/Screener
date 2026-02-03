#!/usr/bin/env python3
"""
Weekly Screener Scheduler
Runs the EMA Crossover Screener every Saturday at 7:17 PM Singapore time.
Start with: nohup python3 scheduler.py &
"""

import subprocess
import time
from datetime import datetime, timedelta
import os
import sys

# Configuration
SCREENER_DIR = "/Users/johan/Downloads/Screener"
RUN_HOUR = 9   # 9 AM
RUN_MINUTE = 0
RUN_WEEKDAY = 5  # Saturday (0=Monday, 5=Saturday)


def get_next_run_time():
    """Calculate the next Saturday at 7:17 PM."""
    now = datetime.now()
    
    # Calculate days until next Saturday
    days_until_saturday = (RUN_WEEKDAY - now.weekday()) % 7
    if days_until_saturday == 0:
        # It's Saturday, check if we've passed the run time
        if now.hour > RUN_HOUR or (now.hour == RUN_HOUR and now.minute >= RUN_MINUTE):
            days_until_saturday = 7  # Next Saturday
    
    next_run = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    next_run += timedelta(days=days_until_saturday)
    
    return next_run


def run_screener():
    """Run the screener and email script."""
    log_file = os.path.join(SCREENER_DIR, "logs", "scheduler.log")
    
    with open(log_file, "a") as log:
        log.write(f"\n{'='*60}\n")
        log.write(f"{datetime.now()}: Starting scheduled run\n")
        log.flush()
        
        try:
            result = subprocess.run(
                ["/bin/bash", os.path.join(SCREENER_DIR, "run_screener_and_email.sh")],
                cwd=SCREENER_DIR,
                capture_output=True,
                text=True,
                timeout=1800  # 30 minute timeout
            )
            
            log.write(f"Exit code: {result.returncode}\n")
            if result.stdout:
                log.write(f"Output (last 500 chars):\n{result.stdout[-500:]}\n")
            if result.stderr:
                log.write(f"Errors:\n{result.stderr[-500:]}\n")
                
        except subprocess.TimeoutExpired:
            log.write("ERROR: Script timed out after 30 minutes\n")
        except Exception as e:
            log.write(f"ERROR: {e}\n")
        
        log.write(f"{datetime.now()}: Run completed\n")


def main():
    """Main scheduler loop."""
    print(f"EMA Crossover Scheduler started at {datetime.now()}")
    print(f"Will run every Saturday at {RUN_HOUR}:{RUN_MINUTE:02d}")
    
    # Log startup
    log_file = os.path.join(SCREENER_DIR, "logs", "scheduler.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a") as log:
        log.write(f"\n{datetime.now()}: Scheduler started\n")
    
    while True:
        next_run = get_next_run_time()
        sleep_seconds = (next_run - datetime.now()).total_seconds()
        
        if sleep_seconds > 0:
            print(f"Next run: {next_run} (sleeping {sleep_seconds/3600:.1f} hours)")
            time.sleep(sleep_seconds)
        
        # Run the screener
        run_screener()
        
        # Sleep a bit to avoid double-runs
        time.sleep(120)


if __name__ == "__main__":
    main()
