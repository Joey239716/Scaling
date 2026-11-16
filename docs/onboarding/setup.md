# Dev environment setup

This gets you a local Kubernetes cluster with everything the experiments need: the app
we scale, metrics-server, Prometheus and KEDA. Then it runs your first experiment.
Everything runs on your laptop; you don't need a cloud account.

Everyone uses the **same setup script, versions and settings**, so results from different
laptops and different controllers are comparable. Don't start your own cluster by hand for
experiments.

## 1. Install the tools

| Tool | What it's for | macOS (Homebrew) | Other |
|---|---|---|---|
| Docker Desktop | Runs Minikube's node and builds the app image | [docker.com](https://docs.docker.com/get-docker/) | same |
| Minikube | Local Kubernetes | `brew install minikube` | [minikube.sigs.k8s.io](https://minikube.sigs.k8s.io/docs/start/) |
| kubectl | Talk to the cluster | `brew install kubectl` | [kubernetes.io](https://kubernetes.io/docs/tasks/tools/) |
| Helm | Installs Prometheus and KEDA | `brew install helm` | [helm.sh](https://helm.sh/docs/intro/install/) |
| Python 3.11+ | Runner, telemetry, Locust | [python.org](https://www.python.org/downloads/) | same |

**Give Docker Desktop enough resources:** Settings → Resources, at least **6 CPUs and 8 GB of memory**.
The cluster takes 4 CPUs and 6 GB; the rest is for Locust, which runs on your machine.

## 2. Clone and install the Python packages

```bash
git clone https://github.com/daryanfadavi/Scaling.git
cd Scaling
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # includes locust and requests
```

## 3. Bring up the cluster (one command)

```bash
./scripts/setup_cluster.sh
```

The first run takes about 10 minutes (downloads). It's safe to run again at any time: it only
fixes what's missing, and it rebuilds and redeploys the app. It:

1. Starts Minikube (profile and kubectl context `scale`: 4 CPUs, 6 GB, Kubernetes v1.35.0).
2. Enables metrics-server (what HPA reads CPU from).
3. Installs Prometheus (kube-prometheus-stack chart 93.3.0, namespace `monitoring`).
4. Installs KEDA (chart 2.21.0, namespace `keda`).
5. Builds the app image `scale-app:v1` inside Minikube and deploys it (namespace `scale`).
6. Checks that each part is healthy and prints a summary ending in `Cluster ready.`

The settings are variables at the top of the script. **Don't change them for your own runs**:
results are only comparable on the same cluster settings. Every run's `metadata.json` records them.

## 4. Run your first experiment

```bash
python experiments/run_experiment.py --controller hpa --scenario gradual_ramp --duration 180
```

It takes about 5 minutes. Watch the scaling while it runs, from another terminal:

```bash
kubectl --context scale -n scale get hpa -w
```

When it finishes, look in `experiments/results/gradual_ramp/hpa/repeat_1/`. `metrics.csv` has one
row per 15 s; `replicas_desired` should go up as the load ramps. See
[experiments/README.md](../../experiments/README.md) for everything the runner does, and
[docs/telemetry.md](../telemetry.md) for what each column means.

## 5. Plug in your controller

KEDA, PHPA and the LLM controller are plugins in `controllers/<name>/`. The contract (what files to
add, what the runner does with them, and the fairness rules) is in
[controllers/README.md](../../controllers/README.md). Each plugin folder's README has specific notes.

## The app and cluster at a glance

| What | Value |
|---|---|
| kubectl context | `scale` |
| Namespace / Deployment / Service | `scale` / `scale-app` / `scale-app` (NodePort) |
| Pod label | `app.kubernetes.io/name=scale-app` |
| Image | `scale-app:v1` (built locally, never pulled) |
| CPU request / limit | 100m / 500m |
| Memory request / limit | 128Mi / 256Mi |
| Replica bounds for every controller | 1 to 10 |
| Prometheus | `kps-prometheus.monitoring.svc:9090` (in cluster) |

Details: [cloud-env/README.md](../../cloud-env/README.md).

## Teardown

```bash
./scripts/teardown_cluster.sh      # deletes the cluster; results in experiments/results/ stay
```

## Troubleshooting

- **`Docker isn't running`:** start Docker Desktop, wait for it to be ready, rerun the setup script.
- **Minikube fails to start with a memory error:** Docker Desktop has less than the 6 GB the cluster needs.
  Raise it in Docker Desktop's settings, then `./scripts/teardown_cluster.sh` and run setup again.
- **`cluster runs vX but K8S_VERSION is vY`:** your cluster was made with other settings. Tear it down and
  run setup again.
- **Setup times out on a health check:** rerun it (it picks up where it left off). If it keeps failing,
  check `kubectl --context scale get pods -A` for pods that aren't `Running`.
- **`No kubectl context 'scale'`** from the runner: run the setup script first.
- **HPA shows `<unknown>` for targets:** metrics-server isn't ready yet. Wait a minute; check `kubectl --context scale top pods -n scale`.
- **You changed the app:** bump the image tag (`v1` → `v2`) in `cloud-env/app/Dockerfile`'s comment,
  `cloud-env/k8s/deployment.yaml` and `scripts/setup_cluster.sh`, then rerun setup. Results with different
  app versions aren't comparable.
- **Why not `kubectl port-forward` for load?** It sends every request to a single pod, so pods added by a
  controller would get no traffic. The runner uses `minikube service` instead, which spreads load across all pods.

If you hit something not covered here, add it to this file once you've solved it.
