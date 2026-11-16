# Telemetry format

Every cluster experiment produces the same kind of record: a **snapshot** of the system
every **15 seconds**. The experiment runner writes snapshots to `metrics.csv` (one row each),
and the LLM controller observes the system through the same snapshots. Because both read
the numbers the same way, every result file and every controller sees identical data.

**All queries live in one module: [`telemetry/queries.py`](../telemetry/queries.py).**
Code reads snapshots through [`telemetry/prometheus.py`](../telemetry/prometheus.py):

```python
from telemetry import latest_snapshot, snapshots_between

latest_snapshot()               # {"timestamp": ..., "replicas_ready": 3.0, ...}  (for controllers)
snapshots_between(start, end)   # list of snapshots, one per 15 s              (for the runner)
```

Controllers must never write their own Prometheus queries. If you need a new signal, add a
field to `telemetry/queries.py` and to this page in a PR. Changing an existing query or
window changes everyone's numbers: don't compare results produced before and after such a change.

## Where the data comes from

| Source | Scraped every | Provides |
|---|---|---|
| The app's `/metrics` (`prometheus-client`, via ServiceMonitor `scale/scale-app`) | 15 s | `scale_app_requests_total{path,status}`, `scale_app_request_duration_seconds` (histogram) |
| kube-state-metrics | 15 s | Deployment replica counts, pod CPU requests, pod phase |
| kubelet / cAdvisor | 10 s | `container_cpu_usage_seconds_total` |

Only requests to `/work` are counted as traffic (health checks and scrapes aren't).
Prometheus is reached through the Kubernetes API server (`kubectl get --raw .../services/kps-prometheus:9090/proxy/...`),
so no port-forward is needed. Set `PROMETHEUS_URL` to use your own instead.

## How a timestamp's value is computed

The runner asks Prometheus for a range query with `step = 15s`, starting when the load test
starts. At each timestamp *t*:

- **Gauges** (replica counts): the most recent scraped value at or before *t*.
- **Rates and averages** (CPU, request rate, latency, errors): the average over the **1 minute
  ending at *t*** (`[1m]` window, about 4 scrapes). Snapshots are 15 s apart, so neighbouring rows
  share 45 s of data: the rate columns are smoothed and respond to a change within about a minute.

## Fields

`metrics.csv` columns, in order:

| Column | Unit | Meaning |
|---|---|---|
| `timestamp` | ISO 8601, UTC | Snapshot time *t* |
| `unix_time` | seconds | Same, as a Unix timestamp |
| `elapsed_s` | seconds | *t* minus load test start |
| `interval_s` | seconds | Always `15` |
| `replicas_desired` | pods | Replicas the Deployment asks for (what the controller set) |
| `replicas_ready` | pods | Pods passing their readiness probe (receiving traffic) |
| `replicas_starting` | pods | Pods that exist but aren't ready yet |
| `cpu_utilization_pct` | % of request | CPU used / CPU requested, over the app's running pods. **Can exceed 100**: the limit (500m) is 5x the request (100m). |
| `request_rate_rps` | req/s | Requests to `/work`, all pods together |
| `latency_p50_ms` | ms | Median server-side latency of `/work` |
| `latency_p95_ms` | ms | 95th percentile server-side latency of `/work` |
| `error_rate` | fraction 0 to 1 | Share of `/work` requests answered with a 5xx status |

## Exact queries

Labels used throughout: `namespace="scale"`, deployment `scale-app`, container `app`,
pods matching `scale-app-.*`.

**replicas_desired** (gauge, latest value)
```promql
kube_deployment_spec_replicas{namespace="scale",deployment="scale-app"}
```

**replicas_ready** (gauge, latest value)
```promql
kube_deployment_status_replicas_ready{namespace="scale",deployment="scale-app"}
```

**replicas_starting** (gauge, latest value)
```promql
clamp_min(
  kube_deployment_status_replicas{namespace="scale",deployment="scale-app"}
  - kube_deployment_status_replicas_ready{namespace="scale",deployment="scale-app"}, 0)
```

**cpu_utilization_pct** (1 m window)
```promql
100 * sum(rate(container_cpu_usage_seconds_total{namespace="scale",pod=~"scale-app-.*",container="app"}[1m]))
/ sum(
    kube_pod_container_resource_requests{namespace="scale",pod=~"scale-app-.*",container="app",resource="cpu"}
    * on (namespace, pod) group_left () (kube_pod_status_phase{namespace="scale",phase="Running"} == 1)
  )
```
This is the Kubernetes definition HPA uses (usage relative to the request, averaged over
pods), with two differences to keep in mind when comparing it to `kubectl get hpa`:
HPA reads metrics-server (a shorter window) and handles not-yet-ready pods specially.
A brand-new pod counts in the denominator from when it's Running, so the value dips
briefly right after a scale-up.

**request_rate_rps** (1 m window)
```promql
sum(rate(scale_app_requests_total{namespace="scale",path="/work"}[1m]))
  or (0 * count(up{namespace="scale",service="scale-app"} == 1))
```

**latency_p50_ms**, **latency_p95_ms** (1 m window; 0.5 and 0.95)
```promql
1000 * histogram_quantile(0.95,
  sum by (le) (rate(scale_app_request_duration_seconds_bucket{namespace="scale",path="/work"}[1m])))
```
Quantiles are estimated from histogram buckets (5, 7.5, 10, 15, 20, 30, 50, 75, 100, 150, 250, 500,
750 ms, 1, 2.5, 5, 10 s), with linear interpolation inside a bucket. This is **server-side**
latency: from the app receiving a request to it responding. Locust's own client-side numbers
(including queueing and network) are saved separately in `locust_stats.csv`.

**error_rate** (1 m window)
```promql
( sum(rate(scale_app_requests_total{namespace="scale",path="/work",status=~"5.."}[1m]))
  or (0 * count(up{namespace="scale",service="scale-app"} == 1)) )
/ sum(rate(scale_app_requests_total{namespace="scale",path="/work"}[1m]))
```
Only counts errors the app itself returned. Requests that never reached a pod (connection
refused, timeouts) show up only in Locust's `locust_failures.csv`.

## Missing data

- **An empty cell means "no data", never zero.** In Python, `latest_snapshot()` returns `None`; pandas
  reads the empty cell as `NaN`. Values are **never** filled in, carried forward or interpolated.
- `request_rate_rps` is `0` (not empty) when Prometheus is scraping the app and there's no traffic.
  It's empty only if no app pod is being scraped at all.
- `latency_p50_ms`, `latency_p95_ms` and `error_rate` are **empty when there were no requests** in the
  window (there's nothing to measure).
- Gauges go empty if kube-state-metrics wasn't scraped for 5 minutes (Prometheus' staleness limit).
  In a healthy cluster this doesn't happen. Many empty replica cells mean something is wrong with
  the cluster, so treat that run as suspect.
- The first rows after the load starts lag behind (1 m windows). Compare controllers on the same
  rows, not on single snapshots.
