"""The service we scale: every /work request burns a fixed amount of CPU.

That makes "more requests" mean "more CPU", which is what HPA scales on.
WORK_MS (env var) is the CPU time the handler burns per request; server
overhead adds roughly 0.8 ms on top. The Deployment sets it so the total is
~5 ms, which puts one pod (250m CPU request) at 100% at ~50 req/s, matching
the simulator's capacity_per_replica. See cloud-env/README.md for calibration.
"""

import os
import time

from fastapi import FastAPI
from prometheus_client import Counter, make_asgi_app

WORK_MS = float(os.environ.get("WORK_MS", "4.2"))

# KEDA scales on this (via Prometheus): total /work req/s across all pods.
REQUESTS = Counter("app_requests", "Requests received by /work")

class CountWorkRequests:
    """Counts /work requests as they arrive, before they wait their turn.
    Counting inside the handler would only count what the pod gets round to
    starting, so an overloaded pod would under-report demand.
    (A plain ASGI wrapper, not @app.middleware, which costs far more CPU.)"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/work":
            REQUESTS.inc()
        await self.app(scope, receive, send)


app = FastAPI()
app.add_middleware(CountWorkRequests)
app.mount("/metrics", make_asgi_app())  # read by Prometheus every 15 s


# Cheap, so it still answers quickly between /work requests and a loaded pod
# keeps passing its readiness probe (which allows 3 s).
@app.get("/healthz")
async def healthz():
    return {"ok": True}


# async on purpose: the work runs on the event loop, one request at a time.
# As a plain `def`, FastAPI ran it in a thread pool, and the spinning worker
# threads starved the event loop of Python's GIL: at ~30 req/s per pod (only
# ~60% CPU) responses took 0.3-2.6 s instead of ~10 ms. Running each request
# in turn on one thread is also simply how a CPU-bound single process behaves.
@app.get("/work")
async def work():
    # Spin until this thread has used WORK_MS of CPU time. Measuring CPU time
    # (not wall time) keeps the cost per request fixed and doesn't depend on
    # how fast the machine is.
    start = time.thread_time()
    deadline = start + WORK_MS / 1000
    x = 0
    while time.thread_time() < deadline:
        x += 1
    return {"cpu_ms": round((time.thread_time() - start) * 1000, 2)}
