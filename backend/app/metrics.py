"""Prometheus metrics at /metrics: what an SRE (or a judge) needs to see the system working.
Exposes counts and latencies only, never payment data. Restrict /metrics at your ingress."""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP = Histogram("vbw_http_request_seconds", "HTTP request latency", ["method", "route", "status"],
                 buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30))
CASES = Counter("vbw_cases_total", "Completed investigations by recommendation", ["decision", "channel"])
CASE_FAILED = Counter("vbw_cases_failed_total", "Investigations that stopped with an error")
INVESTIGATION = Histogram("vbw_investigation_seconds", "End-to-end investigation time", ["channel"],
                          buckets=(.1, .25, .5, 1, 2, 3, 5, 10, 20, 40, 60))
SOURCE = Histogram("vbw_source_call_seconds", "Data-source lookups by source and outcome", ["source", "status"],
                   buckets=(.001, .01, .05, .1, .25, .5, 1, 2, 5, 10))
WEBHOOKS = Counter("vbw_webhook_deliveries_total", "Webhook delivery attempts by outcome", ["outcome"])
AUTO_RELEASED = Counter("vbw_auto_released_total", "Payments released automatically by policy")


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
