# hpa: Kubernetes Horizontal Pod Autoscaler (baseline)

A manifests-only plugin: the runner applies `manifests/hpa.yaml` before the
run and deletes it after.

| Setting | Value |
|---|---|
| Metric | CPU, average utilization relative to the pod's CPU request (100m) |
| Target | 60% |
| Replicas | 1 to 10 |
| Scale up | No stabilization window; at most +100% or +4 pods per 15 s, whichever is larger |
| Scale down | 300 s stabilization window; at most -100% per 15 s |
| Built in | 15 s sync period, 10% tolerance (both kube-controller-manager defaults) |

All values are the Kubernetes defaults, written out so they're visible and
can't change silently between Kubernetes versions. CPU comes from
metrics-server (installed by `scripts/setup_cluster.sh`), not from Prometheus.

The 60% target matches the simulator's `hpa` baseline (`controller/hpa_baseline.py`).
