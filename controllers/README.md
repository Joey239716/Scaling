# controllers/: autoscaler plugins for the cluster experiments

Each folder here is one autoscaler that `experiments/run_experiment.py` can run:

| Plugin | Status | What it is |
|---|---|---|
| [`hpa/`](hpa/) | Ready | Kubernetes HPA on CPU (60% target, 1 to 10 pods): the baseline |
| [`keda/`](keda/) | To build | KEDA ScaledObject (KEDA is already installed by the setup script) |
| [`phpa/`](phpa/) | To build | Predictive Horizontal Pod Autoscaler |
| [`llm/`](llm/) | To build | Our LLM-based controller, running as a separate process |

Not to be confused with [`controller/`](../controller/) (singular). That folder holds the
Camp QMIND **simulator** controllers and isn't used here.

## The plugin contract

A plugin is a folder `controllers/<name>/` containing **any combination** of these.
The runner uses whichever parts exist:

| Part | When the runner uses it | What it must do |
|---|---|---|
| `manifests/` (YAML files) | apply: `kubectl apply -f manifests/`; remove: `kubectl delete -f manifests/` | Kubernetes objects such as an HPA or a KEDA `ScaledObject`. Put `namespace: scale` in each one. |
| `apply.sh` | after `manifests/` | One-time setup that YAML can't express, e.g. installing an operator with Helm. Must be safe to run again. |
| `start.sh` | after `apply.sh`, as a background process | Starts a long-running controller (e.g. `exec python -m controllers.llm.main`). Should run until stopped. stdout and stderr go to `controller.log` in the run's results folder. |
| `stop.sh` | after the process is stopped | Optional extra cleanup for a process-based controller. |
| `remove.sh` | after `stop.sh`, before deleting `manifests/` | Undo `apply.sh`. **Must succeed even if nothing was applied**: the runner calls every plugin's remove steps before each run to guarantee a clean start. |

A folder with none of these (just a README) counts as "not implemented yet", and the
runner refuses to run it with a clear message.

**Stopping a process:** the runner sends `SIGTERM` to `start.sh`'s whole process group, waits
up to 20 s, then sends `SIGKILL`. Handle `SIGTERM` if you need to clean up. Use `exec` in
`start.sh` so your program receives the signal directly.

**Environment** given to every script and process:

| Variable | Value |
|---|---|
| `KUBE_CONTEXT` | `scale` (always pass `--context "$KUBE_CONTEXT"` to kubectl) |
| `SCALE_NAMESPACE` | `scale` |
| `SCALE_DEPLOYMENT` | `scale-app` |
| `SCALE_MIN_REPLICAS`, `SCALE_MAX_REPLICAS` | `1`, `10`: every controller must stay within these |
| `PYTHONPATH` | includes the repo root, so `import telemetry` works |

### Rules every controller follows (fairness)

1. **Target only** Deployment `scale-app` in namespace `scale`. Don't change its CPU or memory
   requests, its image, or anything else about the app. The runner resets replicas at the start of every run.
2. **Stay within 1 to 10 replicas.**
3. **Observe through `telemetry/`**, never your own Prometheus queries:
   `from telemetry import latest_snapshot`. The snapshot format is in
   [docs/telemetry.md](../docs/telemetry.md). (Controllers that Kubernetes runs itself, such as HPA,
   read metrics-server. KEDA and PHPA read their own sources; document which ones in your README.)
4. **Don't know you're being evaluated:** no reading of scenario files, results or run metadata.
5. **LLM decisions go through guardrails** (bounds, cooldown) before any `kubectl scale`, as required by
   [AGENTS.md](../AGENTS.md). Never send raw model output to the Kubernetes API.

## Plugging yours in

1. Put your files in your folder, following the table above.
2. Do a quick check: `python experiments/run_experiment.py --controller <name> --scenario gradual_ramp --duration 180`
3. Check `experiments/results/gradual_ramp/<name>/repeat_1/`: `metrics.csv` should show
   `replicas_desired` changing, and `controller.log` (process controllers) should show your decisions.
4. Write down your settings (targets, windows, cooldowns) in your folder's README, as `hpa/README.md` does.

Running it by hand outside the runner is fine for debugging, but delete what you applied afterwards
(`kubectl --context scale -n scale get hpa,scaledobjects`): the runner removes leftovers anyway.
