#!/usr/bin/env bash
# One-command setup of the local experiment cluster. Safe to re-run: every step
# checks what's already there and only does what's missing or out of date.
#
#   ./scripts/setup_cluster.sh
#
# Brings up: Minikube -> metrics-server -> Prometheus (kube-prometheus-stack)
# -> KEDA -> the app (scale-app:v1) -> health checks + summary.
# Tear down with ./scripts/teardown_cluster.sh.
set -euo pipefail

# ---- Settings (keep these identical across everyone's runs) -----------------
PROFILE="${PROFILE:-scale}"              # Minikube profile = kubectl context name
CPUS="${CPUS:-4}"                        # leave the rest of the host's CPUs for Locust
MEMORY_MB="${MEMORY_MB:-6144}"
K8S_VERSION="${K8S_VERSION:-v1.35.0}"
DRIVER="${DRIVER:-docker}"
METRICS_RESOLUTION="15s"               # metrics-server scrape interval (what HPA sees)

APP_IMAGE="scale-app:v1"                 # must match cloud-env/k8s/deployment.yaml
APP_NAMESPACE="scale"

PROM_CHART_VERSION="93.3.0"              # prometheus-community/kube-prometheus-stack
KEDA_CHART_VERSION="2.21.0"              # kedacore/keda
# -----------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
KUBECTL=(kubectl --context "$PROFILE")
HELM=(helm --kube-context "$PROFILE")

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# Retry a command until it succeeds or the timeout (seconds) runs out.
wait_for() {
  local timeout="$1" what="$2"; shift 2
  local start=$SECONDS
  until "$@" >/dev/null 2>&1; do
    (( SECONDS - start > timeout )) && fail "timed out after ${timeout}s waiting for: $what"
    sleep 5
  done
}

step "Checking tools"
for tool in minikube kubectl helm docker; do
  command -v "$tool" >/dev/null || fail "$tool not found. See docs/onboarding/setup.md (step 1)."
done
docker info >/dev/null 2>&1 || fail "Docker isn't running. Start Docker Desktop and try again."
grep -q "image: $APP_IMAGE" cloud-env/k8s/deployment.yaml \
  || fail "APP_IMAGE ($APP_IMAGE) doesn't match the image in cloud-env/k8s/deployment.yaml"

step "Minikube ($PROFILE: $CPUS CPUs, ${MEMORY_MB} MB, Kubernetes $K8S_VERSION)"
if minikube status -p "$PROFILE" >/dev/null 2>&1; then
  echo "Already running."
  running_version="$("${KUBECTL[@]}" version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
  if [[ "$running_version" != "$K8S_VERSION" ]]; then
    fail "cluster runs $running_version but K8S_VERSION is $K8S_VERSION. Run ./scripts/teardown_cluster.sh first."
  fi
else
  # A stopped profile keeps its original CPU/memory; `minikube start` just resumes it.
  minikube start -p "$PROFILE" --driver="$DRIVER" --cpus="$CPUS" --memory="$MEMORY_MB" \
    --kubernetes-version="$K8S_VERSION"
fi

step "metrics-server (what HPA reads CPU from)"
minikube addons enable metrics-server -p "$PROFILE"
# Minikube's addon reads CPU only every 60 s; the upstream default (and HPA's own
# 15 s sync period) is 15 s. At 60 s, HPA would react up to a minute late, which
# would handicap it against controllers that read Prometheus every 15 s.
current_args="$("${KUBECTL[@]}" -n kube-system get deployment metrics-server \
  -o jsonpath='{.spec.template.spec.containers[0].args}')"
if ! grep -q -- "--metric-resolution=${METRICS_RESOLUTION}" <<<"$current_args"; then
  new_args="$(python3 -c '
import json, sys
args = [a for a in json.loads(sys.argv[1]) if not a.startswith("--metric-resolution=")]
print(json.dumps(args + ["--metric-resolution=" + sys.argv[2]]))' "$current_args" "$METRICS_RESOLUTION")"
  "${KUBECTL[@]}" -n kube-system patch deployment metrics-server --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$new_args}]"
fi
"${KUBECTL[@]}" -n kube-system rollout status deployment/metrics-server --timeout=180s

step "Helm chart repositories"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
helm repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null
helm repo update prometheus-community kedacore >/dev/null

step "Prometheus (kube-prometheus-stack $PROM_CHART_VERSION)"
"${HELM[@]}" upgrade --install kps prometheus-community/kube-prometheus-stack \
  --version "$PROM_CHART_VERSION" --namespace monitoring --create-namespace \
  --values cloud-env/monitoring/kube-prometheus-stack.values.yaml --wait --timeout 10m

step "KEDA ($KEDA_CHART_VERSION)"
"${HELM[@]}" upgrade --install keda kedacore/keda \
  --version "$KEDA_CHART_VERSION" --namespace keda --create-namespace --wait --timeout 10m

step "App image ($APP_IMAGE, built inside Minikube)"
# Always rebuilt: Docker's layer cache makes this fast when nothing changed.
minikube image build -p "$PROFILE" -t "$APP_IMAGE" cloud-env/app

step "App manifests"
"${KUBECTL[@]}" apply -f cloud-env/k8s/namespace.yaml
"${KUBECTL[@]}" apply -f cloud-env/k8s/deployment.yaml -f cloud-env/k8s/service.yaml \
  -f cloud-env/k8s/servicemonitor.yaml
# Pick up a rebuilt image with the same tag.
"${KUBECTL[@]}" -n "$APP_NAMESPACE" rollout restart deployment/scale-app >/dev/null
"${KUBECTL[@]}" -n "$APP_NAMESPACE" rollout status deployment/scale-app --timeout=180s

step "Health checks"
# True if a PromQL query returns at least one series. Goes through the API
# server, so no port-forward is needed.
prom_has_data() {
  local encoded
  encoded="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")"
  "${KUBECTL[@]}" get --raw \
    "/api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy/api/v1/query?query=$encoded" \
    2>/dev/null | grep -q '"value"'
}
echo "metrics-server: waiting for 'kubectl top' to work..."
wait_for 180 "metrics-server" "${KUBECTL[@]}" top pods -n "$APP_NAMESPACE"
echo "Prometheus: waiting for app, CPU and replica metrics..."
wait_for 180 "Prometheus scraping scale-app" prom_has_data "up{namespace=\"$APP_NAMESPACE\"} == 1"
wait_for 180 "cAdvisor CPU metrics" prom_has_data "container_cpu_usage_seconds_total{namespace=\"$APP_NAMESPACE\",container=\"app\"}"
wait_for 180 "kube-state-metrics" prom_has_data "kube_deployment_spec_replicas{namespace=\"$APP_NAMESPACE\"}"
echo "KEDA: checking its operator is available..."
"${KUBECTL[@]}" -n keda wait --for=condition=Available deployment --all --timeout=180s >/dev/null

step "Summary"
cat <<EOF
Cluster ready.
  kubectl context   $PROFILE  (Minikube $CPUS CPUs, ${MEMORY_MB} MB, Kubernetes $K8S_VERSION)
  metrics-server    OK  (kubectl top works, resolution $METRICS_RESOLUTION)
  Prometheus        OK  kube-prometheus-stack $PROM_CHART_VERSION, namespace monitoring, scraping scale-app every 15 s
  KEDA              OK  chart $KEDA_CHART_VERSION, namespace keda
  App               OK  $APP_IMAGE, deployment $APP_NAMESPACE/scale-app ($("${KUBECTL[@]}" -n "$APP_NAMESPACE" get deploy scale-app -o jsonpath='{.status.readyReplicas}') ready)

Next: python experiments/run_experiment.py --controller hpa --scenario gradual_ramp --duration 180
EOF
