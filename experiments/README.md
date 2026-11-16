# experiments/

Two separate things live here:

- **Cluster experiments** (current work): real controllers on the local Kubernetes cluster
  under Locust traffic. `run_experiment.py`, `scenarios/`.
- **Camp QMIND simulator** (earlier onboarding exercise, unchanged): `run.py`, `plot.py`, `configs/`.
  See [camp_qmind.md](../docs/onboarding/camp_qmind.md).

```
experiments/
├── run_experiment.py   # cluster runner (below)
├── scenarios/          # Locust scenarios for the cluster runner
├── run.py, plot.py     # simulator runner and plots
├── configs/            # simulator scenario YAMLs
├── results/            # outputs of both (gitignored)
└── cache/              # simulator LLM response cache (gitignored)
```

## Running a cluster experiment

Set up the cluster first ([docs/onboarding/setup.md](../docs/onboarding/setup.md)), then:

```bash
python experiments/run_experiment.py --controller <name> --scenario <name> \
    [--repeat N] [--duration S] [--initial-replicas N]
```

| Option | Default | Meaning |
|---|---|---|
| `--controller` | required | Folder under [`controllers/`](../controllers/) (`hpa`, `keda`, `phpa`, `llm`) |
| `--scenario` | required | File under [`scenarios/`](scenarios/), without `.py` (e.g. `gradual_ramp`) |
| `--repeat` | 1 | Run repeats 1..N. Existing repeat folders are skipped, never overwritten. |
| `--duration` | 600 | Load test length in seconds |
| `--initial-replicas` | 1 | Pods at the start of each run |

Example: `python experiments/run_experiment.py --controller hpa --scenario gradual_ramp --repeat 3`

### What every run does, in order

1. **Reset:** stop and remove every controller (every plugin's remove steps, plus any HPA or KEDA
   ScaledObject left in namespace `scale`), scale `scale-app` to `--initial-replicas`, and wait until
   exactly that many pods are ready and none are starting or terminating.
2. **Apply** the controller's plugin (`manifests/`, `apply.sh`, then `start.sh` in the background).
3. **Settle:** wait until all pods are ready, then 60 s more.
4. **Load test:** Locust runs the scenario headless for `--duration` seconds against the app's
   NodePort (through `minikube service`, which spreads load across all pods).
5. **Export:** after 30 s (for the last Prometheus scrape), one telemetry snapshot per 15 s for the
   load test window → `metrics.csv`.
6. **Remove** the controller. This runs even if an earlier step failed.
7. **Metadata** → `metadata.json`.

The fixed settings (settle time, replica bounds, kube context) are constants at the top of
`run_experiment.py`. If a run fails, its partial folder is renamed `repeat_N_failed_<time>` so a rerun
redoes it.

### Output

`experiments/results/<scenario>/<controller>/repeat_<N>/`:

| File | Contents |
|---|---|
| `metrics.csv` | One row per 15 s: replicas, CPU, request rate, latency p50/p95, error rate. Format: [docs/telemetry.md](../docs/telemetry.md) |
| `metadata.json` | Controller, scenario, repeat, start/end times, duration, initial replicas, git commit (and whether the tree had uncommitted changes), machine, cluster config (Kubernetes version, Minikube CPUs/memory, Helm chart versions, app image and resources), file hashes of the scenario and plugin, and the exact telemetry queries |
| `locust_stats.csv`, `locust_failures.csv`, `locust_stats_history.csv`, ... | Locust's own client-side numbers |
| `locust.log` | Locust output |
| `controller.log` | stdout/stderr of a process-based controller (e.g. llm) |

Results are gitignored. Share them some other way (or commit a summary), and always keep `metadata.json` with the CSV.

## Controller plugins

See [controllers/README.md](../controllers/README.md) for the plugin contract: `manifests/`, `apply.sh`,
`remove.sh` and `start.sh`/`stop.sh` for controllers that run as a separate process, the environment
they get, and the fairness rules.

## Adding a scenario

See [scenarios/README.md](scenarios/README.md).

## Before adding a new experiment

Open an issue describing the question it answers and what would count as a meaningful result either
way. See [CONTRIBUTING.md](../CONTRIBUTING.md).
