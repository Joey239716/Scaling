# Dev environment setup

This gets you to the Phase 1 milestone: a containerized app running on a local Kubernetes cluster, scaled automatically by HPA under Locust-generated traffic. You do **not** need an AWS/Azure account for any of this — everything runs locally.

> **You build `cloud-env/` yourself before steps 5 and 6.**
> Steps 1–4 (tools, cluster, metrics server) work as written. Steps 5 and 6 deploy and load-test an app that **doesn't exist yet**: `cloud-env/` currently holds only a README. Writing it is the core of this exercise. See [Your task: build `cloud-env/`](#your-task-build-cloud-env) below for what to build. Step 6 working is the definition of done.

## 1. Install the tooling

| Tool | What it's for | Install |
|---|---|---|
| Docker | Containerize the app | [docs.docker.com/get-docker](https://docs.docker.com/get-docker/) |
| kubectl | Talk to the Kubernetes cluster | [kubernetes.io/docs/tasks/tools](https://kubernetes.io/docs/tasks/tools/) |
| Minikube *or* kind | Run Kubernetes locally | [minikube.sigs.k8s.io](https://minikube.sigs.k8s.io/docs/start/) or [kind.sigs.k8s.io](https://kind.sigs.k8s.io/docs/user/quick-start/) |
| Python 3.11+ | The app and workload scripts | [python.org/downloads](https://www.python.org/downloads/) |
| Locust | Workload generation | `pip install locust` |

If you're unsure whether to pick Minikube or kind: either is fine for Phase 1. Minikube has a friendlier UI/dashboard; kind starts faster and is closer to what CI systems usually use. Pick one and be consistent across the team so results are comparable.

## 2. Clone the repo

```bash
git clone https://github.com/<org>/SCALE.git
cd SCALE
```

## 3. Start your local cluster

```bash
# Minikube
minikube start --cpus=6 --memory=5g

# or kind
kind create cluster --name scale
```

**Give Minikube enough CPU.** With the Docker driver, Minikube caps the whole cluster at **2 CPUs** by default, even though `kubectl describe node` reports every CPU on your machine. At ~300 req/s the app, Locust and Prometheus need more than that, and the whole cluster gets throttled: requests stall in 100 ms steps and latency jumps from ~10 ms to ~1 s. Use the same size as the rest of the team so results are comparable. The size is fixed when the cluster is created: to change it, run `minikube delete` and start again. On kind, the limit is Docker Desktop's own CPU setting.

Verify it's up:

```bash
kubectl get nodes
```

## 4. Enable the metrics server

HPA needs the metrics server to read CPU/memory usage.

```bash
# Minikube
minikube addons enable metrics-server

# kind
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

**kind needs that second command.** kind's nodes use self-signed certificates, so without `--kubelet-insecure-tls` the metrics server stays at `0/1` ready and its logs show `x509: cannot validate certificate`. This flag is fine for a local cluster; never use it on a real one.

Check it works (give it a minute):

```bash
kubectl top nodes
```

## Your task: build `cloud-env/`

Everything from here on needs these files, which you write. Keep it small: the whole thing is roughly 100–150 lines.

| Path | What it is | Requirements |
|---|---|---|
| `cloud-env/app/` | The service we scale, plus its `Dockerfile` | A web app (FastAPI is suggested) with a cheap health endpoint, and an endpoint where **every request burns a fixed, small amount of CPU** (a few milliseconds). That makes "more requests" mean "more CPU", which is what HPA scales on. |
| `cloud-env/k8s/` | Deployment, Service and HPA manifests | **Deployment:** must set `resources.requests.cpu` (see the traps below); a readiness probe on the health endpoint is recommended. **Service:** in front of the pods. **HPA:** `autoscaling/v2`, **60% CPU target** to match our simulator's HPA baseline, sensible min/max replicas. |
| `cloud-env/workloads/ramp.py` | A Locust workload | Traffic that ramps up gradually, holds, then drops, so you can see HPA scale up and then back down. Locust's `LoadTestShape` class is the tool for this. |
| a way to run Locust **inside** the cluster | See step 6 | e.g. a Pod using the official `locustio/locust` image, with `ramp.py` mounted from a ConfigMap. |
| `cloud-env/README.md` | How to run it | Update it with the exact commands, so the next person can reproduce your run. |

Three traps everyone hits:

1. **No CPU request means no autoscaling.** HPA measures utilization as a percentage of each container's CPU *request*. Without `resources.requests.cpu` in the Deployment, `kubectl get hpa` shows `<unknown>` forever.
2. **Your image only exists on your laptop.** The cluster can't download `scale-app:local` from anywhere, so load it into the cluster (step 5) or the pods sit in `ErrImagePull`. Use a real tag like `:local`: with `:latest` (or no tag), Kubernetes tries to download the image every time unless you also set `imagePullPolicy: IfNotPresent`.
3. **`kubectl port-forward` sends all traffic to one pod**, even when you point it at a Service. Fine for a quick `curl`, useless for a load test: the pods HPA adds would never get any traffic. That's why Locust has to run inside the cluster (step 6).

## 5. Build and deploy the test app

*Needs `cloud-env/app/` and `cloud-env/k8s/` from [your task](#your-task-build-cloud-env).*

```bash
docker build -t scale-app:local ./cloud-env/app

# Make the image available inside the cluster:
kind load docker-image scale-app:local --name scale   # kind
minikube image load scale-app:local                   # or Minikube

kubectl apply -f cloud-env/k8s/
kubectl get pods -w
```

Quick check that the app answers (this one is fine through port-forward):

```bash
kubectl port-forward svc/scale-app 8080:80   # then, in another terminal:
curl localhost:8080/healthz
```

## 6. Generate load and watch it scale

*Needs `cloud-env/workloads/` from [your task](#your-task-build-cloud-env).*

Run Locust **inside the cluster**, pointed at the Service's in-cluster address (`http://scale-app`), so requests are spread across every pod. Running it on your laptop through `port-forward` won't work (trap 3 above). One way: put `ramp.py` in a ConfigMap and start a Pod from the `locustio/locust` image that mounts it:

```bash
kubectl create configmap locustfile --from-file=cloud-env/workloads/ramp.py
kubectl apply -f <your Locust Pod manifest>
kubectl logs -f <locust pod>      # live request stats
```

Then in another terminal:

```bash
kubectl get hpa -w
```

You should see replica count increase as load ramps up, and decrease again once it drops — that's the Phase 1 milestone. Scaling down is deliberately slow: HPA waits 5 minutes after load drops (its stabilization window, the same one our simulator models).

## Troubleshooting

- **Pods stuck in `Pending`:** usually a resource request issue — check `kubectl describe pod <name>` for scheduling errors.
- **Pods stuck in `ErrImagePull` / `ImagePullBackOff`:** the cluster is trying to download your local image. Load it into the cluster (step 5); if you tagged it `:latest`, also set `imagePullPolicy: IfNotPresent`.
- **HPA shows `<unknown>` for targets:** either your Deployment has no `resources.requests.cpu`, or the metrics server isn't ready. Check `kubectl top pods`: if that errors, it's the metrics server (on kind, see step 4).
- **Metrics server stuck at `0/1` on kind:** the `--kubelet-insecure-tls` patch in step 4 is missing.
- **HPA adds pods but only one is busy:** you're sending load through `kubectl port-forward`. Run Locust inside the cluster (step 6).
- **Can't reach the service for a quick check:** `kubectl port-forward svc/<name> 8080:80` (kind) or `minikube service <name> --url` (Minikube).

If you hit something not covered here, add it to this file once you've solved it.
