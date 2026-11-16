# cloud-env/: the app every controller scales

The test environment for the cluster experiments. `scripts/setup_cluster.sh` builds and
deploys all of it; you shouldn't need to apply these files by hand.

```
cloud-env/
├── app/            # FastAPI service + Dockerfile (image scale-app:v1)
├── k8s/            # Namespace, Deployment, Service, ServiceMonitor
└── monitoring/     # Helm values for Prometheus (kube-prometheus-stack)
```

## The app

- `GET /work`: burns a fixed amount of CPU (20,000 rounds of SHA-256, about 7 ms) and responds.
  More requests per second means proportionally more CPU, which is what CPU-based autoscalers react to.
- `GET /healthz`: cheap check, used by the readiness probe.
- `GET /metrics`: Prometheus metrics for `/work` traffic only:
  `scale_app_requests_total{path,status}` and the histogram `scale_app_request_duration_seconds{path}`.
  The queries over them are in [telemetry/queries.py](../telemetry/queries.py), documented in
  [docs/telemetry.md](../docs/telemetry.md).

## Fixed values every controller targets

| What | Value |
|---|---|
| Namespace | `scale` |
| Deployment / Service | `scale-app` / `scale-app` (NodePort, port 80 → 8000) |
| Pod label (selector) | `app.kubernetes.io/name=scale-app` |
| Container name | `app` |
| Image | `scale-app:v1` (built inside Minikube, `imagePullPolicy: IfNotPresent`) |
| CPU request / limit | `100m` / `500m` |
| Memory request / limit | `128Mi` / `256Mi` |
| `WORK_ROUNDS` | `20000` |
| Replica bounds (all controllers) | 1 to 10 |

**Don't change these for one controller.** HPA and KEDA measure CPU utilization relative to the
CPU request, so a different request changes when they scale. If something here must change, change
it for everyone in a PR, bump the image tag if the app changed (`v1` → `v2`, in `k8s/deployment.yaml`
and `scripts/setup_cluster.sh`), and don't compare results from before and after.

## Things to know

- **Load must go through the Service's NodePort** (the runner uses `minikube service`), not
  `kubectl port-forward`, which sends everything to one pod. Scenarios also close the connection after
  each request (`experiments/scenarios/common.py`), because Kubernetes balances connections, not requests.
- The CPU limit is 5x the request, so per-pod utilization can go up to about 500%.
- Minikube has 4 CPUs in total. Ten pods at their limit (5 CPUs) would oversubscribe it; the scenarios are
  sized to stay well below that.
