"""Run the queries in telemetry/queries.py against Prometheus.

    from telemetry import latest_snapshot, snapshots_between

    latest_snapshot()                 # dict: the current values (for a controller)
    snapshots_between(start, end)     # list of dicts, one per 15 s (for results)

How we reach Prometheus:
- If PROMETHEUS_URL is set (e.g. http://localhost:9090 from your own
  port-forward), we call it directly.
- Otherwise we go through the Kubernetes API server with
  `kubectl get --raw .../services/kps-prometheus:9090/proxy/...`, which needs
  no port-forward. The kubectl context is KUBE_CONTEXT (default "scale").

Missing data: a field Prometheus has no value for at a timestamp (or a NaN,
e.g. latency when there were no requests) is returned as None. Values are
never filled in or interpolated. See docs/telemetry.md.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import urllib.parse
from datetime import datetime, timezone

from telemetry.queries import INTERVAL_SECONDS, QUERIES

PROMETHEUS_SERVICE_PATH = (
    "/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy"
)


def _get(api_path: str, params: dict) -> dict:
    """GET a Prometheus HTTP API path and return the `data` part of the reply."""
    query_string = urllib.parse.urlencode(params)
    base_url = os.getenv("PROMETHEUS_URL")
    if base_url:
        import requests

        response = requests.get(f"{base_url.rstrip('/')}{api_path}?{query_string}", timeout=30)
        response.raise_for_status()
        body = response.json()
    else:
        context = os.getenv("KUBE_CONTEXT", "scale")
        result = subprocess.run(
            ["kubectl", "--context", context, "get", "--raw",
             f"{PROMETHEUS_SERVICE_PATH}{api_path}?{query_string}"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Prometheus query failed: {result.stderr.strip()}")
        body = json.loads(result.stdout)
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {body.get('error', body)}")
    return body["data"]


def _to_float(value: str) -> float | None:
    number = float(value)
    return None if math.isnan(number) or math.isinf(number) else number


def latest_snapshot() -> dict:
    """The current value of every field (None where Prometheus has no data)."""
    now = datetime.now(timezone.utc).timestamp()
    snapshot: dict = {"timestamp": _iso(now), "unix_time": now}
    for field, promql in QUERIES.items():
        result = _get("/api/v1/query", {"query": promql, "time": now})["result"]
        snapshot[field] = _to_float(result[0]["value"][1]) if result else None
    return snapshot


def snapshots_between(start: float, end: float) -> list[dict]:
    """One snapshot per INTERVAL_SECONDS from `start` to `end` (unix seconds).

    Timestamps are start, start+15, ... up to end. Each query is one
    Prometheus range query with step = INTERVAL_SECONDS.
    """
    times = []
    t = start
    while t <= end + 1e-6:
        times.append(round(t, 3))
        t += INTERVAL_SECONDS
    by_time = {t: {"timestamp": _iso(t), "unix_time": t} for t in times}

    for field, promql in QUERIES.items():
        data = _get("/api/v1/query_range", {
            "query": promql, "start": times[0], "end": times[-1], "step": INTERVAL_SECONDS,
        })
        values = {}
        for series in data["result"]:  # every query aggregates to one series
            for ts, value in series["values"]:
                values[round(float(ts), 3)] = _to_float(value)
        for t in times:
            by_time[t][field] = values.get(t)
    return [by_time[t] for t in times]


def _iso(unix_time: float) -> str:
    return datetime.fromtimestamp(unix_time, timezone.utc).isoformat(timespec="seconds")
