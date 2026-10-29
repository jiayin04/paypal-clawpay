"""In-process metrics rendered in the Prometheus text format (no extra packages).

Single uvicorn worker assumed, like the rate limiters: each worker keeps its own
counters. Label values must be low-cardinality (route templates, status codes,
reason codes), never ids, tokens or amounts.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Sequence

DEFAULT_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: Sequence[str], values: Sequence[str], extra: str = "") -> str:
    parts = [f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def _num(value: float) -> str:
    if math.isinf(value):
        return "+Inf"
    return repr(float(value)) if not float(value).is_integer() else str(int(value))


class Counter:
    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        self.name, self.help, self.label_names = name, help_text, tuple(labels)
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, *labels: str, amount: float = 1.0) -> None:
        key = tuple(str(v) for v in labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, *labels: str) -> float:
        with self._lock:
            return self._values.get(tuple(str(v) for v in labels), 0.0)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            yield f"{self.name}{_labels(self.label_names, key)} {_num(value)}"


class Histogram:
    def __init__(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        self.name, self.help, self.label_names = name, help_text, tuple(labels)
        self.buckets = tuple(sorted(buckets))
        self._series: dict[tuple[str, ...], list[float]] = {}  # bucket counts..., sum, count
        self._lock = threading.Lock()

    def observe(self, value: float, *labels: str) -> None:
        key = tuple(str(v) for v in labels)
        with self._lock:
            series = self._series.setdefault(key, [0.0] * (len(self.buckets) + 2))
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    series[i] += 1
            series[-2] += value
            series[-1] += 1

    def count(self, *labels: str) -> float:
        with self._lock:
            series = self._series.get(tuple(str(v) for v in labels))
            return series[-1] if series else 0.0

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            items = sorted((k, list(v)) for k, v in self._series.items())
        for key, series in items:
            for bound, count in zip(self.buckets, series, strict=False):
                le = 'le="' + _num(bound) + '"'
                yield f"{self.name}_bucket{_labels(self.label_names, key, le)} {_num(count)}"
            inf = 'le="+Inf"'
            yield f"{self.name}_bucket{_labels(self.label_names, key, inf)} {_num(series[-1])}"
            yield f"{self.name}_sum{_labels(self.label_names, key)} {_num(series[-2])}"
            yield f"{self.name}_count{_labels(self.label_names, key)} {_num(series[-1])}"


class Registry:
    def __init__(self) -> None:
        self.http_requests = Counter(
            "clawpay_http_requests_total", "HTTP requests by route template and status.",
            ("method", "route", "status"),
        )
        self.http_duration = Histogram(
            "clawpay_http_request_duration_seconds", "HTTP request latency by route template.",
            ("method", "route"),
        )
        self.auth_failures = Counter(
            "clawpay_auth_failures_total", "Rejected device credentials by token kind and reason.",
            ("kind", "reason"),
        )
        self.audit_events = Counter(
            "clawpay_audit_events_total", "Audit events written from Python (RPC-internal events excluded).",
            ("event_type",),
        )
        self.payment_results = Counter(
            "clawpay_payment_results_total", "PayPal results applied to payment attempts.",
            ("source", "classification"),
        )
        self.paypal_requests = Counter(
            "clawpay_paypal_requests_total", "Outbound PayPal API calls by operation and outcome.",
            ("operation", "outcome"),
        )
        self.paypal_duration = Histogram(
            "clawpay_paypal_request_duration_seconds", "Outbound PayPal API latency.", ("operation",),
        )
        self.merchant_requests = Counter(
            "clawpay_merchant_requests_total", "Merchant agent (A2A) calls by merchant, method and outcome.",
            ("merchant", "method", "outcome"),
        )
        self.merchant_duration = Histogram(
            "clawpay_merchant_request_duration_seconds", "Merchant agent (A2A) latency.", ("merchant", "method"),
        )
        self.reconcile_runs = Counter(
            "clawpay_reconcile_runs_total", "Background reconciliation iterations by outcome.", ("outcome",),
        )
        self.reconcile_claimed = Counter(
            "clawpay_reconcile_claimed_attempts_total", "Payment attempts claimed by the reconciler.",
        )

    def all(self) -> list[Counter | Histogram]:
        return [v for v in vars(self).values() if isinstance(v, (Counter, Histogram))]

    def render(self) -> str:
        return "\n".join(line for metric in self.all() for line in metric.render()) + "\n"


# Process-wide registry, like the logging configuration.
METRICS = Registry()


def reset_metrics() -> None:
    """Test helper: start from empty counters."""
    fresh = Registry()
    for name, value in vars(fresh).items():
        setattr(METRICS, name, value)
