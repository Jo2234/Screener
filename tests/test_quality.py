"""Provider responses and quality gates are tested without network or email."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

import screener
from reliability import FetchFailure, FetchGuard, QualityPolicy, quality_summary


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError('Tests must not use network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(screener.time, 'sleep', lambda *args: None)
    monkeypatch.setattr(screener, '_fetch_guard', None)


def response(status=200, payload=None, invalid_json=False):
    def decode():
        if invalid_json:
            raise ValueError('invalid json')
        return payload
    return SimpleNamespace(status_code=status, json=decode)


def chart(closes=(1, 2), symbol='FAKE', timestamps=None):
    if timestamps is None:
        timestamps = [n * 86400 for n in range(len(closes))]
    return {'chart': {'error': None, 'result': [{'meta': {'symbol': symbol, 'dataGranularity': '1d'},
            'timestamp': timestamps,
            'indicators': {'quote': [{key: list(closes) for key in ('open', 'high', 'low', 'close', 'volume')}]}}]}}


# Representative shape of Yahoo's HTTP 200 answer for a known but delisted symbol:
# metadata identifies the symbol, but there are no timestamps or quote series.
def delisted(symbol='FAKE', timestamp_key=True):
    result = {'meta': {'symbol': symbol, 'currency': 'USD', 'instrumentType': 'EQUITY',
                       'dataGranularity': '1d', 'validRanges': ['1d', '5d', 'max']},
              'indicators': {'quote': [{}]}}
    if timestamp_key:
        result['timestamp'] = None
    return {'chart': {'error': None, 'result': [result]}}


@pytest.mark.parametrize('fake, kind', [
    (response(429), 'rate_limit'), (response(503), 'http_service'),
    (response(404, invalid_json=True), 'http_error'),
    (response(404, {'chart': {'error': {'code': 'Not Found'}}}), 'not_found'),
    (response(200, invalid_json=True), 'json_error'),
    (response(200, {}), 'data_error'),
    (response(200, {'chart': {'result': []}}), 'missing_result'),
    (response(200, {'chart': {'result': [{'meta': {'symbol': 'fake'}, 'timestamp': []}]}}), 'no_data'),
    (response(200, delisted()), 'no_data'),
    (response(200, delisted(timestamp_key=False)), 'no_data'),
    (response(200, chart([None, None])), 'empty_prices'),
    # Malformed or unconfirmed charts must stay operational, never ordinary gaps.
    (response(200, {'chart': {'error': None, 'result': [{}]}}), 'data_error'),
    (response(200, {'chart': {'error': None, 'result': [{'timestamp': {}}]}}), 'data_error'),
    (response(200, {'chart': {'error': None, 'result': [{'timestamp': False}]}}), 'data_error'),
    (response(200, {'chart': {'error': None, 'result': [{'meta': {'symbol': 'FAKE'}, 'timestamp': False}]}}), 'data_error'),
    (response(200, {'chart': {'error': None, 'result': [{'meta': {'symbol': 'FAKE'}, 'timestamp': 0}]}}), 'data_error'),
    (response(200, {'chart': {'error': None, 'result': [{'meta': {'symbol': 'FAKE'}, 'timestamp': ''}]}}), 'data_error'),
    (response(200, {'chart': {'error': None, 'result': [{'meta': {}, 'timestamp': None}]}}), 'data_error'),
    (response(200, {'chart': {'error': None, 'result': [None]}}), 'data_error'),
    (response(200, {'chart': {'error': None, 'result': {}}}), 'data_error'),
    (response(200, delisted(symbol='OTHER')), 'data_error'),
    (response(200, chart([None, None], symbol='OTHER')), 'data_error'),
    (response(200, chart(timestamps='ab')), 'data_error'),
    (response(200, chart(timestamps=[True, False])), 'data_error'),
    (response(200, chart(timestamps=[0])), 'data_error'),
    (response(200, chart([float('inf'), 2])), 'data_error'),
    (response(200, chart([-1, 2])), 'data_error'),
])
def test_provider_reasons(monkeypatch, fake, kind):
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: fake)
    with pytest.raises(FetchFailure) as failure:
        screener.fetch_via_direct_api('FAKE')
    assert failure.value.kind == kind


@pytest.mark.parametrize('timestamp', [
    pytest.param(float('nan'), id='nan'),
    pytest.param(float('inf'), id='positive-infinity'),
    pytest.param(float('-inf'), id='negative-infinity'),
    pytest.param(1e100, id='out-of-range-float'),
    pytest.param(10 ** 400, id='out-of-range-integer'),
    pytest.param(-(2 ** 63), id='nat-sentinel'),
])
def test_invalid_timestamp_is_typed_operational_failure(monkeypatch, timestamp):
    payload = chart([10.] * 200, timestamps=[timestamp] * 200)
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: response(200, payload))
    with pytest.raises(FetchFailure) as failure:
        screener.fetch_via_direct_api('FAKE')
    assert failure.value.kind == 'data_error' and failure.value.retryable
    result = screener.process_single_stock({'symbol': 'FAKE', 'name': 'Fake'}, False)
    assert result.error_kind == 'data_error'
    assert len(result.fetch_attempts) == screener.MAX_RETRIES
    summary = quality_summary([screener.CSVResults(
        'NYSE', 'NYSE', total_symbols=100, errors=2, failed=[result, result])], QualityPolicy())
    assert summary['status'] == 'failed' and not summary['email_allowed']
    assert summary['totals']['operational_errors'] == 2


@pytest.mark.parametrize('exception, kind', [(requests.Timeout(), 'timeout'), (requests.ConnectionError(), 'transport')])
def test_transport_is_not_missing_symbol(monkeypatch, exception, kind):
    def fail(*args, **kwargs):
        raise exception
    monkeypatch.setattr(requests, 'get', fail)
    result = screener.process_single_stock({'symbol': 'FAKE', 'name': 'Fake', 'exchange': 'Singapore'}, False)
    assert result.error_kind == kind
    assert len(result.fetch_attempts) == screener.MAX_RETRIES
    assert {row['symbol'] for row in result.fetch_attempts} == {'FAKE'}
    assert 'not found' not in result.error.lower()


def test_rate_limit_retries_then_succeeds_without_alias(monkeypatch):
    calls = []
    responses = [response(429), response(200, chart())]
    def get(url, **kwargs):
        calls.append(url)
        return responses.pop(0)
    monkeypatch.setattr(requests, 'get', get)
    frame, source = screener.fetch_stock_data('FAKE', 'Singapore', False)
    assert len(frame) == 2 and source == 'fetch'
    assert len(calls) == 2 and all(url.endswith('/FAKE') for url in calls)


def test_definitive_missing_can_try_valid_alias_once(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return response(404, {'chart': {'error': {'code': 'Not Found'}}})
    monkeypatch.setattr(requests, 'get', get)
    with pytest.raises(FetchFailure) as failure:
        screener.fetch_stock_data('FAKE', 'Singapore', False)
    assert failure.value.kind == 'not_found'
    assert len(calls) == 2
    assert [row['symbol'] for row in failure.value.attempts] == ['FAKE', 'FAKE.SI']


def test_ambiguous_primary_is_not_erased_by_missing_alias(monkeypatch):
    responses = [response(404, {}), response(404, {'chart': {'error': {'code': 'Not Found'}}})]
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: responses.pop(0))
    with pytest.raises(FetchFailure) as failure:
        screener.fetch_stock_data('FAKE', 'Singapore', False)
    assert failure.value.kind == 'http_error'
    assert len(failure.value.attempts) == 2


def failed(kind='not_found', symbol='FAKE'):
    return screener.CrossoverResult(symbol, 'Fake', None, None, None, None, None, None, None,
                                   error=f'Test {kind}', error_kind=kind)


def group(total, errors=0, kind='not_found', name='NYSE'):
    return screener.CSVResults(name, name, total_symbols=total, errors=errors,
                               failed=[failed(kind, str(n)) for n in range(errors)])


@pytest.mark.parametrize('groups, status', [
    ([group(100)], 'complete'), ([group(100, 5)], 'partial'),
    ([group(100, 6)], 'failed'), ([group(100, 1, 'rate_limit')], 'partial'),
    ([group(100, 2, 'rate_limit')], 'failed'),
    ([group(4000), group(10, 10, name='SGX')], 'failed'),
    ([group(100, 100)], 'failed'), ([], 'failed'),
])
def test_quality_boundaries(groups, status):
    summary = quality_summary(groups, QualityPolicy())
    assert summary['status'] == status
    assert summary['gate_passed'] == summary['email_allowed'] == (status != 'failed')
    assert summary['totals']['successful_symbols'] == sum(g.total_symbols - g.errors for g in groups)


def test_166_gaps_distinct_from_166_provider_failures():
    assert quality_summary([group(4127, 166)], QualityPolicy())['status'] == 'partial'
    assert quality_summary([group(4127, 166, 'transport')], QualityPolicy())['status'] == 'failed'


def test_market_operational_boundary_even_when_overall_rate_is_small():
    assert quality_summary([group(4000), group(100, 2, 'transport', 'SGX')], QualityPolicy())['gate_passed']
    assert not quality_summary([group(4000), group(100, 3, 'transport', 'SGX')], QualityPolicy())['gate_passed']


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_circuit_pauses_then_opens_and_time_budget(monkeypatch):
    clock = FakeClock()
    guard = FetchGuard(failure_limit=2, cooldown=60, max_pauses=1, clock=clock, sleep=clock.sleep)
    transient = FetchFailure('rate_limit', '429', retryable=True)
    guard.observe(transient)
    guard.observe()  # A genuine successful fetch resets the streak.
    guard.observe(FetchFailure('not_found', 'missing'))  # Definitive answers are not outage evidence.
    guard.check()
    assert clock.sleeps == []
    guard.observe(transient)
    guard.observe(FetchFailure('timeout', 'timeout', retryable=True))
    guard.check()  # First streak pauses new requests for the cooldown, then resumes.
    assert clock.sleeps == [60] and clock.now == 60
    guard.observe(transient)
    guard.observe(transient)
    with pytest.raises(FetchFailure, match='repeated transient'):
        guard.check()
    assert clock.sleeps == [60]
    monkeypatch.setattr(screener, '_fetch_guard', guard)
    monkeypatch.setattr(requests, 'get', lambda *a, **k: pytest.fail('Circuit must prevent request'))
    result = screener.process_single_stock({'symbol': 'FAKE', 'name': 'Fake'}, False)
    assert result.error_kind == 'provider_circuit_open'
    with pytest.raises(FetchFailure) as failure:
        FetchGuard(seconds=-1).check()
    assert failure.value.kind == 'fetch_budget_exhausted'


def test_pause_never_outlives_fetch_budget():
    clock = FakeClock()
    guard = FetchGuard(seconds=30, failure_limit=1, cooldown=60, clock=clock, sleep=clock.sleep)
    guard.observe(FetchFailure('rate_limit', '429', retryable=True))
    with pytest.raises(FetchFailure) as failure:
        guard.check()
    assert failure.value.kind == 'fetch_budget_exhausted'
    assert clock.sleeps == [30]


def test_brief_throttle_recovers_through_pause(monkeypatch):
    clock = FakeClock()
    guard = FetchGuard(failure_limit=3, cooldown=60, clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(screener, '_fetch_guard', guard)
    responses = [response(429)] * 3 + [response(200, chart())]
    monkeypatch.setattr(requests, 'get', lambda *a, **k: responses.pop(0))
    with pytest.raises(FetchFailure) as failure:
        screener.fetch_stock_data('A', 'NYSE', False)
    assert failure.value.kind == 'rate_limit' and len(failure.value.attempts) == screener.MAX_RETRIES
    frame, source = screener.fetch_stock_data('B', 'NYSE', False)
    assert source == 'fetch' and len(frame) == 2
    assert clock.sleeps == [60] and not responses


def test_duplicate_provider_bar_is_deduplicated(monkeypatch):
    payload = chart((1, 2, 3))
    payload['chart']['result'][0]['timestamp'] = [0, 86400, 86400]
    monkeypatch.setattr(requests, 'get', lambda *a, **k: response(200, payload))
    frame = screener.fetch_via_direct_api('FAKE')
    assert list(frame['Close']) == [1, 3]


@pytest.mark.parametrize('kind', ['not_found', 'no_data', 'empty_prices', 'missing_report_week', 'insufficient_history'])
def test_provider_confirmed_gaps_reduce_coverage_but_are_not_operational(kind):
    summary = quality_summary([group(100, 5, kind)], QualityPolicy())
    assert summary['status'] == 'partial'
    assert summary['totals']['coverage'] == .95 and summary['totals']['operational_errors'] == 0


@pytest.mark.parametrize('kind', ['rate_limit', 'timeout', 'transport', 'http_service', 'http_error', 'json_error',
                                  'data_error', 'missing_result', 'provider_error', 'provider_circuit_open',
                                  'fetch_budget_exhausted', 'processing_error'])
def test_uncertain_failures_are_operational(kind):
    summary = quality_summary([group(100, 2, kind)], QualityPolicy())
    assert summary['status'] == 'failed' and summary['totals']['operational_errors'] == 2


def test_reporting_week_missing_is_not_success(monkeypatch):
    frame = pd.DataFrame({'Close': [10.] * 200}, index=pd.bdate_range(end='2025-01-01', periods=200))
    monkeypatch.setattr(screener, 'fetch_stock_data', lambda *args: (frame, 'cache'))
    assert screener.process_single_stock({'symbol': 'FAKE', 'name': 'Fake'}).error_kind == 'missing_report_week'


def test_no_crossover_counts_as_analysis(monkeypatch, tmp_path):
    frame = pd.DataFrame({'Close': [10.] * 200}, index=pd.bdate_range(end=screener.TARGET_WEEK_END, periods=200))
    monkeypatch.setattr(screener, 'fetch_stock_data', lambda *args: (frame, 'test'))
    csv = tmp_path / 'NYSE.csv'
    csv.write_text('Symbol,Name\nFAKE,Fake\n')
    results = screener.process_csv_file(csv)
    assert not results.errors and not results.bullish and not results.bearish
    assert quality_summary([results], QualityPolicy())['totals']['successful_symbols'] == 1


@pytest.mark.parametrize('csv_text', ['', 'Name,Other\nFake,1\n', 'Symbol,Name\n ,Fake\n'])
def test_empty_or_invalid_market_fails_quality(tmp_path, csv_text):
    csv = tmp_path / 'Empty.csv'
    csv.write_text(csv_text)
    result = screener.process_csv_file(csv)
    assert result.input_error
    assert not quality_summary([group(100), result], QualityPolicy())['gate_passed']


def configure_run(monkeypatch, tmp_path, result=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(screener, 'OUTPUT_DIR', tmp_path / 'output')
    monkeypatch.setattr(screener, 'CHARTS_DIR', tmp_path / 'output/charts')
    monkeypatch.setattr(screener, 'CACHE_DB', tmp_path / 'cache.db')
    symbols = tmp_path / 'symbols'
    symbols.mkdir()
    monkeypatch.setattr(screener, 'SYMBOL_DATA_DIR', symbols)
    if result:
        (symbols / 'NYSE.csv').write_text('Symbol\nFAKE\n')
        monkeypatch.setattr(screener, 'process_csv_file', lambda *args: result)


@pytest.mark.parametrize('result, code, status', [(group(100), 0, 'complete'), (group(100, 4), 0, 'partial'),
                                               (group(10, 10, 'rate_limit'), 2, 'failed'), (None, 2, 'failed')])
def test_actual_pdf_and_status_survive_quality_failure(tmp_path, monkeypatch, result, code, status):
    configure_run(monkeypatch, tmp_path, result)
    assert screener.run_screener(False) == code
    summary = json.loads((tmp_path / 'output/run_summary.json').read_text())
    assert summary['status'] == status
    assert summary['report_generated']
    assert (tmp_path / 'output' / summary['report_path']).read_bytes().startswith(b'%PDF')


def test_pdf_failure_retains_json_and_removes_partial_pdf(tmp_path, monkeypatch):
    configure_run(monkeypatch, tmp_path, group(100))
    def broken(groups, path, **kwargs):
        path.write_bytes(b'broken PDF')
        raise ValueError('broken layout')
    monkeypatch.setattr(screener, 'generate_pdf_report', broken)
    assert screener.run_screener() == 2
    summary = json.loads((tmp_path / 'output/run_summary.json').read_text())
    assert not summary['email_allowed'] and not summary['report_generated']
    assert not list((tmp_path / 'output').glob('*.pdf'))
    assert any('PDF generation failed' in error for error in summary['violations'])


def test_real_cli_empty_input_fails_and_saves_diagnostic(tmp_path):
    result = subprocess.run([sys.executable, screener.__file__, '--no-cache'], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 2
    summary = json.loads((tmp_path / 'output/run_summary.json').read_text())
    assert summary['status'] == 'failed' and summary['totals']['total_symbols'] == 0
    assert list((tmp_path / 'output').glob('*.pdf'))


@pytest.mark.parametrize('value', ['nan', 'inf', '-0.1', '1.1', 'nonsense'])
def test_cli_rejects_invalid_policy_before_run(monkeypatch, value):
    monkeypatch.setattr(screener, 'run_screener', lambda *args, **kwargs: pytest.fail('Invalid options must not scan'))
    with pytest.raises(SystemExit) as failure:
        screener.main(['--min-coverage', value])
    assert failure.value.code == 2


@pytest.mark.parametrize('cli_exit, summary', [(2, {'email_allowed': False}), (0, {'email_allowed': False}), (0, None)])
def test_wrapper_suppresses_failed_or_missing_status_email(tmp_path, cli_exit, summary):
    checkout = tmp_path / 'checkout with spaces'
    checkout.mkdir()
    wrapper = checkout / 'run_screener_and_email.sh'
    shutil.copy2(Path(screener.__file__).with_name(wrapper.name), wrapper)
    (checkout / '.email_config').write_text('# Offline fake config\n')
    (checkout / 'screener.py').write_text(
        'from pathlib import Path\nimport json\nout=Path("output");out.mkdir()\n'
        '(out / "fake.pdf").write_bytes(b"%PDF")\n' +
        (f'(out / "run_summary.json").write_text(json.dumps({summary!r}))\n' if summary else '') +
        f'raise SystemExit({cli_exit})\n')
    (checkout / 'send_email.py').write_text('from pathlib import Path\nPath("EMAIL_CALLED").touch()\n')
    import os
    result = subprocess.run(['/bin/bash', str(wrapper)], cwd=tmp_path,
                            env={**os.environ, 'PYTHON_PATH': sys.executable}, capture_output=True, text=True)
    assert result.returncode != 0
    assert not (checkout / 'EMAIL_CALLED').exists()
    assert (checkout / 'output/fake.pdf').exists()


def test_workflow_keeps_failure_evidence_and_uses_complete_requirements():
    workflow = (Path(screener.__file__).parent / '.github/workflows/weekly_screener.yml').read_text()
    assert 'pip install -r requirements.txt' in workflow
    upload = workflow.split('- name: Upload report and quality evidence')[1]
    assert 'if: always()' in upload and 'output/run_summary.json' in upload and 'output/*.pdf' in upload
    assert 'continue-on-error' not in workflow


def test_email_discloses_accepted_gaps_and_refuses_failed_gate(tmp_path, monkeypatch):
    import send_email
    monkeypatch.setenv('RESEND_API_KEY', 'offline-test-placeholder')
    monkeypatch.setenv('RECIPIENT_EMAIL', 'offline@example.invalid')
    monkeypatch.delenv('CC_EMAIL', raising=False)
    calls = []
    monkeypatch.setattr(send_email.resend.Emails, 'send', lambda params: calls.append(params) or {'id': 'offline'})
    pdf = tmp_path / 'EMA_Crossover_Report_2026-09-04.pdf'
    pdf.write_bytes(b'%PDF offline')
    summary = quality_summary([group(100, 4)], QualityPolicy())
    summary.update(report_generated=True, report_path=pdf.name)
    status = tmp_path / 'run_summary.json'
    status.write_text(json.dumps(summary))
    send_email.send_email(str(pdf))
    assert len(calls) == 1
    assert 'Partial coverage' in calls[0]['subject']
    assert '96 of 100' in calls[0]['html']
    summary.update(gate_passed=False, email_allowed=False, status='failed')
    status.write_text(json.dumps(summary))
    with pytest.raises(SystemExit, match='email suppressed'):
        send_email.send_email(str(pdf))
    assert len(calls) == 1


def test_pdf_contains_quality_and_escaped_failure_evidence(tmp_path, monkeypatch):
    texts = []
    original = screener.Paragraph
    def record(text, *args, **kwargs):
        texts.append(text)
        return original(text, *args, **kwargs)
    monkeypatch.setattr(screener, 'Paragraph', record)
    result = group(2, 2, 'rate_limit')
    result.failed[0].error = 'Provider error <bad> & uncertain'
    screener.generate_pdf_report([result], tmp_path / 'failed.pdf', quality_summary([result], QualityPolicy()))
    assert any('Data quality: FAILED' in text for text in texts)
    assert any('Provider error &lt;bad&gt; &amp; uncertain' in text for text in texts)
    assert any('Minimum coverage' in text for text in texts)


def test_access_failure_does_not_try_alias(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, 'get', lambda *a, **k: calls.append(1) or response(403, {}))
    with pytest.raises(FetchFailure) as failure:
        screener.fetch_stock_data('FAKE', 'Singapore', False)
    assert failure.value.kind == 'http_error'
    assert len(calls) == 1


def history_chart(symbol):
    days = pd.bdate_range(end=screener.TARGET_WEEK_END, periods=200)
    return chart([10.] * len(days), symbol=symbol, timestamps=[int(day.timestamp()) for day in days])


@pytest.mark.parametrize('bad_payload, code, status, operational', [
    ({'chart': {'error': None, 'result': [{}]}}, 2, 'failed', 2),
    ({'chart': {'error': None, 'result': [{'timestamp': False}]}}, 2, 'failed', 2),
    (chart([10.] * 200, symbol='BAD0', timestamps=[float('nan')] * 200), 2, 'failed', 2),
    (chart([10.] * 200, symbol='BAD0', timestamps=[float('inf')] * 200), 2, 'failed', 2),
    (chart([10.] * 200, symbol='BAD0', timestamps=[float('-inf')] * 200), 2, 'failed', 2),
    (delisted('BAD0'), 0, 'partial', 0),
])
def test_gate_counts_malformed_charts_operationally_end_to_end(tmp_path, monkeypatch, bad_payload, code, status, operational):
    configure_run(monkeypatch, tmp_path)
    symbols = [f'BAD{n}' for n in range(2)] + [f'OK{n}' for n in range(98)]
    (screener.SYMBOL_DATA_DIR / 'NYSE.csv').write_text('Symbol,Exchange\n' + ''.join(f'{s},NYSE\n' for s in symbols))
    def get(url, **kwargs):
        symbol = url.rsplit('/', 1)[1]
        if symbol.startswith('BAD'):
            payload = json.loads(json.dumps(bad_payload).replace('BAD0', symbol))
            return response(200, payload)
        return response(200, history_chart(symbol))
    monkeypatch.setattr(requests, 'get', get)
    assert screener.run_screener(False) == code
    summary = json.loads((tmp_path / 'output/run_summary.json').read_text())
    assert summary['status'] == status and summary['report_generated']
    assert summary['totals']['successful_symbols'] == 98
    assert summary['totals']['operational_errors'] == operational
    assert summary['email_allowed'] == (code == 0)

    import send_email
    monkeypatch.setenv('RESEND_API_KEY', 'offline-test-placeholder')
    monkeypatch.setenv('RECIPIENT_EMAIL', 'offline@example.invalid')
    monkeypatch.delenv('CC_EMAIL', raising=False)
    calls = []
    monkeypatch.setattr(send_email.resend.Emails, 'send', lambda params: calls.append(params) or {'id': 'offline'})
    pdf = tmp_path / 'output' / summary['report_path']
    if code:
        with pytest.raises(SystemExit, match='email suppressed'):
            send_email.send_email(str(pdf))
        assert not calls
    else:
        send_email.send_email(str(pdf))
        assert len(calls) == 1 and 'Partial coverage' in calls[0]['subject']
