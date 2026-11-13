# cloud-env/

The Phase 1 test environment: a small app that HPA scales on a local Kubernetes cluster, driven by Locust traffic. Tool setup (Docker, kubectl, Minikube/kind, metrics server) is in [docs/onboarding/setup.md](../docs/onboarding/setup.md), steps 1–4.

```
cloud-env/
├── app/        # FastAPI service + Dockerfile
├── k8s/        # Deployment, Service, HPA
└── workloads/  # Locust workloads + the Job that runs them in-cluster
```

## How it lines up with the simulator

| | Real cluster | Simulator / HPA baseline |
|---|---|---|
| Capacity per pod | 250m CPU request, ~5 ms CPU per `/work` request → 100% at ~50 req/s | `capacity_per_replica = 50` |
| Replicas | min 1, max 20, starts at 2 | same |
| HPA | 60% CPU target, scale up immediately, 5 min scale-down window | same (`controller/hpa_baseline.py`) |
| Workloads | `ramp.py` and `spike.py` follow the simulator's tick timing (1 tick = 15 s) | `experiments/configs/ramp.yaml`, `spike.yaml` |

Known differences:
- **CPU can read above 100% here.** The CPU limit (1 core) is above the request, so HPA can see up to ~400%; the simulator caps at 100%. Setting limit = request would match the cap, but the pod then gets paused during bursts (CFS throttling): we measured ~480 ms average latency at ~60% CPU, versus ~115 ms with the higher limit.
- **HPA sees CPU up to ~1–2 minutes late.** Minikube's metrics-server addon refreshes every 60 s (`--metric-resolution=60s`), and the reading is an average over that window. The simulator's HPA sees each tick's CPU immediately.
- **Pods start in seconds**, not the simulator's 30 s startup delay.
- **The 10% tolerance** is the Kubernetes default, not set in the manifest.

## Workloads

Every workload runs a 20-minute pattern (the simulator's 80 ticks), then 7.5 minutes at 20 req/s so you can watch HPA's 5-minute scale-down, then stops: **27.5 minutes each**. Each simulated user sends 1 request/s, so users = req/s.

| File | Pattern (first 20 min) | What it tests | Simulator twin |
|---|---|---|---|
| `ramp.py` | 100 req/s, climbs to 500 between 2:30 and 12:30, holds | Gradual growth; HPA should cope well | `ramp.yaml` |
| `spike.py` | 100, instant jump to 500 from 7:30 to 12:30, back to 100 | Sudden load with no warning; HPA lags behind | `spike.yaml` |
| `periodic.py` | Smooth wave 100 → 400 → 100, two 10-minute cycles | Predictable cycles; does HPA track them or lag? | none yet |
| `bursty.py` | 100 baseline, random 30–90 s bursts to 250–500, every 1–3 min | Over-reacting to brief spikes, flapping | none yet |
| `step.py` | 100 → 200 → 300 → 400 → 500, 4 minutes per step | How long HPA takes to settle at each level | none yet |

`bursty.py` is random but repeatable: the bursts come from a fixed seed (`BURST_SEED` in `locust-job.yaml`, default 42), so every controller faces the same traffic. The schedule is printed at the top of the Locust logs.

Shared code lives in `common.py`: the user class (including the new-connection-per-request fix) and `RateShape`, which handles the cooldown and stop. A new workload only has to subclass `common.RateShape` and write `rate(t)`. See `step.py` for the shortest example.

## Run it

Commands use `kubectl`. On Minikube, if your `kubectl` is more than one minor version away from the cluster (Docker Desktop's bundled one often is), use `minikube kubectl --` in its place.

**1. Build and deploy** (from the repo root):

```bash
docker build -t scale-app:local ./cloud-env/app
minikube image load scale-app:local                 # or: kind load docker-image scale-app:local --name scale
kubectl apply -f cloud-env/k8s/
kubectl rollout status deployment/scale-app
```

After a rebuild with the same tag, run `kubectl rollout restart deployment/scale-app` so the pods pick up the new image.

**2. Run a workload** (27.5 minutes). First pick one by setting `WORKLOAD` in `cloud-env/workloads/locust-job.yaml` (default `ramp.py`). Then:

```bash
kubectl create configmap workloads --from-file=cloud-env/workloads/
kubectl apply -f cloud-env/workloads/locust-job.yaml
kubectl logs -f job/locust-run      # live request stats
```

The ConfigMap holds the whole folder, because each workload imports `common.py`.

**3. Watch it scale**, in another terminal:

```bash
kubectl get hpa scale-app -w
```

At the 60% target each pod takes ~30 req/s, so expect roughly `req/s ÷ 30` replicas: ~17 at 500 req/s. Scale-down starts about 5 minutes after load drops.

**Run it again, or switch workload:** delete the old Job and ConfigMap first. A Job runs once, and the ConfigMap must be recreated to pick up edited scripts:

```bash
kubectl delete job locust-run
kubectl delete configmap workloads
```

**Clean up everything:** `kubectl delete -f cloud-env/k8s/` plus the two deletes above.

## Re-checking the calibration

Do this if you change the app, the CPU request, or the machine. The ~0.8 ms of server overhead per request varies by machine.

1. Delete the HPA (`kubectl delete hpa scale-app`) and pin the app at 2 pods (`kubectl scale deployment/scale-app --replicas=2`).
2. Send a steady 50 req/s from inside the cluster for 2–3 minutes: a Locust pod with a plain user class (no `LoadTestShape`) and `--headless -u 50 -r 50 -t 160s`.
3. After about 2 minutes, `kubectl top pods -l app=scale-app` should show ~125m per pod (50%). If it doesn't, adjust `WORK_MS` in `k8s/deployment.yaml` and repeat.
4. Re-apply the HPA: `kubectl apply -f cloud-env/k8s/hpa.yaml`.

Use a new connection per request in the calibration user too (see `ramp.py`), or the load won't split evenly.

Measured on 2026-10-31 (Minikube 1.39, Kubernetes 1.37, Docker driver, Windows 10): 50.3 req/s → 124m + 128m across 2 pods (~5.0 ms per request), ~104 ms average latency.

## First results (2026-10-31)

- **Full ramp, before the connection fix:** HPA scaled 2 → 11 pods and back down to 1 after the drop, but load plateaued at ~345 req/s instead of 500. Locust's keep-alive connections stayed on the oldest pods, which saturated while newer pods sat half idle. Average CPU read ~62%, inside HPA's tolerance, so it stopped scaling. Fixed with a new connection per request.
- **5-minute check after the fix** (100 → 300 req/s over 2 min, then hold): CPU was even across pods, 0 failures in 73k requests, and the HPA reached 9 pods. Latency during the ramp was high (587 ms average) because the HPA lagged behind the 60 s metrics refresh. **The full 25-minute ramp has not been rerun since the fix.**

## Troubleshooting

- **A pod shows `0/1` ready under load:** its readiness probe is timing out. `/healthz` is `async` so it doesn't queue behind `/work`, and the probe allows 3 s. Keep both if you change the app.
- **HPA shows `<unknown>`:** normal for ~1 minute after deploying. If it persists, see setup.md troubleshooting.
- **`job.batch "locust-run"` already exists:** delete the old Job first (see "Run it again").
- **Locust exits with `ModuleNotFoundError: No module named 'common'`:** the ConfigMap was made from a single file. Recreate it from the whole folder (`--from-file=cloud-env/workloads/`).
- **Job shows `Failed` but the run looks fine:** Locust exits with an error if *any* request failed. Check `kubectl logs job/locust-run` for the error report. `ConnectionRefusedError` during scale-down means the `preStop` sleep in the Deployment is missing.
- **HPA scales up but a few pods do most of the work:** the load generator is reusing connections. Each request must open a new one (`Connection: close` in `ramp.py`).
