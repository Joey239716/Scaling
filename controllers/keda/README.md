# keda: KEDA autoscaler on request rate (baseline)

A manifests-only plugin: the runner applies `manifests/scaledobject.yaml` before the
run and deletes it after. KEDA itself is installed by `scripts/setup_cluster.sh`
(Helm chart 2.21.0, namespace `keda`). KEDA turns the ScaledObject into an HPA named
`keda-hpa-scale-app`, which does the actual scaling.

| Setting | Value |
|---|---|
| Metric | Request rate to `/work`, all pods together, from Prometheus |
| Query | `sum(rate(scale_app_requests_total{namespace="scale",path="/work"}[1m]))`: the same query as telemetry's `request_rate_rps` |
| Prometheus | `http://kps-prometheus.monitoring.svc:9090` (inside the cluster) |
| Target | 9 req/s per pod (`threshold: "9"`; desired pods = ceil(total req/s / 9)) |
| Replicas | 1 to 10 |
| Polling | Every 15 s (`pollingInterval`) |
| Scale up | Same as `hpa/`: no stabilization window; at most +100% or +4 pods per 15 s, whichever is larger |
| Scale down | Same as `hpa/`: 300 s stabilization window; at most -100% per 15 s |
| Built in | 10% tolerance and 15 s sync period of the HPA that KEDA creates |

**Why 9 req/s:** that's the load at which one pod sits at the HPA's 60% CPU target
(about 7 ms of CPU per request against the 100m request; see
[experiments/scenarios/README.md](../../experiments/scenarios/README.md)). So HPA and
KEDA aim for the same number of pods, and the comparison is about the *signal* (CPU
from metrics-server vs request rate from Prometheus), not a different operating point.
If the app's CPU per request changes, recheck this number against a run's
`cpu_utilization_pct` and `request_rate_rps` columns.

**This number depends on the machine.** On a Windows laptop (Docker Desktop, Locust on
the same host), a test run measured about 18 ms of CPU per request: 18.2 req/s on 7 pods
at 48% CPU. There, 60% CPU is about 3.3 req/s per pod, so a threshold of 9 lets each pod
run near 160% CPU, and KEDA would run fewer pods than HPA. Calibrate the threshold on
the machine that collects the final results, from steady rows of any run's `metrics.csv`:

    threshold = 60 × request_rate_rps / (cpu_utilization_pct × replicas_ready)

(the example above: 60 × 18.2 / (47.7 × 7) ≈ 3.3).

`cooldownPeriod` only matters when scaling to zero, which `minReplicaCount: 1` rules out.

**Observation note (controllers/README.md, rule 3):** KEDA reads Prometheus itself with
the query above rather than through `telemetry/`, because KEDA runs inside the cluster.
The query is copied from `telemetry/queries.py`, so it sees the same numbers.

Quick check: `python experiments/run_experiment.py --controller keda --scenario gradual_ramp --duration 180`.
