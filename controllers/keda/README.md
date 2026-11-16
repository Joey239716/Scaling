# keda: KEDA autoscaler (to build)

**Status: not implemented.** The runner refuses to run it until this folder has a
`manifests/` folder (or `apply.sh` / `start.sh`). Read the contract in
[controllers/README.md](../README.md) first.

KEDA itself is already installed by `scripts/setup_cluster.sh` (Helm chart 2.21.0,
namespace `keda`), so this plugin most likely needs only a manifest:

```
controllers/keda/
├── README.md
└── manifests/
    └── scaledobject.yaml     # kind: ScaledObject, namespace: scale, scaleTargetRef: scale-app
```

Things to decide and write down here:
- **Trigger(s):** CPU (`type: cpu`, utilization relative to the 100m request, like HPA) and/or
  Prometheus (`type: prometheus`, e.g. request rate). Prometheus is reachable inside the cluster at
  `http://kps-prometheus.monitoring.svc:9090`.
  The app's metric names are in [docs/telemetry.md](../../docs/telemetry.md).
- `minReplicaCount: 1`, `maxReplicaCount: 10` (the shared bounds), `pollingInterval`, `cooldownPeriod`.
- Any `advanced.horizontalPodAutoscalerConfig.behavior`: KEDA creates an HPA under the hood, so the
  same scale-up and scale-down windows apply.

Quick check: `python experiments/run_experiment.py --controller keda --scenario gradual_ramp --duration 180`.
