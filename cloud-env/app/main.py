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

WORK_MS = float(os.environ.get("WORK_MS", "4.2"))

app = FastAPI()


# async: runs on the event loop instead of queueing behind busy /work requests
# in the thread pool, so a loaded pod still passes its readiness probe.
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/work")
def work():
    # Spin until this thread has used WORK_MS of CPU time. Measuring CPU time
    # (not wall time) keeps the cost per request fixed even when requests
    # overlap, and doesn't depend on how fast the machine is.
    start = time.thread_time()
    deadline = start + WORK_MS / 1000
    x = 0
    while time.thread_time() < deadline:
        x += 1
    return {"cpu_ms": round((time.thread_time() - start) * 1000, 2)}
