# cloud-env/

The test environment: a small app on a local Kubernetes cluster, scaled by either HPA (on CPU) or KEDA (on request rate), driven by Locust traffic. Tool setup (Docker, kubectl, Minikube/kind, metrics server) is in [docs/onboarding/setup.md](../docs/onboarding/setup.md), steps 1–4.

```
cloud-env/
├── app/               # FastAPI service + Dockerfile (also reports req counts at /metrics)
├── k8s/               # Deployment, Service, HPA (the default autoscaler)
├── keda/              # KEDA alternative: ScaledObject + Prometheus settings
├── workloads/         # Locust workloads + the Job that runs them in-cluster
├── run_experiment.py  # one workload x one autoscaler -> results/
└── results/           # run outputs (git-ignored)
```

## HPA vs KEDA

| | HPA (`k8s/hpa.yaml`) | KEDA (`keda/scaledobject.yaml`) |
|---|---|---|
| Watches | Average CPU, % of each pod's 250m request | Total `/work` req/s from Prometheus |
| Target | 60% CPU | 30 req/s per pod (= 60% of a pod's 50 req/s) |
| Formula | `ceil(pods × cpu / 60%)` | `ceil(total req/s ÷ 30)` |
| Metric freshness | metrics-server every 15 s | Prometheus scrape + KEDA poll every 15 s, rate over 45 s |
| Replicas, tolerance, scale-up/down rules | 1–20, 10%, immediate up, 5 min down | identical (copied into the ScaledObject) |

Only the metric differs, so a difference in results comes from *what* each one watches. Keep the two files' `behavior` blocks in sync if you change one.

What that difference means in practice: CPU only rises once pods are doing the work, while request rate counts requests as they arrive (before they queue), so KEDA should react earlier to sudden load. When pods are overloaded, CPU tops out (one Python process uses at most ~1 core), but the request count keeps rising.

### Cluster size matters

Create Minikube with `--cpus=6 --memory=5g` (see setup.md step 3). Minikube's default is 2 CPUs for the whole cluster, which throttles everything at ~300 req/s and makes latency results meaningless. Check with `docker inspect minikube --format '{{.HostConfig.NanoCpus}}'`; it should print `6000000000`.

### One-time KEDA setup

Needs [Helm](https://helm.sh/docs/intro/install/). From the repo root:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add kedacore https://kedacore.github.io/charts
helm repo update
helm install prometheus prometheus-community/prometheus --version 29.47.2 \
  -n monitoring --create-namespace -f cloud-env/keda/prometheus-values.yaml --wait
helm install keda kedacore/keda --version 2.21.0 -n keda --create-namespace --wait
```

Both survive `minikube stop`/`start`.

## Running experiments

`run_experiment.py` does a whole run: removes any old test and autoscaler, starts from 2 idle pods, turns on the chosen autoscaler, runs the workload, and records everything.

```bash
python cloud-env/run_experiment.py --workload spike --autoscaler hpa
python cloud-env/run_experiment.py --workload spike --autoscaler keda
# full-length version (27.5 min instead of 8):
python cloud-env/run_experiment.py --workload spike --autoscaler keda --long
# if your kubectl is too old for the cluster:
python cloud-env/run_experiment.py --workload spike --autoscaler keda --kubectl "minikube kubectl --"
```

Workloads: `ramp`, `spike`, `periodic`, `bursty`, `step` (8 min each, or 27.5 with `--long`), and `smoke` (5 min, for checking a setup works). With the ~1.5 min reset before each run, all 10 short runs (5 workloads × 2 autoscalers) take about 1.6 hours. Needs `pyyaml` (in the repo's `requirements.txt`). It also sets metrics-server to a 15 s refresh if it isn't already, because Minikube resets it on restart.

Each run writes `cloud-env/results/<time>_<workload>_<autoscaler>/` (with `_long` on the end for `--long` runs):

| File | Contents |
|---|---|
| `replicas.csv` | Every 15 s: ready replicas, desired replicas, and the metric the autoscaler saw (CPU % for HPA, req/s per pod for KEDA) |
| `locust_stats_history.csv` | Locust's req/s, response times and failures over time |
| `locust_stats.csv`, `locust_failures.csv` | Run totals and any errors |
| `locust.log` | Locust's console output (includes `bursty.py`'s burst schedule) |
| `run.json` | Workload, autoscaler, length, scale-down wait, seed, git commit, start/end times |

Copying Locust's CSVs off the cluster uses `minikube cp`; on kind, copy `/data/results` out of the node container yourself.

## How it lines up with the simulator

| | Real cluster | Simulator / HPA baseline |
|---|---|---|
| Capacity per pod | 250m CPU request, ~5 ms CPU per `/work` request → 100% at ~50 req/s | `capacity_per_replica = 50` |
| Replicas | min 1, max 20, starts at 2 | same |
| HPA | 60% CPU target, scale up immediately, 5 min scale-down window | same (`controller/hpa_baseline.py`) |
| Workloads | `ramp.py` and `spike.py` follow the simulator's tick timing (1 tick = 15 s) | `experiments/configs/ramp.yaml`, `spike.yaml` |

Known differences:
- **CPU can read above 100% here.** The CPU limit (1 core) is above the request, so HPA can see up to ~400%; the simulator caps at 100%. Setting limit = request would match the cap, but the pod then gets paused during bursts (CFS throttling): we measured ~480 ms average latency at ~60% CPU, versus ~115 ms with the higher limit.
- **HPA sees CPU ~15–30 s late.** Minikube's metrics-server addon defaults to a 60 s refresh (`--metric-resolution=60s`), which made HPA 1–2 minutes late; `run_experiment.py` sets it to 15 s. The simulator's HPA sees each tick's CPU immediately.
- **Pods start in seconds**, not the simulator's 30 s startup delay.
- **The 10% tolerance** is the Kubernetes default, not set in the manifest.

## Workloads

Every workload is written as a 20-minute pattern (the simulator's 80 ticks), followed by a cooldown at 20 req/s so you can watch the autoscaler scale down. Each simulated user sends 1 request/s, so users = req/s. `RUN_LENGTH` (set by `run_experiment.py`) picks the speed:

| | Pattern | Cooldown | Total | Scale-down wait |
|---|---|---|---|---|
| **short** (default) | played 4× faster: 5 min | 3 min | **8 min** | 1 min (set by `run_experiment.py`) |
| **long** (`--long`) | real time: 20 min | 7.5 min | **27.5 min** | 5 min (Kubernetes default) |

Short runs are for quick comparisons; their scale-down wait is cut to 1 minute for both autoscalers so it fits in the cooldown. Use `--long` for results that match the simulator's timing and Kubernetes's defaults. The times in the table below are on the 20-minute timeline; divide by 4 for a short run.

| File | Pattern (first 20 min) | What it tests | Simulator twin |
|---|---|---|---|
| `ramp.py` | 100 req/s, climbs to 500 between 2:30 and 12:30, holds | Gradual growth; HPA should cope well | `ramp.yaml` |
| `spike.py` | 100, instant jump to 500 from 7:30 to 12:30, back to 100 | Sudden load with no warning; HPA lags behind | `spike.yaml` |
| `periodic.py` | Smooth wave 100 → 400 → 100, two 10-minute cycles | Predictable cycles; does HPA track them or lag? | none yet |
| `bursty.py` | 100 baseline, random 30–90 s bursts to 250–500, every 1–3 min | Over-reacting to brief spikes, flapping | none yet |
| `step.py` | 100 → 200 → 300 → 400 → 500, 4 minutes per step | How long HPA takes to settle at each level | none yet |

`bursty.py` is random but repeatable: the bursts come from a fixed seed (`BURST_SEED` in `locust-job.yaml`, default 42), so every controller faces the same traffic. The schedule is printed at the top of the Locust logs.

Shared code lives in `common.py`: the user class (which reconnects every 10 requests so load spreads onto new pods) and `RateShape`, which handles the cooldown and stop. A new workload only has to subclass `common.RateShape` and write `rate(t)`. See `step.py` for the shortest example.

## Run it by hand

`run_experiment.py` above is the normal way. These are the manual steps, useful for debugging. Commands use `kubectl`. On Minikube, if your `kubectl` is more than one minor version away from the cluster (Docker Desktop's bundled one often is), use `minikube kubectl --` in its place.

**1. Build and deploy** (from the repo root):

```bash
docker build -t scale-app:local ./cloud-env/app
minikube image load scale-app:local                 # or: kind load docker-image scale-app:local --name scale
kubectl apply -f cloud-env/k8s/
kubectl rollout status deployment/scale-app
```

After a rebuild with the same tag, run `kubectl rollout restart deployment/scale-app` so the pods pick up the new image.

**2. Run a workload** (8 minutes; set `RUN_LENGTH: long` in the Job for 27.5). First pick one by setting `WORKLOAD` in `cloud-env/workloads/locust-job.yaml` (default `ramp.py`). The manifests keep the 5-minute scale-down wait, so a short run by hand ends before HPA scales down. Then:

```bash
kubectl create configmap workloads --from-file=cloud-env/workloads/
kubectl apply -f cloud-env/workloads/locust-job.yaml
kubectl logs -f job/locust-run      # live request stats
```

The ConfigMap holds the whole folder, because each workload imports `common.py`.

**3. Watch it scale**, in another terminal:

```bash
kubectl get hpa -w      # HPA's is "scale-app"; KEDA's is "keda-hpa-scale-app"
```

At the 60% target each pod takes ~30 req/s, so expect roughly `req/s ÷ 30` replicas: ~17 at 500 req/s. Scale-down starts about 5 minutes after load drops (1 minute in a short `run_experiment.py` run).

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

Use `common.AppUser` in the calibration locustfile too (e.g. a file containing just `from common import AppUser`), so connections are handled the same way as in real runs.

Measured on 2026-11-12 (Minikube 1.39, 6 CPUs / 5 GB, Kubernetes 1.37, Docker driver, Windows 10): 50.3 req/s → 234m total across 2 pods (~4.7 ms per request), 64 ms average latency, 0 failures.

## First results (2026-10-31)

**These ran on a 2-CPU Minikube cluster (the default), which was throttled above ~250 req/s, and on an app that handled `/work` in a thread pool. Their latency numbers aren't valid.** See "Fixes found 2026-11-12" below.

- **Full ramp, before the connection fix:** HPA scaled 2 → 11 pods and back down to 1 after the drop, but load plateaued at ~345 req/s instead of 500. Locust's keep-alive connections stayed on the oldest pods, which saturated while newer pods sat half idle. Average CPU read ~62%, inside HPA's tolerance, so it stopped scaling. Fixed with a new connection per request.
- **5-minute check after the fix** (100 → 300 req/s over 2 min, then hold): CPU was even across pods, 0 failures in 73k requests, and the HPA reached 9 pods. Latency during the ramp was high (587 ms average) because the HPA lagged behind the 60 s metrics refresh. **The full 25-minute ramp has not been rerun since the fix.**

## Fixes found 2026-11-12

High latency (median ~1 s at only ~60% CPU) had three causes, found while adding KEDA:

1. **Minikube capped the whole cluster at 2 CPUs.** At ~300 req/s the cluster used ~2 cores in total and everything was throttled in 100 ms slices. Fixed by creating the cluster with `--cpus=6 --memory=5g`.
2. **`/work` ran in a thread pool.** The spinning worker threads starved the main thread (which accepts and answers requests) of Python's GIL. It now runs on the main thread, one request at a time.
3. **A new connection on every request** made Locust reuse source ports faster than the node's connection table expired them, so connection attempts were dropped and retried after 1 s. Locust now reconnects every 10 requests, which still spreads load onto new pods within ~10 s.

Same test before and after (10 fixed pods, steady 300 req/s): median latency 1100 ms → 74 ms, worst case 7.7 s → 0.6 s, 247 → 299.5 req/s delivered, 0 failures.

**Open question:** Locust reports a median of ~60–75 ms even at light load, while single `curl` requests from inside the cluster take ~6 ms. It's the same in every run, so it doesn't bias HPA vs KEDA comparisons, but absolute latencies are probably inflated by the load generator.

## Troubleshooting

- **Latency in 100 ms steps (~100, 200, 400 ms ...) while pods aren't busy:** the whole cluster is CPU-throttled. Check the Minikube size (see "Cluster size matters").
- **A pod shows `0/1` ready under load:** its readiness probe is timing out (it allows 3 s). Only expected when a pod is far past capacity (~200 req/s each).
- **HPA shows `<unknown>`:** normal for ~1 minute after deploying. If it persists, see setup.md troubleshooting.
- **`job.batch "locust-run"` already exists:** delete the old Job first (see "Run it again").
- **Locust exits with `ModuleNotFoundError: No module named 'common'`:** the ConfigMap was made from a single file. Recreate it from the whole folder (`--from-file=cloud-env/workloads/`).
- **Job shows `Failed` but the run looks fine:** Locust exits with an error if *any* request failed. Check `kubectl logs job/locust-run` for the error report. `ConnectionRefusedError` during scale-down means the `preStop` sleep in the Deployment is missing.
- **The autoscaler adds pods but a few pods do most of the work:** connections are being reused forever. Workloads must use `common.AppUser`, which reconnects every 10 requests. (Reconnecting on *every* request causes the opposite problem at ~300 req/s: dropped connection attempts and 1 s TCP retries.)
