#!/usr/bin/env python3
"""
Multi-CSV EMA Crossover Stock Screener

Scans all CSV files in Symbol_Data folder, detects EMA crossovers,
and generates a single PDF report with summaries and charts for each CSV.
"""

import os
import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
import random
import sys
from xml.sax.saxutils import escape
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, List
from io import BytesIO

from reporting import get_previous_full_week
from reliability import FetchFailure, FetchGuard, QualityPolicy, quality_summary

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import yfinance as yf

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch, cm
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, 
    PageBreak, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT


# ============================================================================
# Configuration
# ============================================================================

# EMA periods
SHORT_EMA = 78
LONG_EMA = 165

# Target week uses the explicit reporting calendar, not the host timezone.
TARGET_WEEK_START, TARGET_WEEK_END = get_previous_full_week()

# How many days of history to fetch (need enough for 165-day EMA to stabilize)
HISTORICAL_DAYS = 400

# Directories
SYMBOL_DATA_DIR = Path("Symbol_Data")
OUTPUT_DIR = Path("output")
CHARTS_DIR = OUTPUT_DIR / "charts"

# Cache settings
CACHE_DB = Path("stock_cache.db")
CACHE_EXPIRY_HOURS = 24

# Parallel processing settings
MAX_WORKERS = 8  # Moderate parallelism with rate limiting

# Rate limiting settings
MAX_RETRIES = 3             # Number of retry attempts
BASE_DELAY = 2.0            # Base delay in seconds for exponential backoff
MIN_REQUEST_INTERVAL = 0.5  # Minimum seconds between requests (stricter pacing)

# Semaphore for rate limiting concurrent requests
_request_semaphore = threading.Semaphore(3)  # Max 3 concurrent requests (conservative)
_last_request_time = threading.local()
_fetch_guard = None  # Initialized per run, never contacts providers at import time.

# Exchange suffix mapping based on Exchange column values
EXCHANGE_SUFFIXES = {
    # Asian markets
    'SZSC': '.HK',      # Shenzhen-Hong Kong Stock Connect
    'SHSC': '.HK',      # Shanghai-Hong Kong Stock Connect  
    'SEHK': '.HK',      # Hong Kong Stock Exchange
    'SGX': '.SI',       # Singapore Exchange
    'NSEI': '.NS',      # National Stock Exchange of India
    'BSEI': '.BO',      # Bombay Stock Exchange
    'TSE': '.T',        # Tokyo Stock Exchange
    'KOSDAQ': '.KQ',    # Korea KOSDAQ
    'KSC': '.KS',       # Korea Stock Exchange
    # US markets (no suffix needed)
    'NYSE': '',
    'NASDAQ': '',
    'NasdaqGM': '',
    'NasdaqGS': '',
    'ARCA': '',
    'BATS': '',
    'AMEX': '',
    'OTCPK': '',
}

# Known ticker aliases (some data sources use different symbols than yfinance)
TICKER_ALIASES = {
    'BRKA': 'BRK-A',    # Berkshire Hathaway Class A
    'BRKB': 'BRK-B',    # Berkshire Hathaway Class B
    'BFA': 'BF-A',      # Brown-Forman Class A
    'BFB': 'BF-B',      # Brown-Forman Class B
}

# Thread-local storage for database connections
_thread_local = threading.local()


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class CrossoverResult:
    """Result of crossover detection for a single stock."""
    symbol: str
    company: str
    crossover_type: Optional[str]  # 'bullish', 'bearish', or None
    date: Optional[datetime]
    price: Optional[float]
    current_price: Optional[float]
    pct_change: Optional[float]
    short_ema: Optional[float]
    long_ema: Optional[float]
    df: Optional[pd.DataFrame] = None
    chart_path: Optional[Path] = None
    error: Optional[str] = None
    exchange: Optional[str] = None
    category: str = ""
    error_kind: Optional[str] = None
    fetch_attempts: List[Dict] = field(default_factory=list)

    @property
    def chart_id(self) -> str:
        """One safe identity for chart files and PDF links, including raw aliases."""
        identity = json.dumps(
            [self.category, self.exchange, self.symbol, self.crossover_type],
            ensure_ascii=True, separators=(",", ":")
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        label = re.sub(r"[^A-Za-z0-9_-]", "_", self.symbol)[:40]
        return f"chart_{label}_{digest}"


@dataclass
class CSVResults:
    """Results for a single CSV file."""
    csv_name: str
    display_name: str
    bullish: List[CrossoverResult] = field(default_factory=list)
    bearish: List[CrossoverResult] = field(default_factory=list)
    failed: List[CrossoverResult] = field(default_factory=list)
    total_symbols: int = 0
    errors: int = 0
    input_error: Optional[str] = None


# ============================================================================
# Caching Layer (Thread-Safe)
# ============================================================================

def init_cache():
    """Initialize the SQLite cache database."""
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_cache (
            symbol TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            fetched_at TEXT,
            PRIMARY KEY (symbol, date)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            symbol TEXT PRIMARY KEY,
            last_fetched TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_cache_valid(symbol: str) -> bool:
    """Check if cached data for a symbol is still valid."""
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT last_fetched FROM fetch_log WHERE symbol = ?", (symbol,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return False
    
    last_fetched = datetime.fromisoformat(row[0])
    return (datetime.now() - last_fetched).total_seconds() < CACHE_EXPIRY_HOURS * 3600


def get_cached_data(symbol: str) -> pd.DataFrame:
    """Retrieve cached price data for a symbol."""
    conn = sqlite3.connect(CACHE_DB)
    df = pd.read_sql_query(
        "SELECT date, open, high, low, close, volume FROM price_cache WHERE symbol = ? ORDER BY date",
        conn,
        params=(symbol,)
    )
    conn.close()
    
    if not df.empty:
        df['date'] = pd.to_datetime(df['date'], utc=True).dt.tz_localize(None)
        df.set_index('date', inplace=True)
        df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
    
    return df


def cache_data(symbol: str, df: pd.DataFrame):
    """Cache price data for a symbol."""
    conn = sqlite3.connect(CACHE_DB)
    cursor = conn.cursor()
    
    now = datetime.now().isoformat()
    
    for date, row in df.iterrows():
        date_str = pd.Timestamp(date).tz_localize(None).isoformat() if hasattr(date, 'tz') and date.tz else pd.Timestamp(date).isoformat()
        cursor.execute("""
            INSERT OR REPLACE INTO price_cache 
            (symbol, date, open, high, low, close, volume, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, date_str, row['Open'], row['High'], row['Low'], 
              row['Close'], row['Volume'], now))
    
    cursor.execute("""
        INSERT OR REPLACE INTO fetch_log (symbol, last_fetched)
        VALUES (?, ?)
    """, (symbol, now))
    
    conn.commit()
    conn.close()


# ============================================================================
# Symbol Processing
# ============================================================================

def get_yfinance_symbol(ticker: str, exchange: str = None) -> str:
    """
    Convert a ticker to yfinance-compatible symbol by adding exchange suffix if needed.
    Handles special cases:
    - Known ticker aliases (e.g., BRKA -> BRK-A)
    - HK tickers: pad numeric tickers to 4 digits (e.g., 700 -> 0700.HK)
    - US tickers: replace dots with dashes (e.g., BRK.B -> BRK-B)
    """
    ticker = str(ticker).strip().upper()
    
    # Check for known ticker aliases first
    if ticker in TICKER_ALIASES:
        ticker = TICKER_ALIASES[ticker]
    
    if exchange:
        exchange = str(exchange).strip().upper()
        suffix = EXCHANGE_SUFFIXES.get(exchange, '')
        
        # Special handling for Hong Kong tickers
        if suffix == '.HK':
            # Numeric HK tickers need to be 4 digits with leading zeros
            if ticker.isdigit():
                ticker = ticker.zfill(4)
            return f"{ticker}{suffix}"
        
        # For other exchanges with suffixes
        if suffix:
            return f"{ticker}{suffix}"
    
    # For US markets: replace dots with dashes (e.g., BRK.B -> BRK-B)
    ticker = ticker.replace('.', '-')
    return ticker


def read_csv_symbols(csv_path: Path) -> List[Dict]:
    """
    Read symbols from a CSV file. Handles both 'Symbol' and 'Ticker' column names.
    Returns list of dicts with 'symbol', 'name', and 'exchange' keys.
    """
    df = pd.read_csv(csv_path)
    
    # Find the symbol column (could be 'Symbol' or 'Ticker')
    symbol_col = None
    for col in ['Symbol', 'Ticker', 'symbol', 'ticker']:
        if col in df.columns:
            symbol_col = col
            break
    
    if not symbol_col:
        print(f"  ⚠️ No Symbol/Ticker column found in {csv_path.name}")
        return []
    
    # Find name column
    name_col = None
    for col in ['Name', 'Security', 'Company', 'name', 'security']:
        if col in df.columns:
            name_col = col
            break
    
    # Find exchange column
    exchange_col = None
    for col in ['Exchange', 'exchange']:
        if col in df.columns:
            exchange_col = col
            break
    
    symbols = []
    for _, row in df.iterrows():
        ticker = row[symbol_col]
        if pd.isna(ticker) or str(ticker).strip() == '':
            continue
            
        symbol_data = {
            'symbol': str(ticker).strip(),
            'name': str(row[name_col]).strip() if name_col and not pd.isna(row[name_col]) else str(ticker),
            'exchange': str(row[exchange_col]).strip() if exchange_col and not pd.isna(row[exchange_col]) else None
        }
        symbols.append(symbol_data)
    
    return symbols


# ============================================================================
# Data Fetching
# ============================================================================

# HTTP session for direct API calls
_http_session = None
_http_lock = threading.Lock()

def get_http_session():
    """Get or create a shared HTTP session."""
    global _http_session
    if _http_session is None:
        with _http_lock:
            if _http_session is None:
                import requests
                _http_session = requests.Session()
                _http_session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                })
    return _http_session

def fetch_via_direct_api(yf_symbol: str) -> pd.DataFrame:
    """
    Fetch stock data directly from Yahoo Finance API.
    Uses fresh requests to avoid session-based rate limiting.
    """
    import requests
    
    # Calculate date range
    end_date = TARGET_WEEK_END + timedelta(days=5)
    start_date = end_date - timedelta(days=HISTORICAL_DAYS)
    
    # Convert to timestamps
    start_ts = int(start_date.timestamp())
    end_ts = int(end_date.timestamp())
    
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    params = {
        'period1': start_ts,
        'period2': end_ts,
        'interval': '1d',
        'events': 'history'
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
    except requests.Timeout as exc:
        raise FetchFailure('timeout', 'Yahoo request timed out', retryable=True) from exc
    except requests.RequestException as exc:
        raise FetchFailure('transport', 'Yahoo transport request failed', retryable=True) from exc

    if response.status_code == 429:
        raise FetchFailure('rate_limit', 'Yahoo HTTP 429 rate limit', retryable=True)
    if response.status_code >= 500:
        raise FetchFailure('http_service', f'Yahoo HTTP {response.status_code}', retryable=True)
    try:
        data = response.json()
    except ValueError as exc:
        if response.status_code != 200:
            raise FetchFailure('http_error', f'Yahoo HTTP {response.status_code}; no valid error payload') from exc
        raise FetchFailure('json_error', 'Yahoo returned invalid JSON', retryable=True) from exc

    if isinstance(data, dict) and isinstance(data.get('chart'), dict):
        error = data['chart'].get('error')
        if isinstance(error, dict) and str(error.get('code', '')).lower() in ('not found', 'notfound'):
            raise FetchFailure('not_found', 'Yahoo explicitly reported symbol not found')
    if response.status_code != 200:
        raise FetchFailure('http_error', f'Yahoo HTTP {response.status_code}')

    try:
        chart = data['chart']
        if chart.get('error'):
            raise FetchFailure('provider_error', 'Yahoo returned an application error', retryable=True)
        results = chart['result']
        if results is None or results == []:
            raise FetchFailure('missing_result', 'Yahoo returned no chart result; symbol identity is unconfirmed', retryable=True)
        if not isinstance(results, list) or not isinstance(results[0], dict):
            raise TypeError('chart.result must be a list of objects')
        result = results[0]
        # Gaps are ordinary only when Yahoo's metadata confirms it answered for this symbol.
        meta = result.get('meta')
        confirmed = (isinstance(meta, dict) and isinstance(meta.get('symbol'), str)
                     and meta['symbol'].strip().upper() == yf_symbol.strip().upper())
        timestamps = result.get('timestamp')
        if timestamps is None or timestamps == []:
            if not confirmed:
                raise ValueError('Chart without timestamps lacks matching symbol metadata')
            raise FetchFailure('no_data', 'Yahoo chart has no historical timestamps')
        if not isinstance(timestamps, list) or not all(
                isinstance(ts, (int, float)) and not isinstance(ts, bool) and np.isfinite(ts)
                for ts in timestamps):
            raise TypeError('chart timestamps must be a list of finite numbers')
        quote = result['indicators']['quote'][0]
        columns = {name: quote[key] for name, key in (
            ('Open', 'open'), ('High', 'high'), ('Low', 'low'), ('Close', 'close'), ('Volume', 'volume'))}
        if not all(isinstance(values, list) and len(values) == len(timestamps) for values in columns.values()):
            raise ValueError('Quote series must be lists aligned with timestamps')
        df = pd.DataFrame(columns)
        df.index = pd.to_datetime(timestamps, unit='s', utc=True).tz_localize(None)
        if df.index.hasnans:
            raise ValueError('Chart timestamps must convert to valid dates')
        for column in df.columns:
            df[column] = pd.to_numeric(df[column], errors='raise')
        df = df.dropna(subset=['Close']).sort_index()
        # Yahoo occasionally repeats a bar; keep the latest copy rather than failing the symbol.
        df = df[~df.index.duplicated(keep='last')]
        if df.empty:
            if not confirmed:
                raise ValueError('Chart without closing prices lacks matching symbol metadata')
            raise FetchFailure('empty_prices', 'Yahoo chart contains no usable closing prices')
        if not np.isfinite(df['Close']).all() or (df['Close'] <= 0).any():
            raise ValueError('Invalid closing prices')
        return df
    except FetchFailure:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError) as exc:
        raise FetchFailure('data_error', 'Yahoo chart payload is malformed', retryable=True) from exc


def fetch_stock_data(symbol: str, exchange: str = None, use_cache: bool = True) -> Tuple[pd.DataFrame, str]:
    """
    Fetch historical stock data for a symbol with retry logic and rate limiting.
    Uses direct Yahoo Finance API calls to avoid yfinance rate limiting.
    Tries multiple symbol variations if the primary lookup fails.
    """
    # Build list of symbol variations to try
    yf_symbol = get_yfinance_symbol(symbol, exchange)
    
    # Create fallback variations based on known patterns
    symbol_variations = [yf_symbol]
    
    # Add exchange-specific fallbacks if not already in the primary symbol
    if exchange:
        # NSE stocks often need .NS suffix
        if 'NSE' in str(exchange).upper() or exchange == 'NSEI':
            if not symbol.endswith('.NS'):
                symbol_variations.append(f"{symbol}.NS")
        
        # Singapore stocks often need .SI suffix
        if 'SGX' in str(exchange).upper() or 'SINGAPORE' in str(exchange).upper():
            if not symbol.endswith('.SI'):
                symbol_variations.append(f"{symbol}.SI")
        
        # Hong Kong stocks often need .HK suffix
        if any(x in str(exchange).upper() for x in ['HK', 'HKSE', 'SEHK', 'SZSC', 'SHSC']):
            if not symbol.endswith('.HK'):
                # For numeric symbols (China stocks), add padding if needed
                if symbol.isdigit():
                    symbol_variations.append(f"{symbol.zfill(4)}.HK")
                else:
                    symbol_variations.append(f"{symbol}.HK")
        
        # Bombay Stock Exchange
        if 'BSE' in str(exchange).upper() or exchange == 'BSEI':
            if not symbol.endswith('.BO'):
                symbol_variations.append(f"{symbol}.BO")
    
    # Remove duplicates while preserving order
    symbol_variations = list(dict.fromkeys(symbol_variations))
    
    # Try cache first for primary symbol
    cache_key = yf_symbol
    if use_cache and is_cache_valid(cache_key):
        df = get_cached_data(cache_key)
        if not df.empty:
            return df, 'cache'
    
    attempts = []
    for try_symbol in symbol_variations:
        for attempt in range(MAX_RETRIES):
            try:
                with _request_semaphore:
                    if _fetch_guard:
                        _fetch_guard.check()
                    time.sleep(MIN_REQUEST_INTERVAL + random.uniform(0, 0.2))
                    df = fetch_via_direct_api(try_symbol)
                    if _fetch_guard:
                        _fetch_guard.observe()
                if use_cache:
                    cache_data(cache_key, df)
                return df, 'fetch'
            except FetchFailure as failure:
                attempts.append({'symbol': try_symbol, 'attempt': attempt + 1,
                                 'kind': failure.kind, 'message': str(failure)})
                if _fetch_guard:
                    _fetch_guard.observe(failure)
                if failure.retryable and attempt < MAX_RETRIES - 1:
                    time.sleep(min(8, BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)))
                    continue
                if failure.retryable or failure.kind in ('provider_circuit_open', 'fetch_budget_exhausted'):
                    # An alias cannot remedy a provider outage. Preserve all attempt evidence.
                    raise FetchFailure(failure.kind, str(failure), attempts=attempts) from failure
                if failure.kind not in ('not_found', 'no_data', 'empty_prices') and 'HTTP 404' not in str(failure):
                    raise FetchFailure(failure.kind, str(failure), attempts=attempts) from failure
                break  # A missing/no-data/404 response may justify a valid alias.
    final = attempts[-1]
    # Never let a later not-found alias erase an earlier ambiguous data/HTTP failure.
    ambiguous = [row for row in attempts if row['kind'] != 'not_found']
    if ambiguous:
        final = ambiguous[-1]
    raise FetchFailure(final['kind'], final['message'], attempts=attempts)


# ============================================================================
# EMA Calculation & Crossover Detection
# ============================================================================

def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Calculate Exponential Moving Average for given period."""
    return df['Close'].ewm(span=period, adjust=False).mean()


def detect_crossover(df: pd.DataFrame, start_date: datetime, end_date: datetime):
    """
    Detect if a crossover occurred during the specified date range.
    """
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        start_ts = pd.Timestamp(start_date).tz_localize(df.index.tz)
        end_ts = pd.Timestamp(end_date).tz_localize(df.index.tz) + pd.Timedelta(hours=23, minutes=59)
    else:
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date) + pd.Timedelta(hours=23, minutes=59)
    
    mask = (df.index >= start_ts) & (df.index <= end_ts)
    week_df = df[mask]
    
    if len(week_df) < 2:
        return None, None, None
    
    before_week = df[df.index < start_ts]
    if before_week.empty:
        return None, None, None
    
    prev_short_ema = before_week[f'EMA_{SHORT_EMA}'].iloc[-1]
    prev_long_ema = before_week[f'EMA_{LONG_EMA}'].iloc[-1]
    prev_diff = prev_short_ema - prev_long_ema
    
    for date, row in week_df.iterrows():
        curr_short_ema = row[f'EMA_{SHORT_EMA}']
        curr_long_ema = row[f'EMA_{LONG_EMA}']
        curr_diff = curr_short_ema - curr_long_ema
        
        if prev_diff < 0 and curr_diff >= 0:
            return 'bullish', date, {
                'close': row['Close'],
                'short_ema': curr_short_ema,
                'long_ema': curr_long_ema,
            }
        
        if prev_diff > 0 and curr_diff <= 0:
            return 'bearish', date, {
                'close': row['Close'],
                'short_ema': curr_short_ema,
                'long_ema': curr_long_ema,
            }
        
        prev_diff = curr_diff
    
    return None, None, None


# ============================================================================
# Single Stock Processing
# ============================================================================

def process_single_stock(symbol_data: Dict, use_cache: bool = True) -> CrossoverResult:
    """
    Process a single stock: fetch data, calculate EMAs, detect crossover.
    """
    symbol = symbol_data['symbol']
    company = symbol_data['name']
    exchange = symbol_data.get('exchange')
    
    try:
        df, source = fetch_stock_data(symbol, exchange, use_cache)
    except FetchFailure as failure:
        return CrossoverResult(
            symbol=symbol, company=company, exchange=exchange, crossover_type=None,
            date=None, price=None, current_price=None, pct_change=None,
            short_ema=None, long_ema=None, error=str(failure),
            error_kind=failure.kind, fetch_attempts=failure.attempts)
    
    if df.empty:
        return CrossoverResult(
            symbol=symbol, company=company, exchange=exchange, crossover_type=None,
            date=None, price=None, current_price=None, pct_change=None,
            short_ema=None, long_ema=None, df=None, 
            error="No usable historical data; symbol identity is unconfirmed", error_kind="no_data"
        )
    
    # Check if we have enough data for reliable EMA calculation
    if len(df) < LONG_EMA:
        return CrossoverResult(
            symbol=symbol, company=company, exchange=exchange, crossover_type=None,
            date=None, price=None, current_price=None, pct_change=None,
            short_ema=None, long_ema=None, df=None,
            error=f"Insufficient data ({len(df)} days) - need {LONG_EMA} days for EMA",
            error_kind="insufficient_history"
        )
    
    days = df.index.tz_localize(None).normalize() if df.index.tz is not None else df.index.normalize()
    week_days = (days >= pd.Timestamp(TARGET_WEEK_START)) & (days <= pd.Timestamp(TARGET_WEEK_END))
    if week_days.sum() < 2 or not (days < pd.Timestamp(TARGET_WEEK_START)).any():
        return CrossoverResult(
            symbol=symbol, company=company, exchange=exchange, crossover_type=None,
            date=None, price=None, current_price=None, pct_change=None,
            short_ema=None, long_ema=None, error='Insufficient observations for the reporting week',
            error_kind='missing_report_week')

    df[f'EMA_{SHORT_EMA}'] = calculate_ema(df, SHORT_EMA)
    df[f'EMA_{LONG_EMA}'] = calculate_ema(df, LONG_EMA)
    
    crossover_type, crossover_date, crossover_data = detect_crossover(
        df, TARGET_WEEK_START, TARGET_WEEK_END
    )
    
    if crossover_type:
        current_price = df['Close'].iloc[-1]
        pct_change = ((current_price - crossover_data['close']) / 
                      crossover_data['close']) * 100
        
        return CrossoverResult(
            symbol=symbol, company=company, exchange=exchange, crossover_type=crossover_type,
            date=crossover_date, price=crossover_data['close'],
            current_price=current_price, pct_change=pct_change,
            short_ema=crossover_data['short_ema'], long_ema=crossover_data['long_ema'],
            df=df, error=None
        )
    
    return CrossoverResult(
        symbol=symbol, company=company, exchange=exchange, crossover_type=None,
        date=None, price=None, current_price=None, pct_change=None,
        short_ema=None, long_ema=None, df=None, error=None
    )


# ============================================================================
# Chart Generation
# ============================================================================

def generate_chart(result: CrossoverResult, output_dir: Path) -> Path:
    """Generate an annotated chart for a crossover."""
    
    df = result.df
    chart_df = df.iloc[-120:]
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    price_color = '#2E86AB'
    short_ema_color = '#F18F01'
    long_ema_color = '#C73E1D'
    crossover_color = '#45B69C' if result.crossover_type == 'bullish' else '#C73E1D'
    
    ax.plot(chart_df.index, chart_df['Close'], label='Close Price', 
            color=price_color, linewidth=1.5, alpha=0.9)
    ax.plot(chart_df.index, chart_df[f'EMA_{SHORT_EMA}'], 
            label=f'{SHORT_EMA}-day EMA', color=short_ema_color, 
            linewidth=2, linestyle='--')
    ax.plot(chart_df.index, chart_df[f'EMA_{LONG_EMA}'], 
            label=f'{LONG_EMA}-day EMA', color=long_ema_color, 
            linewidth=2, linestyle='--')
    
    ax.axvline(x=result.date, color=crossover_color, linestyle=':', 
               linewidth=2, alpha=0.7, label='Crossover')
    ax.scatter([result.date], [result.price], 
               color=crossover_color, s=150, zorder=5, marker='o', 
               edgecolors='white', linewidths=2)
    
    crossover_label = 'BULLISH' if result.crossover_type == 'bullish' else 'BEARISH'
    annotation_text = (
        f"{crossover_label} CROSSOVER\n"
        f"Date: {result.date.strftime('%Y-%m-%d')}\n"
        f"Price: ${result.price:.2f}\n"
        f"{SHORT_EMA} EMA: ${result.short_ema:.2f}\n"
        f"{LONG_EMA} EMA: ${result.long_ema:.2f}\n"
        f"Current: ${result.current_price:.2f}\n"
        f"Change: {result.pct_change:+.2f}%"
    )
    
    box_color = '#d4edda' if result.crossover_type == 'bullish' else '#f8d7da'
    props = dict(boxstyle='round,pad=0.5', facecolor=box_color, 
                 alpha=0.9, edgecolor='gray')
    ax.text(0.02, 0.98, annotation_text, transform=ax.transAxes, fontsize=9,
            verticalalignment='top', fontfamily='monospace', bbox=props)
    
    ax.set_title(f'{result.symbol} - {result.company[:40]}', 
                 fontsize=12, fontweight='bold', pad=10)
    ax.set_xlabel('Date', fontsize=10)
    ax.set_ylabel('Price', fontsize=10)
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, alpha=0.3)
    
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    plt.xticks(rotation=45, ha='right')
    
    plt.tight_layout()
    
    output_path = output_dir / f"{result.chart_id}.png"
    plt.savefig(output_path, dpi=120, bbox_inches='tight', 
                facecolor='white', edgecolor='none')
    plt.close()
    
    return output_path


# ============================================================================
# PDF Generation
# ============================================================================

def generate_pdf_report(all_results: List[CSVResults], output_path: Path, summary=None):
    """Generate a comprehensive PDF report with all results."""
    
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=1*cm,
        leftMargin=1*cm,
        topMargin=1.5*cm,
        bottomMargin=1.5*cm
    )
    
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=20,
        alignment=TA_CENTER,
        spaceAfter=20,
        textColor=HexColor('#2E86AB')
    )
    
    section_style = ParagraphStyle(
        'SectionTitle',
        parent=styles['Heading2'],
        fontSize=16,
        spaceBefore=15,
        spaceAfter=10,
        textColor=HexColor('#2E86AB')
    )
    
    subsection_style = ParagraphStyle(
        'SubsectionTitle',
        parent=styles['Heading3'],
        fontSize=12,
        spaceBefore=10,
        spaceAfter=5,
        textColor=HexColor('#333333')
    )
    
    bullish_style = ParagraphStyle(
        'Bullish',
        parent=styles['Normal'],
        fontSize=10,
        textColor=HexColor('#155724'),
        leftIndent=10
    )
    
    bearish_style = ParagraphStyle(
        'Bearish',
        parent=styles['Normal'],
        fontSize=10,
        textColor=HexColor('#721c24'),
        leftIndent=10
    )
    
    failed_style = ParagraphStyle(
        'Failed',
        parent=styles['Normal'],
        fontSize=9,
        textColor=HexColor('#6c757d'),
        leftIndent=10
    )
    
    normal_style = ParagraphStyle(
        'CustomNormal',
        parent=styles['Normal'],
        fontSize=10
    )
    
    story = []
    
    # Title page
    story.append(Paragraph("EMA Crossover Report", title_style))
    story.append(Paragraph(
        f"<b>{SHORT_EMA}/{LONG_EMA} Day EMA Analysis</b>",
        ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=14, alignment=TA_CENTER)
    ))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"Target Week: {TARGET_WEEK_START.strftime('%B %d, %Y')} - {TARGET_WEEK_END.strftime('%B %d, %Y')}",
        ParagraphStyle('DateRange', parent=styles['Normal'], fontSize=12, alignment=TA_CENTER)
    ))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ParagraphStyle('Generated', parent=styles['Normal'], fontSize=10, alignment=TA_CENTER, textColor=HexColor('#666666'))
    ))
    story.append(Spacer(1, 30))
    
    if summary is not None:
        totals = summary['totals']
        story.append(Paragraph(f"<b>Data quality: {summary['status'].upper()}</b>", subsection_style))
        story.append(Paragraph(
            f"Successfully analyzed: {totals['successful_symbols']}/{totals['total_symbols']} "
            f"({totals['coverage']:.2%}); failures: {totals['failed_symbols']}; "
            f"operational errors: {totals['operational_errors']}. No-crossover analyses count as successful.", normal_style))
        policy = summary['policy']
        story.append(Paragraph(
            f"Minimum coverage: run {policy['min_coverage']:.2%}, each market {policy['min_market_coverage']:.2%}. "
            f"Maximum operational error rate: run {policy['max_operational_error_rate']:.2%}, "
            f"each market {policy['max_market_operational_error_rate']:.2%}. "
            "Missing symbols and short history remain gaps; incomplete reports do not establish absence of crossovers.", normal_style))
        for violation in summary['violations']:
            story.append(Paragraph(escape(violation), failed_style))
        story.append(Spacer(1, 12))

    # Summary table
    summary_data = [['Category', 'Symbols', 'Bullish', 'Bearish', 'Overall', 'Errors']]
    total_symbols = 0
    total_bullish = 0
    total_bearish = 0
    total_errors = 0
    
    # Track which rows have bullish/bearish overall for conditional formatting
    overall_values = []
    
    for csv_result in all_results:
        n_bullish = len(csv_result.bullish)
        n_bearish = len(csv_result.bearish)
        if n_bullish == 0 and n_bearish == 0:
            overall = '\u2014'  # em dash
        elif n_bearish / csv_result.total_symbols > n_bullish / csv_result.total_symbols:
            overall = 'Bearish'
        else:
            overall = 'Bullish'
        overall_values.append(overall)
        summary_data.append([
            csv_result.display_name,
            str(csv_result.total_symbols),
            str(n_bullish),
            str(n_bearish),
            overall,
            str(csv_result.errors)
        ])
        total_symbols += csv_result.total_symbols
        total_bullish += n_bullish
        total_bearish += n_bearish
        total_errors += csv_result.errors
    
    # Overall for TOTAL row
    if total_bullish == 0 and total_bearish == 0:
        total_overall = '\u2014'
    elif total_bearish / max(total_symbols, 1) > total_bullish / max(total_symbols, 1):
        total_overall = 'Bearish'
    else:
        total_overall = 'Bullish'
    overall_values.append(total_overall)
    summary_data.append(['TOTAL', str(total_symbols), str(total_bullish), str(total_bearish), total_overall, str(total_errors)])
    
    summary_table = Table(summary_data, colWidths=[3.5*cm, 2*cm, 1.8*cm, 1.8*cm, 2*cm, 1.5*cm])
    
    table_style_commands = [
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#2E86AB')),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
        ('TOPPADDING', (0, 0), (-1, 0), 10),
        ('BACKGROUND', (0, -1), (-1, -1), HexColor('#e9ecef')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#dee2e6')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [HexColor('#ffffff'), HexColor('#f8f9fa')]),
    ]
    
    # Conditional coloring for the Overall column (column index 4)
    for row_idx, val in enumerate(overall_values):
        data_row = row_idx + 1  # offset for header row
        if val == 'Bullish':
            table_style_commands.append(('TEXTCOLOR', (4, data_row), (4, data_row), HexColor('#155724')))
            table_style_commands.append(('FONTNAME', (4, data_row), (4, data_row), 'Helvetica-Bold'))
        elif val == 'Bearish':
            table_style_commands.append(('TEXTCOLOR', (4, data_row), (4, data_row), HexColor('#721c24')))
            table_style_commands.append(('FONTNAME', (4, data_row), (4, data_row), 'Helvetica-Bold'))
    
    summary_table.setStyle(TableStyle(table_style_commands))
    story.append(summary_table)
    
    # Process each CSV category
    for csv_result in all_results:
        story.append(PageBreak())
        story.append(Paragraph(csv_result.display_name, section_style))
        story.append(Paragraph(
            f"Total: {csv_result.total_symbols} symbols | "
            f"Bullish: {len(csv_result.bullish)} | "
            f"Bearish: {len(csv_result.bearish)}",
            normal_style
        ))
        story.append(Spacer(1, 15))
        
        if csv_result.input_error:
            story.append(Paragraph(escape(csv_result.input_error), failed_style))

        # Bullish crossovers
        if csv_result.bullish:
            story.append(Paragraph("🟢 Bullish Crossovers", subsection_style))
            sorted_bullish = sorted(csv_result.bullish, key=lambda x: x.pct_change or 0, reverse=True)
            for i, result in enumerate(sorted_bullish, 1):
                anchor_id = result.chart_id
                link_text = (
                    f"<a href=\"#{anchor_id}\" color=\"#155724\">"
                    f"{i}. <b>{result.symbol}</b></a> - {result.company[:35]} | "
                    f"Date: {result.date.strftime('%Y-%m-%d')} | "
                    f"Price: ${result.price:.2f} | "
                    f"Change: <b>{result.pct_change:+.2f}%</b>"
                )
                story.append(Paragraph(link_text, bullish_style))
            story.append(Spacer(1, 10))
        else:
            story.append(Paragraph("🟢 Bullish Crossovers: None", subsection_style))
            story.append(Spacer(1, 10))
        
        # Bearish crossovers
        if csv_result.bearish:
            story.append(Paragraph("🔴 Bearish Crossovers", subsection_style))
            sorted_bearish = sorted(csv_result.bearish, key=lambda x: x.pct_change or 0)
            for i, result in enumerate(sorted_bearish, 1):
                anchor_id = result.chart_id
                link_text = (
                    f"<a href=\"#{anchor_id}\" color=\"#721c24\">"
                    f"{i}. <b>{result.symbol}</b></a> - {result.company[:35]} | "
                    f"Date: {result.date.strftime('%Y-%m-%d')} | "
                    f"Price: ${result.price:.2f} | "
                    f"Change: <b>{result.pct_change:+.2f}%</b>"
                )
                story.append(Paragraph(link_text, bearish_style))
            story.append(Spacer(1, 10))
        else:
            story.append(Paragraph("🔴 Bearish Crossovers: None", subsection_style))
        
        # Failed symbols section
        if csv_result.failed:
            story.append(Spacer(1, 10))
            story.append(Paragraph("⚠️ Failed Symbols", subsection_style))
            for i, result in enumerate(csv_result.failed, 1):
                error_text = (
                    f"{i}. <b>{escape(result.symbol)}</b> - {escape(result.company[:30])} | "
                    f"<i>{escape(result.error or '')}</i>"
                )
                story.append(Paragraph(error_text, failed_style))
            story.append(Spacer(1, 10))
    
    # Charts section
    story.append(PageBreak())
    story.append(Paragraph("Charts", title_style))
    
    for csv_result in all_results:
        all_crossovers = csv_result.bullish + csv_result.bearish
        if not all_crossovers:
            continue
            
        story.append(Paragraph(csv_result.display_name, section_style))
        
        for result in all_crossovers:
            if result.chart_path and result.chart_path.exists():
                anchor_id = result.chart_id
                
                crossover_emoji = "🟢" if result.crossover_type == 'bullish' else "🔴"
                
                # Create chart block with title - keep together on same page
                chart_title = Paragraph(
                    f'<a name="{anchor_id}"/><b>{crossover_emoji} {result.symbol}</b> - {result.company[:50]}',
                    subsection_style
                )
                img = Image(str(result.chart_path), width=19*cm, height=12*cm)
                
                # Use KeepTogether to ensure title stays with chart
                chart_block = KeepTogether([chart_title, img, Spacer(1, 5)])
                story.append(chart_block)
    
    # Build PDF
    doc.build(story)
    print(f"\n📄 PDF Report saved: {output_path}")


# ============================================================================
# Main Screener
# ============================================================================

# Display name overrides for proper capitalization/abbreviation
DISPLAY_NAME_OVERRIDES = {
    'Nse': 'NSE',
    'Nyse': 'NYSE',
    'Singapore': 'SGX',
    'Etfs': 'ETFs',
}


def process_csv_file(csv_path: Path, use_cache: bool = True) -> CSVResults:
    """Process a single CSV file and return results."""
    
    csv_name = csv_path.stem
    display_name = csv_name.replace('_', ' ').replace('-', ' ').title()
    # Apply display name overrides for proper capitalization
    for wrong, correct in DISPLAY_NAME_OVERRIDES.items():
        display_name = display_name.replace(wrong, correct)
    
    print(f"\n{'='*60}")
    print(f"  Processing: {display_name}")
    print(f"{'='*60}")
    
    try:
        symbols = read_csv_symbols(csv_path)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        return CSVResults(csv_name=csv_name, display_name=display_name,
                          input_error=f'Unreadable CSV: {type(exc).__name__}')
    if not symbols:
        return CSVResults(csv_name=csv_name, display_name=display_name,
                          input_error='CSV contains no valid symbols or has no Symbol/Ticker column')
    
    print(f"📊 {len(symbols)} symbols found")
    
    results = CSVResults(
        csv_name=csv_name,
        display_name=display_name,
        total_symbols=len(symbols)
    )
    
    processed = 0
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {
            executor.submit(process_single_stock, sym_data, use_cache): sym_data
            for sym_data in symbols
        }
        
        for future in as_completed(future_to_symbol):
            sym_data = future_to_symbol[future]
            processed += 1
            
            try:
                result = future.result()
                result.category = csv_name
                
                if result.error:
                    results.errors += 1
                    results.failed.append(result)
                    status = "❌"
                elif result.crossover_type == 'bullish':
                    # Generate chart
                    result.chart_path = generate_chart(result, CHARTS_DIR)
                    results.bullish.append(result)
                    status = f"🟢 BULLISH"
                elif result.crossover_type == 'bearish':
                    # Generate chart
                    result.chart_path = generate_chart(result, CHARTS_DIR)
                    results.bearish.append(result)
                    status = f"🔴 BEARISH"
                else:
                    status = "✓"
                
                print(f"[{processed:4}/{len(symbols)}] {sym_data['symbol']:8} {status}")
                
            except Exception as e:
                results.errors += 1
                error_result = CrossoverResult(
                    symbol=sym_data['symbol'], company=sym_data['name'], crossover_type=None,
                    date=None, price=None, current_price=None, pct_change=None,
                    short_ema=None, long_ema=None, df=None, exchange=sym_data.get('exchange'),
                    category=csv_name, error=f"Processing error: {type(e).__name__}", error_kind="processing_error"
                )
                results.failed.append(error_result)
                print(f"[{processed:4}/{len(symbols)}] {sym_data['symbol']:8} ❌ Error")
    
    print(f"\n  {display_name}: {len(results.bullish)} bullish, {len(results.bearish)} bearish, {len(results.failed)} failed")
    
    return results


def run_screener(use_cache: bool = True, policy=None):
    """Return a process status; preserve diagnostic evidence before failing the gate."""
    global _fetch_guard
    policy = policy or QualityPolicy()
    _fetch_guard = FetchGuard()
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    groups = []
    run_error = None
    try:
        init_cache()
        for csv_path in sorted(SYMBOL_DATA_DIR.glob('*.csv')):
            groups.append(process_csv_file(csv_path, use_cache))
    except Exception as exc:
        run_error = f'Run interrupted: {type(exc).__name__}'
    finally:
        _fetch_guard = None
    summary = quality_summary(groups, policy)
    if run_error:
        summary['violations'].append(run_error)
        summary.update(status='failed', gate_passed=False, email_allowed=False)
    summary.update(target_week_start=TARGET_WEEK_START.date().isoformat(),
                   target_week_end=TARGET_WEEK_END.date().isoformat(),
                   generated_at=datetime.now().astimezone().isoformat(),
                   report_generated=False, report_path=None)
    summary_path = OUTPUT_DIR / 'run_summary.json'
    summary_path.write_text(json.dumps(summary, indent=2) + '\n')
    pdf_path = OUTPUT_DIR / f"EMA_Crossover_Report_{TARGET_WEEK_END:%Y-%m-%d}.pdf"
    try:
        generate_pdf_report(groups, pdf_path, summary=summary)
        summary.update(report_generated=True, report_path=pdf_path.name)
    except Exception as exc:
        summary['violations'].append(f'PDF generation failed: {type(exc).__name__}')
        summary.update(status='failed', gate_passed=False, email_allowed=False)
        if pdf_path.exists():
            pdf_path.unlink()  # Do not retain a half-written PDF as a valid report.
    summary_path.write_text(json.dumps(summary, indent=2) + '\n')
    print(f"Data quality: {summary['status'].upper()}; "
          f"analyzed {summary['totals']['successful_symbols']}/{summary['totals']['total_symbols']}; "
          f"errors {summary['totals']['failed_symbols']}")
    for violation in summary['violations']:
        print(f'ERROR: {violation}')
    print(f'Diagnostic summary: {summary_path.absolute()}')
    return 0 if summary['email_allowed'] else 2


def main(argv=None):
    import argparse
    import math

    def fraction(value):
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError('must be a fraction from 0 to 1')
        if not math.isfinite(number) or not 0 <= number <= 1:
            raise argparse.ArgumentTypeError('must be a finite fraction from 0 to 1')
        return number

    parser = argparse.ArgumentParser(description='EMA screener with per-run and per-market data quality gates')
    parser.add_argument('--no-cache', action='store_true')
    defaults = QualityPolicy()
    for name, help_text in (
        ('min_coverage', 'Minimum successfully analyzed fraction across all symbols'),
        ('min_market_coverage', 'Minimum successfully analyzed fraction in each CSV market'),
        ('max_operational_error_rate', 'Maximum provider/processing error fraction across all symbols'),
        ('max_market_operational_error_rate', 'Maximum provider/processing error fraction in each CSV market')):
        parser.add_argument('--' + name.replace('_', '-'), type=fraction,
                            default=getattr(defaults, name), help=help_text)
    args = parser.parse_args(argv)
    policy = QualityPolicy(**{name: getattr(args, name) for name in defaults.__dataclass_fields__})
    return run_screener(use_cache=not args.no_cache, policy=policy)


if __name__ == '__main__':
    sys.exit(main())
