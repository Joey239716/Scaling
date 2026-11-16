"""The synthetic service we autoscale: a tiny web app whose requests cost CPU.

Endpoints:
    GET /healthz   cheap liveness/readiness check (used by Kubernetes)
    GET /work      does a fixed amount of CPU work, then responds
    GET /metrics   Prometheus metrics (request count and latency, see below)

Why burn CPU on purpose? HPA scales on CPU utilization. A real service's cost
per request varies; here every /work request costs about the same, so "more
requests per second" turns directly into "more CPU".

Metrics (scraped by Prometheus every 15 s; queries live in telemetry/queries.py):
    scale_app_requests_total{path, status}           counter
    scale_app_request_duration_seconds{path}         histogram
Only /work is counted: health checks and scrapes are not traffic.
"""

import hashlib
import os
import socket
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# How much CPU each /work request burns: rounds of SHA-256 hashing.
# Set by the WORK_ROUNDS environment variable in cloud-env/k8s/deployment.yaml.
WORK_ROUNDS = int(os.getenv("WORK_ROUNDS", "20000"))

# Paths that count as traffic in the metrics.
TRAFFIC_PATHS = {"/work"}

REQUESTS = Counter(
    "scale_app_requests_total",
    "Requests handled, by path and HTTP status code.",
    ["path", "status"],
)
LATENCY = Histogram(
    "scale_app_request_duration_seconds",
    "Time from receiving a request to sending the response, in seconds.",
    ["path"],
    # Fine-grained in the few-ms to ~1 s range, where this app's latency lives.
    buckets=(0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.05, 0.075, 0.1,
             0.15, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 10.0),
)

app = FastAPI()


@app.middleware("http")
async def record_metrics(request: Request, call_next):
    path = request.url.path
    if path not in TRAFFIC_PATHS:
        return await call_next(request)
    start = time.perf_counter()
    status = 500  # if the handler raises, count it as a server error
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        LATENCY.labels(path=path).observe(time.perf_counter() - start)
        REQUESTS.labels(path=path, status=str(status)).inc()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# A plain `def` (not `async def`): FastAPI runs it in a worker thread, so one
# slow request doesn't block the server from accepting others.
@app.get("/work")
def work() -> dict:
    start = time.perf_counter()
    digest = b"scale"
    for _ in range(WORK_ROUNDS):
        digest = hashlib.sha256(digest).digest()
    return {
        "pod": socket.gethostname(),  # which replica served this request
        "work_ms": round((time.perf_counter() - start) * 1000, 1),
    }
