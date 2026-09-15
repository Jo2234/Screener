"""Offline quality policy and bounded fetch guard for the weekly screener."""
from collections import Counter
from dataclasses import asdict, dataclass
import math
import threading
import time


class FetchFailure(Exception):
    def __init__(self, kind, message, *, retryable=False, attempts=None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.attempts = attempts or []


class FetchGuard:
    """Stop an outage consuming an entire scheduled runner budget.

    A streak of transient failures first pauses all new requests for a cooldown
    so a short throttling burst can clear; after max_pauses the circuit stays open.
    """
    def __init__(self, seconds=1800, failure_limit=20, cooldown=60, max_pauses=2,
                 clock=None, sleep=None):
        # Resolve time functions lazily so tests can substitute a fake clock.
        self.clock = clock or (lambda: time.monotonic())
        self.sleep = sleep or (lambda seconds: time.sleep(seconds))
        self.deadline = self.clock() + seconds
        self.failure_limit = failure_limit
        self.cooldown = cooldown
        self.max_pauses = max_pauses
        self.failures = 0
        self.pauses = 0
        self.resume_at = 0
        self.open = False
        self.lock = threading.Lock()

    def check(self):
        while True:
            with self.lock:
                if self.open:
                    raise FetchFailure('provider_circuit_open', 'Fetches stopped after repeated transient provider failures')
                now = self.clock()
                if now >= self.deadline:
                    raise FetchFailure('fetch_budget_exhausted', 'Run fetch budget exhausted; symbol was not analyzed')
                wait = min(self.resume_at, self.deadline) - now
                if wait <= 0:
                    return
            self.sleep(wait)

    def observe(self, failure=None):
        with self.lock:
            if failure is None:
                self.failures = 0
            elif failure.retryable:
                self.failures += 1
                if self.failures >= self.failure_limit and not self.open:
                    self.failures = 0
                    if self.pauses >= self.max_pauses:
                        self.open = True
                    else:
                        self.pauses += 1
                        self.resume_at = self.clock() + self.cooldown


@dataclass(frozen=True)
class QualityPolicy:
    min_coverage: float = .95
    min_market_coverage: float = .90
    max_operational_error_rate: float = .01
    max_market_operational_error_rate: float = .02

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f'{key} must be a finite fraction from 0 to 1')


# Ordinary gaps are well-formed provider answers that the symbol has no usable
# prices: explicit not-found, no/empty price history, stale history ending before
# the reporting week (typically suspended or delisted) and short history. They
# still reduce coverage; transport, HTTP, malformed and processing errors do not
# belong here.
ORDINARY_GAPS = {'not_found', 'no_data', 'empty_prices', 'missing_report_week', 'insufficient_history'}


def quality_summary(groups, policy):
    violations = []

    def counts(name, total, failed, input_error=None):
        kinds = Counter(result.error_kind or 'processing_error' for result in failed)
        errors = sum(kinds.values())
        operational = sum(n for kind, n in kinds.items() if kind not in ORDINARY_GAPS)
        successful = total - errors
        coverage = successful / total if total else 0
        rate = operational / total if total else 0
        min_coverage = policy.min_coverage if name == 'TOTAL' else policy.min_market_coverage
        max_rate = policy.max_operational_error_rate if name == 'TOTAL' else policy.max_market_operational_error_rate
        if total == 0 or successful == 0:
            violations.append(f'{name}: no successfully analyzed symbols')
        if input_error:
            violations.append(f'{name}: {input_error}')
        if coverage < min_coverage:
            violations.append(f'{name}: coverage {coverage:.2%} below {min_coverage:.2%}')
        if rate > max_rate:
            violations.append(f'{name}: operational error rate {rate:.2%} above {max_rate:.2%}')
        return {'market': name, 'total_symbols': total, 'processed_symbols': total,
                'successful_symbols': successful, 'failed_symbols': errors,
                'operational_errors': operational, 'coverage': coverage,
                'operational_error_rate': rate, 'failure_kinds': dict(kinds),
                'input_error': input_error}

    markets = [counts(g.csv_name, g.total_symbols, g.failed, g.input_error) for g in groups]
    totals = counts('TOTAL', sum(g.total_symbols for g in groups), [f for g in groups for f in g.failed])
    passed = not violations
    return {'schema_version': 1, 'status': 'failed' if not passed else 'partial' if totals['failed_symbols'] else 'complete',
            'gate_passed': passed, 'email_allowed': passed, 'policy': asdict(policy),
            'totals': totals, 'markets': markets, 'violations': violations,
            'failures': [{'market': g.csv_name, 'symbol': f.symbol, 'exchange': f.exchange,
                          'kind': f.error_kind or 'processing_error', 'message': f.error,
                          'attempts': f.fetch_attempts} for g in groups for f in g.failed]}
