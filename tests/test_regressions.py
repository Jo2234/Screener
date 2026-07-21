"""Offline regression tests: synthetic prices and fake local delivery only."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from dataclasses import replace

import pandas as pd
import pytest

import reporting
import scheduler
import screener


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    import socket

    def fail(*args, **kwargs):
        raise AssertionError("Regression tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "create_connection", fail)


def test_scheduled_utc_instant_uses_just_completed_singapore_week():
    instant = datetime(2026, 9, 4, 23, tzinfo=timezone.utc)
    assert reporting.get_previous_full_week(instant) == (
        datetime(2026, 8, 31), datetime(2026, 9, 4)
    )
    # One minute before Saturday Singapore still excludes the incomplete week.
    assert reporting.get_previous_full_week(datetime(2026, 9, 4, 15, 59, tzinfo=timezone.utc)) == (
        datetime(2026, 8, 24), datetime(2026, 8, 28)
    )


@pytest.mark.parametrize("day, expected_friday", [
    (date(2026, 9, 4), datetime(2026, 8, 28)),
    (date(2026, 9, 5), datetime(2026, 9, 4)),
    (date(2026, 9, 6), datetime(2026, 9, 4)),
    (date(2026, 9, 7), datetime(2026, 9, 4)),
    (date(2027, 1, 2), datetime(2027, 1, 1)),
])
def test_calendar_dates(day, expected_friday):
    assert reporting.get_previous_full_week(day)[1] == expected_friday


def test_clock_is_explicit_when_no_as_of_is_given(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == reporting.REPORTING_TIMEZONE
            return datetime(2026, 9, 4, 23, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(reporting, "datetime", Clock)
    assert reporting.get_previous_full_week()[1] == datetime(2026, 9, 4)


def test_ambiguous_naive_instant_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        reporting.get_previous_full_week(datetime(2026, 9, 4, 23))


def test_reporting_timezone_override(tmp_path):
    code = "from reporting import *; from datetime import timezone; print(get_previous_full_week(datetime(2026,9,4,23,tzinfo=timezone.utc))[1].date())"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(screener.__file__).parent,
        env={**os.environ, "REPORT_TIMEZONE": "UTC"}, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "2026-08-28"


def test_cross_market_charts_and_pdf_keep_distinct_images(tmp_path, monkeypatch):
    monkeypatch.setattr(screener, "TARGET_WEEK_START", datetime(2026, 8, 31))
    monkeypatch.setattr(screener, "TARGET_WEEK_END", datetime(2026, 9, 4))
    monkeypatch.setattr(screener, "CHARTS_DIR", tmp_path)
    market_indexes = {}

    def fake_fetch(symbol, exchange, use_cache):
        # A long downtrend followed by a sharp Monday jump produces a real
        # bullish EMA crossover. Each exchange retains its session timezone.
        tz = "Asia/Kolkata" if exchange == "NSEI" else "America/New_York"
        index = pd.bdate_range(end="2026-09-04", periods=220, tz=tz)
        market_indexes[exchange] = index
        prices = [120 - n / 10 for n in range(215)] + [500] * 5
        if exchange == "NYSE":
            prices = [price * 2 for price in prices]
        return pd.DataFrame({"Close": prices}, index=index), "synthetic"

    monkeypatch.setattr(screener, "fetch_stock_data", fake_fetch)
    results = []
    for category, exchange, company in [
        ("NSE", "NSEI", "Hindustan Aeronautics"),
        ("NYSE", "NYSE", "Halliburton"),
    ]:
        csv_file = tmp_path / f"{category}.csv"
        csv_file.write_text(f"Symbol,Name,Exchange\nHAL,{company},{exchange}\n")
        result = screener.process_csv_file(csv_file, use_cache=False)
        assert len(result.bullish) == 1
        assert not result.failed
        results.append(result)

    first, second = [group.bullish[0] for group in results]
    assert first.symbol == second.symbol == "HAL"
    assert first.category == "NSE" and second.category == "NYSE"
    assert first.exchange == "NSEI" and second.exchange == "NYSE"
    assert first.chart_path != second.chart_path
    assert first.chart_path.read_bytes() != second.chart_path.read_bytes()
    for result in (first, second):
        pd.testing.assert_index_equal(result.df.index, market_indexes[result.exchange])

    embedded_paths = []
    real_image = screener.Image

    def record_image(filename, **kwargs):
        embedded_paths.append(Path(filename))
        return real_image(filename, **kwargs)

    monkeypatch.setattr(screener, "Image", record_image)
    pdf = tmp_path / "report.pdf"
    screener.generate_pdf_report(results, pdf)
    assert pdf.read_bytes().startswith(b"%PDF")
    assert embedded_paths == [first.chart_path, second.chart_path]

    # Aliases, sanitized symbols, categories, exchanges, and directions cannot
    # collapse into the same filename/bookmark identity.
    variants = [
        first, second, replace(first, symbol="A/B"), replace(first, symbol="A_B"),
        replace(first, symbol="BRKA"), replace(first, symbol="BRK-A"),
        replace(first, category="NYSE"), replace(first, exchange="NYSE"),
        replace(first, crossover_type="bearish"),
    ]
    assert len({value.chart_id for value in variants}) == len(variants)
    assert all("/" not in value.chart_id for value in variants)
    assert replace(first).chart_id == first.chart_id


def test_scheduler_uses_reporting_timezone():
    next_run = scheduler.get_next_run_time(datetime(2026, 9, 4, 23, tzinfo=timezone.utc))
    assert next_run.isoformat() == "2026-09-05T09:00:00+08:00"
    # At the scheduled minute, choose next week to avoid a duplicate run.
    assert scheduler.get_next_run_time(next_run).isoformat() == "2026-09-12T09:00:00+08:00"


@pytest.mark.parametrize("override", [None, "/custom environment/bin/python"])
def test_scheduler_resolves_moved_checkout_and_passes_interpreter(tmp_path, monkeypatch, override):
    checkout = tmp_path / "moved checkout"
    checkout.mkdir()
    source = Path(scheduler.__file__)
    shutil.copy2(source, checkout / source.name)
    spec = importlib.util.spec_from_file_location("relocated_scheduler", checkout / source.name)
    relocated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relocated)
    monkeypatch.chdir(tmp_path)
    if override:
        monkeypatch.setenv("PYTHON_PATH", override)
    else:
        monkeypatch.delenv("PYTHON_PATH", raising=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="fake run", stderr="")

    monkeypatch.setattr(relocated.subprocess, "run", fake_run)
    relocated.run_screener()
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["/bin/bash", str(checkout / "run_screener_and_email.sh")]
    assert kwargs["cwd"] == checkout
    assert kwargs["env"]["PYTHON_PATH"] == (override or sys.executable)
    assert (checkout / "logs" / "scheduler.log").exists()


@pytest.mark.parametrize("custom_config,custom_python", [(False, True), (True, False)])
def test_wrapper_runs_from_any_directory_with_local_config(tmp_path, custom_config, custom_python):
    checkout = tmp_path / "checkout with spaces"
    checkout.mkdir()
    wrapper = checkout / "run_screener_and_email.sh"
    shutil.copy2(Path(scheduler.__file__).with_name(wrapper.name), wrapper)
    # These are fake entrypoints, not the real screener or email sender.
    stub = '''import json, os, pathlib, sys
root = pathlib.Path.cwd()
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps([pathlib.Path(__file__).name, str(root), sys.argv[1:], os.environ["TEST_LOCAL_CONFIG"]]) + "\\n")
if pathlib.Path(__file__).name == "screener.py":
    (root / "output").mkdir()
    (root / "output" / "fake report.pdf").write_bytes(b"fake PDF")
'''
    (checkout / "screener.py").write_text(stub)
    (checkout / "send_email.py").write_text(stub)
    config = (tmp_path / "custom config") if custom_config else (checkout / ".email_config")
    config.write_text("export TEST_LOCAL_CONFIG='loaded from local file'\n")
    env = dict(os.environ)
    env.pop("PYTHON_PATH", None)
    env.pop("EMAIL_CONFIG", None)
    if custom_config:
        env["EMAIL_CONFIG"] = str(config)
    if custom_python:
        env["PYTHON_PATH"] = sys.executable
    else:
        # A minimal active environment proves the PATH fallback without relying
        # on the machine's default python3 installation.
        bin_dir = tmp_path / "active environment" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python3").symlink_to(sys.executable)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    subprocess.run(["/bin/bash", str(wrapper)], cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    calls = [json.loads(line) for line in (checkout / "calls.jsonl").read_text().splitlines()]
    assert calls == [
        ["screener.py", str(checkout), ["--no-cache"], "loaded from local file"],
        ["send_email.py", str(checkout), [str(checkout / "output" / "fake report.pdf")], "loaded from local file"],
    ]
